"""Caller framing must override only the frozen generation copy, without models."""
from copy import deepcopy

import pytest
from sqlalchemy import select

from app import models as m
from app.core.db import Base, build_engine, build_session_factory
from app.services.ai_services import seed_local_services
from app.services.context_production_inputs import collect_context_inputs, restore_context_inputs
from app.services.revision_cinematography import apply_cinematography_override, explicit_cinematography


@pytest.mark.parametrize('instruction,key',[
    ('固定中近景，手掌与脸同时入镜。','medium_close_up'),
    ('本次景别固定中景，画面覆盖人物腰部以上及完整脸部。','medium'),
    ('改为微距超级特写。','macro'),
    ('景别改成超级特写。','extreme_close_up'),
    ('Use a medium close-up.','medium_close_up'),
    ('Use a medium shot.','medium'),
    ('从特写改为中景。','medium'),
    ('不要特写，改成中景。','medium'),
    ('Change the framing from close-up to medium shot.','medium'),
    ('Do not use a macro shot. Use a medium shot.','medium'),
    ('全景拍摄。','full'),
])
def test_explicit_shot_size_keeps_close_up_and_medium_distinct(instruction,key):
    result=explicit_cinematography(instruction)
    assert result['shot_size']==key
    assert result['source']=='instruction' and result['source_text']


@pytest.mark.parametrize('instruction',[
    None,'','P02只有水杯大小。','不要中景。','不要改为中景。',
    '中景或特写都可以。','中近景／中景二选一。','Use either a medium shot or a close-up.',
    '不要使用固定中景。','Do not use a medium shot.','不要特写，保持原镜头。',
    '对白：“改为中景。”，产品不变。','产品图片中近景标注保持不变。',
    '中景。特写。',
    '机位不固定。','不采用中景。','不切换为中景。',
])
def test_unknown_negative_ambiguous_or_content_is_not_a_camera_override(instruction):
    assert explicit_cinematography(instruction)=={}


def test_camera_position_retains_direction_and_lens_while_only_framing_changes():
    photo={'shot_size':'微距超级特写（Macro Shot）','framing':'product close-up',
           'camera_position':'正面固定近景，略高机位','lens':'35 mm','lighting':'暖光'}
    before=deepcopy(photo)
    move,effective,audit=apply_cinematography_override('缓慢前推',photo,'改成中近景。')
    assert move=='缓慢前推'
    assert effective['shot_size']=='中近景（Medium Close-Up）'
    assert effective['framing']=='product Medium Close-Up'
    assert effective['camera_position']=='正面固定中近景，略高机位'
    assert effective['lens']=='35 mm' and effective['lighting']=='暖光'
    assert photo==before and audit['before']['photography']==before
    assert audit['after']['photography']==effective and audit['changed']
    assert apply_cinematography_override(move,effective,'改成中近景。')[:2]==(move,effective)
    effective['lens']='changed copy'
    assert audit['after']['photography']['lens']=='35 mm' and photo==before


def test_explicit_static_camera_changes_motion_without_guessing_a_new_angle():
    photo={'camera_position':'正面略高机位','shot_size':'特写'}
    move,effective,audit=apply_cinematography_override('缓慢左移',photo,'机位固定。')
    assert move=='固定（holds static）' and effective==photo
    assert audit['camera_move']=='固定（holds static）'
    assert photo=={'camera_position':'正面略高机位','shot_size':'特写'}


@pytest.fixture
def scene_case(settings):
    settings.h3_demo_mode=False
    engine=build_engine(settings.database_url)
    Base.metadata.create_all(engine)
    try:
        with build_session_factory(engine)() as session:
            seed_local_services(session,settings)
            service=session.scalar(select(m.AIService).where(m.AIService.module=='video_generation'))
            service.settings_json={**service.settings_json,'workflow_profile':'context_ir','h3_context_ir':True}
            project=m.Project(name='Isolated camera override')
            session.add(project);session.flush()
            script=m.ScriptRevision(project_id=project.id,revision=1,raw_text='Original macro script.',status='approved')
            shot=m.StoryboardShot(project_id=project.id,order=1,status='approved',aspect_ratio='9:16',
                visual_prompt='Show P02 in the living room.',camera_move='固定',action='P02在手掌中。')
            session.add_all([script,shot]);session.flush()
            detail=m.ShotDetail(shot_id=shot.id,photography_json={
                'shot_size':'微距超级特写（Macro Shot）','camera_position':'正面固定近景。','lens':'35 mm'},
                source_json={'production':{}})
            session.add(detail);session.commit()
            yield session,settings,shot,detail,script
    finally:
        engine.dispose()


@pytest.mark.parametrize('instruction,expected',[
    ('固定中近景，手掌、P02与完整脸部同框。','中近景（Medium Close-Up）'),
    ('本次景别固定中景，画面覆盖人物腰部以上及完整脸部。','中景（Medium Shot）'),
])
def test_real_collector_freezes_effective_camera_and_preserves_approved_sources(scene_case,instruction,expected):
    session,settings,shot,detail,script=scene_case
    original={'photo':deepcopy(detail.photography_json),'move':shot.camera_move,
              'action':shot.action,'prompt':shot.visual_prompt,'revision':shot.revision,'script':script.raw_text}
    snapshot=collect_context_inputs(session,settings,shot,instruction=instruction)
    photo=snapshot['effective_cinematography']['photography']
    assert photo['shot_size']==expected
    assert 'Macro Shot' not in snapshot['prompt'] and '微距' not in snapshot['prompt']
    assert '正面固定近景' not in snapshot['prompt']
    assert photo['camera_position'].startswith('正面固定'+('中近景' if '中近景' in expected else '中景'))
    assert photo['lens']=='35 mm'
    assert snapshot['effective_cinematography']['camera_move']=='固定（holds static）'
    camera=snapshot['revision_overrides_audit']['cinematography']
    assert camera['source_text']==[instruction.rstrip('。')] and camera['before']['photography']==original['photo']
    assert camera['after']==snapshot['effective_cinematography']
    assert snapshot['screenplay_snapshot']['detail']['photography_json']==original['photo']
    assert snapshot['screenplay_snapshot']['shot']['camera_move']=='固定'
    assert collect_context_inputs(session,settings,shot,instruction=instruction)==snapshot
    assert restore_context_inputs(session,settings,shot,snapshot)==([],[])
    session.expire_all()
    assert detail.photography_json==original['photo'] and shot.camera_move==original['move']
    assert shot.action==original['action'] and shot.visual_prompt==original['prompt']
    assert shot.revision==original['revision'] and script.raw_text==original['script']


def test_negative_camera_instruction_leaves_old_generation_values_and_has_no_override_audit(scene_case):
    session,settings,shot,detail,_=scene_case
    snapshot=collect_context_inputs(session,settings,shot,instruction='P02颜色不变，不要改为中景。')
    assert snapshot['effective_cinematography']=={'camera_move':'固定','photography':detail.photography_json}
    assert 'cinematography' not in snapshot['revision_overrides_audit']
    assert 'Macro Shot' in snapshot['prompt']
