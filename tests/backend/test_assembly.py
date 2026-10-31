from pathlib import Path
from app.schemas_product import TimelineRequest
from app.services.assembly import command_for, preview_snapshot
from app.core.errors import AppError
import pytest


def test_transition_preview_keeps_tail_head_offsets_and_true_overlap():
    base={'clips':[{'duration':5.0},{'duration':4.0},{'duration':6.0}], 'transitions':[{'type':'crossfade','duration':0.5},{'type':'slide','duration':0.75}], 'output':{'fps':24,'width':640,'height':384}}
    result=preview_snapshot(base,1)
    assert result['clips'][0]['trim_start']==2.25
    assert result['clips'][1]['trim_start']==0
    assert result['duration']==2.75 and result['expected_frames']==66
    assert len(base['clips'])==3 and base['output']['width']==640
    with pytest.raises(AppError):preview_snapshot(base,5)


def test_timeline_validation_rejects_unknown_effect_and_out_of_range_specs():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):TimelineRequest.model_validate({'clips':[{'video_id':'x'}],'transitions':[{'type':'fake-animation'}]})
    with pytest.raises(ValidationError):TimelineRequest.model_validate({'clips':[{'video_id':'x'}],'output':{'width':0}})


def test_missing_input_is_rejected_before_ffmpeg_command(settings):
    snapshot={'output':{'fps':24,'width':640,'height':384,'audio_policy':'preserve'},'clips':[{'relative_path':'missing.mp4','sha256':'invalid'}], 'service_snapshot':{'settings':{}}}
    with pytest.raises(AppError) as exc:command_for(settings,snapshot,Path('unused.mp4'))
    assert exc.value.code=='media_not_found'


def test_subtitle_preview_uses_actual_preview_time_window():
    base={'clips':[{'duration':5.,'trim_start':.5},{'duration':4.}], 'transitions':[{'type':'crossfade','duration':.5}], 'output':{'fps':24,'width':640,'height':384,'subtitles':True},'subtitle_cues':[{'start':0,'end':2,'text':'too early'},{'start':3.5,'end':5,'text':'tail'},{'start':4.5,'end':7,'text':'head'}]}
    preview=preview_snapshot(base,0)
    assert preview['clips'][0]['trim_start']==4.
    assert [c['text'] for c in preview['subtitle_cues']]==['tail','head']
    assert preview['subtitle_cues'][0]['start']==0 and preview['subtitle_cues'][-1]['end']<=preview['duration']
