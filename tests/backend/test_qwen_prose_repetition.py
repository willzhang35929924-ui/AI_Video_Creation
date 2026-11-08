"""CPU decoding contracts; no weights, inference, service calls, or GPU use."""
from copy import deepcopy
import importlib.util
import os
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import patch
import uuid

import pytest


ROOT=Path(__file__).resolve().parents[2]
SCRIPTS=ROOT/'scripts'
if (SCRIPTS/'local_inference/bundle/serve_screenplay_qwen.py').is_file():
    SCRIPTS=SCRIPTS/'local_inference/bundle'


@pytest.fixture
def server(tmp_path):
    name='_isolated_qwen_prose_'+uuid.uuid4().hex
    spec=importlib.util.spec_from_file_location(name,SCRIPTS/'serve_screenplay_qwen.py')
    module=importlib.util.module_from_spec(spec)
    old_bytecode=sys.dont_write_bytecode
    sys.modules[name]=module
    try:
        with patch.dict(os.environ,{'VF_RUNTIME_ROOT':str(tmp_path),'VF_QWEN_MODEL':str(tmp_path/'no-weights')}):
            spec.loader.exec_module(module)
        yield module
    finally:
        sys.dont_write_bytecode=old_bytecode
        sys.modules.pop(name,None)


def request(server,**changes):
    return server.ChatRequest(model=server.MODEL_ID,messages=[{'role':'user','content':'Write the brief.'}],
                              **changes)


def word_ids(text):
    vocabulary={}
    return [vocabulary.setdefault(word,len(vocabulary)+100) for word in text.split()]


class Matrix:
    """Small 2-D array for always-run slicing/score preservation contracts."""
    def __init__(self,rows):self.rows=deepcopy(rows);self.shape=(len(rows),len(rows[0]))
    def tolist(self):return deepcopy(self.rows)
    def clone(self):return Matrix(self.rows)
    def __getitem__(self,key):
        if isinstance(key,int):return self.rows[key]
        row,column=key
        selected=self.rows[row]
        if isinstance(row,int):return selected[column]
        if isinstance(column,(tuple,list)):return Matrix([[values[index] for index in column] for values in selected])
        return Matrix([values[column] for values in selected])
    def __setitem__(self,key,value):
        row,column=key;indices=list(range(len(self.rows)))[row]
        if isinstance(indices,int):indices=[indices]
        data=value.rows if isinstance(value,Matrix) else value
        columns=column if isinstance(column,(list,tuple)) else list(range(self.shape[1]))[column]
        if isinstance(columns,int):columns=[columns]
        for target,values in zip(indices,data):
            for index,number in zip(columns,values):self.rows[target][index]=number


class Penalty:
    def __init__(self,penalty=1.1):self.penalty=penalty;self.seen=[]
    def __call__(self,input_ids,scores):
        self.seen.append(input_ids.tolist())
        result=scores.clone()
        for index in set(input_ids.rows[0]):
            score=result.rows[0][index]
            result.rows[0][index]=score*self.penalty if score<0 else score/self.penalty
        return result


def test_long_actual_failure_shape_stops_on_generated_repeat_and_keeps_input(server):
    paragraph=("The product's physical size remains constant throughout the shot, and the camera holds static. "
        "The product's plush texture and embroidered features are clearly visible, and the lighting highlights "
        "the product's soft, fluffy surface. The product's round base is fully visible, and its height is "
        "exactly half the person's face height.")
    period=word_ids(paragraph)
    assert 32<=len(period)<=256
    prompt=[7,8,9]
    tokens=[prompt+period*3]
    before=deepcopy(tokens)
    guard=server.RepeatedOutputGuard(len(prompt),check_every=1)
    assert guard(tokens,None) is True
    assert guard.triggered is True
    assert tokens==before
    receipt=guard.receipt()
    assert receipt['triggered'] is True


def test_repeated_prompt_does_not_count_as_repeated_output(server):
    prompt=list(range(40))*4
    guard=server.RepeatedOutputGuard(len(prompt),check_every=1)
    assert guard([prompt+list(range(40))*2],None) is False
    assert guard.triggered is False


