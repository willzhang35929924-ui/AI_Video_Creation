"""Isolated quality/lineage/gate contracts; these tests are not model evidence.

Synthetic FFmpeg media verifies source selection and version publication. All
queue responses and native model boundaries are explicitly controlled here.
Real SeedVR evidence is the separately recorded107-frame hardware smoke.
"""
import asyncio
import copy
import importlib.util
import io
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
import urllib.error

import httpx
import pytest
from sqlalchemy import select

from app import models as m
from app.core.config import PROJECT_ROOT
from app.core.errors import AppError
from app.schemas_quality import QualitySettings
from app.services import video_quality as q
from app.services.ai_services import service_snapshot
from test_video_quality import context, source


def verified_seed():
    return {'state':'verified','manifest_sha256':'a'*64,'verified_resolutions':['720p'],
            'verified_aspect_ratios':['16:9'],'manifest':{'components':[]},'limits':[]}


def test_seed_plan_requires_actual_verified_scope_and_never_claims_4k():
    settings=QualitySettings(processing='seedvr2')
    assert not q.plan(settings)['supported']
    proof=verified_seed()
    assert q.plan(settings,None,proof)['supported']
    assert q.plan(settings,None,proof)['temporal_model']
    for resolution in ['1080p','2k','4k']:
        assert not q.plan(settings.model_copy(update={'resolution':resolution}),None,proof)['supported']
    assert not q.plan(settings.model_copy(update={'aspect_ratio':'9:16'}),None,proof)['supported']
    assert not q.plan(settings,None,{**proof,'verified_aspect_ratios':[]})['supported']


def derivative(session,parent,path,kind,extra=None):
    video=m.VideoRevision(project_id=parent.project_id,shot_id=parent.shot_id,generation_job_id=parent.generation_job_id,
        relative_path=path,revision=parent.revision+1,metadata_json={'source_video_id':parent.id,'processing_kind':kind,**(extra or {})})
    session.add(video);session.flush();return video


@pytest.mark.parametrize('protected',['audio_mix','lip_sync'])
def test_only_upscale_layers_are_peeled_and_audio_or_lip_stage_remains_direct_source(context,settings,protected):
    session,_,_,_,raw=context;source(context,settings)
    protected_path=settings.media_root/(protected+'.mp4');protected_path.write_bytes(b'explicit isolated protected-stage bytes')
    stage=derivative(session,raw,protected_path.name,protected)
    old=derivative(session,stage,'old-upscale.mp4','video_upscale',{'quality_render_id':'old-q','quality':{'method':'realesrgan_framewise'}})
    old_metadata=copy.deepcopy(old.metadata_json)
    render=q.create_render(session,settings,old.id,QualitySettings())
    assert render.source_video_id==stage.id
    assert render.snapshot_json['root_source_video_id']==raw.id
    assert render.snapshot_json['requested_source_video_id']==old.id
    assert render.snapshot_json['source_relative_path']==stage.relative_path
    assert old.metadata_json==old_metadata and protected_path.read_bytes()==b'explicit isolated protected-stage bytes'


def test_legacy_protected_metadata_is_not_treated_as_a_quality_parent(context,settings):
    session,_,_,_,raw=context;source(context,settings)
    path=settings.media_root/'legacy.mp4';path.write_bytes(b'isolated legacy stage')
    stage=derivative(session,raw,path.name,None,{'audio_mix':{'task':'old'},'quality_render_id':'inherited','quality':{'method':'ordinary_lanczos_scaling'}})
    assert q.create_render(session,settings,stage.id,QualitySettings()).source_video_id==stage.id


def test_lineage_cycle_and_cross_project_are_rejected_before_queue_creation(context,settings):
    session,_,_,_,raw=context;source(context,settings)
    raw.metadata_json={**raw.metadata_json,'source_video_id':raw.id,'processing_kind':'video_upscale'}
    with pytest.raises(AppError,match='版本关系异常'):q.create_render(session,settings,raw.id,QualitySettings())
    assert not list(session.scalars(select(m.QualityRender)))
    raw.metadata_json={k:v for k,v in raw.metadata_json.items() if k not in {'source_video_id','processing_kind'}}
    other=m.Project(name='Other isolated project');session.add(other);session.flush()
    invalid=derivative(session,raw,'not-opened.mp4','video_upscale');invalid.project_id=other.id
    with pytest.raises(AppError,match='同一项目'):q.create_render(session,settings,invalid.id,QualitySettings())


