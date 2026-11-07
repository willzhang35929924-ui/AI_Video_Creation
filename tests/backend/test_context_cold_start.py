"""Cold-start admission and no-submit recovery; isolated DB, no GPU or services."""
import asyncio
import json
from io import BytesIO
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image
from sqlalchemy import func, select

from app import models as m
from app.core.errors import AppError
from app.services import h3_context_provider as provider, studio_pipeline as pipeline
from app.services.h3_workflow_router import _read_template, freeze_workflow
from app.services.providers import VideoGenerationRequest
from test_context_production_inputs import add_shot, session


HEALTH = dict(service='video-factory-h3ir-qwen4b-v1', profile='h3ir-qwen4b-32k',
    model_id='Qwen/Qwen3-VL-4B-Instruct', offline=True, busy=False, loaded=False,
    script_sha256='a'*64, inference_phase='idle', gpu_release_status='not_used', last_gpu_cleanup=None)


def nodes():
    graph, _ = _read_template(freeze_workflow('fast')['workflow_id'])
    return {item['class_type']: {'input': {'required': {}}} for item in graph.values()}


def healthy(request):
    if request.url.path == '/queue':return httpx.Response(200,json={'queue_running':[],'queue_pending':[]})
    if request.url.path == '/object_info':return httpx.Response(200,json=nodes())
    if request.url.path == '/health':return httpx.Response(200,json=HEALTH)
    raise AssertionError('Unexpected I/O '+str(request.url))


@pytest.mark.parametrize('path',['/queue','/object_info'])
@pytest.mark.parametrize('fault',['refused','timeout',503])
def test_read_only_admission_waits_when_comfy_is_starting(path,fault):
    calls=[]
    def reply(request):
        calls.append((request.method,request.url.path))
        if request.url.path==path:
            if fault=='refused':raise httpx.ConnectError('connection refused',request=request)
            if fault=='timeout':raise httpx.ReadTimeout('cold startup',request=request)
            return httpx.Response(fault,json={'detail':'starting'})
        return healthy(request)
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            with pytest.raises(AppError) as error:
                await provider.require_context_ready(client,'http://localhost:8188')
            assert error.value.code=='context_ir_comfy_starting'
    asyncio.run(exercise())
    assert all(method=='GET' for method,_ in calls)
    assert not any(path in ['/upload/image','/prompt','/health'] for _,path in calls)


@pytest.mark.parametrize('invalid,code',[
    ('queue','context_ir_queue_invalid'),('missing','context_ir_nodes_missing'),('nodes','context_ir_nodes_invalid')])
def test_invalid_identity_or_missing_nodes_are_not_disguised_as_starting(invalid,code):
    def reply(request):
        if invalid=='queue' and request.url.path=='/queue':return httpx.Response(200,json={'ok':True})
        if request.url.path=='/object_info':return httpx.Response(200,json=[] if invalid=='nodes' else {})
        return healthy(request)
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            with pytest.raises(AppError) as error:await provider.require_context_ready(client,'http://localhost:8188')
            assert error.value.code==code
    asyncio.run(exercise())


def test_comfy_wait_budget_is_300_seconds_and_qwen_keeps_its_120_seconds():
    entry={'status':'pending','attempts':0,'child_id':None,'history':[]}
    pipeline._defer_context_dispatch(entry,'context_ir_comfy_starting','starting',now=100)
    pipeline._defer_context_dispatch(entry,'context_ir_comfy_starting','starting',now=399)
    assert entry['status']=='retrying' and entry['attempts']==0
    pipeline._defer_context_dispatch(entry,'context_ir_comfy_starting','starting',now=400)
    assert entry['status']=='exception' and entry['code']=='context_ir_comfy_unavailable'
    assert 'retry_after' not in entry
    pipeline._defer_context_dispatch(entry,'context_ir_qwen_starting','restoring',now=500)
    assert entry['context_dispatch_wait']=={'since':500,'code':'context_ir_qwen_starting'}
    pipeline._defer_context_dispatch(entry,'context_ir_qwen_starting','restoring',now=620)
    assert entry['code']=='context_ir_qwen_unavailable' and entry['attempts']==0


@pytest.fixture
def context_case(session,settings):
    project,shot=add_shot(session,'Isolated cold startup')
    run=pipeline.create_pipeline(session,settings,project.id,{'shot_ids':[shot.id]})
    session.commit()
    entry=next(n for n in run.nodes_json if n['kind']=='video')
    return SimpleNamespace(db=session,settings=settings,project=project,shot=shot,run=run,entry=entry,
        app=SimpleNamespace(state=SimpleNamespace(settings=settings)))


