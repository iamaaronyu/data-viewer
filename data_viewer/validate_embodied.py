"""Validate available original samples and the serving API; emit an honest report."""
import argparse
import hashlib
import io
import json
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
from PIL import Image
from fastapi.testclient import TestClient
from .embodied import Episode,is_rlds
from .server import AppConfig,create_app


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--root',required=True);ap.add_argument('--output',required=True);ap.add_argument('--per-dataset',type=int,default=2);args=ap.parse_args()
    root=Path(args.root).resolve();groups=defaultdict(list)
    for p in root.rglob('*'):
        if not p.is_file() or '_catalog' in p.parts or '/meta/' in p.as_posix():continue
        if is_rlds(p) or p.suffix.lower() in ('.h5','.hdf5','.parquet'):groups[p.relative_to(root).parts[0]].append(p)
    report={'root':str(root),'created_unix':time.time(),'scope':'Sampled original files and first/last steps; not exhaustive training or physics validation','datasets':{}}
    with TestClient(create_app(AppConfig(root=root)),base_url='http://127.0.0.1:8765') as client:
        for dataset in ('droid','oxe','bridgedata-v2','libero','agibot-world-2026','behavior-1k','egodex','robomind'):
            selected=sorted(groups[dataset])[:args.per_dataset];results=[]
            for path in selected:
                row={'path':path.relative_to(root).as_posix()}
                try:
                    e=Episode(path);row.update(e.describe());row['frames_checked']=[]
                    for step in sorted(set([0,e.length//2,e.length-1])):
                        response=client.get('/api/embodied/step',params={'path':row['path'],'step':step});assert response.status_code==200,response.text
                        raw=response.json()['values']
                        for key,a in e.arrays.items():
                            expected=a[step].tolist();actual=raw[key]
                            # JSON intentionally represents non-finite sentinels as null.
                            def finite(v):
                                a=np.asarray(v,dtype=float);return np.where(np.isfinite(a),a,np.nan)
                            np.testing.assert_allclose(finite(actual),finite(expected),equal_nan=True)
                        checked={'step':step,'images':[]}
                        for camera in list(e.images)+list(e.videos):
                            data,mime=e.frame(camera,step);image=Image.open(io.BytesIO(data));image.load()
                            response=client.get('/api/embodied/frame',params={'path':row['path'],'step':step,'camera':camera});assert response.status_code==200,response.text
                            assert response.content==data
                            checked['images'].append({'camera':camera,'size':list(image.size),'sha256':hashlib.sha256(data).hexdigest()})
                        row['frames_checked'].append(checked)
                    row['status']='passed' if not e.warnings else 'passed_with_warnings'
                except Exception as exc:row.update(status='failed',error=str(exc))
                results.append(row)
            report['datasets'][dataset]={'available_files':len(groups[dataset]),'checked':results,'status':'not_available' if not selected else ('failed' if any(x['status']=='failed' for x in results) else 'sampled')}
    Path(args.output).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8');print(json.dumps({k:v['status'] for k,v in report['datasets'].items()}))
    return int(any(v['status']=='failed' for v in report['datasets'].values()))


if __name__=='__main__':raise SystemExit(main())