@pytest.mark.parametrize('generated',[
    [1,2,3]*200,
    list(range(40))+[999]+list(range(40))+[998]+list(range(40)),
    list(range(800)),
])
def test_short_chants_separated_sections_and_normal_prose_are_not_runaways(server,generated):
    guard=server.RepeatedOutputGuard(4,check_every=1)
    assert guard([[90,91,92,93]+generated],None) is False
    assert guard.triggered is False


def test_eos_is_not_misclassified_as_a_loop(server):
    guard=server.RepeatedOutputGuard(2,eos_ids=(999,),check_every=1)
    assert guard([[5,6]+list(range(40))*3+[999]],None) is False
    assert guard.triggered is False


def test_default_stride_detects_a_long_loop_within_eight_output_tokens(server):
    period=list(range(100,137))
    output=period*4
    guard=server.RepeatedOutputGuard(2)
    first_trigger=None
    for length in range(1,len(output)+1):
        if guard([[1,2]+output[:length]],None):
            first_trigger=length
            break
    assert len(period)*3<=first_trigger<len(period)*3+8
    assert guard.receipt()['checked_generated_tokens']==first_trigger


@pytest.mark.parametrize('changes',[
    {'prompt_tokens':-1},{'min_period':2},{'max_period':513},
    {'repetitions':2},{'repetitions':5},{'check_every':0},{'check_every':33},
])
def test_guard_policy_is_explicitly_bounded(server,changes):
    with pytest.raises(ValueError):server.RepeatedOutputGuard(**{'prompt_tokens':2,**changes})


def test_penalty_sees_only_generated_tokens_and_preserves_eos_scores(server):
    stock=Penalty()
    processor=server.OutputOnlyRepetitionPenalty(4,penalty=1.1,eos_ids=(9,),processor=stock)
    inputs=Matrix([[1,1,2,2,3,3,4,9]])
    before=inputs.tolist()
    scores=Matrix([[11.0]*10]);scores.rows[0][4]=-5.0
    result=processor(inputs,scores)
    assert stock.seen==[[[3,3,4,9]]]
    assert result.rows[0][1]==result.rows[0][2]==11.0
    assert result.rows[0][3]==pytest.approx(10.0)
    assert result.rows[0][4]==pytest.approx(-5.5)
    assert result.rows[0][9]==11.0
    assert inputs.tolist()==before


def test_empty_generated_prefix_is_an_exact_noop(server):
    stock=Penalty();processor=server.OutputOnlyRepetitionPenalty(4,processor=stock)
    scores=Matrix([[11.0]*10])
    assert processor(Matrix([[1,1,2,2]]),scores) is scores
    assert stock.seen==[]


@pytest.mark.parametrize('format_value',[None,{'type':'json_schema','json_schema':{}},{'type':'json_object'}])
def test_default_profile_keeps_existing_decoding_unchanged(server,format_value):
    kwargs,receipt,guard=server.generation_prose_controls(request(server,response_format=format_value),object(),4)
    assert kwargs=={} and guard is None
    assert receipt['enabled'] is False


@pytest.mark.parametrize('format_value',[{'type':'json_schema','json_schema':{}},{'type':'json_object'}])
def test_guided_json_bypasses_h3ir_prose_controls_before_transformers_import(server,monkeypatch,format_value):
    monkeypatch.setattr(server,'H3IR_PROSE_REPEAT_GUARD',True)
    kwargs,receipt,guard=server.generation_prose_controls(request(server,response_format=format_value),object(),4)
    assert kwargs=={} and guard is None
    assert receipt['enabled'] is False