def route_for_seed(session):
    service=m.AIService(name='Isolated Seed route',module='video_upscale',provider='comfyui',base_url='http://127.0.0.1:8189',
        default_model='seedvr2_3b_fp8_e4m3fn.safetensors',settings_json={'runtime_family':'seedvr2'},enabled=True,is_default=False)
    session.add(service);session.flush()
    return service,{'slot':'video_upscale','selected':{'service_snapshot':service_snapshot(service)},'profile':{}}


def test_explicit_seed_route_requires_explicit_matching_processing_and_is_frozen(context,settings,monkeypatch):
    session,_,_,_,video=context;source(context,settings);service,route=route_for_seed(session)
    monkeypatch.setattr(q,'seedvr_status',lambda _:verified_seed())
    with pytest.raises(AppError,match='明确保存为SeedVR2'):
        q.create_render(session,settings,video.id,QualitySettings(),route_snapshot=route)
    assert not list(session.scalars(select(m.QualityRender)))
    item=q.create_render(session,settings,video.id,QualitySettings(processing='seedvr2'),route_snapshot=route)
    assert item.snapshot_json['method']=='seedvr2_temporal_video'
    assert item.snapshot_json['route']['selected']['service_snapshot']['service_revision']==service.revision
    route['selected']['service_snapshot']['settings']['later_change']=True
    assert 'later_change' not in item.snapshot_json['route']['selected']['service_snapshot']['settings']
    service.revision+=1
    with pytest.raises(AppError,match='固定的服务配置已改变'):
        q.create_render(session,settings,video.id,QualitySettings(processing='seedvr2'),route_snapshot=route)


def test_seed_unknown_fingerprint_cannot_be_silently_submitted_again(context,settings,monkeypatch):
    session,_,_,_,video=context;source(context,settings)
    monkeypatch.setattr(q,'seedvr_status',lambda _:verified_seed())
    first=q.create_render(session,settings,video.id,QualitySettings(processing='seedvr2'));first.status='unknown';session.flush()
    with pytest.raises(AppError,match='状态不确定'):
        q.create_render(session,settings,video.id,QualitySettings(processing='seedvr2'))
    assert len(list(session.scalars(select(m.QualityRender))))==1


def test_seed_uncertain_dispatch_stays_unknown_and_preserves_source(context,settings,monkeypatch):
    session,factory,_,_,video=context;path,digest=source(context,settings)
    monkeypatch.setattr(q,'seedvr_status',lambda _:verified_seed())
    item=q.create_render(session,settings,video.id,QualitySettings(processing='seedvr2'));session.commit();identifier=item.id
    def uncertain(*_args):raise q._native_unknown('controlled post-submit network uncertainty')
    monkeypatch.setattr(q,'_run_seedvr_native',uncertain)
    q.run_render(settings,factory,identifier);session.expire_all();item=session.get(m.QualityRender,identifier)
    assert item.status=='unknown' and item.video_id is None and item.result_json['source_preserved']
    assert q._file_sha(path)==digest and len(list(session.scalars(select(m.VideoRevision))))==1


def test_restart_does_not_repeat_a_submitted_native_job(context,settings,monkeypatch):
    session,factory,_,_,video=context;source(context,settings)
    monkeypatch.setattr(q,'seedvr_status',lambda _:verified_seed())
    item=q.create_render(session,settings,video.id,QualitySettings(processing='seedvr2'))
    submission=settings.media_root/'only-isolated-submission.json'
    submission.write_text(json.dumps({'status':'SUBMITTED','prompt_id':'controlled-not-a-real-prompt'}),encoding='utf-8')
    item.status='running';item.snapshot_json={**item.snapshot_json,'native_execution':{'paths':{'submission':str(submission)}}}
    session.commit();identifier=item.id
    q.recover_interrupted_quality(factory);session.expire_all();item=session.get(m.QualityRender,identifier)
    assert item.status=='unknown'
    with pytest.raises(AppError,match='状态不确定'):
        q.create_render(session,settings,video.id,QualitySettings(processing='seedvr2'))


