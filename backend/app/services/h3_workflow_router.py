"""Freeze and bind the audited local OpenH3-IR production graphs."""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import PurePosixPath

from app.core.config import PROJECT_ROOT
from app.core.errors import AppError
from app.services.revision_semantics import CALLER_CONTRACT_POLICY


CONTEXT_IR_VERSION = 'local-h3ir-qwen4b-v3'
QUALITY = 'H3_R2V_OpenH3IR_Quality'
FAST = 'H3_R2V_OpenH3IR_SMD4_Fast'
MODES = {'high': (QUALITY, 30), 'standard': (QUALITY, 20), 'fast': (FAST, 4)}
# Pin the audited source graphs, accepting platform line endings. A job also
# records the exact source bytes, so retries detect even formatting changes.
TEMPLATE_SHA256 = {
    QUALITY: 'a880040aa60bacdad4d6e2546f2afcd5129a180f91f8c53470aa7fd5d2f41b52',
    FAST: '33252f8ce8e08638cf402c957c788b24729b5b0c4e948aa00b17174cc2aca1ff',
}
ASPECTS = {'16:9', '21:9', '4:3', '1:1', '3:4', '9:16'}
DIALOGUE_LANGUAGES = {'English', 'Spanish', 'Portuguese', 'French', 'German', 'Italian', 'Russian',
                      'Arabic', 'Chinese', 'Japanese', 'Korean'}
MIN_NATIVE_SHOT_SECONDS = 1.2
MEDIA = {
    'picture': (9, {'subject', 'environment', 'style'}, {'.png', '.jpg', '.jpeg', '.webp'}),
    'video': (3, {'subject', 'structure'}, {'.mp4', '.mov', '.mkv', '.webm'}),
}


def _read_template(workflow_id):
    filename = workflow_id + '.api.json'
    try:
        raw = (PROJECT_ROOT / 'workflows' / filename).read_bytes()
    except OSError as exc:
        raise AppError(503, 'h3_workflow_missing', f'本地 H3 工作流不可读取：{filename}') from exc
    if hashlib.sha256(raw.replace(b'\r\n', b'\n')).hexdigest() != TEMPLATE_SHA256[workflow_id]:
        raise AppError(409, 'h3_workflow_changed', f'本地 H3 工作流已偏离审计版本：{filename}')
    document = json.loads(raw)
    return document['prompt'], hashlib.sha256(raw).hexdigest()


def _select(mode):
    if not isinstance(mode, str) or mode not in MODES:
        raise AppError(422, 'h3_quality_mode', '本地 H3 制作模式必须为高质量、标准或 Fast')
    workflow_id, steps = MODES[mode]
    graph, sha256 = _read_template(workflow_id)
    return graph, {
        'quality_mode': mode,
        'workflow_id': workflow_id,
        'workflow_filename': workflow_id + '.api.json',
        'workflow_display_filename': workflow_id + '.json',
        'actual_steps': steps,
        'context_ir_enabled': True,
        'context_ir_version': CONTEXT_IR_VERSION,
        'workflow_sha256': sha256,
        'model_name': graph['1']['inputs']['reference_model'],
    }


def freeze_workflow(mode: str) -> dict:
    """Select the real template and freeze the requested preset for a new job."""
    return _select('fast' if mode == 'preview' else mode)[1]


def native_megapixels(aspect_ratio, width, height):
    if (not isinstance(aspect_ratio, str) or aspect_ratio not in ASPECTS
            or type(width) is not int or type(height) is not int
            or width <= 0 or height <= 0 or width % 32 or height % 32):
        raise AppError(422, 'h3_context_canvas', 'Context-IR 需要支持的画幅以及正的 32 倍数尺寸')
    # OpenH3-IR serializes the MP budget to hundredths. Verify that this budget
    # maps to the frozen dimensions under h3ir.grid.canvas_for_aspect.
    megapixels = max(0.25, round(width * height / 1_000_000, 2))
    if megapixels > 2.5:
        raise AppError(422, 'h3_context_canvas', 'Context-IR 画面像素预算超过 2.5 MP')
    a, b = map(int, aspect_ratio.split(':'))
    ratio = a / b
    native_height = math.sqrt(megapixels * 1_000_000 / ratio)
    native = (max(32, round(native_height * ratio / 32) * 32),
              max(32, round(native_height / 32) * 32))
    if native != (width, height):
        raise AppError(422, 'h3_context_canvas', '所请求画幅与 Context-IR 实际生成尺寸不一致',
                       {'requested': [width, height], 'actual_canvas': list(native)})
    return megapixels


def _uploaded_file(value):
    if not isinstance(value, str) or value != value.strip():
        raise AppError(422, 'h3_context_media_file', 'Context-IR 素材必须使用已上传的 ComfyUI 输入文件')
    name = value.removesuffix(' [input]')
    if not re.fullmatch(r'[A-Za-z0-9._/-]+', name) or any(part in {'', '.', '..'} for part in name.split('/')):
        raise AppError(422, 'h3_context_media_file', 'Context-IR 素材必须使用已上传的 ComfyUI 输入文件')
    return PurePosixPath(name)


