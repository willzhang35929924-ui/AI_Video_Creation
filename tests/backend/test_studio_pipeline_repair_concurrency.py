"""Isolated orchestration race; the provider await is never a model request."""
import asyncio
import copy

from fastapi import Request
import pytest
from sqlalchemy import select

from app import models as m
from app.api import studio
from app.services import gpu_gate, studio_pipeline
from test_reference_budget_repair import setup_shot


def test_repair_waits_for_inflight_pipeline_and_keeps_accepted_child(client,settings,monkeypatch):
    case=setup_shot(client,settings,run_status='running')
    with client.app.state.session_factory() as session:
        run=session.get(m.StudioRun,case.run_id)
        run.nodes_json=[*run.nodes_json,{'id':'video:inflight','kind':'video','target_id':case.shot_id,
            'dependencies':[],'status':'pending','attempts':0,'child_id':None}]
        session.commit()

    async def exercise():
        started=asyncio.Event();returned=asyncio.Event();calls=[]

        async def provider_boundary(app,session,config,run,entry,dispatch):
            if entry.get('child_id'):return
            assert dispatch
            calls.append(entry['id']);started.set()
            await returned.wait()
            entry.update(status='running',child_id='accepted-provider-child')

        async def healthy_idle():return {'state':'idle','reason':'healthy_idle'}

        monkeypatch.setattr(studio_pipeline,'_video',provider_boundary)
        monkeypatch.setattr(gpu_gate,'owned_model_status',healthy_idle)
        with client.app.state.session_factory() as worker_session,client.app.state.session_factory() as repair_session:
            run=worker_session.get(m.StudioRun,case.run_id)
            worker=asyncio.create_task(studio_pipeline.advance_pipeline(client.app,settings,worker_session,run))
            await asyncio.wait_for(started.wait(),2)
            repair=asyncio.create_task(studio.repair(case.project_id,Request({'type':'http','app':client.app}),repair_session))
            await asyncio.sleep(0)
            assert not repair.done(), 'Repair must wait for the older graph writer.'
            returned.set()
            await asyncio.wait_for(worker,5)
            report=await asyncio.wait_for(repair,5)
            assert any(action['code']=='keyframe_reference_nodes_requeued' for action in report['actions'])
        assert calls==['video:inflight']

    asyncio.run(exercise())
    with client.app.state.session_factory() as session:
        run=session.get(m.StudioRun,case.run_id);nodes=copy.deepcopy(run.nodes_json)
        keyframe=next(n for n in nodes if n['kind']=='keyframe')
        assert keyframe['code'] is None and keyframe['child_id']
        assert next(n for n in nodes if n['id']=='video:inflight')['child_id']=='accepted-provider-child'


@pytest.mark.parametrize('control',['pause','product_change'])
def test_mixed_repair_respects_new_control_after_awaited_advance(client,settings,monkeypatch,control):
    case=setup_shot(client,settings,run_status='running');calls=[]
    with client.app.state.session_factory() as session:
        actor=session.scalar(select(m.ShotCharacter).where(m.ShotCharacter.shot_id==case.shot_id))
        run=session.get(m.StudioRun,case.run_id)
        run.nodes_json=[*run.nodes_json,{'id':'character:'+actor.character_id,'kind':'character','target_id':actor.character_id,
            'dependencies':[],'status':'exception','code':'capability_unverified','error':'previous capability preflight',
            'attempts':1,'child_id':None,'route':{}}]
        session.commit()

    async def changed_during_advance(app,config,session,run):
        calls.append(run.id)
        with client.app.state.session_factory() as controller:
            latest=controller.get(m.StudioRun,run.id)
            latest.status='paused'
            if control=='product_change':latest.snapshot_json={**latest.snapshot_json,'product_references_changed':True}
            controller.commit()
        await asyncio.sleep(0)

    async def healthy_idle():return {'state':'idle','reason':'healthy_idle'}
    monkeypatch.setattr(studio_pipeline,'advance_pipeline',changed_during_advance)
    monkeypatch.setattr(gpu_gate,'owned_model_status',healthy_idle)
    response=client.post(f'/api/studio/projects/{case.project_id}/repair',json={})
    assert response.status_code==200,response.text
    assert len(calls)==1
    with client.app.state.session_factory() as session:
        run=session.get(m.StudioRun,case.run_id)
        assert run.status=='paused'
        character=next(n for n in run.nodes_json if n['kind']=='character')
        assert character['status']=='exception' and character['child_id'] is None