def test_seed_release_uses_fixed_controller_without_mutating_global_h3_settings(context,settings,monkeypatch):
    session,factory,_,_,video=context;source(context,settings)
    monkeypatch.setattr(q,'seedvr_status',lambda _:verified_seed())
    item=q.create_render(session,settings,video.id,QualitySettings(processing='seedvr2'));session.commit()
    settings.comfyui_base_url='http://127.0.0.1:8188';seen=[]
    async def release(local):
        seen.append(local.comfyui_base_url);assert local is not settings
        return {'controlled':'no network/GPU'}
    monkeypatch.setattr(q,'confirm_gpu_release',release)
    monkeypatch.setattr(q,'run_render',lambda *args:seen.append('controlled render boundary'))
    assert asyncio.run(q.run_ai_locked(settings,factory,item.id))
    assert seen==['http://127.0.0.1:8189','controlled render boundary']
    assert settings.comfyui_base_url=='http://127.0.0.1:8188'


@pytest.mark.parametrize('defer_first',[False,True,'unknown','collect'])
def test_seed_native_boundary_collect_and_prepared_defer_reuse_with_real_ffmpeg(context,settings,monkeypatch,defer_first):
    """The native boundary is controlled; resulting media is explicitly synthetic.

    Verifies worker state/provenance/mode/audio behavior, never marks a model
    manifest verified and never submits a real Comfy prompt.
    """
    session,factory,_,_,video=context;path,digest=source(context,settings)
    runtime=settings.media_root/'controlled-runtime';model_root=runtime/'models';model_root.mkdir(parents=True)
    files=[];components=[]
    for name,payload in [('diffusion_models/test.bin',b'controlled diffusion'),('vae/test.bin',b'controlled vae')]:
        target=model_root/name;target.parent.mkdir(exist_ok=True);target.write_bytes(payload)
        files.append((name,len(payload),q._file_sha(target)))
        components.append({'path':str(target),'bytes':len(payload),'sha256':q._file_sha(target)})
    workflow=runtime/'fixture-workflow.json';workflow.write_text('{}')
    caps={**verified_seed(),'manifest':{'workflow_path':str(workflow),'workflow_sha256':q._file_sha(workflow),'components':components}}
    monkeypatch.setattr(q,'seedvr_status',lambda _:copy.deepcopy(caps))
    controlled_video=runtime/'synthetic-native-boundary.mp4'
    subprocess.run([settings.ffmpeg_path,'-v','error','-n','-i',str(path),'-vf','scale=1280:720','-c:v','libx264','-crf','0','-c:a','copy',str(controlled_video)],check=True,capture_output=True)
    calls={'prepare':0,'attempts':0,'accepted':0,'collect':0,'real_esrgan_publication':0}
    class Busy(RuntimeError):pass
    helper=SimpleNamespace(SPECS=files,MODELS=model_root,RUNTIME=runtime,REPO='controlled/unit-only',COMMIT='not-model-evidence',LocalGPUQueueBusyError=Busy)
    def write(p,v):Path(p).write_text(json.dumps(v),encoding='utf-8')
    helper.write_json=write
    def prepare(args):
        calls['prepare']+=1
        write(args.output,{'original_source_sha256':digest,'input_sha256':digest,'comfy_upload':{'actual_path':str(path)},'expected_frames':24,'sample_only':False,
            'prompt':{'15':{'inputs':{'format.codec.encoding.crf':18}}},'parameters':{}})
    def submit(args):
        calls['attempts']+=1
        frozen=json.loads(Path(args.prepared).read_text());assert frozen['prompt']['15']['inputs']['format.codec.encoding.crf']==0
        if defer_first=='unknown':
            write(args.output,{'status':'UNKNOWN_DO_NOT_RESUBMIT'})
            raise TimeoutError('controlled ambiguous submit timeout')
        if defer_first is True and calls['attempts']==1:raise Busy('controlled other queue appeared before submit')
        calls['accepted']+=1;write(args.output,{'status':'SUBMITTED','prompt_id':'explicit-controlled-not-real',
            'prepared':str(args.prepared),'prepared_sha256':q._file_sha(args.prepared),'base_url':args.base_url})
    def collect(args):
        calls['collect']+=1
        dest=Path(args.delivery)
        assert dest.resolve().is_relative_to((helper.RUNTIME/'outputs').resolve()), 'Native collector must receive a path inside its managed outputs directory'
        if defer_first=='collect' and calls['collect']==1:raise OSError('Controlled collection failure after accepted inference')
        dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(controlled_video,dest)
        write(args.output,{'status':'PASS_TECHNICAL_FILE','frames_processed':24,'source_sha256':digest,
            'checks':{'frames_equal':True,'full_decode_exit_code':0},'output':str(dest),'output_sha256':q._file_sha(dest)})
    helper.prepare_command=prepare;helper.submit_command=submit;helper.collect_command=collect
    helper.api=lambda *_:{'explicit-controlled-not-real':{'status':{'status_str':'success','completed':True}}}
    monkeypatch.setattr(q.importlib.util,'spec_from_file_location',lambda *_:SimpleNamespace(loader=SimpleNamespace(exec_module=lambda _:None)))
    monkeypatch.setattr(q.importlib.util,'module_from_spec',lambda _:helper)
    def bad_publication(*_):calls['real_esrgan_publication']+=1;raise AssertionError('Seed must not publish RealESRGAN proof')
    monkeypatch.setattr(q,'publish_sr_verification',bad_publication)
    item=q.create_render(session,settings,video.id,QualitySettings(processing='seedvr2',mode='high'));session.commit();identifier=item.id
    q.run_render(settings,factory,identifier)
    if defer_first=='unknown':
        session.expire_all();item=session.get(m.QualityRender,identifier)
        assert item.status=='unknown' and item.snapshot_json['native_execution']['inference_submitted'] is None
        assert calls['accepted']==0 and len(list(session.scalars(select(m.VideoRevision))))==1
        with pytest.raises(AppError,match='状态不确定'):
            q.create_render(session,settings,video.id,QualitySettings(processing='seedvr2',mode='high'))
        return
    if defer_first is True:
        session.expire_all();item=session.get(m.QualityRender,identifier)
        assert item.status=='queued' and item.snapshot_json['native_execution']['status']=='deferred'
        assert calls['accepted']==0
        q.run_render(settings,factory,identifier)
    if defer_first=='collect':
        session.expire_all();item=session.get(m.QualityRender,identifier)
        assert item.status=='failed' and calls['accepted']==1
        assert q.create_render(session,settings,video.id,QualitySettings(processing='seedvr2',mode='high')).id==identifier
        from app.services.seedvr_recovery import resume_native
        resumed=resume_native(session,settings,identifier);session.commit()
        assert resumed.id==identifier and resumed.snapshot_json['native_execution']['prompt_id']=='explicit-controlled-not-real'
        q.run_render(settings,factory,identifier)
        assert calls['collect']==2 and calls['attempts']==1
    session.expire_all();item=session.get(m.QualityRender,identifier)
    assert item.status=='generated',item.error_message
    assert calls['prepare']==1 and calls['accepted']==1 and calls['real_esrgan_publication']==0
    assert item.result_json['method']=='seedvr2_temporal_video' and item.result_json['temporal_model']
    assert item.result_json['final_actual']['frames']==24 and item.result_json['final_actual']['full_decode']['exit_code']==0
    assert item.result_json['command'][item.result_json['command'].index('-crf')+1]=='16'
    assert item.result_json['command'][item.result_json['command'].index('-preset')+1]=='slow'
    assert q._file_sha(path)==digest and len(list(session.scalars(select(m.VideoRevision))))==2


