"""Submit the project's real OpenH3-IR graphs to the local ComfyUI runtime."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import subprocess
import sys
from collections import defaultdict
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from PIL import Image

from app.core.config import PROJECT_ROOT
from app.core.errors import AppError
from app.core.paths import safe_media_path
from app.core.trusted_service_origins import is_loopback_service, is_trusted_local_service, service_origin, local_service_client
from app.services.gpu_gate import _connection_refused, gpu_submission
from app.services.h3_workflow_router import build_graph
from app.services.motion_references import prepare_motion_reference
from app.services.providers import ComfyUIH3Provider, JobAccepted, ProviderHealth


MEDIA_LIMIT = 64 * 1024**2
IMAGE_TYPES = {'PNG': ('png', 'image/png'), 'JPEG': ('jpg', 'image/jpeg'), 'WEBP': ('webp', 'image/webp')}
WINDOWS_SCOPE_ROOT = Path('E:/ComfyUI/output/h3_local_backend_profile_v3')


def context_origin(url, profile_settings=None):
    service_origin(url, origin_only=True)
    if not (is_loopback_service(url) or is_trusted_local_service(url, profile_settings)):
        raise AppError(409, 'context_ir_local_origin_required', '请在系统设置明确确认本地推理节点的精确地址，再选择 Context-IR 三档')
    return url.rstrip('/')


def graph_digest(graph):
    return hashlib.sha256(json.dumps(graph, sort_keys=True).encode()).hexdigest()


def launch_memory_supervisor(logdir, prompt_id):
    script = PROJECT_ROOT / 'scripts/h3_context_runtime_supervisor.py'
    try:
        with (logdir / 'context-memory.stdout.log').open('xb') as stdout, (logdir / 'context-memory.stderr.log').open('xb') as stderr:
            subprocess.Popen([sys.executable, '-B', str(script), str(logdir), prompt_id], cwd=PROJECT_ROOT,
                             stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                             creationflags=subprocess.CREATE_NO_WINDOW)
    except OSError as exc:
        # The provider already accepted the request. Preserve its ID for polling.
        (logdir / 'context-memory-launch-error.json').write_text(json.dumps({'error': str(exc)}), encoding='utf-8')


def _declared_prop_states(prompt):
    # The app freezes this structured line alongside the shot. Read the final
    # line so a separate prompt override cannot displace screenplay metadata.
    for line in reversed(prompt.splitlines()):
        if line.startswith('道具镜头状态：'):
            try:
                rows = json.loads(line.partition('：')[2])
            except (ValueError, TypeError):
                return {}
            if not isinstance(rows, list):
                return {}
            props, ambiguous = {}, set()
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get('道具'), str):
                    continue
                name, holder = row['道具'], row.get('holder') or row.get('location')
                if name in props:
                    ambiguous.add(name)
                if isinstance(holder, str) and holder.strip():
                    props[name] = {'holder': ' '.join(holder.split())[:120], 'state': row.get('state')}
            return {name: prop for name, prop in props.items() if name not in ambiguous}
    return {}


def _frozen_product_staging(images, motion, prop_states):
    """Make the visible stage layout explicit before the longer shot description."""
    if motion or any(ref.get('role') != 'product' or ref.get('source') != 'screenplay_prop_reference' for ref in images):
        return ''
    product_ids = [ref.get('product_id') for ref in images]
    if None in product_ids or len(set(product_ids)) != len(product_ids):
        return ''
    hands = {}
    display = []
    for index, ref in enumerate(images, 1):
        prop = prop_states.get(ref.get('product_name'), {})
        holder = prop.get('holder')
        binding = f'<Subject {index}> (from <Picture {index}>)'
        if holder == '展台' and re.fullmatch(r'(?:[^，。；]*[，,]\s*)?静置于展台[。.]?', str(prop.get('state') or '')):
            display.append(binding)
        for hand, marker in [('right', '右手'), ('left', '左手')]:
            if holder in {marker, f'主角{marker}', f'protagonist{marker}'}:
                if hand in hands:
                    return ''
                hands[hand] = binding
    if not display or len(hands) + len(display) != len(images):
        return ''
    roles = [f'{binding} is the product handled by the protagonist\'s {hand} hand at the stated moment.'
             for hand, binding in hands.items()]
    roles.extend(f'{binding} stays on the display stand and is visible in frame during the action.' for binding in display)
    return ('Required shot staging from the screenplay. ' + ' '.join(roles) +
            ' Show these as distinct physical products. Any count of products in the hands refers only to the handled products. '
            ' In the numbered shot body, state the display placement before describing the hand action and use each exact '
            'Subject tag with its own position. Keep the display product in view without transferring it to either hand.')


def reference_layout(images, motion, *, prop_states=None):
    """Build declarations from frozen caller bindings. Never infer an identity from footage."""
    if len(images) > 9 or len(motion) > 3:
        raise AppError(422, 'context_ir_reference_limit', '每镜支持最多 9 张图与 3 段动作视频，请调整引用后重试')
    tray, directions = [], []
    groups, actions = defaultdict(list), defaultdict(list)
    for index, ref in enumerate(images, 1):
        label = f'picture{index}'
        role = ref.get('role') or ref.get('asset_type')
        actor = ref.get('actor_id') or ref.get('character_id')
        product = ref.get('product_id')
        note = 'Use the visible elements of this shot reference only where the shot calls for them.'
        tray_role = 'subject'
        if role == 'product' or (role == 'product_image' and product):
            if product:
                groups['product', str(product)].append(label)
                image_notes = ' '.join(str(ref.get('image_notes') or '').split())[:500]
                state = (ref.get('visible_state') or {}).get('visible_state')
                description = image_notes or ' '.join(str(state or ref.get('product_name') or 'The visible product').split())[:300]
                note = f'product-unit: {description}'
            else:
                note = 'Use the visible product appearance.'
        elif actor and role not in {'scene', 'scene_reference', 'composition', 'keyframe'}:
            groups['character', str(actor)].append(label)
            note = 'character-unit: The declared foreground person. Read their visible face, hair, wardrobe and pose as attributes of one person.'
        elif role in {'scene', 'scene_reference', 'environment'}:
            tray_role, note = 'environment', 'Use the location, light and visible spatial layout.'
        elif role in {'composition', 'keyframe'}:
            note = ('Use this shot reference for staging and framing. Distinguish physical subjects from flat printed '
                    'illustrations and lettering, which are features of the surface carrying them. Appearance follows '
                    'the individually bound references where present.')
        tray.append({'kind': 'picture', 'label': label, 'role': tray_role, 'note': note})
        directions.append(f'Use <Picture {index}> for its declared purpose.')
    for index, ref in enumerate(motion, 1):
        label = f'video{index}'
        actor = str(ref.get('target_character_id') or '')
        action = ref.get('reference_role') == 'action'
        if actor:
            if not action or ('character', actor) not in groups:
                raise AppError(422, 'context_motion_target_invalid', '动作参考目标必须是本镜已有身份图片的人物')
            actions[actor].append(label)
            note = 'Movement and timing for the explicitly bound character, whose appearance comes from the identity pictures.'
            directions.append(f'Cite <Video {index}> as the movement source in the definition of its explicitly bound character.')
        elif action:
            note = 'Reference for visible actions, poses and their timing. Appearance follows the new shot instructions and identity pictures.'
            directions.append(f'Define <Video {index}> separately as the action and pose reference and cite it in the shot. '
                              f'Use weak_reference for its retention marker. Apply the observed action to the shot as directed; '
                              f'the clip supplies no project character identity.')
        else:
            note = 'Camera movement, framing rhythm and shot timing for the new scene.'
            directions.append(f'Give <Video {index}> its own standalone definition as the camera and timing reference. '
                              f'Use weak_reference for its retention marker. Cite it as a filming reference in the shot. '
                              f'Define the new subjects independently of its contents.')
        tray.append({'kind': 'video', 'label': label, 'role': 'subject' if action else 'structure',
                     'soundtrack': 'off', 'note': note})
    group_names = {key: f'{key[0]}-{index}' for index, key in enumerate(groups, 1)}
    named_products_only = not motion and all(ref.get('source') == 'screenplay_prop_reference' for ref in images)
    visible_products = []
    if any(kind == 'product' for kind, _ in groups):
        directions.append('Product pictures govern the physical product only. Keep their backdrop, furniture, decor, '
                          'support surface, and lighting out of the new scene unless the shot explicitly asks for those '
                          'elements. Use the shot location, its set dressing and lighting, and any separately declared '
                          'scene reference for the environment. Keep the product appearance in that new environment.')
    for key, members in groups.items():
        numbers = ', '.join(f'<Picture {label.removeprefix("picture")}>' for label in members)
        directions.append(f'Group {numbers} into one {key[0]} subject definition. In the first numbered shot, repeat the '
                          'exact subject tag from that definition in the visible action or placement.')
        if key[0] == 'product' and len(groups) == 1 and len(members) == len(images) and not motion:
            directions.append('For this single bound product, start its definition with <Subject 1> is. In the numbered '
                              'shot body, use the literal <Subject 1> at the first mention of the pictured product. '
                              'Keep the tag in a visible action or placement sentence, even when the product is stationary. '
                              'Use its picture for the physical product shown, and keep separately described products '
                              'as distinct subjects. Do not create a second subject for the pictured product.')
            visible_products.append('<Subject 1>')
        elif named_products_only and key[0] == 'product' and len(members) == 1:
            index = int(members[0].removeprefix('picture'))
            subject = f'<Subject {index}>'
            prop = (prop_states or {}).get(images[index-1].get('product_name'), {})
            holder = prop.get('holder')
            placement = f' Its declared holder or position is {holder}.' if holder else ''
            directions.append(f'Define {subject} from <Picture {index}> only; keep other products separate.'
                              f'{placement} Describe {subject} in the numbered shot at that position, '
                              'including a stationary product on the display.')
            visible_products.append(subject)
    if visible_products:
        directions.append('Before returning the brief, verify the numbered shot body itself contains every exact tag: '
                          + ', '.join(visible_products) + '. Use the tags as nouns in the visible action or placement prose. '
                          'Generic names, definitions, summary and retention entries alone do not count. '
                          'Only use attached media labels and do not invent movement for a stationary reference.')
    declarations = {
        'reference_groups': '\n'.join(f'{key[0]} {group_names[key]}: {", ".join(members)}' for key, members in groups.items()),
        'required_references': ', '.join(slot['label'] for slot in tray if slot['role'] in {'subject', 'structure'}),
        'motion_targets': '\n'.join(f'{group_names["character", actor]}: {", ".join(members)}' for actor, members in actions.items()),
    }
    return tray, declarations, directions


def context_compiler_intent(prompt, negative_prompt, directions, *, silent=False, staging=''):
    parts = [staging, prompt] if staging else [prompt]
    if negative_prompt:
        parts.append('Visual exclusions for this shot\n' + negative_prompt)
    if directions:
        parts.append('Reference instructions for the written brief\n' + '\n'.join(directions))
        parts.append('Write each required heading once, in the order specified by the writer. Start detailed_description with '
                     'one short, unnumbered style sentence. For a photographic live-action request copy this complete sentence '
                     'without adding any clauses: The target video uses a photographic live-action style. '
                     'On the next line write [Shot 1] and the scene, lighting, camera, and action prose. '
                     'Do not put the shot marker before the style sentence. Keep sound only under its own headings.')
        parts.append('In retention_analysis use one line per defined native entity. Start each line with its actual angle-bracket '
                     'tag, followed by the retention marker and its reason. Describe location, lighting, lens and camera in the '
                     'shot prose, and sound in its sound fields. Do not add plain-key entries like location, lighting, lens, '
                     'ambience or music to retention_analysis.')
    if silent:
        parts.append('Set the non_diegetic_music field to exactly N/A, with no additional words.')
    parts.append('Write the actual camera action inside the numbered shot as prose. Use an exact camera term such as '
                 'holds static, pushes in, pulls out, pans left, pans right, tilts up, tilts down, shakes slightly, or '
                 'tracks alongside. Choose a phrase supported by the requested action. Copy the chosen phrase into the '
                 'camera sentence verbatim, including its verb form.')
    if '首段是本次明确的镜头制作要求。' not in prompt:
        camera = re.search(r'^机位与运镜：([^\r\n]+)', prompt, re.M)
        if camera:
            clauses = re.split(r'[，、；。！？,;.!?]', camera[1])
            positive = ' '.join(clause for clause in clauses if not re.search(r'不|无|禁止|避免', clause))
            movements = []
            if re.search(r'微晃|轻微晃动', positive):
                movements.append('shakes slightly')
            push_in = re.search(r'向前推进|(?:缓缓|缓慢|慢慢)(?:推近|靠近)|轻微推近', positive)
            if push_in:
                phrase = 'pushes in at slow speed' if re.match(r'缓缓|缓慢|慢慢', push_in[0]) else 'pushes in'
                movements.append(phrase)
            if movements:
                sentence = 'The camera ' + ' and '.join(movements) + '.'
                parts.append(f'Begin the first numbered shot with this exact camera sentence: {sentence} '
                             'Keep the specified angle, framing and other shot actions in the description.')
    return '\n'.join(parts)


def read_image(settings, ref):
    try:
        source = safe_media_path(settings.media_root, ref['relative_path'], must_exist=True)
        if source.stat().st_size > MEDIA_LIMIT:
            raise AppError(422, 'context_ir_image_size', '参考图超过 64 MiB，请选择尺寸合适的原图')
        data = source.read_bytes()
        if hashlib.sha256(data).hexdigest() != ref['sha256']:
            raise AppError(409, 'reference_hash_changed', '已锁定的参考图发生变化，请检查素材版本')
        with Image.open(BytesIO(data)) as image:
            file_type = IMAGE_TYPES.get(image.format)
            image.verify()
    except (KeyError, TypeError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise AppError(422, 'context_ir_image_invalid', '参考图已丢失或无法完整读取') from exc
    if file_type is None:
        raise AppError(422, 'context_ir_image_format', 'Context-IR 参考图支持 PNG、JPEG 和 WebP')
    return data, file_type


async def upload_verified(client, origin, data, filename, mime, folder):
    response = await client.post(origin + '/upload/image', files={'image': (filename, data, mime)},
                                 data={'type': 'input', 'subfolder': folder, 'overwrite': 'false'}, timeout=90)
    response.raise_for_status()
    uploaded = response.json()
    if not isinstance(uploaded, dict) or uploaded.get('name') != filename or uploaded.get('subfolder') != folder or uploaded.get('type') != 'input':
        raise AppError(502, 'context_ir_media_upload_path', 'ComfyUI 返回的上传位置与此任务不一致')
    view = await client.get(origin + '/view', params={'filename': filename, 'subfolder': folder, 'type': 'input'}, timeout=90)
    view.raise_for_status()
    if hashlib.sha256(view.content).digest() != hashlib.sha256(data).digest():
        raise AppError(502, 'context_ir_media_upload_hash', 'ComfyUI 参考文件的内容校验失败')
    return f'{folder}/{filename} [input]'


async def has_work(client, origin, *, allow_refused=False):
    try:
        response = await client.get(origin + '/queue', timeout=5)
        response.raise_for_status()
    except httpx.ConnectError as exc:
        if allow_refused and _connection_refused(exc):
            return False
        raise
    queue = response.json()
    if not isinstance(queue, dict) or not all(isinstance(queue.get(key), list) for key in ('queue_running', 'queue_pending')):
        raise AppError(502, 'context_ir_queue_invalid', '无法确认本机 ComfyUI 队列状态')
    return bool(queue['queue_running'] or queue['queue_pending'])


async def require_context_qwen(client, origin, *, runtime_required=False):
    """Use the same local identity and idle/release contract as the native guard."""
    host = urlsplit(origin).hostname
    host = f'[{host}]' if ':' in host else host
    try:
        response = await client.get(origin + '/video_factory/runtime' if runtime_required else f'http://{host}:8772/health', timeout=10)
        if response.status_code in (429, 502, 503, 504):
            raise AppError(503, 'context_ir_qwen_starting', '本地 Qwen 正在启动或恢复；未提交视频任务')
        response.raise_for_status()
        vision = response.json()
        if runtime_required:
            runtime = vision
            if (not isinstance(runtime, dict) or runtime.get('schema_version') != 1
                    or runtime.get('service') != 'video-factory-native-context-runtime-v1'
                    or runtime.get('profile_id') != 'local-h3ir-qwen4b-v3'
                    or runtime.get('memory_policy') != 'runtime_owned'
                    or runtime.get('watcher_running') is not True
                    or not isinstance(runtime.get('source_baseline'), dict)
                    or runtime['source_baseline'].get('ok') is not True):
                raise AppError(503, 'context_ir_runtime_required',
                               '此地址没有经过检查的 Context-IR 推理节点及原生内存调度，请运行本地节点安装检查并填写 8788 端口')
            vision = runtime.get('qwen_health')
    except httpx.TransportError as exc:
        raise AppError(503, 'context_ir_qwen_starting', '本地 Qwen 正在恢复或未启动；未提交视频任务') from exc
    except (httpx.HTTPStatusError, ValueError) as exc:
        raise AppError(503, 'context_ir_qwen_identity', '本地 Qwen 健康接口回执无效，请检查 8772 服务；未提交视频任务') from exc
    context_length = vision.get('max_model_len', None if runtime_required else 32768) if isinstance(vision, dict) else None
    if (not isinstance(vision, dict) or vision.get('service') != 'video-factory-h3ir-qwen4b-v1'
            or vision.get('profile') != 'h3ir-qwen4b-32k'
            or vision.get('model_id') != 'Qwen/Qwen3-VL-4B-Instruct'
            or vision.get('offline') is not True
            or type(context_length) is not int or context_length < 32768
            or any(type(vision.get(key)) is not bool for key in ('busy', 'loaded'))
            or not re.fullmatch(r'[a-fA-F0-9]{64}', str(vision.get('script_sha256', '')))):
        raise AppError(503, 'context_ir_qwen_identity', '8772 返回的并非已配置的本地离线 Qwen；未提交视频任务')
    if vision.get('gpu_release_status') == 'unconfirmed' or vision.get('inference_phase') == 'gpu_release_unconfirmed':
        raise AppError(503, 'context_ir_qwen_release_unknown', '本地 Qwen 未确认释放显存，请检查模型服务；未提交视频任务')
    if vision['busy'] or vision['loaded']:
        raise AppError(503, 'context_ir_qwen_starting', '本地 Qwen 尚在处理任务，等待释放显存；未提交视频任务')
    cleanup = vision.get('last_gpu_cleanup')
    release = vision.get('gpu_release_status')
    if (vision.get('inference_phase') != 'idle'
            or not (release == 'not_used' and cleanup is None
                    or release == 'confirmed' and isinstance(cleanup, dict) and cleanup.get('released') is True)):
        raise AppError(503, 'context_ir_qwen_release_unknown', '本地 Qwen 未返回有效的空闲及显存释放回执；未提交视频任务')
    if runtime_required and runtime.get('ready') is not True:
        raise AppError(503, 'context_ir_runtime_not_ready',
                       '本地推理节点尚未就绪；请查看设置页的节点、模型文件与内存调度检查结果，未提交视频任务')
    return runtime if runtime_required else vision


async def check_context_qwen(url, profile_settings=None, *, runtime_required=False):
    async with context_client(url, timeout=4) as client:
        await require_context_qwen(client, context_origin(url, profile_settings), runtime_required=runtime_required)


async def require_context_ready(client, origin, *, required_nodes=None, runtime_required=False):
    """Read-only admission check; never upload media or submit a graph here."""
    try:
        busy = await has_work(client, origin)
        response = await client.get(origin + '/object_info', timeout=5)
        response.raise_for_status()
        nodes = response.json()
    except httpx.TransportError as exc:
        raise AppError(503, 'context_ir_comfy_starting',
                       '本地 Context-IR ComfyUI 正在启动或暂不可达；等待就绪，视频未提交') from exc
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (429, 502, 503, 504):
            raise AppError(503, 'context_ir_comfy_starting',
                           '本地 Context-IR ComfyUI 正在启动或恢复；等待就绪，视频未提交') from exc
        raise AppError(503, 'context_ir_comfy_health_invalid', 'ComfyUI 就绪检查失败，请检查服务地址；视频未提交') from exc
    except ValueError as exc:
        raise AppError(502, 'context_ir_nodes_invalid', 'ComfyUI 节点清单回执无效；视频未提交') from exc
    required = set(required_nodes or {'VideoFactoryLocalH3IRCompile', 'VideoFactoryLocalVLMGuard'})
    if not isinstance(nodes, dict):
        raise AppError(502, 'context_ir_nodes_invalid', 'ComfyUI 节点清单回执无效；视频未提交')
    missing = sorted(required - nodes.keys())
    if missing:
        raise AppError(503, 'context_ir_nodes_missing', 'ComfyUI 缺少当前工作流所需节点；视频未提交', {'missing_nodes': missing})
    if busy:
        raise AppError(409, 'local_gpu_busy', 'ComfyUI 队列尚未空闲；等待原任务结束，视频未提交')
    await require_context_qwen(client, origin, runtime_required=runtime_required)


async def check_context_ready(url, workflow, profile_settings=None, *, runtime_required=False):
    from app.services.h3_workflow_router import _read_template
    graph, _ = _read_template(workflow['workflow_id'])
    required = {node['class_type'] for node in graph.values()}
    origin = context_origin(url, profile_settings)
    async with context_client(origin, timeout=5) as client:
        await require_context_ready(client, origin, required_nodes=required, runtime_required=runtime_required)


def context_client(origin, legacy=None, **kwargs):
    parts = urlsplit(origin)
    host = f'[{parts.hostname}]' if ':' in (parts.hostname or '') else parts.hostname
    origins = [origin, f'http://{host}:8772']
    if legacy:
        origins.append(legacy)
    return local_service_client(origins, **kwargs)


async def probe_context_health(settings, snapshot=None):
    health = ProviderHealth('comfyui_h3_context_ir', False, False, False, 'real',
                            base_url=settings.h3_context_comfyui_url, workflow_name='H3 Context-IR')
    busy = False
    try:
        profile = (snapshot or {}).get('settings') or {}
        origin = context_origin((snapshot or {}).get('base_url') or settings.h3_context_comfyui_url, profile)
        health.base_url = origin
        async with context_client(origin, timeout=4) as client:
            busy = await has_work(client, origin)
            health.connected = True
            response = await client.get(origin + '/object_info/VideoFactoryLocalH3IRCompile')
            response.raise_for_status()
            if 'VideoFactoryLocalH3IRCompile' not in response.json():
                health.error = '本地 ComfyUI 缺少 Context-IR 编译节点'
            elif not busy:
                await require_context_qwen(client, origin, runtime_required=settings.container_mode
                                           or not is_loopback_service(origin)
                                           or profile.get('h3_context_ir') is True
                                           or profile.get('workflow_profile') == 'context_ir')
                health.ready = True
            health.reference_to_video_available = health.ready
    except AppError as exc:
        health.error = exc.message
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        health.error = '本地 Qwen 服务不可达' if health.connected else '本地 Context-IR ComfyUI 不可达'
    return {**health.to_dict(), 'busy': busy}


def context_history(history, workflow, expected_graph_sha):
    """Require the real graph and compiler outputs before a result is marked generated."""
    executed = history.get('prompt') or []
    outputs = history.get('outputs') or {}
    if (len(executed) < 3 or not expected_graph_sha or graph_digest(executed[2]) != expected_graph_sha
            or not isinstance(outputs, dict)):
        raise AppError(502, 'context_ir_execution_mismatch', 'ComfyUI 执行历史与提交的工作流快照不一致')
    main_receipts = outputs.get('4', {}).get('local_backend_profile_receipt') or []
    prompt = outputs.get('14', {}).get('text') or []
    report = outputs.get('15', {}).get('text') or []
    if any(len(values) != 1 or not isinstance(values[0], str) or not values[0].strip()
           for values in (main_receipts, prompt, report)):
        raise AppError(502, 'context_ir_compiler_evidence_missing', '任务缺少实际 Context-IR 编译回执或输入报告')
    evidence = {'workflow': workflow, 'compiler_receipt': main_receipts[0], 'compiled_prompt': prompt[0],
                'compiled_prompt_sha256': hashlib.sha256(prompt[0].encode()).hexdigest(), 'media_report': report[0]}
    if workflow.get('quality_mode') == 'fast':
        native = outputs.get('19', {}).get('smd_sampling_audit_receipt') or []
        if len(native) != 1 or not isinstance(native[0], str) or not native[0].strip():
            raise AppError(502, 'context_ir_smd_evidence_missing', '四步结果缺少实际 SMD 采样回执')
        evidence['smd_sampling_receipt'] = native[0]
    return evidence


async def preserve_context_receipts(origin, evidence, logdir):
    """Retrieve verified native receipt bytes without mounting the GPU machine."""
    paths = {'compiler': evidence['compiler_receipt']}
    if evidence.get('smd_sampling_receipt'):
        paths['sampling'] = evidence['smd_sampling_receipt']
    parent = paths['compiler'].replace('\\', '/').rsplit('/', 1)[0]
    paths['source_gate'] = parent + '/compiler-001/source-gate.json'
    paths['reference_binding'] = parent + '/compiler-001/reference-binding.final.json'
    target = logdir / 'native-receipts'
    target.mkdir(exist_ok=True)
    records = {}
    async with local_service_client([origin], timeout=30) as client:
        for name, path in paths.items():
            response = await client.get(origin + '/video_factory/evidence', params={'path': path})
            response.raise_for_status()
            payload = response.json()
            try:
                data = base64.b64decode(payload['raw_base64'], validate=True)
                document = json.loads(data)
                if (not isinstance(document, dict) or len(data) > 4 * 1024**2
                        or type(payload['bytes']) is not int or len(data) != payload['bytes']
                        or hashlib.sha256(data).hexdigest() != payload['sha256']):
                    raise ValueError('invalid receipt')
            except (KeyError, TypeError, ValueError) as exc:
                raise AppError(502, 'context_ir_receipt_invalid', '原生节点回执的字节或 SHA256 不匹配；保留原任务，未重复生成') from exc
            if name == 'source_gate' and (document.get('status') != 'PASS' or document.get('source') != 'written'
                                          or document.get('fallback_reason')):
                raise AppError(502, 'context_ir_source_gate_failed', '真实编译回执未通过 written 来源检查')
            if name == 'reference_binding' and document.get('status') != 'PASS':
                raise AppError(502, 'context_ir_binding_failed', '真实人物与产品引用绑定回执未通过')
            (target / (name + '.json')).write_bytes(data)
            records[name] = {'native_path': path, 'sha256': payload['sha256'], 'bytes': len(data)}
    return records


class ContextIRH3Provider(ComfyUIH3Provider):
    def __init__(self, settings, workflow, legacy_origin=None, *, profile_settings=None):
        super().__init__(settings)
        self.workflow = workflow
        self.legacy_origin = legacy_origin
        self.profile_settings = profile_settings or {}

    @gpu_submission
    async def submit(self, request):
        origin = context_origin(self.settings.comfyui_base_url, self.profile_settings)
        if not all(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', value)
                   for value in (request.job_id, request.project_id, request.shot_id)):
            raise AppError(422, 'context_ir_task_id', '任务、镜头或项目 ID 无效')
        if not isinstance(request.prompt, str) or not request.prompt.strip():
            raise AppError(422, 'context_ir_prompt_required', '请填写本镜头的生成要求')
        prop_states = _declared_prop_states(request.prompt)
        tray, declarations, directions = reference_layout(request.reference_assets, request.motion_references,
                                                          prop_states=prop_states)
        staging = _frozen_product_staging(request.reference_assets, request.motion_references, prop_states)
        silent = request.parameters.get('context_silent', False)
        intent = context_compiler_intent(request.prompt, request.negative_prompt, directions, silent=silent, staging=staging)
        folder = f'video_factory/context_ir/{request.job_id}'
        output_prefix = f'projects/{request.project_id}/{request.shot_id}/{request.job_id}'
        args = dict(intent=intent, duration_seconds=request.duration_seconds, aspect_ratio=request.aspect_ratio,
                    seed=request.seed, width=request.parameters['width'], height=request.parameters['height'],
                    output_prefix=output_prefix, silent=silent,
                    spoken_language=request.parameters.get('context_spoken_language', 'English'), **declarations)
        # Validate the frozen recipe and canvas before uploading anything.
        draft_tray = [{**slot, 'file': f'{folder}/pending{index}.' + ('png' if slot['kind'] == 'picture' else 'mp4') + ' [input]'}
                      for index, slot in enumerate(tray, 1)]
        draft_graph = build_graph(self.workflow, tray=draft_tray, **args)
        logdir = PROJECT_ROOT / 'logs' / 'jobs' / request.job_id
        logdir.mkdir(parents=True, exist_ok=True)
        images, motion = [], []
        supervise = sys.platform == 'win32' and origin == 'http://127.0.0.1:8188'
        prompt_started = False
        try:
            async with context_client(origin, self.legacy_origin, timeout=10) as client:
                await require_context_ready(client, origin,
                    required_nodes={node['class_type'] for node in draft_graph.values()},
                    runtime_required=self.settings.container_mode or not is_loopback_service(origin)
                    or self.profile_settings.get('h3_context_ir') is True
                    or self.profile_settings.get('workflow_profile') == 'context_ir')
                if (self.legacy_origin and self.legacy_origin != origin
                        and (not supervise or self.legacy_origin != 'http://127.0.0.1:8189')
                        and await has_work(client, self.legacy_origin, allow_refused=True)):
                    return None
                if supervise:
                    from app.services.context_peer_memory import prepare_context_peer_memory

                    memory = await prepare_context_peer_memory(client, self.legacy_origin)
                    (logdir / 'context-peer-memory.json').write_text(json.dumps(memory, ensure_ascii=False, indent=2), encoding='utf-8')
                    if not memory['ready']:
                        if memory['state'] == 'peer_busy':
                            return None
                        if memory['state'] == 'commit_below_guard':
                            raise AppError(503, 'h3_windows_commit_low', 'Windows 提交内存余量低于 2 GiB；未提交 H3。请释放内存或检查分页文件。')
                        if memory['state'] == 'commit_unverified':
                            raise AppError(503, 'h3_windows_commit_unverified', '无法读取 Windows 提交内存余量；未提交 H3。')
                        raise AppError(503, 'h3_peer_memory_unverified', '无法确认本项目原生 ComfyUI（8189）的空闲状态或缓存释放；未提交 H3。')
                for index, ref in enumerate(request.reference_assets, 1):
                    data, (extension, mime) = await asyncio.to_thread(read_image, self.settings, ref)
                    uploaded = await upload_verified(client, origin, data, f'picture{index}-{ref["sha256"][:16]}.{extension}', mime, folder)
                    tray[index - 1]['file'] = uploaded
                    images.append({'asset_id': ref['asset_id'], 'sha256': ref['sha256'], 'picture_index': index,
                                   'workflow_slot': tray[index - 1]['label'], 'actual_uploaded_image': uploaded})
                for index, ref in enumerate(request.motion_references, 1):
                    data, processed = await asyncio.to_thread(prepare_motion_reference, self.settings, ref)
                    uploaded = await upload_verified(client, origin, data, f'video{index}-{processed["sha256"][:16]}.mp4', 'video/mp4', folder)
                    slot = tray[len(images) + index - 1]
                    slot['file'] = uploaded
                    motion.append({'video_id': ref['video_id'], 'video_index': index, 'source_sha256': ref['source_sha256'],
                                   'processed': processed, 'workflow_slot': slot['label'], 'actual_uploaded_video': uploaded})
                graph = build_graph(self.workflow, tray=tray, **args)
                digest = graph_digest(graph)
                record = {'prompt': graph, 'workflow': self.workflow, 'graph_sha256': digest,
                          'reference_uploads': images, 'motion_reference_uploads': motion}
                with (logdir / 'submitted_prompt.json').open('x', encoding='utf-8') as stream:
                    json.dump(record, stream, ensure_ascii=False, indent=2)
                if supervise:
                    scopes = sorted(path.name for path in WINDOWS_SCOPE_ROOT.iterdir() if path.is_dir()) if WINDOWS_SCOPE_ROOT.is_dir() else []
                    prior = {'scopes': scopes}
                    (logdir / 'context-memory-before.json').write_text(json.dumps(prior), encoding='utf-8')
                try:
                    # The native supervisor permits a 120s upstream validation
                    # window. Keep the caller alive through that exact one POST.
                    prompt_started = True
                    response = await client.post(origin + '/prompt', json={'prompt': graph, 'client_id': request.job_id}, timeout=150)
                    if response.status_code == 408 or response.status_code >= 500:
                        raise AppError(502, 'context_ir_submission_unknown', 'ComfyUI 受理结果未知，请核对原任务，未重复提交')
                    if response.is_error:
                        (logdir / 'submission_rejected.json').write_bytes(response.content)
                        raise AppError(422, 'context_ir_graph_rejected', 'ComfyUI 拒绝此 Context-IR 工作流，请查看节点验证日志')
                    payload = response.json()
                except (httpx.TransportError, ValueError) as exc:
                    raise AppError(502, 'context_ir_submission_unknown', 'ComfyUI 受理结果未知，请核对原任务，未重复提交') from exc
                prompt_id = payload.get('prompt_id') if isinstance(payload, dict) else None
                if not isinstance(prompt_id, str) or not prompt_id:
                    raise AppError(502, 'context_ir_submission_unknown', 'ComfyUI 未返回任务 ID，请核对原任务，未重复提交')
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            retryable = isinstance(exc, httpx.TransportError) or exc.response.status_code in (429, 502, 503, 504)
            if not prompt_started and retryable:
                (logdir / 'context-no-submit.json').write_text(json.dumps({'job_id': request.job_id,
                    'phase': 'pre_prompt_media_or_health', 'prompt_submission_started': False,
                    'code': 'context_ir_comfy_starting'}), encoding='utf-8')
                raise AppError(503, 'context_ir_comfy_starting',
                    '本地 ComfyUI 正在启动或连接中断；尚未提交生成任务，等待服务恢复') from exc
            raise AppError(503, 'context_ir_comfy_request_failed', 'ComfyUI 提交前请求被拒绝，请检查接口地址、权限与响应；视频未提交') from exc
        except ValueError as exc:
            raise AppError(502, 'context_ir_comfy_response_invalid', 'ComfyUI 提交前回执无效；视频未提交') from exc
        except FileExistsError as exc:
            raise AppError(409, 'context_ir_duplicate_submission', '此任务已有提交快照，请核对原任务后新建重试') from exc
        (logdir / 'submission_response.json').write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        if supervise:
            launch_memory_supervisor(logdir, prompt_id)
        return JobAccepted(prompt_id, 'queued', metadata={'workflow': self.workflow['workflow_id'],
            'workflow_sha256': digest, 'log_dir': str(logdir), 'reference_uploads': images,
            'motion_reference_uploads': motion, 'sampling_receipt': {'steps': self.workflow['actual_steps']}})