def invoke(c):
    asyncio.run(pipeline._video(c.app,c.db,c.settings,c.run,c.entry,True))


def test_cold_preflight_never_creates_jobs_or_uses_attempts_then_submits_once(context_case,monkeypatch):
    c=context_case;checks=[];submitted=[]
    async def admission(*args,**kwargs):
        checks.append(args)
        if len(checks)<=3:raise AppError(503,'context_ir_comfy_starting','starting')
    async def generate(request,db,shot,payload,**kwargs):
        submitted.append(payload.seed)
        job=m.GenerationJob(project_id=shot.project_id,shot_id=shot.id,provider='comfyui_h3',
            status='queued',provider_job_id='isolated-exact-provider-id',prompt='No inference',seed=payload.seed)
        db.add(job);db.flush();return job,None
    monkeypatch.setattr(provider,'check_context_ready',admission)
    from app.api import routes
    monkeypatch.setattr(routes,'generate_for_shot',generate)
    for _ in range(3):
        invoke(c)
        assert c.entry['status']=='retrying' and c.entry['attempts']==0 and c.entry['child_id'] is None
        assert c.db.scalar(select(func.count()).select_from(m.GenerationJob))==0
    invoke(c);invoke(c)
    assert len(submitted)==1 and len(checks)==4
    assert c.entry['attempts']==1 and c.entry['status']=='running'
    assert 'retry_after' not in c.entry and 'context_dispatch_wait' not in c.entry
    assert c.db.scalar(select(func.count()).select_from(m.GenerationJob))==1


@pytest.mark.parametrize('error_code',['context_ir_comfy_starting','context_ir_comfy_unavailable'])
def test_attached_known_pre_submit_failure_refunds_attempt_and_keeps_history(context_case,error_code,tmp_path,monkeypatch):
    c=context_case
    job=m.GenerationJob(project_id=c.project.id,shot_id=c.shot.id,provider='comfyui_h3',status='failed',
        error_code=error_code,error_message='Cold startup before prompt',prompt='No inference',seed=17)
    c.db.add(job);c.db.flush();c.entry.update(child_id=job.id,attempts=1,status='running')
    from app.core import config
    monkeypatch.setattr(config,'PROJECT_ROOT',tmp_path)
    # Legacy submit always created its own log directory before checking HTTP.
    (tmp_path/'logs/jobs'/job.id).mkdir(parents=True)
    invoke(c)
    assert c.entry['attempts']==0 and c.entry['child_id'] is None and c.entry['status']=='retrying'
    assert c.entry['code']=='context_ir_comfy_starting' and c.entry['history'][-1]['child_id']==job.id
    assert c.db.get(m.GenerationJob,job.id).status=='failed'


@pytest.mark.parametrize('evidence',['missing_directory','submission_snapshot'])
def test_legacy_unavailable_without_intact_no_submit_evidence_is_not_refunded(context_case,tmp_path,monkeypatch,evidence):
    c=context_case
    job=m.GenerationJob(project_id=c.project.id,shot_id=c.shot.id,provider='comfyui_h3',status='failed',
        error_code='context_ir_comfy_unavailable',error_message='Legacy row',prompt='No inference')
    c.db.add(job);c.db.flush();c.entry.update(child_id=job.id,attempts=1,status='running')
    from app.core import config
    monkeypatch.setattr(config,'PROJECT_ROOT',tmp_path)
    if evidence=='submission_snapshot':
        directory=tmp_path/'logs/jobs'/job.id;directory.mkdir(parents=True)
        (directory/'submitted_prompt.json').write_text('{}',encoding='utf-8')
    invoke(c)
    assert c.entry['attempts']==1 and c.entry['child_id']==job.id and c.entry['status']=='exception'


@pytest.mark.parametrize('status,code,has_id',[
    ('unknown','context_ir_submission_unknown',False),('failed','context_ir_comfy_starting',True)])
def test_unknown_or_accepted_task_is_never_refunded_or_resubmitted(context_case,status,code,has_id):
    c=context_case
    job=m.GenerationJob(project_id=c.project.id,shot_id=c.shot.id,provider='comfyui_h3',status=status,
        error_code=code,error_message='Original outcome preserved',provider_job_id='accepted-id' if has_id else None,prompt='No inference')
    c.db.add(job);c.db.flush();c.entry.update(child_id=job.id,attempts=1,status='running')
    invoke(c)
    assert c.entry['child_id']==job.id and c.entry['attempts']==1 and c.entry['status'] in ['unknown','exception']
    assert c.db.scalar(select(func.count()).select_from(m.GenerationJob))==1


