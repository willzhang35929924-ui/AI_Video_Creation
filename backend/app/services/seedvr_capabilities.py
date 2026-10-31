"""Exact SeedVR2 target capabilities backed by complete native video results."""
import json
from pathlib import Path

from sqlalchemy import select
from app import models as m
from app.core.paths import safe_media_path
from app.core.errors import AppError
from app.schemas_quality import QualitySettings


def target_supported(quality, status):
    if not status or status.get('state') != 'verified':
        return False
    targets = status.get('verified_targets')
    if targets is not None:
        return any(t.get('resolution') == quality.resolution and t.get('aspect_ratio') == quality.aspect_ratio for t in targets)
    # Legacy deployment proved this one pair, not a Cartesian product.
    return (quality.resolution == '720p' and quality.aspect_ratio == '16:9'
            and '720p' in status.get('verified_resolutions', [])
            and '16:9' in status.get('verified_aspect_ratios', []))


def capabilities(session, settings):
    from app.services.video_quality import seedvr_status, SIZES, _file_sha
    from app.core.config import PROJECT_ROOT
    base = dict(seedvr_status(settings))
    targets = []
    if target_supported(QualitySettings(), base):
        targets.append({'resolution':'720p','aspect_ratio':'16:9','width':1280,'height':720,'source':'runtime_manifest'})
    if base.get('state') == 'verified':
        helper_sha = _file_sha(PROJECT_ROOT/'scripts/deploy_seedvr2.py')
        for render in session.scalars(select(m.QualityRender).where(m.QualityRender.status=='generated').order_by(m.QualityRender.created_at.desc())):
            snapshot = render.snapshot_json or {}; native = snapshot.get('native_execution') or {}
            quality = snapshot.get('settings') or {}; actual = (render.result_json or {}).get('final_actual') or {}
            if quality.get('processing') != 'seedvr2' or native.get('status') != 'completed' or native.get('inference_submitted') is not True:
                continue
            if snapshot.get('seedvr2', {}).get('manifest_sha256') != base.get('manifest_sha256') or snapshot.get('seedvr2', {}).get('helper_sha256') != helper_sha:
                continue
            pair = (quality.get('resolution'), quality.get('aspect_ratio'))
            if pair[0] not in SIZES or pair[1] not in {'16:9','9:16','1:1'} or any((t['resolution'],t['aspect_ratio'])==pair for t in targets):
                continue
            width,height = SIZES[pair[0]]
            if pair[1]=='9:16':width,height=height,width
            elif pair[1]=='1:1':width=height
            if (actual.get('width'),actual.get('height')) != (width,height):
                continue
            try:
                output = safe_media_path(settings.media_root,render.relative_path,must_exist=True)
                receipt_path = output.with_suffix('.seed-result.json')
                if Path(native['receipt_path']).resolve()!=receipt_path.resolve() or _file_sha(receipt_path)!=native['receipt_sha256']:
                    continue
                receipt = json.loads(receipt_path.read_text('utf-8'))
                checks = receipt.get('checks',{})
                if (receipt.get('status')!='PASS_TECHNICAL_FILE' or receipt.get('sample_only') is not False
                        or receipt.get('prompt_id')!=native.get('prompt_id') or not native.get('prompt_id')
                        or receipt.get('comfy_execution_status',{}).get('status_str')!='success'
                        or receipt.get('source_sha256')!=snapshot.get('source_sha256')
                        or receipt.get('frames_processed')!=actual.get('frames')
                        or receipt.get('source_frames')!=actual.get('frames')
                        or not checks or not all(v is True or k=='full_decode_exit_code' and type(v) is int and v==0 for k,v in checks.items())
                        or not all(checks.get(k) is True for k in ('frames_equal','target_dimensions_equal','fps_equal','source_unchanged','audio_packets_equal','actual_graph_matches_frozen'))):
                    continue
                video = session.get(m.VideoRevision,render.video_id)
                if not video or _file_sha(output)!=(video.metadata_json or {}).get('sha256'):
                    continue
                targets.append({'resolution':pair[0],'aspect_ratio':pair[1],'width':width,'height':height,
                    'source':'complete_native_quality_render','render_id':render.id,'video_id':video.id,
                    'frames':actual['frames'],'output_sha256':video.metadata_json['sha256'],'prompt_id':native['prompt_id']})
            except (OSError,ValueError,TypeError,KeyError,AppError):
                continue
    base.update(verified_targets=targets,
        verified_resolutions=sorted({t['resolution'] for t in targets}),
        verified_aspect_ratios=sorted({t['aspect_ratio'] for t in targets}))
    base['limits']=['仅精确列出的分辨率与画幅组合通过完整视频实测；验证通过不代表人工视觉批准。']
    return base