def test_actual_lanczos_publication_keeps_protected_direct_predecessor_and_root(context,settings):
    session,factory,_,_,raw=context;path,_=source(context,settings)
    protected_path=settings.media_root/'mixed.mp4';protected_path.write_bytes(path.read_bytes())
    mixed=derivative(session,raw,protected_path.name,'audio_mix',{'processing_chain':[{'kind':'audio_mix','input_video_id':raw.id}]})
    item=q.create_render(session,settings,mixed.id,QualitySettings());session.commit();identifier=item.id
    q.run_render(settings,factory,identifier);session.expire_all();item=session.get(m.QualityRender,identifier)
    assert item.status=='generated',item.error_message
    output=session.get(m.VideoRevision,item.video_id)
    assert output.metadata_json['source_video_id']==mixed.id and output.metadata_json['root_source_video_id']==raw.id
    assert output.metadata_json['processing_kind']=='video_upscale'
    assert [x['kind'] for x in output.metadata_json['processing_chain']]==['audio_mix','video_upscale']
    assert q.create_render(session,settings,output.id,QualitySettings(resolution='1080p')).source_video_id==mixed.id


@pytest.mark.parametrize('peer_state',['busy','timeout','bad_json'])
def test_other_local_comfy_busy_or_unknown_defers_without_free(monkeypatch,peer_state):
    real_client=httpx.AsyncClient;events=[]
    def handler(request):
        events.append((request.method,str(request.url)))
        if request.url.port==8188:
            if peer_state=='timeout':raise httpx.ReadTimeout('controlled timeout',request=request)
            if peer_state=='bad_json':return httpx.Response(200,json={'unexpected':True})
            return httpx.Response(200,json={'queue_running':[[1,'external-task']],'queue_pending':[]})
        raise AssertionError('Must stop before target/free')
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:real_client(transport=httpx.MockTransport(handler),**kw))
    assert asyncio.run(q.confirm_gpu_release(SimpleNamespace(comfyui_base_url='http://127.0.0.1:8189'))) is None
    assert events==[('GET','http://127.0.0.1:8188/queue')]