def test_explicit_retry_clears_stale_wait_timestamp(context_case):
    from app.api.studio import retry
    c=context_case;c.entry.update(status='exception',code='context_ir_comfy_unavailable',
        retry_after=10**12,context_dispatch_wait={'since':1,'code':'context_ir_comfy_starting'})
    c.run.nodes_json=[c.entry];c.db.commit()
    retry(c.project.id,{'node_id':c.entry['id']},session=c.db)
    saved=c.db.get(m.StudioRun,c.run.id).nodes_json[0]
    assert saved['status']=='pending' and 'retry_after' not in saved and 'context_dispatch_wait' not in saved


def provider_request(tmp_path):
    return VideoGenerationRequest(job_id='isolated_job',project_id='isolated_project',shot_id='isolated_shot',
        prompt='Ocean waves.',negative_prompt='',duration_seconds=3,aspect_ratio='16:9',seed=17,
        parameters={'width':1024,'height':576},reference_assets=[],output_dir=tmp_path)


@pytest.mark.parametrize('result',['timeout',503,'missing_id','bad_json'])
def test_prompt_post_outcome_stays_unknown_without_retry(settings,tmp_path,monkeypatch,result):
    real=httpx.AsyncClient;calls=[]
    def reply(request):
        calls.append((request.method,request.url.path))
        if request.url.path=='/prompt':
            if result=='timeout':raise httpx.ReadTimeout('unknown accepted result',request=request)
            if result=='bad_json':return httpx.Response(200,text='{')
            return httpx.Response(result if isinstance(result,int) else 200,json={})
        return healthy(request)
    if hasattr(provider,'context_client'):
        monkeypatch.setattr(provider,'context_client',lambda *a,**kw:real(transport=httpx.MockTransport(reply),**kw))
    else:
        monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:real(transport=httpx.MockTransport(reply),**kw))
    monkeypatch.setattr(provider,'PROJECT_ROOT',tmp_path)
    settings.comfyui_base_url='http://localhost:8188'
    async def exercise():
        with pytest.raises(AppError) as error:
            await provider.ContextIRH3Provider(settings,freeze_workflow('fast')).submit(provider_request(tmp_path))
        assert error.value.code=='context_ir_submission_unknown'
    asyncio.run(exercise())
    assert calls.count(('POST','/prompt'))==1
    assert not (tmp_path/'logs/jobs/isolated_job/context-no-submit.json').exists()


@pytest.mark.parametrize('upload_status',[503,401,403,404])
def test_upload_failure_is_definitively_before_prompt_with_correct_retry_class(settings,tmp_path,monkeypatch,upload_status):
    real=httpx.AsyncClient;calls=[]
    def reply(request):
        calls.append((request.method,request.url.path))
        if request.url.path=='/upload/image':return httpx.Response(upload_status,json={'detail':'starting or rejected'})
        return healthy(request)
    if hasattr(provider,'context_client'):
        monkeypatch.setattr(provider,'context_client',lambda *a,**kw:real(transport=httpx.MockTransport(reply),**kw))
    else:
        monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:real(transport=httpx.MockTransport(reply),**kw))
    monkeypatch.setattr(provider,'PROJECT_ROOT',tmp_path)
    settings.comfyui_base_url='http://localhost:8188'
    image=BytesIO();Image.new('RGB',(16,16),'gray').save(image,'PNG')
    monkeypatch.setattr(provider,'read_image',lambda *_:(image.getvalue(),('png','image/png')))
    request=provider_request(tmp_path);request.reference_assets=[{'asset_id':'test','sha256':'a'*64,'role':'product','product_id':'test'}]
    async def exercise():
        with pytest.raises(AppError) as error:
            await provider.ContextIRH3Provider(settings,freeze_workflow('fast')).submit(request)
        assert error.value.code==('context_ir_comfy_starting' if upload_status==503 else 'context_ir_comfy_request_failed')
    asyncio.run(exercise())
    assert ('POST','/prompt') not in calls and calls.count(('POST','/upload/image'))==1
    receipt=tmp_path/'logs/jobs/isolated_job/context-no-submit.json'
    if upload_status==503:
        assert json.loads(receipt.read_text())['prompt_submission_started'] is False
    else:assert not receipt.exists()
