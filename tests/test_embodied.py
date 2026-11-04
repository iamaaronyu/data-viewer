import io
import json
import struct

import h5py
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

from data_viewer.embodied import Episode, is_rlds
from data_viewer.rlds import masked_crc
from data_viewer.server import AppConfig, create_app


def proto_example():
    # Independent protobuf encoder, using the public tensorflow.Example wire schema.
    f=descriptor_pb2.FileDescriptorProto(name='example_test.proto',package='tensorflow',syntax='proto3')
    def field(m,name,n,kind,label=1,type_name=None):
        x=m.field.add(name=name,number=n,type=kind,label=label)
        if type_name:x.type_name=type_name
    for name,kind in [('BytesList',12),('FloatList',2),('Int64List',3)]:
        m=f.message_type.add(name=name);field(m,'value',1,kind,3)
    m=f.message_type.add(name='Feature')
    for i,name in enumerate(['BytesList','FloatList','Int64List'],1):field(m,name.lower(),i,11,type_name='.tensorflow.'+name)
    m=f.message_type.add(name='Features');entry=m.nested_type.add(name='FeatureEntry');entry.options.map_entry=True
    field(entry,'key',1,9);field(entry,'value',2,11,type_name='.tensorflow.Feature');field(m,'feature',1,11,3,'.tensorflow.Features.FeatureEntry')
    m=f.message_type.add(name='Example');field(m,'features',1,11,type_name='.tensorflow.Features')
    pool=descriptor_pool.DescriptorPool();pool.Add(f)
    return message_factory.GetMessageClass(pool.FindMessageTypeByName('tensorflow.Example'))()


@pytest.fixture
def robotics(tmp_path):
    b=io.BytesIO();Image.new('RGB',(20,16),(27,55,99)).save(b,format='JPEG');jpg=b.getvalue()
    e=proto_example();features=e.features.feature
    features['steps/is_first'].int64list.value.extend([1,0,0])
    features['steps/is_last'].int64list.value.extend([0,0,1])
    features['steps/action'].floatlist.value.extend([1.,2.,3.,4.,5.,6.])
    features['steps/observation/image'].byteslist.value.extend([jpg]*3)
    features['steps/language_instruction'].byteslist.value.extend([b'pick cup']*3)
    record=e.SerializeToString();header=struct.pack('<Q',len(record))
    p=tmp_path/'robot-train.tfrecord-00000-of-00001'
    p.write_bytes(header+struct.pack('<I',masked_crc(header))+record+struct.pack('<I',masked_crc(record)))
    with h5py.File(tmp_path/'libero.hdf5','w') as f:
        for i,n in enumerate([3,5]):
            g=f.create_group('data/demo_'+str(i));g['actions']=np.arange(n*2).reshape(n,2);g['obs/image']=np.full((n,16,20,3),27,dtype='u1')
    with h5py.File(tmp_path/'egodex.hdf5','w') as f:
        f['transforms/leftHand']=np.repeat(np.eye(4)[None],4,axis=0);f.attrs['llm_description']='fold'
    return tmp_path,p


def test_rlds_real_wire_shape_image_and_crc(robotics):
    root,p=robotics;e=Episode(p);assert e.length==3
    np.testing.assert_array_equal(e.arrays['steps/action'],[[1,2],[3,4],[5,6]])
    assert e.instructions==['pick cup']*3
    assert Image.open(io.BytesIO(e.frame('steps/observation/image',2)[0])).size==(20,16)
    assert e.timestamp_kind=='frame_index'
    corrupt=bytearray(p.read_bytes());corrupt[-5]^=1;p.write_bytes(corrupt)
    with pytest.raises(ValueError,match='CRC'):Episode(p)


def test_truncated_record_and_staging_rejected(robotics):
    root,p=robotics;assert is_rlds(p) and not is_rlds(str(p)+'.part')
    p.write_bytes(p.read_bytes()[:-9])
    with pytest.raises(ValueError,match='truncated'):Episode(p)


def test_libero_and_pose_not_misclassified_as_image(robotics):
    root,_=robotics;e=Episode(root/'libero.hdf5',1)
    assert e.length==5 and e.record_count==2 and e.arrays['actions'][4].tolist()==[8,9]
    assert e.frame('obs/image',4)[1]=='image/png'
    pose=Episode(root/'egodex.hdf5');assert not pose.images
    assert pose.arrays['transforms/leftHand'].shape==(4,4,4)


