"""lk888's MiniMax-H3 v2 async video contract.

The configured /v1 URL is used for model inventory, while this provider's
documented H3 create/query endpoints are rooted at /v2.  This adapter never
rewrites a hostname and never treats a listing as a successful generation.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from io import BytesIO
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
from PIL import Image, UnidentifiedImageError

from app.core.errors import AppError
from app.services.ai_services import download_public_video, service_request
from app.services.providers import JobAccepted


def _compact_placeholder_contacts(prompt: str):
    """Remove only duplicated, generated placeholder contact directions.

    Match the complete compiler output in its dedicated retention section and
    independently verify each removed material specification against the
    unmodified production JSON. Never shorten unknown or explicit constraints.
    """
    from app.services.cinematic_quality import physical_directions

    marker = 'Reviewed production designs, timed actions and continuity states: '
    start_marker = 'retention_analysis:\n'
    end_marker = '\ndetailed_description:'
    if any(prompt.count(marker_) != 1 for marker_ in (marker, start_marker, end_marker)):
        return prompt, 0
    start = prompt.index(start_marker) + len(start_marker)
    end = prompt.index(end_marker)
    design_start = prompt.index(marker) + len(marker)
    if not start < end < design_start:
        return prompt, 0
    try:
        context, size = json.JSONDecoder().raw_decode(prompt[design_start:])
        production, entities = context['production'], context['entities']
        states = production['prop_states']
        if not isinstance(states, list) or not isinstance(entities, list):
            return prompt, 0
        block = physical_directions(context)
    except (ValueError, TypeError, KeyError, AttributeError):
        return prompt, 0
    retention = prompt[start:end]
    if not block or retention.count(block) != 1 or prompt.count(block) != 1:
        return prompt, 0
    lines = block.split('\n')
    if len(lines) != len(states):
        return prompt, 0
    design_text = prompt[design_start:design_start + size]
    by_id = {}
    for entity in entities:
        if not isinstance(entity, dict) or not isinstance(entity.get('id'), str) or entity['id'] in by_id:
            return prompt, 0
        by_id[entity['id']] = entity
    compacted = 0
    for index, state in enumerate(states):
        if not isinstance(state, dict) or any(k in state for k in ('holder', 'hand', 'location', 'open_state')):
            continue
        entity = by_id.get(state.get('entity_id'))
        if not entity or not isinstance(entity.get('spec'), dict):
            continue
        name = entity.get('logical_id', state.get('prop_key', 'prop'))
        if not isinstance(name, str) or not name or any(c in name for c in '\r\n'):
            continue
        spec = json.dumps(entity['spec'], ensure_ascii=False)
        # The same specification must remain in the parsed, untouched payload.
        if spec not in design_text:
            continue
        prefix = f'CONTACT AND OBJECT COUNT: exactly ONE physical {name}, never duplicated. '
        placeholder = ('Only specified holder holds this object, with the unspecified hand, at specified position. '
                       'Performers without any declared object assignment keep their hands free; other declared object holders retain their own separately assigned props. '
                       'Preserve this static state; looking at an object does not mean taking or opening it. '
                       'Object state: unspecified. Material and appearance: ' + spec)
        if lines[index] != prefix + placeholder:
            continue
        lines[index] = prefix.rstrip()
        compacted += 1
    if not compacted:
        return prompt, 0
    return prompt[:start] + retention.replace(block, '\n'.join(lines), 1) + prompt[end:], compacted


_OPAQUE_ID = re.compile(r'^(?:design|product|pimage|spchar|spjob)_[0-9a-f]{24,32}$|^generation:spjob_[0-9a-f]{24,32}$')
_ID_FIELDS = {'id', 'entity_id', 'character_id', 'source', 'source_ids', 'keyframe_image_ids',
              'anchor_entity_ids', 'matched_entity_ids'}


def _compact_repeated_references(prompt: str):
    """Deduplicate identical product definitions and reversible JSON IDs.

    User prose and dialogue are untouched. Every picture keeps its own explicit
    reference to the first identical product definition. ID aliases preserve
    equality relationships and are returned in the submission audit metadata.
    """
    prefix, suffix = 'subject_definitions:\n', '\nsummary:'
    if prompt.count(prefix) != 1 or prompt.count(suffix) != 1:
        return prompt, {}
    begin = prompt.index(prefix) + len(prefix)
    end = prompt.index(suffix)
    if begin >= end:
        return prompt, {}
    section = prompt[begin:end]
    lines = section.split('\n')
    definitions = {}
    seen_pictures = set()
    for index, line in enumerate(lines):
        match = re.fullmatch(r'<Picture ([1-9][0-9]*)> defines only product (.+)', line)
        if not match:
            continue
        picture, definition = match.groups()
        if picture in seen_pictures:
            return prompt, {}
        seen_pictures.add(picture)
        if definition in definitions:
            first = definitions[definition]
            lines[index] = (f'<Picture {picture}> defines only the same product, purpose, '
                            f'visibility and constraints as <Picture {first}>.')
        else:
            definitions[definition] = picture
    prompt = prompt[:begin] + '\n'.join(lines) + prompt[end:]
    # Alias only known machine identifiers in designated structured fields.
    # Find a prefix that cannot collide with any user-authored prompt text.
    prefix = '~'
    while prefix in prompt:
        prefix += '_'
    aliases = {}
    def alias(value):
        if value not in aliases:
            aliases[value] = f'{prefix}{len(aliases)+1}'
        return aliases[value]
    def transform(value, field=None):
        if isinstance(value, dict):
            return {(alias(k) if field == 'wardrobe_states' and isinstance(k, str) and _OPAQUE_ID.fullmatch(k) else k):
                    transform(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [transform(v, field) for v in value]
        if isinstance(value, str) and field in _ID_FIELDS and _OPAQUE_ID.fullmatch(value):
            return alias(value)
        return value
    markers = ('Reviewed screenplay style, synopsis and continuity constraints: ',
               'Reviewed production designs, timed actions and continuity states: ',
               'Environment: ', 'Declared visibility: ')
    # Only consider recognized, line-local compiler sections; unknown JSON and
    # ordinary user text never enter this transformation.
    lines = prompt.split('\n')
    for index, line in enumerate(lines):
        marker = next((m for m in markers if m in line), None)
        if marker is None:
            continue
        if marker == 'Declared visibility: ':
            if not re.match(r'^<Picture [1-9][0-9]*> defines only product ', line):
                continue
        elif not line.startswith(marker):
            continue
        start = line.index(marker) + len(marker)
        try:
            value, length = json.JSONDecoder().raw_decode(line[start:])
        except (ValueError, TypeError):
            continue
        if not isinstance(value, dict) or line[start + length:].strip():
            continue
        compact = json.dumps(transform(value), ensure_ascii=False, separators=(',', ':'))
        lines[index] = line[:start] + compact + line[start + length:]
    return '\n'.join(lines), {alias: original for original, alias in aliases.items()}


def _adapt_prompt(prompt: str, negative: str):
    """Compact only the exact current generated rules block when required.

    User screenplay and production payload are never truncated or rewritten.
    A stale/unknown rule block, duplicated block, or still-too-long result fails
    closed; downstream snapshots record the exact text actually submitted.
    """
    from app.services.cinematic_rules import rules_bundle

    original = prompt + ('\nAvoid: ' + negative if negative.strip() else '')
    result = original
    adjustment = 'none'
    aliases = {}
    if len(result) > 7000:
        rules = rules_bundle()
        version = rules['versions']['production_rules_version']
        header = 'Cinematic production rules (version ' + version + '):\n'
        tail = '\nReviewed production designs, timed actions and continuity states: '
        text_all = '\n'.join(rules['visual_language'].values())
        text_product = '\n'.join(rules['visual_language'][key] for key in ('scene', 'camera', 'audio'))
        text_product += ('\nPreserve each referenced product shape, proportions, color, packaging and visible structure. '
                         'Exact printed text and logo reproduction require visual review or post-production overlays. '
                         'Do not invent performers or unsupported accessories.')
        blocks = [header + text_all + tail, header + text_product + tail]
        matches = [block for block in blocks if result.count(block) == 1]
        if len(matches) != 1 or sum(result.count(block) for block in blocks) != 1:
            raise AppError(422, 'lk888_prompt_too_long',
                           '零度 H3 的提示词含负面要求超过 7000 字；请在审片修改区打开“编辑本次提示词”删减后重试，系统不会截断用户内容')
        compact = (header + 'Live-action realism; preserve identity, costumes, physical contact, continuity, '
                   'camera, timing, dialogue and sound.' + tail)
        result = result.replace(matches[0], compact, 1)
        adjustment = 'system_rules_compacted'
        if len(result) > 7000:
            result, compacted = _compact_placeholder_contacts(result)
            if compacted:
                adjustment = 'system_rules_and_placeholder_contacts_compacted'
        if len(result) > 7000:
            previous = result
            result, aliases = _compact_repeated_references(result)
            if result != previous:
                adjustment += '_and_reference_json_compacted'
    if len(result) > 7000:
        raise AppError(422, 'lk888_prompt_too_long',
                       '零度 H3 的提示词超过 7000 字；请在审片修改区打开“编辑本次提示词”删减后重试，系统不会自动截断')
    return result, {'prompt_adjustment': adjustment, 'original_prompt_characters': len(original),
                    'sent_prompt_characters': len(result), 'sent_prompt_sha256': hashlib.sha256(result.encode('utf-8')).hexdigest(),
                    'sent_prompt': result, 'prompt_identifier_aliases': aliases}


class Lk888H3Video:
    """Explicit reference-image-to-video adapter for the lk888 H3 endpoint."""

    name = 'cloud_lk888_h3_v2'
    _model_names = {'minimax-h3', 'MiniMax-H3'}
    _ratios = {'21:9', '16:9', '4:3', '1:1', '3:4', '9:16'}
    _task_id = re.compile(r'^[A-Za-z0-9_-]{1,128}$')
    _max_body_bytes = 64_000_000
    _max_image_bytes = 20_000_000

    def __init__(self, settings, snapshot, secret):
        self.settings, self.snapshot, self.secret = settings, snapshot, secret

    def _origin_snapshot(self):
        try:
            parsed = urlsplit(self.snapshot.get('base_url', ''))
            port = parsed.port
            if (parsed.scheme != 'https' or parsed.hostname != 'api.lk888.ai' or
                    port not in (None, 443) or parsed.username is not None or
                    parsed.password is not None or parsed.query or parsed.fragment or
                    parsed.path.rstrip('/') not in ('', '/v1')):
                raise ValueError('invalid lk888 origin')
        except (ValueError, TypeError) as error:
            raise AppError(422, 'lk888_endpoint_invalid',
                           '零度 H3 仅支持 https://api.lk888.ai 或其 /v1 模型列表地址；请核对服务地址') from error
        return {**self.snapshot, 'base_url': urlunsplit((parsed.scheme, parsed.netloc, '', '', ''))}

    @staticmethod
    def _reference_data(ref):
        try:
            path = Path(ref['absolute_path'])
            if path.stat().st_size > Lk888H3Video._max_image_bytes:
                raise AppError(422, 'lk888_reference_too_large', '零度 H3 单张参考图不能超过 20 MB')
            data = path.read_bytes()
        except (KeyError, TypeError, OSError) as error:
            raise AppError(409, 'reference_unreadable', '已锁定参考图无法读取，请检查素材') from error
        if hashlib.sha256(data).hexdigest() != ref.get('sha256'):
            raise AppError(409, 'reference_hash_changed', '已锁定参考图哈希已变化')
        try:
            with Image.open(BytesIO(data)) as image:
                mime = {'PNG': 'image/png', 'JPEG': 'image/jpeg', 'WEBP': 'image/webp'}.get(image.format)
                width, height = image.size
                image.verify()
            if not mime or not 300 <= width <= 5760 or not 300 <= height <= 5760 or not .4 <= width / height <= 2.5:
                raise AppError(422, 'lk888_reference_invalid', '参考图须为 JPEG、PNG 或 WebP，边长 300–5760 像素且宽高比在 0.4–2.5 之间')
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as error:
            raise AppError(422, 'lk888_reference_invalid', '参考图无法完整解码') from error
        return mime, data

    async def submit(self, request):
        snapshot = self._origin_snapshot()
        if self.snapshot.get('model_id') not in self._model_names:
            raise AppError(422, 'lk888_h3_model_invalid', '零度 H3 请使用 minimax-h3；其他型号须经独立适配')
        if getattr(request, 'motion_references', []):
            raise AppError(422, 'lk888_motion_unsupported', '此接口尚未接入视频动作参考，未忽略该素材或提交任务')
        if len(request.reference_assets) > 9:
            raise AppError(422, 'lk888_reference_count', '零度 H3 最多接受 9 张已锁定参考图，不会自动丢弃其他参考素材')
        if not isinstance(request.prompt, str) or not request.prompt.strip():
            raise AppError(422, 'lk888_prompt_invalid', '零度 H3 必须填写非空画面提示词')
        negative = request.negative_prompt or ''
        if not isinstance(negative, str):
            raise AppError(422, 'lk888_prompt_invalid', '负面提示词格式无效')
        prompt, prompt_receipt = _adapt_prompt(request.prompt, negative)
        duration = request.duration_seconds
        if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not 4 <= duration <= 15 or duration != int(duration):
            raise AppError(422, 'lk888_duration_invalid', '零度 H3 视频时长须为 4–15 秒的整数')
        if request.aspect_ratio not in self._ratios:
            raise AppError(422, 'lk888_ratio_invalid', '零度 H3 不支持此画幅，请选择明确的横竖画幅')
        try:
            width, height = int(request.parameters['width']), int(request.parameters['height'])
            if width <= 0 or height <= 0:
                raise ValueError('invalid dimensions')
            numerator, denominator = map(int, request.aspect_ratio.split(':'))
            if abs(width / height - numerator / denominator) > .025:
                raise ValueError('aspect mismatch')
        except (KeyError, TypeError, ValueError, OverflowError, ZeroDivisionError) as error:
            raise AppError(422, 'lk888_dimensions_invalid', '视频宽高必须与选定画幅一致') from error
        # The relay accepts named resolution tiers rather than arbitrary pixels.
        # Never silently lower the user's selected target; decoded output still
        # needs verification because the service controls its final dimensions.
        resolution = {768: '768P', 1080: '1080P', 1440: '2K', 2160: '4K'}.get(min(width, height))
        if not resolution:
            raise AppError(422, 'lk888_resolution_unsupported',
                           '零度 H3 请选择短边为 768、1080、1440 或 2160 像素的输出档位；不会静默更改分辨率')
        content = [{'type': 'text', 'text': prompt}]
        manifest = []
        estimated = len(prompt.encode('utf-8')) + 8192
        for index, ref in enumerate(request.reference_assets, 1):
            mime, data = self._reference_data(ref)
            estimated += ((len(data) + 2) // 3) * 4 + 512
            if estimated > self._max_body_bytes:
                raise AppError(422, 'lk888_request_too_large', '零度 H3 参考素材编码后超过请求上限；请减少或压缩参考图')
            content.append({'type': 'image_url', 'image_url': {'url': 'data:' + mime + ';base64,' + base64.b64encode(data).decode('ascii')}, 'role': 'reference_image'})
            manifest.append({'picture_index': index, 'asset_id': ref.get('asset_id'), 'sha256': ref.get('sha256'),
                             'character_id': ref.get('character_id'), 'character_version_id': ref.get('character_version_id'),
                             'role': ref.get('role')})
        body = {'model': 'MiniMax-H3', 'content': content, 'resolution': resolution,
                'duration': int(duration), 'ratio': request.aspect_ratio}
        if len(json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) > self._max_body_bytes:
            raise AppError(422, 'lk888_request_too_large', '零度 H3 请求超过 64 MB；请减少或压缩参考图')
        try:
            response = await service_request(self.settings, snapshot, 'POST', '/v2/video_generation', self.secret,
                                             json=body, client_timeout=120.0)
        except httpx.TransportError as error:
            raise AppError(502, 'cloud_submission_unknown', '零度 H3 提交结果未知，请在供应商后台核对；未自动重复提交') from error
        if response.status_code == 408 or response.status_code >= 500 or 300 <= response.status_code < 400:
            raise AppError(502, 'cloud_submission_unknown', f'零度 H3 返回 HTTP {response.status_code}，受理结果未知；未自动重提')
        if response.status_code >= 400:
            raise AppError(502, 'cloud_video_submit_failed', f'零度 H3 拒绝请求（HTTP {response.status_code}）；请检查模型权限、余额及配置')
        try:
            payload = response.json()
            identifier = payload.get('task_id') if isinstance(payload, dict) else None
        except (ValueError, TypeError) as error:
            raise AppError(502, 'cloud_submission_unknown', '零度 H3 已响应但无法读取任务 ID；请核对原任务，未自动重提') from error
        if not isinstance(identifier, str) or not self._task_id.fullmatch(identifier):
            raise AppError(502, 'cloud_submission_unknown', '零度 H3 已响应但未返回有效任务 ID；请核对原任务，未自动重提')
        return JobAccepted(identifier, 'queued', metadata={
            'workflow': 'lk888-minimax-h3-v2-reference-image', 'provider': self.name, 'mode': 'cloud',
            'request_model': 'MiniMax-H3', 'configured_model': self.snapshot['model_id'],
            'seed_applied': False, 'negative_prompt_applied': bool(negative.strip()),
            'negative_prompt_method': 'included in prompt as natural-language avoidance instruction',
            'request_resolution': resolution, 'requested_width': width, 'requested_height': height,
            'actual_dimensions_unverified': True, 'reference_uploads': manifest, **prompt_receipt,
        })

    async def get_job(self, identifier):
        snapshot = self._origin_snapshot()
        if not isinstance(identifier, str) or not self._task_id.fullmatch(identifier):
            raise AppError(422, 'lk888_task_id_invalid', '任务 ID 格式无效；不会请求其他地址')
        response = await service_request(self.settings, snapshot, 'GET', '/v2/query/video_generation/' + quote(identifier, safe=''),
                                         self.secret, client_timeout=30.0)
        if response.status_code >= 400 or response.status_code < 200 or response.status_code >= 300:
            raise AppError(502, 'cloud_video_status_unavailable', f'零度 H3 任务查询 HTTP {response.status_code}；保留原任务 ID')
        try:
            task = response.json()['task']
            if not isinstance(task, dict) or task.get('id', identifier) != identifier:
                raise ValueError('task id mismatch')
            state = task['status']
        except (ValueError, TypeError, KeyError) as error:
            raise AppError(502, 'cloud_video_status_unavailable', '零度 H3 任务状态无法解析，保留原任务 ID') from error
        states = {'queued': 'queued', 'running': 'running', 'succeeded': 'generated', 'failed': 'failed', 'cancelled': 'cancelled'}
        if state not in states:
            raise AppError(502, 'cloud_video_status_unavailable', '零度 H3 返回未知状态，保留原任务 ID')
        url = None
        if state == 'succeeded':
            content = task.get('content')
            url = content.get('url') if isinstance(content, dict) else None
            if not isinstance(url, str) or urlsplit(url).scheme != 'https':
                raise AppError(502, 'cloud_video_status_unavailable', '零度 H3 成功状态缺少有效 HTTPS 视频地址，保留原任务 ID')
        return {'provider_job_id': identifier, 'status': states[state], 'output_url': url,
                'raw': {'task_id': identifier, 'status': state},
                'error': '零度 H3 任务失败，请在供应商后台核对原任务' if state == 'failed' else None}

    async def download(self, url):
        return await download_public_video(self.settings, url)

    async def cancel(self, identifier):
        raise AppError(409, 'cloud_cancel_unverified', '零度 H3 运行中取消能力未验证；请核对供应商原任务')
