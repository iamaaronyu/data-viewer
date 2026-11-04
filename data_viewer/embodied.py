"""Read-only robotics episode views over RLDS, HDF5 and LeRobot Parquet."""
from __future__ import annotations
import io
import json
import math
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlencode

import h5py
import numpy as np
import pyarrow.parquet as pq
from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse, Response

from . import media, rlds
from .util import json_safe


def is_rlds(path):
    return re.search(r'\.tfrecord(?:-\d+-of-\d+)?$', Path(path).name) is not None


def supports_trajectory(path, fmt, columns):
    """Advertise a view only for recognized episode layouts."""
    if not Path(path).is_file(): return False
    if fmt == 'rlds': return True
    if fmt == 'parquet': return 'episode_index' in columns and any(k == 'action' or k.startswith('action.') for k in columns)
    if fmt == 'hdf5':
        with h5py.File(path,'r',locking=False) as f:
            if 'transforms' in f or 'actions' in f or 'action' in f: return True
            if 'puppet/joint_position' in f and 'observations/rgb_images' in f:
                return isinstance(f['puppet/joint_position'],h5py.Dataset) and f['puppet/joint_position'].ndim == 2
            if 'data' in f and isinstance(f['data'],h5py.Group):
                return any(isinstance(g,h5py.Group) and ('actions' in g or 'action' in g) for g in f['data'].values())
    return False


@lru_cache(maxsize=16)
def offsets(path, size, modified):
    return rlds.record_offsets(path)


def h5_nodes(path):
    with h5py.File(path, 'r',locking=False) as f:
        if 'data' in f and isinstance(f['data'], h5py.Group):
            demos = [k for k in f['data'] if isinstance(f['data'][k], h5py.Group)]
            if demos: return ['data/'+k for k in sorted(demos)]
        return ['/']


def _text(value):
    if isinstance(value, bytes): return value.decode('utf8', errors='replace')
    return str(value)


