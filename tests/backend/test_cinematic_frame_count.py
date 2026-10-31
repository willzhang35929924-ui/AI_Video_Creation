import json
import subprocess
from types import SimpleNamespace

import pytest

from app.services.cinematic_quality import inspect_generated
from app.services.quality import inspect_media


def job():
    return SimpleNamespace(parameters_json={
        'screenplay_snapshot': {'cinematic': {'versions': {}}},
        'service_snapshot': {'provider': 'video_api'},
        'width': 1280, 'height': 720, 'duration_seconds': 5,
    })


def stream(**changes):
    return {'codec_type': 'video', 'width': 1280, 'height': 720,
            'avg_frame_rate': '24/1', 'nb_read_frames': '120', **changes}


def checks(video, duration='5.0'):
    return [{'check_type': 'media_decode', 'status': 'passed',
             'details': {'streams': [video], 'format': {'duration': duration}}}]


def test_probe_requests_counted_frames_and_quality_uses_them(tmp_path, settings, monkeypatch):
    path = tmp_path / 'video.mp4'
    path.write_bytes(b'video')
    calls = []
    video = stream(nb_frames='N/A')
    def run(command, **kwargs):
        calls.append(command)
        if '-show_streams' in command:
            return subprocess.CompletedProcess(command, 0, json.dumps({'streams': [video], 'format': {'duration': '5.0'}}), '')
        return subprocess.CompletedProcess(command, 0, '', '')
    monkeypatch.setattr('app.services.quality.shutil.which', lambda name: name)
    monkeypatch.setattr('app.services.quality.subprocess.run', run)
    report = inspect_generated(job(), inspect_media(path, settings))[0]
    assert '-count_frames' in calls[0]
    assert report['status'] == 'passed'
    assert report['details']['actual']['frames'] == 120
    assert report['details']['frame_count_source'] == 'nb_read_frames'


def test_counted_frames_and_duration_are_strictly_checked():
    assert inspect_generated(job(), checks(stream(nb_frames='120', nb_read_frames='119')))[0]['status'] == 'failed'
    assert inspect_generated(job(), checks(stream(), duration='4.5'))[0]['status'] == 'failed'


@pytest.mark.parametrize('value', ['N/A', '0/0', '', None])
def test_invalid_frame_rate_is_explicit_quality_failure(value):
    result = inspect_generated(job(), checks(stream(avg_frame_rate=value)))[0]
    assert result['status'] == 'failed'
    assert result['details']['actual']['fps'] is None
    assert 'fps' in result['details']['unavailable_fields']


@pytest.mark.parametrize('value', ['N/A', '', None])
def test_invalid_or_missing_frame_counts_are_not_estimated(value):
    result = inspect_generated(job(), checks(stream(nb_frames=value, nb_read_frames=value)))[0]
    assert result['status'] == 'failed'
    assert result['details']['actual']['frames'] is None
    assert 'frames' in result['details']['unavailable_fields']


def test_fallback_to_reported_frames_only_when_count_missing():
    result = inspect_generated(job(), checks(stream(nb_frames='120', nb_read_frames='N/A')))[0]
    assert result['status'] == 'passed'
    assert result['details']['frame_count_source'] == 'nb_frames'


@pytest.mark.parametrize(('frames','duration','expected'), [
    (124,'5.258344','warning'),(120,'5.2','warning'),(132,'5.5','warning'),
    (133,'5.5','failed'),(124,'5.51','failed'),(119,'5.1','failed'),
    (124,'4.99','failed'),(120,'4.95','failed'),(120,'5.0','passed'),
])
def test_cloud_duration_warning_has_bounded_nonshort_tolerance(frames,duration,expected):
    result=inspect_generated(job(),checks(stream(nb_read_frames=str(frames)),duration))[0]
    assert result['status']==expected
    assert result['details']['actual']['frames']==frames
    assert result['details']['actual']['duration']==float(duration)
    assert result['details']['expected']['frames']==120


def test_duration_warning_never_hides_wrong_dimensions_or_local_failure():
    result=inspect_generated(job(),checks(stream(width=640,nb_read_frames='124'),'5.25'))[0]
    assert result['status']=='failed'
    local=job();local.parameters_json['service_snapshot']['provider']='comfyui'
    result=inspect_generated(local,checks(stream(nb_read_frames='126'),'5.25'))[0]
    assert result['status']=='failed'


def test_real_provider_aligned_dimensions_and_extra_frames_are_warnings():
    j=job();j.parameters_json.update(width=1080,height=1920,duration_seconds=4)
    first=checks(stream(width=1088,height=1920,nb_read_frames='107',start_time='0',duration='4.458333'),duration='4.458333')
    third=checks(stream(width=1080,height=1920,nb_read_frames='107',start_time='0.083333',duration='4.458333'),duration='4.541666')
    third[0]['details']['streams'].append({'codec_type':'audio','start_time':'0','duration':'4.533344'})
    for data,actual_width in ((first,1088),(third,1080)):
        result=inspect_generated(j,data)[0]
        assert result['status']=='warning',result
        assert result['details']['actual']['width']==actual_width and result['details']['expected']['width']==1080
        assert result['details']['actual']['frames']==107 and result['details']['expected']['frames']==96
        assert result['details']['alignment']['stream_timing_verified']


def test_alignment_warning_rejects_missing_timing_large_size_ratio_and_short_output():
    j=job();j.parameters_json.update(width=1080,height=1920,duration_seconds=4)
    def evaluate(**kwargs):
        values={'width':1088,'height':1920,'nb_read_frames':'107','start_time':'0','duration':'4.458333'};values.update(kwargs);video=stream(**values)
        return inspect_generated(j,checks(video,duration='4.541666'))[0]['status']
    assert evaluate(start_time=None)=='failed'
    assert evaluate(duration=None)=='failed'
    assert evaluate(width=1104)=='failed'
    assert evaluate(height=1080,width=1920)=='failed'
    assert evaluate(nb_read_frames='95')=='failed'
    assert evaluate(avg_frame_rate='25/1')=='failed'
    assert evaluate(duration='20')=='failed'
    video=stream(width=1080,height=1920,nb_read_frames='107',start_time='0',duration='4.458333')
    assert inspect_generated(j,checks(video,duration='5.01'))[0]['status']=='failed'
