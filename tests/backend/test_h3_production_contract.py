"""Frozen caller constraints use the same preview/submit preparation path."""
import json
import pytest
from app.core.errors import AppError
from app.services.h3_context_provider import prepare_context_compilation
from app.services.h3_workflow_router import build_graph,freeze_workflow


def image(product='product-one',**extra):
    return {'role':'product','product_id':product,'source':'product_library','product_name':'Tree toy',
            'image_notes':'same green variant','constraint_target_keys':['P02'],**extra}


def test_constraints_and_immutable_identity_are_frozen_separately_and_reach_real_graph():
    refs=[image(),image()]
    constraints={'version':1,'items':[{'target_key':'P02','measurements':[{'axis':'height','value':'3.5','unit':'cm'}]}]}
    prompt='本次修订P02高3.5cm。\n已绑定制作设定：'+json.dumps([{'类别':'prop','名称':'Tree toy','内容':{
        'color':'green','material':'plush','dimensions':'height 3.5cm','immutable_features':'yellow star'}}],ensure_ascii=False)
    tray,decl,intent,contract=prepare_context_compilation(prompt,'',refs,[],{'revision_constraints':constraints})
    assert contract['constraint_target_groups']=={'P02':'product-1'}
    assert contract['revision_constraints']==constraints
    description=contract['product_groups']['product-1']['description']
    assert 'yellow star' in description and 'dimensions' not in description
    assert '3.5cm' in intent
    uploaded=[{**slot,'file':f'video_factory/picture{i}.png [input]'} for i,slot in enumerate(tray,1)]
    graph=build_graph(freeze_workflow('fast'),tray=uploaded,intent=intent,duration_seconds=4,aspect_ratio='9:16',
        seed=123,width=576,height=1024,output_prefix='project/shot/job',production_contract=contract,**decl)
    assert json.loads(graph['4']['inputs']['production_contract'])==contract
    assert graph['4']['class_type']=='VideoFactoryLocalH3IRCompile'


def test_one_target_cannot_be_silently_bound_to_two_products():
    with pytest.raises(AppError) as error:
        prepare_context_compilation('P02缩小','',[image(),image('another-product')],[],{})
    assert error.value.code=='revision_target_ambiguous'


def test_text_only_target_is_explicitly_requested_in_one_written_subject():
    constraints={'version':1,'items':[{'target_key':'P02'}]}
    _,_,intent,contract=prepare_context_compilation('P02高3.5cm','',[],[],{'revision_constraints':constraints})
    assert 'literal caller label P02' in intent
    assert contract['constraint_target_groups']=={}
