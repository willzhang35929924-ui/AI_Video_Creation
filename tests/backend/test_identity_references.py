"""Isolated contracts; these assertions do not certify generated human likeness."""
import copy
import hashlib
import json
import pytest
from sqlalchemy import select
from app import models as m
from app.core.errors import AppError
from app.services.identity_references import actor_references
from app.services.screenplay import shot_bundle,generation_inputs
from app.services.cinematic_quality import physical_directions,visual_inputs
from app.services.cinematic_rules import fingerprint
from app.services.providers import bind_ordered_reference_slots
from test_studio_pipeline import studio,seed_approved,existing_visuals,png


def cast(studio):
    return shot_bundle(studio.session,studio.shots[0].id)['participants']


def test_compiler_rejects_cross_character_approved_version_even_with_matching_asset(studio):
    seed_approved(studio);people=cast(studio)
    people[0]['character_version_id']=people[1]['character_version_id']
    people[0]['reference_asset_ids_json']=[studio.cast[1][1].standard_face_asset_id]
    with pytest.raises(AppError) as error:actor_references(studio.session,studio.settings,people)
    assert error.value.code=='identity_version_owner_mismatch'


def test_distinct_actor_ids_cannot_reuse_identical_reference_bytes(studio):
    seed_approved(studio);s=studio.session;people=cast(studio)
    first=s.get(m.CharacterAsset,studio.cast[0][1].standard_face_asset_id)
    second=s.get(m.CharacterAsset,studio.cast[1][1].standard_face_asset_id)
    (studio.settings.media_root/second.relative_path).write_bytes((studio.settings.media_root/first.relative_path).read_bytes())
    second.sha256=first.sha256;s.flush()
    for p,(_,v) in zip(people,studio.cast):p['reference_asset_ids_json']=[v.standard_face_asset_id]
    with pytest.raises(AppError) as error:actor_references(s,studio.settings,people)
    assert error.value.code=='identity_reference_shared_across_actors'


def prepare_compiler(studio):
    s=studio.session;scene,frames=existing_visuals(studio);shot=studio.shots[0]
    detail=s.scalar(select(m.ShotDetail).where(m.ShotDetail.shot_id==shot.id))
    detail.source_json={**detail.source_json,'production':{'visual_ids':[scene.id,frames[0].id]}}
    people=list(s.scalars(select(m.ShotCharacter).where(m.ShotCharacter.shot_id==shot.id).order_by(m.ShotCharacter.order)))
    for p,position in zip(people,['左侧','中央','右侧']):p.performance_json={'position':position,'action':'stands still'}
    frames[0].snapshot_json={**frames[0].snapshot_json,'inputs_hash':fingerprint(visual_inputs(s,shot))}
    s.add(m.DialogueLine(shot_id=shot.id,participant_id=people[2].id,order=1,text='Received.'))
    s.add(m.DialogueLine(shot_id=shot.id,participant_id=people[0].id,order=2,text='Thank you.'));s.commit()
    return shot,scene,frames[0],detail,people


def test_subject_picture_order_and_first_vocal_speaker_order_are_independent(studio):
    shot,scene,keyframe,_,people=prepare_compiler(studio)
    _,refs,prompt,snapshot=generation_inputs(studio.session,shot,studio.settings)
    assert [r['picture_index'] for r in refs]==[1,2,3,4,5]
    assert len({r['sha256'] for r in refs[:3]})==3
    assert '<Subject 3> (S1)' in prompt and '<Subject 1> (S2)' in prompt
    assert 'screen left' in prompt and 'screen center' in prompt and 'screen right' in prompt
    manifest=snapshot['reference_manifest']
    assert [r['role'] for r in manifest['references']]==['identity','identity','identity','scene','composition']
    assert manifest['speakers'][0]['participant_id']==people[2].id
    assert manifest['sha256']==fingerprint({k:v for k,v in manifest.items() if k!='sha256'})


def test_independent_identity_route_omits_synthesized_faces_without_changing_formal_assets(studio):
    shot,scene,keyframe,detail,people=prepare_compiler(studio);s=studio.session
    _,full,_,original=generation_inputs(s,shot,studio.settings)
    before=[(p.character_version_id,list(p.reference_asset_ids_json)) for p in people]
    detail.source_json={**detail.source_json,'production':{**detail.source_json['production'],'requested_controls':{'identity_reference_policy':'independent_faces_scene'}}}
    keyframe.snapshot_json={**keyframe.snapshot_json,'inputs_hash':fingerprint(visual_inputs(s,shot))};s.flush()
    _,isolated,prompt,snapshot=generation_inputs(s,shot,studio.settings)
    assert len(full)==5 and len(isolated)==4
    assert [r['asset_id'] for r in isolated]==[r['asset_id'] for r in full[:-1]]
    assert all(r.get('role')!='composition' for r in isolated)
    assert snapshot['reference_manifest']['excluded_references'][0]['asset_id']==keyframe.id
    assert before==[(p.character_version_id,list(p.reference_asset_ids_json)) for p in people]
    assert '<Picture 5>' not in prompt and snapshot['reference_manifest']['policy']=='independent_faces_scene'


def test_provider_replaces_extra_template_slots_in_exact_numeric_order():
    graph={'base':{'class_type':'LoadImage','inputs':{'image':'old'}} ,'r2v':{'class_type':'MiniMaxH3ReferenceToVideo','inputs':{'ref_images.ref_image_8':['stale',0],'ref_images.ref_image_0':['base',0]}}}
    metadata={'reference_paths':[['base','inputs','image'],['stale','inputs','image']]}
    paths=bind_ordered_reference_slots(graph,metadata,[{},{}])
    assert list(graph['r2v']['inputs'])==['ref_images.ref_image_0','ref_images.ref_image_1']
    assert len(paths)==2 and metadata['reference_tags']==['<Picture 1>','<Picture 2>']


def test_handoff_constraints_allow_receiver_contact_without_object_duplication():
    context={'production':{'prop_states':[{'prop_key':'NOTEBOOK','holder':'giver','hand':'right'}],
        'beats':[{'character_id':'giver','action':'passes notebook to receiver','contact_target':'receiver'}]},'entities':[]}
    directions=physical_directions(context)
    assert 'exactly ONE physical NOTEBOOK' in directions and 'receiver may contact and receive this SAME single object' in directions
    assert 'BOTH HANDS EMPTY' not in directions and 'Preserve this static state' not in directions