class Episode:
    def __init__(self, path, episode=0):
        self.path = Path(path); self.episode = episode
        self.arrays = {}; self.images = {}; self.videos = {}; self.instructions = []
        self.timestamps = None; self.timestamp_kind = 'frame_index'; self.warnings = []
        self.length = 0
        if is_rlds(path): self._rlds()
        elif self.path.suffix.lower() in ('.h5', '.hdf5'): self._hdf5()
        elif self.path.suffix.lower() == '.parquet': self._parquet()
        else: raise ValueError('Unsupported episode file; extract robotics archives before opening')
        if self.length <= 0: raise ValueError('No time-aligned frames found')

    def _rlds(self):
        stat = self.path.stat(); index = offsets(str(self.path), stat.st_size, stat.st_mtime_ns)
        if not 0 <= self.episode < len(index): raise IndexError('Episode out of range')
        self.record_count = len(index)
        data = rlds.read_record(self.path, index[self.episode])
        self.length = len(data.get('steps/is_first', [])) or len(data.get('steps/is_last', []))
        if not self.length: raise ValueError('RLDS episode lacks step boundary fields')
        schema_path = self.path.parent/'features.json'; shapes = {}; dtypes = {}
        def walk(node, prefix=''):
            if 'sequence' in node: walk(node['sequence']['feature'], prefix)
            elif 'featuresDict' in node:
                for key, value in node['featuresDict']['features'].items(): walk(value, prefix+'/'+key if prefix else key)
            elif 'tensor' in node:
                shapes[prefix] = tuple(int(d) for d in node['tensor'].get('shape', {}).get('dimensions', []))
                dtypes[prefix] = node['tensor'].get('dtype')
        if schema_path.exists(): walk(json.loads(schema_path.read_text()))
        for key, values in data.items():
            if not key.startswith('steps/') or not values: continue
            if isinstance(values[0], bytes):
                if 'instruction' in key or 'language' in key:
                    self.instructions = [_text(v) for v in values]
                elif media.sniff_mime(values[0]) in ('image/jpeg', 'image/png', 'image/webp'):
                    if len(values) != self.length: raise ValueError('Image/step length mismatch: '+key)
                    self.images[key] = values
                continue
            if len(values) % self.length: raise ValueError('Feature/step length mismatch: '+key)
            shape = shapes.get(key)
            a = np.asarray(values,dtype=dtypes.get(key))
            if shape is not None:
                if math.prod(shape) * self.length != a.size: raise ValueError('Schema shape mismatch: '+key)
                a = a.reshape((self.length,)+shape)
            else: a = a.reshape(self.length, -1)
            self.arrays[key] = a
        self._time()

    def _hdf5(self):
        nodes = h5_nodes(self.path)
        if not 0 <= self.episode < len(nodes): raise IndexError('Episode out of range')
        self.node = nodes[self.episode]; self.record_count = len(nodes)
        with h5py.File(self.path, 'r',locking=False) as f:
            g = f[self.node]; datasets = {}
            g.visititems(lambda k, v: datasets.update({k: v}) if isinstance(v, h5py.Dataset) and v.shape else None)
            preferred = [d.shape[0] for k,d in datasets.items() if ('action' in k or k.startswith('transforms/') or 'joint' in k) and d.ndim > 0]
            if not preferred: preferred = [d.shape[0] for d in datasets.values() if d.ndim > 1]
            if not preferred: raise ValueError('No temporal HDF5 datasets')
            from collections import Counter
            self.length = Counter(preferred).most_common(1)[0][0]
            for key,d in datasets.items():
                if d.shape[0] != self.length:
                    self.warnings.append('Excluded non-aligned field: '+key); continue
                if d.ndim >= 3 and media.image_like(d.shape[1:]): self.images[key] = ('h5', self.node, key)
                elif d.ndim==1 and d.dtype.kind in 'SO':
                    sample=d[0]
                    encoded=sample.tobytes() if isinstance(sample,np.ndarray) else sample
                    if isinstance(encoded,bytes) and (media.sniff_mime(encoded) or '').startswith('image/'):
                        self.images[key]=('h5_encoded',self.node,key)
                    elif 'language' in key: self.instructions=[_text(v) for v in d[:]]
                elif d.dtype.kind in 'biuf' and math.prod(d.shape[1:]) <= 4096: self.arrays[key] = d[:]
                elif d.dtype.kind in 'SO' and 'language' in key: self.instructions = [_text(v) for v in d[:]]
            for owner in (g, f):
                for key in ('llm_description', 'language_instruction', 'instruction'):
                    if key in owner.attrs and not self.instructions: self.instructions = [_text(owner.attrs[key])]
            paired = self.path.with_suffix('.mp4')
            if paired.exists():
                self.videos['egocentric'] = {'path': paired, 'offset': 0.0, 'end': None, 'fps': 30.0}
                if 'transforms' in f:
                    self.timestamps = np.arange(self.length)/30.0; self.timestamp_kind = 'dataset_30hz'
        self._time()

    def _parquet(self):
        table = pq.read_table(self.path)
        if 'episode_index' not in table.column_names: raise ValueError('Not a LeRobot episode table')
        ids = sorted(set(table['episode_index'].to_pylist()))
        if not 0 <= self.episode < len(ids): raise IndexError('Episode out of range')
        import pyarrow.compute as pc
        self.episode_id = int(ids[self.episode]); self.record_count = len(ids)
        table = table.filter(pc.equal(table['episode_index'], self.episode_id))
        if 'frame_index' in table.column_names: table = table.sort_by([('frame_index','ascending')])
        self.length = table.num_rows
        for key in table.column_names:
            try:
                a = np.asarray(table[key].to_pylist())
                if a.dtype.kind in 'biuf' and a.ndim <= 3: self.arrays[key] = a
            except (ValueError, TypeError): pass
        dataset = next((p for p in self.path.parents if (p/'meta/info.json').exists()), None)
        if dataset:
            info = json.loads((dataset/'meta/info.json').read_text()); self._lerobot_videos(dataset, info)
            tasks=dataset/'meta/tasks.jsonl'
            if tasks.exists() and 'task_index' in self.arrays:
                task_map={row['task_index']:row.get('task',row.get('task_name','')) for row in (json.loads(line) for line in tasks.read_text().splitlines() if line.strip())}
                self.instructions=[task_map.get(int(i),'') for i in self.arrays['task_index'].reshape(-1)]
        else: self.warnings.append('LeRobot meta/info.json is missing; video associations unavailable')
        self._time()

    def _lerobot_videos(self, dataset, info):
        features = info.get('features', {}); video_template = info.get('video_path', '')
        for camera, feature in features.items():
            if feature.get('dtype') != 'video': continue
            if '{video_key}' not in video_template: continue
            if '{episode_index' in video_template:
                video = dataset/video_template.format(episode_index=self.episode_id, episode_chunk=self.episode_id//info.get('chunks_size',1000), video_key=camera)
                offset = 0.0; end = None
            else:
                # v3 episode metadata maps frame timestamps into concatenated videos.
                matches = []
                for p in sorted((dataset/'meta/episodes').rglob('*.parquet')):
                    t = pq.read_table(p)
                    if 'episode_index' not in t.column_names: continue
                    for row in t.to_pylist():
                        if row['episode_index'] == self.episode_id: matches.append(row)
                    if matches: break
                if not matches: self.warnings.append('Missing v3 video metadata: '+camera); continue
                row = matches[0]; prefix='videos/'+camera+'/'
                needed = [prefix+k for k in ('chunk_index','file_index','from_timestamp','to_timestamp')]
                if not all(k in row for k in needed): self.warnings.append('Incomplete v3 video mapping: '+camera); continue
                video=dataset/video_template.format(video_key=camera, chunk_index=row[needed[0]], file_index=row[needed[1]])
                offset=float(row[needed[2]]); end=float(row[needed[3]])
            if not video.resolve().is_relative_to(dataset.resolve()): raise ValueError('Video escapes dataset directory')
            if video.is_file(): self.videos[camera]={'path':video,'offset':offset,'end':end,'fps':float(feature.get('info',{}).get('video.fps',info.get('fps',30)))}
            else: self.warnings.append('Missing video: '+str(video.relative_to(dataset)))

    def _time(self):
        for key,a in self.arrays.items():
            if key.split('/')[-1] == 'timestamp' and a.size == self.length:
                self.timestamps=np.asarray(a).reshape(-1); self.timestamp_kind='recorded_seconds'; break
        if self.timestamps is not None and (not np.isfinite(self.timestamps).all() or np.any(np.diff(self.timestamps)<0)):
            raise ValueError('Invalid/non-monotonic timestamps')

    def frame(self, camera, step):
        if not 0 <= step < self.length: raise IndexError('Frame out of range')
        if camera in self.images:
            image=self.images[camera]
            if isinstance(image, tuple):
                with h5py.File(self.path,'r',locking=False) as f: a=f[image[1]][image[2]][step]
                if image[0]=='h5_encoded':
                    data=a.tobytes() if isinstance(a,np.ndarray) else a
                    return data,media.sniff_mime(data)
                png=media.array_to_png(a)
                if png is None: raise ValueError('Unsupported image shape')
                return png,'image/png'
            return image[step],media.sniff_mime(image[step])
        if camera in self.videos:
            import av
            v=self.videos[camera]
            t=float(self.timestamps[step]-self.timestamps[0]) if self.timestamps is not None else step/v['fps']
            target=v['offset']+t
            if v['end'] is not None and target > v['end']+1/v['fps']: raise ValueError('Frame exceeds episode video interval')
            with av.open(str(v['path'])) as container:
                stream=container.streams.video[0]; container.seek(max(0,int(target/stream.time_base)),stream=stream,backward=True)
                for frame in container.decode(stream):
                    if frame.time is not None and frame.time+0.5/v['fps']>=target:
                        out=io.BytesIO();frame.to_image().save(out,format='PNG');return out.getvalue(),'image/png'
            raise ValueError('Video frame missing')
        raise KeyError('Unknown camera')

    def describe(self):
        return {'length':self.length,'episodes':self.record_count,'episode':self.episode,'cameras':list(self.images)+list(self.videos),
                'instruction':next((s for s in self.instructions if s),''),'timestamp_kind':self.timestamp_kind,
                'fields':{k:{'shape':list(a.shape),'dtype':str(a.dtype),'nonfinite':int(np.size(a)-np.isfinite(a).sum())} for k,a in self.arrays.items()},
                'warnings':self.warnings,'action_units':'Original dataset units; no normalization or coordinate conversion'}


def router(root):
    root=Path(root).resolve(); routes=APIRouter()
    def resolve(p):
        path=(root/p).resolve()
        if not path.is_relative_to(root): raise HTTPException(403,'Path outside data root')
        if not path.is_file(): raise HTTPException(404,'File missing')
        return path
    @lru_cache(maxsize=2)
    def cached(path, size, modified, episode): return Episode(path,episode)
    def load(path,episode):
        p=resolve(path); s=p.stat()
        try: return cached(str(p),s.st_size,s.st_mtime_ns,episode)
        except (ValueError,IndexError,KeyError,OSError) as e: raise HTTPException(400,str(e)) from e
    @routes.get('/embodied')
    def page(): return RedirectResponse('/')
    @routes.get('/api/embodied/files')
    def files():
        rows=[]
        for p in root.rglob('*'):
            if len(rows)>=10000: break
            if not p.is_file() or any(x in p.parts for x in ('.git','_catalog')): continue
            if p.resolve().is_relative_to(root) and (is_rlds(p) or p.suffix.lower() in ('.h5','.hdf5','.parquet')):
                if '/meta/' in p.as_posix(): continue
                rows.append({'path':p.relative_to(root).as_posix(),'bytes':p.stat().st_size})
        return {'files':sorted(rows,key=lambda x:x['path']),'limit':10000}
    @routes.get('/api/embodied/episode')
    def episode(path:str,episode:int=0): return load(path,episode).describe()
    @routes.get('/api/embodied/step')
    def step(path:str,episode:int=0,step:int=0):
        e=load(path,episode)
        if not 0<=step<e.length: raise HTTPException(400,'Frame out of range')
        return json_safe({'step':step,'timestamp':float(e.timestamps[step]) if e.timestamps is not None else None,
                          'values':{k:a[step].tolist() for k,a in e.arrays.items()},
                          'instruction':e.instructions[min(step,len(e.instructions)-1)] if e.instructions else ''})
    @routes.get('/api/embodied/frame')
    def frame(path:str,camera:str,episode:int=0,step:int=0):
        try: data,mime=load(path,episode).frame(camera,step)
        except (ValueError,IndexError,KeyError,OSError) as e: raise HTTPException(400,str(e)) from e
        return Response(data,media_type=mime,headers={'Cache-Control':'private, max-age=60'})
    @routes.get('/api/embodied/trajectory')
    def trajectory(path:str,field:str,episode:int=0):
        e=load(path,episode)
        if field not in e.arrays: raise HTTPException(400,'Unknown numeric field')
        a=e.arrays[field].reshape(e.length,-1); indices=np.linspace(0,e.length-1,min(e.length,5000),dtype=int)
        return json_safe({'indices':indices.tolist(),'dimensions':a.shape[1], 'shown_dimensions':min(8,a.shape[1]),
                          'values':a[indices,:8].tolist(),'time':e.timestamps[indices].tolist() if e.timestamps is not None else None})
    return routes
