from types import SimpleNamespace

import pytest

from app.services.cinematic_quality import inspect_generated
from app.services.h3_workflow_router import build_graph, freeze_workflow


@pytest.mark.parametrize(('requested_seconds', 'author_seconds'), [(1.0, 1.2), (1.199, 1.2), (1.2, 1.2), (1.5, 1.5)])
def test_short_shot_meets_compiler_minimum_without_changing_expected_frames(requested_seconds, author_seconds):
    graph = build_graph(
        freeze_workflow('fast'), tray=[], intent='One daylight shot.', duration_seconds=requested_seconds,
        aspect_ratio='9:16', seed=7, width=576, height=1024, reference_groups='',
        required_references='', motion_targets='', output_prefix='projects/p/s/j',
    )
    assert graph['4']['inputs']['seconds'] == author_seconds
    probe = {'streams': [{'codec_type': 'video', 'width': 576, 'height': 1024,
                          'avg_frame_rate': '24/1', 'nb_read_frames': '39'}],
             'format': {'duration': '1.625'}}
    job = SimpleNamespace(parameters_json={'duration_seconds': requested_seconds, 'width': 576, 'height': 1024,
                                           'screenplay_snapshot': {'cinematic': True}})
    check = inspect_generated(job, [{'check_type': 'media_decode', 'status': 'passed', 'details': probe}])[0]
    assert check['details']['expected']['frames'] == 39
    assert check['status'] == 'passed'
