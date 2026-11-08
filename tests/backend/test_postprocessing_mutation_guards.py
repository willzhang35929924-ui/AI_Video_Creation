"""Post-generation audio/quality processing still owns its immutable inputs."""
import hashlib

import httpx
import pytest

from app import models as m
from test_shot_framing import case, endpoint, payload, assert_unchanged
from test_studio_voice import voice_project, save


@pytest.fixture(autouse=True)
def no_provider(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError('Mutation guards must not request model services')
    monkeypatch.setattr(httpx.AsyncClient, 'request', forbidden)


def test_postprocessing_blocks_framing_then_reopens_after_completion(case):
    with case.client.app.state.session_factory() as db:
        db.get(m.GenerationJob, case.job).status='postprocessing';db.commit()
    result=case.client.put(endpoint(case),json=payload(case))
    assert result.status_code==409 and result.json()['error']['code']=='shot_framing_blocked'
    assert_unchanged(case)
    with case.client.app.state.session_factory() as db:
        db.get(m.GenerationJob, case.job).status='generated';db.commit()
    result=case.client.put(endpoint(case),json=payload(case))
    assert result.status_code==200,result.text
    assert set(result.json()['updated_shot_ids'])=={case.first,case.second}


def test_postprocessing_blocks_reference_edit_unbinding_and_job_deletion_then_reopens(client,settings):
    settings.media_root.mkdir(parents=True,exist_ok=True)
    source=settings.media_root/'reference-original.mp4';source.write_bytes(b'Unchanged isolated reference bytes')
    with client.app.state.session_factory() as db:
        project=m.Project(name='Postprocessing reference ownership');product=m.Product(name='Isolated plush')
        db.add_all([project,product]);db.flush()
        shot=m.StoryboardShot(project_id=project.id,order=1);db.add(shot);db.flush()
        reference=m.ReferenceVideo(name='Reference',filename=source.name,relative_path=source.name,
            sha256=hashlib.sha256(source.read_bytes()).hexdigest(),metadata_json={'duration_seconds':6},state='active',revision=1)
        binding=m.ProjectProductBinding(project_id=project.id,product_id=product.id,snapshot_json={})
        db.add_all([reference,binding]);db.flush()
        job=m.GenerationJob(project_id=project.id,shot_id=shot.id,provider='isolated-no-inference',
            status='postprocessing',prompt='No inference',parameters_json={'motion_references':[{'video_id':reference.id}]})
        db.add(job);db.commit()
        reference_id,product_id,binding_id,job_id=reference.id,product.id,binding.id,job.id
    url='/api/reference-videos/'+reference_id
    edit={'revision':1,'name':'Renamed','category':'','tags':[],'notes':'','source_note':'','usage_scope':'abstract_style_only'}
    unlink=f'/api/product-library/{product_id}/project-bindings/{binding_id}'
    deletion={'resource':'video_jobs','ids':[job_id],'mode':'keep','cascade':False}
    assert client.patch(url,json=edit).status_code==409
    assert client.delete(url+'?revision=1').status_code==409
    denied=client.delete(unlink)
    assert denied.status_code==409 and denied.json()['error']['code']=='product_binding_busy'
    preview=client.post('/api/deletions/preview',json=deletion)
    assert preview.status_code==200 and preview.json()['blockers']
    with client.app.state.session_factory() as db:
        assert db.get(m.ReferenceVideo,reference_id).revision==1
        assert db.get(m.ProjectProductBinding,binding_id) is not None
        db.get(m.GenerationJob,job_id).status='generated';db.commit()
    changed=client.patch(url,json=edit)
    assert changed.status_code==200,changed.text
    assert client.delete(unlink).status_code==200
    preview=client.post('/api/deletions/preview',json=deletion)
    assert preview.status_code==200 and preview.json()['blockers']==[]
    assert source.read_bytes()==b'Unchanged isolated reference bytes'


def test_postprocessing_blocks_voice_and_structured_shot_edits_then_reopens(client,voice_project):
    fixture=voice_project
    with client.app.state.session_factory() as db:
        video=db.get(m.VideoRevision,fixture.video)
        job_id=video.generation_job_id
        db.get(m.GenerationJob,job_id).status='postprocessing';db.commit()
    participant='/api/participants/'+fixture.participants[0]
    edit={'performance':{'action':'Updated performance after completion'}}
    denied=client.patch(participant,json=edit)
    assert denied.status_code==409 and denied.json()['error']['code']=='generation_active'
    denied=save(client,fixture)
    assert denied.status_code==409 and denied.json()['error']['code']=='studio_voice_generation_active'
    with client.app.state.session_factory() as db:
        assert db.get(m.StoryboardShot,fixture.shots[0]).revision==1
        assert db.get(m.ShotCharacter,fixture.participants[0]).performance_json['action']=='核对记录'
        db.get(m.GenerationJob,job_id).status='generated';db.commit()
    changed=client.patch(participant,json=edit)
    assert changed.status_code==200,changed.text
    changed=save(client,fixture)
    assert changed.status_code==200,changed.text
    assert fixture.path.is_file()