def test_lerobot_episode_boundaries_sorting_and_video_mapping(tmp_path):
    (tmp_path/'data/chunk-000').mkdir(parents=True);(tmp_path/'meta').mkdir()
    info={'features':{},'fps':30};(tmp_path/'meta/info.json').write_text(json.dumps(info))
    p=tmp_path/'data/chunk-000/file-000.parquet'
    pq.write_table(pa.table({'episode_index':[4,4,5,5],'frame_index':[1,0,0,1],'timestamp':[.1,0.,0.,.1],'action':[[3.,4.],[1.,2.],[8.,9.],[10.,11.]]}),p)
    e=Episode(p,0);assert e.length==2 and e.episode_id==4
    assert e.arrays['action'].tolist()==[[1.,2.],[3.,4.]]
    assert e.timestamps.tolist()==[0.,.1]
    assert Episode(p,1).arrays['action'][0].tolist()==[8.,9.]


def test_api_bounds_media_and_root_confinement(robotics):
    root,p=robotics
    with TestClient(create_app(AppConfig(root=root)),base_url='http://127.0.0.1:8765') as c:
        assert c.get('/embodied').status_code==200
        assert any(x['path']==p.name for x in c.get('/api/embodied/files').json()['files'])
        assert c.get('/api/embodied/episode',params={'path':p.name}).json()['length']==3
        assert c.get('/api/embodied/episode',params={'path':'../secret'}).status_code==403
        assert c.get('/api/embodied/step',params={'path':p.name,'step':3}).status_code==400
        r=c.get('/api/embodied/frame',params={'path':p.name,'camera':'steps/observation/image','step':0})
        assert r.status_code==200 and r.headers['content-type']=='image/jpeg'
        sid=c.post('/api/open',json={'path':p.name}).json()['id']
        page=c.post(f'/api/s/{sid}/rows',json={'limit':2}).json()
        assert len(page['rows'])==2


def test_v3_video_offsets_are_applied(tmp_path):
    import av
    (tmp_path/'meta/episodes/chunk-000').mkdir(parents=True);(tmp_path/'data/chunk-000').mkdir(parents=True);(tmp_path/'videos/cam/chunk-000').mkdir(parents=True)
    video=tmp_path/'videos/cam/chunk-000/file-000.mp4'
    with av.open(str(video),'w') as c:
        stream=c.add_stream('mpeg4',rate=10);stream.width=32;stream.height=32;stream.pix_fmt='yuv420p'
        for color in [(255,0,0)]*5+[(0,0,255)]*5:
            a=np.zeros((32,32,3),dtype='u1');a[:]=color
            for packet in stream.encode(av.VideoFrame.from_ndarray(a,format='rgb24')):c.mux(packet)
        for packet in stream.encode():c.mux(packet)
    (tmp_path/'meta/info.json').write_text(json.dumps({'fps':10,'features':{'cam':{'dtype':'video'}},'video_path':'videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4'}))
    meta=pa.table({'episode_index':[1], 'videos/cam/chunk_index':[0], 'videos/cam/file_index':[0], 'videos/cam/from_timestamp':[.5], 'videos/cam/to_timestamp':[1.]})
    pq.write_table(meta,tmp_path/'meta/episodes/chunk-000/file-000.parquet')
    p=tmp_path/'data/chunk-000/file-000.parquet';pq.write_table(pa.table({'episode_index':[1]*5,'frame_index':list(range(5)),'timestamp':[i/10 for i in range(5)],'action':[[float(i)] for i in range(5)]}),p)
    e=Episode(p)
    for step in (0,4):
        image=np.asarray(Image.open(io.BytesIO(e.frame('cam',step)[0])))
        assert image[:,:,2].mean()>200 and image[:,:,0].mean()<20


def test_unified_views_and_legacy_redirect(robotics):
    root,p=robotics
    pq.write_table(pa.table({'value':[1,2]}),root/'ordinary.parquet')
    with TestClient(create_app(AppConfig(root=root)),base_url='http://127.0.0.1:8765') as c:
        assert c.get('/embodied',follow_redirects=False).headers['location']=='/'
        html=c.get('/').text
        assert 'data-tab="trajectory"' in html and 'href="/embodied"' not in html
        assert c.post('/api/open',json={'path':p.name}).json()['views']==['trajectory']
        assert c.post('/api/open',json={'path':'ordinary.parquet'}).json()['views']==[]
        h=c.post('/api/open',json={'path':'libero.hdf5'});assert h.status_code==200,h.text
        assert h.json()['views']==['trajectory']
        e=c.get('/api/embodied/episode',params={'path':'libero.hdf5','episode':1})
        assert e.status_code==200 and e.json()['length']==5