def _validate_tray(tray):
    if not isinstance(tray, list) or len(tray) > 12:
        raise AppError(422, 'h3_context_media_limit', 'Context-IR 最多接收 9 张图片和 3 段参考视频')
    counts = {'picture': 0, 'video': 0}
    labels = set()
    for item in tray:
        kind = item.get('kind') if isinstance(item, dict) else None
        if not isinstance(kind, str) or kind not in MEDIA:
            raise AppError(422, 'h3_context_media_kind', 'Context-IR 仅接受图片和参考视频')
        limit, roles, suffixes = MEDIA[kind]
        counts[kind] += 1
        if counts[kind] > limit:
            raise AppError(422, 'h3_context_media_limit', 'Context-IR 最多接收 9 张图片和 3 段参考视频')
        label = item.get('label', '')
        if (not isinstance(label, str) or not re.fullmatch(r'[A-Za-z0-9-]+', label)
                or not re.search(r'[A-Za-z0-9]', label) or label.lower() in labels | {'speaks'}):
            raise AppError(422, 'h3_context_media_label', 'Context-IR 素材标签无效或重复')
        labels.add(label.lower())
        role = item.get('role')
        if not isinstance(role, str) or role not in roles or item.get('soundtrack', 'off') != 'off':
            raise AppError(422, 'h3_context_media_role', 'Context-IR 素材角色无效或包含未允许的参考音轨')
        if _uploaded_file(item.get('file')).suffix.lower() not in suffixes:
            raise AppError(422, 'h3_context_media_format', 'Context-IR 素材文件格式不受支持')


def _native_frame_count(seconds):
    # Mirrors h3ir.grid.frames_for_seconds for the audited 24 fps workflows.
    frames = max(5, round(seconds * 24))
    return frames + (5 - frames % 17) % 17


def build_graph(snapshot, *, tray, intent, duration_seconds, aspect_ratio, seed, width, height,
                reference_groups, required_references, motion_targets, output_prefix,
                spoken_language='English', silent=True,production_contract=None):
    """Bind frozen job inputs and leave the real compiler in the sampler path."""
    mode = snapshot.get('quality_mode') if isinstance(snapshot, dict) else None
    if not isinstance(mode, str) or mode not in MODES:
        raise AppError(409, 'h3_workflow_snapshot_invalid', '任务缺少可识别的 H3 工作流快照')
    graph, expected = _select(mode)
    if any(snapshot.get(key) != value for key, value in expected.items()):
        raise AppError(409, 'h3_workflow_snapshot_changed', '实际 H3 工作流与原任务快照不一致')
    if not isinstance(intent, str) or not intent.strip():
        raise AppError(422, 'h3_context_prompt', 'Context-IR 镜头生成要求不能为空')
    if type(duration_seconds) not in (float, int) or not 1 <= duration_seconds <= 149:
        raise AppError(422, 'h3_context_duration', 'Context-IR 镜头时长必须介于 1 和 149 秒')
    native_seconds = max(duration_seconds, MIN_NATIVE_SHOT_SECONDS)
    if _native_frame_count(native_seconds) != _native_frame_count(duration_seconds):
        raise AppError(422, 'h3_context_duration', '本镜时长无法同时符合 Context-IR 编译要求和原任务的输出帧数')
    if type(seed) is not int or not 0 <= seed < 2**64:
        raise AppError(422, 'h3_context_seed', 'Context-IR 需要有效的整数种子')
    if not isinstance(spoken_language, str) or spoken_language not in DIALOGUE_LANGUAGES or type(silent) is not bool:
        raise AppError(422, 'h3_context_audio', 'Context-IR 的语音语言或音乐设置无效')
    if not all(isinstance(value, str) for value in (reference_groups, required_references, motion_targets)):
        raise AppError(422, 'h3_context_references', 'Context-IR 的参考分组格式无效')
    _validate_tray(tray)
    if _uploaded_file(output_prefix).as_posix() != output_prefix:
        raise AppError(422, 'h3_context_output', 'Context-IR 输出需要本次任务的相对路径')
    megapixels = native_megapixels(aspect_ratio, width, height)

    # The standalone canvas keeps a disconnected director and a disabled
    # upstream Main as examples. The production graph runs only the guarded Main.
    graph.pop('3', None)
    graph.pop('17', None)
    graph['2']['inputs']['tray'] = json.dumps(tray, ensure_ascii=False)
    graph['4']['inputs'].update(
        intent=intent, seconds=native_seconds, aspect=aspect_ratio, seed=seed,
        megapixels=megapixels, sizing='match', spoken_language=spoken_language, silent=silent,
        reference_groups=reference_groups, required_references=required_references,
        motion_targets=motion_targets, caller_contract_policy=CALLER_CONTRACT_POLICY,
    )
    graph['5']['inputs']['noise_seed'] = seed
    if production_contract is not None:
        if not isinstance(production_contract,dict) or production_contract.get('version')!=1:
            raise AppError(422,'h3_production_contract','Context-IR 制作约束快照无效')
        graph['4']['inputs']['production_contract']=json.dumps(production_contract,ensure_ascii=False,sort_keys=True)
    graph['13']['inputs']['filename_prefix'] = output_prefix
    if expected['workflow_id'] == QUALITY:
        graph['7']['inputs']['steps'] = expected['actual_steps']
    return graph