def test_active_h3ir_profile_adds_output_only_processors_without_changing_stop_controls(server,monkeypatch):
    fake=ModuleType('transformers')
    fake.LogitsProcessorList=list;fake.StoppingCriteriaList=list
    fake.RepetitionPenaltyLogitsProcessor=Penalty
    monkeypatch.setitem(sys.modules,'transformers',fake)
    monkeypatch.setattr(server,'H3IR_PROSE_REPEAT_GUARD',True)
    model=SimpleNamespace(generation_config=SimpleNamespace(eos_token_id=[9,10]))
    body=request(server,stop=['END'])
    stop_kwargs,stops=server.generation_stop_controls(body,SimpleNamespace(tokenizer=object()))
    existing=object()
    kwargs,receipt,guard=server.generation_prose_controls(body,model,4,existing_stopping=[existing])
    assert kwargs['repetition_penalty']==1.0
    assert len(kwargs['logits_processor'])==1
    assert kwargs['stopping_criteria']==[existing,guard]
    assert stop_kwargs['stop_strings']==stops==['END']
    assert receipt['enabled'] is True
    assert isinstance(kwargs['logits_processor'][0],server.OutputOnlyRepetitionPenalty)


@pytest.mark.parametrize('ended,matched,count,limit,expected',[
    (True,None,10,20,'stop'),(False,'END',10,20,'stop'),
    (False,None,20,20,'length'),(False,None,10,20,'time_limit'),
])
def test_normal_finish_reasons_are_unchanged(server,ended,matched,count,limit,expected):
    assert server.generation_finish_reason(ended,matched,count,limit)==expected


def test_repetition_termination_can_never_be_reported_as_success(server):
    guard=SimpleNamespace(triggered=True)
    assert server.generation_finish_reason(False,None,300,4096,guard)=='repetition_limit'
    assert server.generation_finish_reason(True,'END',300,4096,guard)=='repetition_limit'


def test_h3ir_wrapper_enables_policy_but_base_screenplay_does_not(server,tmp_path):
    assert server.H3IR_PROSE_REPEAT_GUARD is False
    name='_qwen_repeat_profile_'+uuid.uuid4().hex
    spec=importlib.util.spec_from_file_location(name,SCRIPTS/'serve_h3ir_qwen4b.py')
    wrapper=importlib.util.module_from_spec(spec)
    old_bytecode=sys.dont_write_bytecode
    try:
        with patch.dict(sys.modules),patch.dict(os.environ,{'VF_RUNTIME_ROOT':str(tmp_path),'VF_QWEN_MODEL':str(tmp_path/'no-weights')}):
            sys.modules[name]=wrapper;spec.loader.exec_module(wrapper)
            assert wrapper.runtime.H3IR_PROSE_REPEAT_GUARD is True
            assert wrapper.runtime.PROSE_OUTPUT_REPETITION_PENALTY==1.1
    finally:sys.dont_write_bytecode=old_bytecode
    assert server.H3IR_PROSE_REPEAT_GUARD is False


def test_actual_transformers_cpu_processor_preserves_prompt_and_eos(server):
    torch=pytest.importorskip('torch')
    pytest.importorskip('transformers')
    inputs=torch.tensor([[1,1,2,2,3,3,4,9]],device='cpu')
    scores=torch.full((1,10),11.0,device='cpu');scores[0,4]=-5.0
    processor=server.OutputOnlyRepetitionPenalty(4,penalty=1.1,eos_ids=(9,))
    result=processor(inputs,scores)
    assert result[0,1].item()==result[0,2].item()==11.0
    assert result[0,3].item()==pytest.approx(10.0)
    assert result[0,4].item()==pytest.approx(-5.5)
    assert result[0,9].item()==11.0
    assert inputs.tolist()==[[1,1,2,2,3,3,4,9]]
    guard=server.RepeatedOutputGuard(4,check_every=1)
    assert guard(torch.tensor([[1,1,2,2]+list(range(100,140))*3],device='cpu'),None)
    # Decoding controls outlive individual callbacks but must retain neither
    # the input/scores tensor nor the GPU-sized generated prefix.
    def assert_no_saved_tensor(value):
        assert not isinstance(value,torch.Tensor)
        if isinstance(value,dict):
            for child in value.values():assert_no_saved_tensor(child)
        elif isinstance(value,(list,tuple,set)):
            for child in value:assert_no_saved_tensor(child)
    assert_no_saved_tensor(vars(guard))
    assert_no_saved_tensor({key:value for key,value in vars(processor).items() if key!='processor'})
    assert_no_saved_tensor(vars(processor.processor))
    for name in ('logits_indices','cu_seq_lens_q'):
        assert getattr(processor.processor,name,None) is None