def test_connection_refused_peer_is_not_started_but_only_idle_target_is_freed(monkeypatch):
    real_client=httpx.AsyncClient;events=[]
    def handler(request):
        events.append((request.method,str(request.url)))
        if request.url.port==8188:
            error=httpx.ConnectError('controlled refusal',request=request)
            error.__cause__=ConnectionRefusedError(111,'controlled refusal');raise error
        if request.url.path=='/queue':return httpx.Response(200,json={'queue_running':[],'queue_pending':[]})
        if request.url.path=='/free':return httpx.Response(200,json={})
        return httpx.Response(200,json={'devices':[{'type':'cuda','vram_total':10*1024**3,'vram_free':8*1024**3,'torch_vram_total':0}]})
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:real_client(transport=httpx.MockTransport(handler),**kw))
    value=asyncio.run(q.confirm_gpu_release(SimpleNamespace(comfyui_base_url='http://127.0.0.1:8189')))
    assert value['known_local_queues']['8188']['state']=='not_started_connection_refused'
    assert [('POST','http://127.0.0.1:8189/free')]==[x for x in events if x[0]=='POST']


def seed_helper():
    spec=importlib.util.spec_from_file_location('seed_deployment_contract',PROJECT_ROOT/'scripts/deploy_seedvr2.py')
    value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value);return value


@pytest.mark.parametrize('peer_state',['busy','timeout','refused'])
def test_native_cli_checks_known_same_card_peer_before_any_submit(monkeypatch,peer_state):
    helper=seed_helper();calls=[]
    class Opener:
        def open(self,url,timeout):
            calls.append(url)
            if ':8188/' in url:
                if peer_state=='timeout':raise TimeoutError('controlled unknown')
                if peer_state=='refused':raise urllib.error.URLError(ConnectionRefusedError(111,'controlled refusal'))
                payload={'queue_running':[[1,'external']],'queue_pending':[]}
            else:payload={'queue_running':[],'queue_pending':[]}
            return io.BytesIO(json.dumps(payload).encode())
    monkeypatch.setattr(helper.urllib.request,'build_opener',lambda *_:Opener())
    if peer_state=='refused':assert helper.local_queues_idle()['8188']['state']=='not_started_connection_refused'
    else:
        with pytest.raises(helper.LocalGPUQueueBusyError):helper.local_queues_idle()
        assert calls==['http://127.0.0.1:8188/queue']
