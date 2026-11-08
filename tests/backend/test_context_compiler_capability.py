"""Compiler capability admission is read-only and separate from cold startup."""
import asyncio
from copy import deepcopy

import httpx
import pytest

from app.core.errors import AppError
from app.services import h3_context_provider as provider
from test_context_cold_start import HEALTH


COMPILER='VideoFactoryLocalH3IRCompile'
POLICY=['STRING',{'default':'revision_semantics_v2'}]


def inventory(definition):
    return {COMPILER:definition,'VideoFactoryLocalVLMGuard':{'input':{'required':{}}}}


@pytest.mark.parametrize('definition',[
    {}, {'input':{}}, {'input':None},
    {'input':{'optional':{'production_contract':['INT',{}],'caller_contract_policy':POLICY}}},
    {'input':{'optional':{'production_contract':'STRING','caller_contract_policy':POLICY}}},
    {'input':{'optional':{'production_contract':[],'caller_contract_policy':POLICY}}},
    {'input':{'optional':{'production_contract':['STRING',{'default':''}]}}},
    {'input':{'optional':{'production_contract':['STRING',{}],
                          'caller_contract_policy':['STRING',{'default':'legacy'}]}}},
    {'input':{'optional':{'production_contract':['STRING',{}],
                          'caller_contract_policy':['STRING',{'default':'revision_semantics_v1'}]}}},
    {'input':{'optional':{'production_contract':['STRING',{}],
                          'caller_contract_policy':['STRING',{}]}}},
    {'input':{'optional':{'production_contract':['STRING',{}],
                          'caller_contract_policy':['INT',{'default':'revision_semantics_v2'}]}}},
    {'input':{'optional':{'production_contract':['STRING',{}],
                          'caller_contract_policy':'revision_semantics_v2'}}},
    {'input':{'optional':{'production_contract':['STRING',{}],
                          'caller_contract_policy':[]}}},
])
def test_older_or_malformed_compiler_is_rejected_before_upload_or_qwen(definition):
    calls=[]
    def reply(request):
        calls.append((request.method,request.url.path))
        if request.url.path=='/queue':return httpx.Response(200,json={'queue_running':[],'queue_pending':[]})
        if request.url.path=='/object_info':return httpx.Response(200,json=inventory(definition))
        raise AssertionError('No upload, model request or Qwen probe is permitted: '+str(request.url))
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            with pytest.raises(AppError) as error:
                await provider.require_context_ready(client,'http://localhost:8188')
        assert error.value.status_code==409
        assert error.value.code=='context_ir_compiler_update_required'
        assert '更新项目自有节点' in error.value.message and '空闲后重启' in error.value.message
    asyncio.run(exercise())
    assert calls==[('GET','/queue'),('GET','/object_info')]


@pytest.mark.parametrize('group',['required','optional'])
@pytest.mark.parametrize('policy_group',['required','optional'])
def test_string_contract_in_required_or_optional_admits_current_local_service(group,policy_group):
    calls=[]
    definition={'input':{group:{'production_contract':['STRING',{'default':''}]}}}
    definition['input'].setdefault(policy_group,{})['caller_contract_policy']=deepcopy(POLICY)
    before=deepcopy(definition)
    def reply(request):
        calls.append((request.method,request.url.path))
        if request.url.path=='/queue':return httpx.Response(200,json={'queue_running':[],'queue_pending':[]})
        if request.url.path=='/object_info':return httpx.Response(200,json=inventory(definition))
        if request.url.path=='/health':return httpx.Response(200,json=HEALTH)
        raise AssertionError('Unexpected side effect '+str(request.url))
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            await provider.require_context_ready(client,'http://localhost:8188')
    asyncio.run(exercise())
    assert definition==before
    assert calls==[('GET','/queue'),('GET','/object_info'),('GET','/health')]


def test_busy_queue_has_priority_over_upgrade_diagnostic():
    calls=[]
    def reply(request):
        calls.append((request.method,request.url.path))
        if request.url.path=='/queue':return httpx.Response(200,json={'queue_running':[[1,'existing-task']],'queue_pending':[]})
        if request.url.path=='/object_info':return httpx.Response(200,json=inventory({}))
        raise AssertionError(str(request.url))
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            with pytest.raises(AppError) as error:
                await provider.require_context_ready(client,'http://localhost:8188')
            assert error.value.code=='local_gpu_busy'
    asyncio.run(exercise())
    assert all(method=='GET' for method,_ in calls)


@pytest.mark.parametrize('current',[False,True])
def test_connection_health_reports_compiler_upgrade_without_claiming_generation_ready(settings,monkeypatch,current):
    calls=[];real=httpx.AsyncClient
    definition={'input':{'optional':{'production_contract':['STRING',{}],
                                     'caller_contract_policy':deepcopy(POLICY)}}} if current else {}
    def reply(request):
        calls.append((request.method,request.url.path))
        if request.url.path=='/queue':return httpx.Response(200,json={'queue_running':[],'queue_pending':[]})
        if request.url.path=='/object_info/'+COMPILER:return httpx.Response(200,json={COMPILER:definition})
        if request.url.path=='/health':return httpx.Response(200,json=HEALTH)
        raise AssertionError(str(request.url))
    if hasattr(provider,'context_client'):
        monkeypatch.setattr(provider,'context_client',lambda *a,**kw:real(transport=httpx.MockTransport(reply),**kw))
    else:
        monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:real(transport=httpx.MockTransport(reply),**kw))
    result=asyncio.run(provider.probe_context_health(settings))
    assert result['connected'] is True
    assert result['ready'] is current
    if not current:
        assert '更新项目自有节点' in result['error']
        assert ('GET','/health') not in calls
    assert all(method=='GET' for method,_ in calls)
