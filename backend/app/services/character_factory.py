from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import random
import re
import time
from pathlib import Path

from PIL import Image, ImageDraw
from sqlalchemy import func, select

from app.core.config import PROJECT_ROOT
from app.core.errors import AppError
from app.core.paths import safe_media_path
from app.core.secrets import redact, reveal
from app.models import AIService, AuditEvent, Character, CharacterAsset, CharacterBatch, CharacterGenerationJob, CharacterVersion
from app.models.entities import utcnow
from app.schemas_product import IdentityProfile
from app.services.ai_services import resolve_service, service_request


def qwen_image_body(body, reference_files):
    """Build Qwen's documented /images/generations request without dropping inputs."""
    model=body.get('model')
    maximum={'qwen-image-3.0':3,'qwen-image-3.0-pro':3,'qwen-image-2.1-pro':10}.get(model)
    if maximum is None:
        raise AppError(422,'qwen_image_model_unsupported','千问图像格式支持 qwen-image-3.0、qwen-image-3.0-pro 或 qwen-image-2.1-pro；请核对模型')
    if len(reference_files)>maximum:
        raise AppError(422,'qwen_image_reference_limit',f'所选千问模型最多接受 {maximum} 张参考图；请调整选择，系统不会丢弃参考图')
    urls=[]
    for _name,data,_mime in reference_files:
        if len(data)>10*1024*1024:
            raise AppError(422,'qwen_image_too_large','千问参考图单张不能超过 10 MB')
        try:
            with Image.open(io.BytesIO(data)) as image:
                mime={'JPEG':'image/jpeg','PNG':'image/png','BMP':'image/bmp','TIFF':'image/tiff','WEBP':'image/webp','GIF':'image/gif'}.get(image.format)
                image.verify()
        except (OSError,ValueError,Image.DecompressionBombError) as exc:
            raise AppError(422,'qwen_image_invalid','千问参考图无法解码') from exc
        if not mime:
            raise AppError(422,'qwen_image_invalid','千问参考图格式须为 JPEG、PNG、BMP、TIFF、WEBP 或 GIF')
        urls.append('data:'+mime+';base64,'+base64.b64encode(data).decode('ascii'))
    result=dict(body)
    if urls:result['image']=urls[0] if len(urls)==1 else urls
    return result

TASKS = {
    'front_face': ('front_face', 'A strictly front-facing head-and-shoulders portrait, eyes at the camera.'),
    'left_45': ('left_45', 'Head-and-shoulders portrait, head turned 45 degrees to the subject\'s left; the nose points toward the RIGHT edge of the image.'),
    'right_45': ('right_45', 'Head-and-shoulders portrait, head turned 45 degrees to the subject\'s right; the nose points toward the LEFT edge of the image.'),
    'profile_left': ('profile', 'A clear head-and-shoulders LEFT-side profile portrait. Nose points to the LEFT edge of the image; one eye visible.'),
    'profile_right': ('profile', 'A clear right side profile portrait.'),
    'half_body': ('half_body', 'A waist-up standing portrait, cropped at the belt. Head and torso fill the frame; legs and shoes are outside the frame.'),
    'full_body': ('full_body', 'Full body standing, head to shoes fully visible, natural human proportions.'),
    'natural': ('expression', 'A close portrait with a natural neutral expression.'),
    'smile': ('expression', 'A close portrait with a gentle natural smile.'),
    'serious': ('expression', 'A close portrait with a calm serious expression.'),
    'surprised': ('expression', 'A close portrait with a subtle surprised expression.'),
    'sad': ('expression', 'A close portrait with a restrained sad expression.'),
    'default_outfit': ('outfit', 'Three-quarter wardrobe portrait showing the default outfit clearly.'),
    'custom_outfit': ('outfit', 'Three-quarter wardrobe portrait in the requested alternative clothing. Keep facial identity.'),
    'custom_hair': ('outfit', 'Portrait with the requested hairstyle variation. Keep facial identity.'),
    'series_look': ('outfit', 'Three-quarter series wardrobe and styling reference. Keep facial identity.')}


# This is an explicit, deliberately small visual lexicon, not a translation
# model. The immutable source profile remains in the plan and actual prompt.
_CANDIDATE_VISUAL_LEXICON = [
    (r'东亚', 'East Asian'),
    (r'方形脸|方脸', 'square face'), (r'鹅蛋脸|椭圆脸', 'oval face'), (r'圆脸', 'round face'),
    (r'黑色短发|短黑发', 'short black hair'), (r'黑色齐肩发|齐肩黑发', 'shoulder-length black hair'),
    (r'银色短发|短银发', 'short silver hair'), (r'短发', 'short hair'),
    (r'黑色头发|黑发', 'black hair'), (r'银发|银色头发', 'silver hair'),
    (r'深棕色?眼睛|深棕眼睛|深棕眼', 'dark brown eyes'), (r'棕色?眼睛|棕眼', 'brown eyes'),
    (r'略宽鼻梁', 'slightly broad nasal bridge'), (r'宽鼻梁', 'broad nasal bridge'),
    (r'宽眉', 'broad eyebrows'), (r'轻微胡茬|淡胡茬|胡茬', 'slight facial stubble'),
    (r'胡须', 'facial beard'), (r'自然毛孔|可见毛孔', 'visible natural skin pores'),
    (r'自然肤质|自然皮肤纹理|真实皮肤纹理', 'natural skin texture'),
    (r'正常成年比例|自然成年比例|成年比例', 'natural adult proportions'),
    (r'橄榄绿', 'olive green'), (r'鼠尾草绿', 'sage green'),
    (r'深灰', 'dark gray'), (r'墨蓝|深蓝|藏蓝', 'navy blue'), (r'米白', 'off-white'),
    (r'棉质|纯棉', 'cotton'), (r'工作夹克', 'work jacket'), (r'夹克', 'jacket'),
    (r'工作衫|衬衫', 'shirt'), (r'风衣', 'trench coat'), (r'工装裤', 'work trousers'),
    (r'长裤', 'trousers'), (r'内搭', 'inner top'),
    (r'未经修饰的实拍摄影|真实摄影|实拍摄影|写实摄影', 'unretouched live-action photography'),
    (r'自然光|自然照明', 'natural lighting'), (r'实景灯光|实际灯光', 'practical lighting'),
]


def candidate_language_adapter(profile, wardrobe):
    """Promote only explicit source facts into early English conditioning.

    Gender is never inferred from a name, profession, clothing or hairstyle.
    Conflicting explicit identity statements stop candidate generation.
    """
    identity_fields = {'description': profile.description, 'facial_features': profile.facial_features,
                       'body': profile.body, 'fixed_traits': '; '.join(profile.fixed_traits)}
    gender_sources = []
    patterns = {'male': r'男性(?!化)|成年男人|成年男子|成年男士|\b(?:male|man)\b',
                'female': r'女性(?!化)|成年女人|成年女子|成年女士|\b(?:female|woman)\b'}
    for field, text in identity_fields.items():
        for gender, pattern in patterns.items():
            for match in re.finditer(pattern, text, re.I):
                before = text[max(0, match.start()-8):match.start()]
                if re.search(r'(?:不是|并非|不要|避免|非|not\s+(?:an?\s+)?)\s*$', before, re.I): continue
                gender_sources.append({'field': field, 'source': match.group(), 'value': gender})
    explicit = {item['value'] for item in gender_sources}
    if len(explicit)>1:
        raise AppError(409, 'candidate_identity_gender_conflict', '人物原文同时明确男性与女性身份，请先解决冲突；未根据姓名或职业选择性别', {'sources': gender_sources})
    gender = next(iter(explicit), 'unspecified')
    fields = {'description': profile.description, 'facial_features': profile.facial_features,
              'hair': profile.hair, 'eyes': profile.eyes, 'skin': profile.skin,
              'body': profile.body, 'style': profile.style, 'fixed_traits': '; '.join(profile.fixed_traits),
              'wardrobe': wardrobe}
    mappings, uncovered, english = [], [], []
    wardrobe_hints = []
    for field, source in fields.items():
        field_hints = []
        for chunk in re.split(r'[，,、;；。\n]+', source):
            residual = chunk; hits = []
            for pattern, hint in _CANDIDATE_VISUAL_LEXICON:
                for match in re.finditer(pattern, residual):
                    hits.append((match.start(), hint))
                    mappings.append({'field': field, 'source': match.group(), 'english': hint})
                residual = re.sub(pattern, lambda m: ' '*len(m.group()), residual)
            # Gender has already been handled independently with provenance.
            # Remove only its known identity phrasing for the lexical gap report.
            for pattern in patterns.values(): residual = re.sub(pattern, '', residual, flags=re.I)
            residual = re.sub(r'原创|成年|人物|角色|一位|一个|的|和|与|及', '', residual)
            remaining = ''.join(re.findall(r'[\u3400-\u9fff]+', residual))
            if remaining: uncovered.append({'field': field, 'unmapped_chinese': remaining, 'source': chunk.strip()})
            hints = list(dict.fromkeys(hint for _, hint in sorted(hits)))
            if hints: field_hints.append(' '.join(hints) if field=='wardrobe' else ', '.join(hints))
        if field=='wardrobe': wardrobe_hints=field_hints
        else: english.extend(field_hints)
    from app.services.identity_profiles import age_description
    subject = 'Unretouched live-action casting photograph of one character'+(' of '+gender+' gender' if gender!='unspecified' else '')+age_description(profile)+'.'
    translated = subject+' '+', '.join(dict.fromkeys(english))+'.'
    if wardrobe_hints: translated+=' Wardrobe: '+'; '.join(wardrobe_hints)+'.'
    warnings=[]
    if gender=='unspecified': warnings.append('人物原文未明确性别，未根据姓名、职业、发型或服装推断；候选外观仍需最终审片核对。')
    if uncovered: warnings.append('候选英文适配仅覆盖明确的有限视觉词；以下字段仍含未覆盖中文：'+', '.join(dict.fromkeys(v['field'] for v in uncovered))+'。原文保留，不代表SDXL已理解这些语义。')
    return translated, {'version': 'candidate-visual-lexicon-1', 'gender': gender, 'gender_sources': gender_sources,
                        'mapped_visual_facts': mappings, 'unmapped_chinese': uncovered,
                        'full_translation': False, 'limits': 'Finite source-backed lexical mapping only; no semantic translation, likeness or generation-quality guarantee.'}, warnings


def build_batch_plan(session, settings, character_id, payload):
    version = session.get(CharacterVersion, payload.version_id)
    if not version or version.character_id != character_id:
        raise AppError(422, 'version_mismatch', '请选择当前人物的身份版本')
    if version.approval_state == 'approved':
        raise AppError(409, 'character_version_locked', '已发布身份版本不可追加或覆盖素材；请创建独立新版本')
    try:
        from app.services.identity_profiles import resolve_profile, age_description
        character=session.get(Character,character_id)
        profile = resolve_profile(version.identity_json,name=character.display_name if character else '',description=character.description if character else '')
    except ValueError as exc:
        raise AppError(422,'invalid_identity_profile','现有身份档案不符合结构要求，请修正草稿档案') from exc
    anchor = session.get(CharacterAsset, version.standard_face_asset_id) if version.standard_face_asset_id else None
    if payload.kind == 'references' and (not anchor or anchor.status != 'approved' or anchor.character_version_id != version.id):
        raise AppError(409, 'approved_anchor_required', '先人工批准并锁定唯一标准脸，再批量生成参考素材')
    if payload.kind == 'candidates' and version.standard_face_asset_id:
        raise AppError(409, 'anchor_locked', '标准脸已锁定；更换标准脸必须创建新身份版本')
    project_references, reference_revision = [], 0
    project_id = getattr(payload, 'project_id', None)
    if project_id:
        from app.services.character_reference import saved_reference_snapshot
        project_references, reference_revision = saved_reference_snapshot(session, settings, project_id, character_id)
    references = ([{'id': anchor.id, 'sha256': anchor.sha256, 'relative_path': anchor.relative_path,
        'mime_type': anchor.mime_type, 'purpose': 'approved_identity', 'order': 0,
        'character_id': character_id, 'source': 'approved_standard_face'}] if anchor else []) + project_references
    if len(references) > 4:
        raise AppError(409, 'character_reference_budget', '当前图像适配器最多使用4张参考图（含已批准标准脸），请减少项目参考图后重新预览')
    service, snapshot = resolve_service(session, 'character_generation', payload.service_id, project_id=project_id, model_id=payload.model_id)
    if payload.concurrency > int(service.settings_json.get('concurrency', 1)):
        raise AppError(422, 'concurrency_limit', '任务并发不能超过服务限制；本机 GPU 串行执行')
    if payload.width % 8 or payload.height % 8:
        raise AppError(422, 'invalid_dimensions', '分辨率必须为 8 的倍数')
    if payload.kind == 'candidates':
        types = ['front_face'] * payload.candidate_count
        if service.provider == 'comfyui' and not project_references:
            snapshot['model_id'] = payload.model_id or service.settings_json.get('candidate_model', '')
    else:
        if len(set(payload.types)) != len(payload.types) or set(payload.types) - TASKS.keys():
            raise AppError(422, 'unsupported_asset_type', '类型重复或未提供经过验证的生成工作流')
        types = [name for name in payload.types for _ in range(payload.count_per_type)]
    if len(types) > payload.max_tasks:
        raise AppError(422, 'batch_limit', '任务总数超过每批最大任务数')
    if not snapshot['model_id']:
        raise AppError(409, 'model_required', '请选择实际模型 ID')
    if service.provider == 'comfyui':
        required_capability = 'reference_image' if references else 'text_to_image'
        if required_capability not in service.capabilities_json:
            raise AppError(409, 'capability_unverified', '请先检测此服务的模型和原生参考图工作流能力')
        if references and snapshot['model_id'] != 'flux-2-klein-4b-fp8.safetensors':
            raise AppError(409, 'reference_model_not_supported', '当前本地图片适配器已验证 Klein 4B 原生参考图工作流；其他模型需要独立工作流适配')
        if payload.kind=='candidates' and snapshot['model_id']=='flux-2-klein-4b-fp8.safetensors' and 'reference_image' not in service.capabilities_json:
            raise AppError(409,'klein_nodes_unverified','Klein无参考候选复用原生Flux2节点，先检测选定UNET、文本编码器、VAE与原生工作流节点')
    elif references and project_references:
        capability = service.capabilities_json.get('reference_image', {})
        if capability.get('state') != 'verified' or capability.get('model_id') != snapshot['model_id']:
            raise AppError(409, 'project_reference_capability_unverified', '此云端模型尚未通过真实参考图编辑验证，不能假装项目人物参考图已应用')
        if len(references) > max(1, int(capability.get('max_reference_images', 1))):
            raise AppError(409, 'character_reference_budget', '参考图片数量超过此云端模型已经实测的输入能力')
    elif len(types) > 1 and service.capabilities_json.get('reference_image' if anchor else 'text_to_image', {}).get('state') != 'verified':
        raise AppError(409, 'single_capability_probe_required', '云端生成能力未验证；请先提交一项明确确认的验证任务，成功后才允许批量')
    identity = ' '.join(['One character'+age_description(profile)+'.', profile.description, profile.facial_features, profile.hair, profile.eyes, profile.skin, profile.identifying_marks, profile.body, profile.style, 'Fixed traits: ' + '; '.join(profile.fixed_traits)])
    if anchor:
        # The image owns facial geometry. Repeating unseen identifying marks in
        # every pose caused Klein to paint a behind-ear mark onto visible cheeks.
        # The full immutable profile remains in the job snapshot for review.
        identity = ' '.join(['The same character'+age_description(profile)+'.', profile.description, profile.hair, profile.eyes, profile.skin, profile.body, profile.style, 'Match visible facial features to the reference photograph. Hidden identity details must remain hidden; do not invent visible facial markings.'])
    wardrobe = payload.wardrobe_description or next((entry.get('description', '') for entry in profile.wardrobes if entry.get('id') == payload.wardrobe_id), '')
    language_hint, language_adapter, language_warnings = candidate_language_adapter(profile, wardrobe) if payload.kind=='candidates' else ('',None,[])
    rng = random.Random(payload.seed)
    jobs = []
    for index, name in enumerate(types):
        task = TASKS[name][1]
        prefix = 'Use the single approved reference photo as the identity anchor. Preserve the exact same face and fixed facial features. ' if anchor else ''
        if project_references:
            prefix = ('The approved standard face remains the identity anchor. ' if anchor else 'Use the saved project character reference photographs as identity constraints. ') + 'Preserve this one character; do not copy a different person. '
            prefix += 'Reference purposes in input order: ' + '; '.join(f"image {i+1}: {r['purpose']}" for i,r in enumerate(references)) + '. Identity-front/side references control facial identity; full-body controls proportions; wardrobe controls clothing only. '
        prompt = (language_hint+'\n' if language_hint else '') + prefix + 'Task: ' + task + '\n' + payload.prompt_template + '\n' + identity + '\nWardrobe (separate from identity): ' + wardrobe + '\nScene constraints: ' + '; '.join(profile.scene_constraints) + '\nOne person only, natural skin texture, no text or watermark.'
        effective_negative = '' if service.provider == 'comfyui' and (payload.kind == 'references' or snapshot['model_id']=='flux-2-klein-4b-fp8.safetensors') else payload.negative_prompt
        jobs.append({'type': name, 'asset_type': TASKS[name][0], 'prompt': prompt, 'negative_prompt': effective_negative, 'seed': rng.randrange(0, 2_000_000_000) if payload.seed_strategy == 'random' else payload.seed + index})
    reference = references[0] if references else None
    plan = {'character_id': character_id, 'version_id': version.id, 'identity_version': version.version, 'kind': payload.kind,
            'service_snapshot': snapshot, 'reference': reference, 'references': references,
            'project_id': project_id, 'project_references': project_references, 'project_reference_revision': reference_revision,
            'profile': profile.model_dump(), 'jobs': jobs,
            'parameters': payload.model_dump(exclude={'confirm', 'preview_hash'}), 'cost_estimate': None,
            'language_adapter': language_adapter, 'warnings': language_warnings,
            'cost_note': '本地无 API 按次费用；云端未配置可验证计价规则，不提供虚构估算'}
    canonical = {**plan, 'service_snapshot': {key: value for key, value in snapshot.items() if key != 'captured_at'}}
    plan['preview_hash'] = hashlib.sha256(json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return plan


def klein_text2image_graph(job):
    """Same native no-reference Flux2 graph used by real scene generation."""
    service,params=job.snapshot_json['service_snapshot'],job.snapshot_json['parameters']
    graph=json.loads((PROJECT_ROOT/'workflows/character_klein_reference_v1.json').read_text(encoding='utf-8'))['prompt']
    for key in ('1000','80','122','123','121'):graph.pop(key)
    graph['70']['inputs']['unet_name']=service['model_id']
    graph['71']['inputs']['clip_name']=service['settings']['text_encoder']
    graph['72']['inputs']['vae_name']=service['settings']['vae']
    graph['74']['inputs']['text']=job.prompt
    graph['73']['inputs']['noise_seed']=job.seed
    graph['63']['inputs'].update(positive=['74',0],negative=['82',0])
    for key in ('66','62'):graph[key]['inputs'].update(width=params['width'],height=params['height'])
    graph['1001']['inputs']['filename_prefix']='character_factory/'+job.id
    return graph


def character_workflow_version(snapshot):
    if snapshot['service_snapshot'].get('provider')!='comfyui':return 'compatible-images-v1'
    if snapshot.get('reference'):return 'character-klein-reference-v1'
    return 'character-klein-text2image-v1' if snapshot['service_snapshot']['model_id']=='flux-2-klein-4b-fp8.safetensors' else 'character-sdxl-candidates-v1'


def native_graph(job, reference):
    snapshot = job.snapshot_json
    service, params = snapshot['service_snapshot'], snapshot['parameters']
    width, height = params['width'], params['height']
    if reference:
        uploaded = [reference] if isinstance(reference, str) else list(reference)
        if len(uploaded) > 4:
            raise AppError(409, 'character_reference_budget', '原生图像工作流最多支持4张已核对参考图')
        graph = json.loads((PROJECT_ROOT / 'workflows/character_klein_reference_v1.json').read_text(encoding='utf-8'))['prompt']
        graph['1000']['inputs']['image'] = uploaded[0]
        graph['70']['inputs']['unet_name'] = service['model_id']
        graph['71']['inputs']['clip_name'] = service['settings']['text_encoder']
        graph['72']['inputs']['vae_name'] = service['settings']['vae']
        graph['74']['inputs']['text'] = job.prompt
        graph['73']['inputs']['noise_seed'] = job.seed
        for node in ('66', '62'):
            graph[node]['inputs'].update(width=width, height=height)
        graph['1001']['inputs']['filename_prefix'] = 'character_factory/' + job.id
        for index, filename in enumerate(uploaded[1:], 1):
            base = 'character_ref_' + str(index)
            graph[base] = {'class_type': 'LoadImage', 'inputs': {'image': filename}}
            graph[base+'_scale'] = {'class_type': 'ImageScaleToTotalPixels', 'inputs': {'image': [base,0], 'upscale_method': 'nearest-exact', 'megapixels': .8, 'resolution_steps': 1}}
            graph[base+'_encode'] = {'class_type': 'VAEEncode', 'inputs': {'pixels': [base+'_scale',0], 'vae': ['72',0]}}
            for polarity in ('positive', 'negative'):
                graph[base+'_'+polarity] = {'class_type': 'ReferenceLatent', 'inputs': {'conditioning': graph['63']['inputs'][polarity], 'latent': [base+'_encode',0]}}
                graph['63']['inputs'][polarity] = [base+'_'+polarity,0]
        return graph
    if service['model_id']=='flux-2-klein-4b-fp8.safetensors':
        return klein_text2image_graph(job)
    return {
        '1': {'class_type': 'CheckpointLoaderSimple', 'inputs': {'ckpt_name': service['model_id']}},
        '2': {'class_type': 'CLIPTextEncode', 'inputs': {'text': job.prompt, 'clip': ['1', 1]}},
        '3': {'class_type': 'CLIPTextEncode', 'inputs': {'text': job.negative_prompt, 'clip': ['1', 1]}},
        '4': {'class_type': 'EmptyLatentImage', 'inputs': {'width': width, 'height': height, 'batch_size': 1}},
        '5': {'class_type': 'KSampler', 'inputs': {'model': ['1', 0], 'positive': ['2', 0], 'negative': ['3', 0], 'latent_image': ['4', 0], 'seed': job.seed, 'steps': 20 if params['quality'] == 'draft' else 28, 'cfg': 6.5, 'sampler_name': 'dpmpp_2m', 'scheduler': 'karras', 'denoise': 1}},
        '6': {'class_type': 'VAEDecode', 'inputs': {'samples': ['5', 0], 'vae': ['1', 2]}},
        '7': {'class_type': 'SaveImage', 'inputs': {'images': ['6', 0], 'filename_prefix': 'character_factory/' + job.id}}}


def credential_for(session, snapshot):
    service = session.get(AIService, snapshot['service_id'])
    if not service or not service.enabled or service.revision != snapshot['service_revision']:
        raise AppError(409, 'service_changed_since_preview', '服务配置或密钥已更改；旧任务快照不变，请明确重试以使用新配置')
    return reveal(service.encrypted_secret)


def store_image(settings, session, job, data):
    if len(data) > 30 * 1024 * 1024:
        raise AppError(422, 'image_too_large', '返回图片超过 30 MiB')
    with Image.open(io.BytesIO(data)) as original:
        original.verify()
    with Image.open(io.BytesIO(data)) as original:
        if original.width * original.height > 20_000_000:
            raise AppError(422, 'image_dimensions_too_large', '返回图片像素数超限')
        image = original.convert('RGB')
    batch = session.get(CharacterBatch, job.batch_id)
    version = session.get(CharacterVersion, batch.character_version_id)
    if version.approval_state == 'approved':
        raise AppError(409, 'version_was_locked', '生成期间版本被锁定，结果不得追加到正式版本')
    relative = f'characters/{batch.character_id}/versions/{version.id}/generated/{job.id}.png'
    dest = safe_media_path(settings.media_root, relative)
    dest.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.BytesIO(); image.save(buffer, 'PNG'); png = buffer.getvalue()
    if dest.exists() and hashlib.sha256(dest.read_bytes()).digest() != hashlib.sha256(png).digest():
        raise AppError(409, 'output_exists', '拒绝覆盖已存在的生成素材')
    if not dest.exists():
        dest.write_bytes(png)
    snapshot = job.snapshot_json
    asset = CharacterAsset(character_version_id=version.id, asset_type=job.asset_type, relative_path=relative, mime_type='image/png', width=image.width, height=image.height, sha256=hashlib.sha256(png).hexdigest(), status='needs_review', is_primary_reference=False, source='generated', tags_json=[snapshot['type'], 'unreviewed_generation'], metadata_json={**snapshot, 'generation_job_id': job.id, 'provider_job_id': job.provider_job_id, 'prompt': job.prompt, 'negative_prompt': job.negative_prompt, 'seed': job.seed, 'workflow_version': snapshot.get('actual_workflow_version') or character_workflow_version(snapshot), 'workflow_sha256':snapshot.get('actual_workflow_sha256')})
    session.add(asset); session.flush()
    job.result_asset_id, job.status, job.error_message = asset.id, 'needs_review', None
    service = session.get(AIService, snapshot['service_snapshot']['service_id'])
    capability = 'reference_image' if snapshot.get('reference') else 'text_to_image'
    if service and service.revision == snapshot['service_snapshot']['service_revision']:
        previous = service.capabilities_json.get(capability, {})
        count = len(snapshot.get('references') or ([snapshot['reference']] if snapshot.get('reference') else []))
        service.capabilities_json = {**service.capabilities_json, capability: {'state': 'verified', 'source': 'actual decoded image', 'job_id': job.id, 'provider_job_id': job.provider_job_id, 'model_id': snapshot['service_snapshot']['model_id'],
            'max_reference_images': max(count, previous.get('max_reference_images', 0))}}
    session.add(AuditEvent(object_type='character_generation_job', object_id=job.id, action='image_decoded_needs_review', details_json={'asset_id': asset.id, 'sha256': asset.sha256}))


def update_batch(settings, session, batch):
    jobs = session.scalars(select(CharacterGenerationJob).where(CharacterGenerationJob.batch_id == batch.id).order_by(CharacterGenerationJob.created_at)).all()
    if any(job.status in ('submitting', 'submitted', 'running', 'cancelling') for job in jobs):
        batch.status = 'running'
    elif any(job.status == 'queued' for job in jobs):
        batch.status = 'paused' if batch.paused else 'queued'
    elif any(job.status == 'failed' for job in jobs):
        batch.status = 'completed_with_errors'
    elif any(job.status == 'unknown' for job in jobs):
        batch.status = 'unknown'
    elif any(job.status == 'needs_review' for job in jobs):
        batch.status = 'needs_review'
    elif any(job.status == 'completed' for job in jobs):
        batch.status = 'completed'
    else:
        batch.status = 'cancelled'
    assets = [session.get(CharacterAsset, job.result_asset_id) for job in jobs if job.result_asset_id]
    if assets:
        fingerprint = hashlib.sha256('|'.join(asset.id + asset.status for asset in assets).encode()).hexdigest()[:10]
        relative = f'characters/{batch.character_id}/batches/{batch.id}/contact_{fingerprint}.png'
        output = safe_media_path(settings.media_root, relative)
        if not output.exists():
            sheet = Image.new('RGB', (960, ((len(assets) + 2) // 3) * 370), '#f4f4f6'); draw = ImageDraw.Draw(sheet)
            for index, asset in enumerate(assets):
                with Image.open(safe_media_path(settings.media_root, asset.relative_path, must_exist=True)) as source:
                    thumb = source.convert('RGB'); thumb.thumbnail((300, 320))
                x, y = index % 3 * 320 + 10, index // 3 * 370 + 8
                sheet.paste(thumb, (x, y)); draw.text((x, y + 325), asset.metadata_json.get('type', asset.asset_type) + ' | ' + asset.status, fill='#252528')
            output.parent.mkdir(parents=True, exist_ok=True); sheet.save(output)
        batch.contact_sheet_path = relative


def enqueue_retry(session, job, automatic=False):
    batch = session.get(CharacterBatch, job.batch_id)
    if automatic:
        previous_retry = session.scalar(select(CharacterGenerationJob).where(CharacterGenerationJob.retry_of == job.id))
        if previous_retry is not None:
            return previous_retry
    limit = batch.snapshot_json['parameters']['max_tasks']
    if session.scalar(select(func.count()).select_from(CharacterGenerationJob).where(CharacterGenerationJob.batch_id == batch.id)) >= limit:
        raise AppError(409, 'batch_limit', '重试将超过每批最大任务数，请新建批次')
    if session.scalar(select(CharacterGenerationJob.id).where(CharacterGenerationJob.retry_of == job.id, CharacterGenerationJob.status.in_(['queued', 'submitting', 'submitted', 'running']))) is not None:
        raise AppError(409, 'retry_active', '此项已有重试任务')
    new = CharacterGenerationJob(batch_id=batch.id, asset_type=job.asset_type, prompt=job.prompt, negative_prompt=job.negative_prompt, seed=job.seed + 1, retry_of=job.id, snapshot_json={**job.snapshot_json, 'attempt': job.snapshot_json.get('attempt', 0) + 1})
    session.add(new); session.flush()
    batch.status = 'paused' if batch.paused else 'queued'
    session.add(AuditEvent(object_type='character_generation_job', object_id=new.id, action='retry_queued', details_json={'retry_of': job.id, 'automatic': automatic}))
    return new


from app.services.gpu_gate import gpu_submission

@gpu_submission
async def process_character_jobs(settings, session_factory):
    with session_factory() as session:
        active = session.scalars(select(CharacterGenerationJob).where(CharacterGenerationJob.status.in_(['submitting', 'submitted', 'running', 'cancelling'])).order_by(CharacterGenerationJob.created_at)).all()
        for job in active:
            try:
                snapshot = job.snapshot_json['service_snapshot']
                if snapshot['provider'] != 'comfyui' or not job.provider_job_id:
                    job.status, job.error_message = 'unknown', '服务重启前的请求结果未知，未自动重复提交；请先核对服务端记录'
                    continue
                secret = credential_for(session, snapshot)
                history_response = await service_request(settings, snapshot, 'GET', '/history/' + job.provider_job_id, secret)
                history_response.raise_for_status()
                history = history_response.json().get(job.provider_job_id)
                if history:
                    (PROJECT_ROOT / job.log_path / 'history.json').write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding='utf-8')
                    state = history.get('status', {})
                    if state.get('status_str') == 'error':
                        details = next((item[1] for item in reversed(state.get('messages', [])) if item[0] in ('execution_error', 'execution_interrupted')), {})
                        job.status = 'cancelled' if job.status == 'cancelling' else 'failed'
                        job.error_message = redact(details.get('exception_message', 'ComfyUI 任务失败或已中断'))[:2000]
                    elif state.get('completed') and state.get('status_str') == 'success':
                        images = [entry for output in history.get('outputs', {}).values() for entry in output.get('images', []) if entry.get('type') == 'output']
                        if len(images) != 1:
                            raise AppError(422, 'image_output_missing', '成功历史必须含一张真实输出图片')
                        response = await service_request(settings, snapshot, 'GET', '/view', secret, params=images[0]); response.raise_for_status()
                        store_image(settings, session, job, response.content)
                        job.error_message = None
                else:
                    queue = (await service_request(settings, snapshot, 'GET', '/queue', secret)).json()
                    if any(row[1] == job.provider_job_id for row in queue.get('queue_running', [])):
                        job.status = 'running' if job.status != 'cancelling' else 'cancelling'
                    elif not any(row[1] == job.provider_job_id for row in queue.get('queue_pending', [])):
                        # /history and /queue are separate reads. A finishing job can
                        # move between them; retain its ID and recheck before failure.
                        missing_file = PROJECT_ROOT / job.log_path / 'missing_status.json'
                        missing_since = json.loads(missing_file.read_text())['since'] if missing_file.exists() else time.time()
                        missing_file.write_text(json.dumps({'since': missing_since, 'checked_at': time.time()}))
                        if time.time() - missing_since > 30:
                            job.status, job.error_message = ('cancelled', None) if job.status == 'cancelling' else ('failed', '连续 30 秒未在实际队列或历史中找到任务；可重新核对历史或明确重试')
            except Exception as exc:
                import httpx
                if isinstance(exc, httpx.HTTPError):
                    job.error_message = 'ComfyUI 状态暂不可达；继续保留实际任务 ID 并重连'
                else:
                    job.status = 'failed'
                    job.error_message = redact(exc.message if isinstance(exc, AppError) else type(exc).__name__ + '：输出读取或校验失败')[:2000]
            if job.status == 'failed' and job.snapshot_json.get('attempt', 0) < job.snapshot_json['parameters'].get('max_retries', 0):
                try:
                    enqueue_retry(session, job, automatic=True)
                except AppError:
                    pass
        session.flush()
        for batch in session.scalars(select(CharacterBatch)):
            update_batch(settings, session, batch)
        session.commit()
        if any(job.status in ('running', 'submitted', 'submitting', 'cancelling') for job in active):
            return
        job = session.scalar(select(CharacterGenerationJob).join(CharacterBatch).where(CharacterGenerationJob.status == 'queued', CharacterBatch.paused.is_(False)).order_by(CharacterGenerationJob.created_at).limit(1))
        if job is None:
            return
        snapshot = job.snapshot_json['service_snapshot']
        cloud_post_accepted = False
        try:
            secret = credential_for(session, snapshot)
            if snapshot['provider'] == 'comfyui':
                queue = (await service_request(settings, snapshot, 'GET', '/queue', secret)).json()
                if queue.get('queue_running') or queue.get('queue_pending'):
                    return
            job.status = 'submitting'; job.log_path = f'logs/character_jobs/{job.id}'
            logdir = PROJECT_ROOT / job.log_path; logdir.mkdir(parents=True, exist_ok=True)
            (logdir / 'snapshot.json').write_text(json.dumps({'prompt': job.prompt, 'negative_prompt': job.negative_prompt, 'seed': job.seed, **job.snapshot_json}, ensure_ascii=False, indent=2), encoding='utf-8')
            session.commit()
            reference = job.snapshot_json.get('reference')
            references = job.snapshot_json.get('references')
            if references is None:
                references = [reference] if reference else []  # compatible historical single-image snapshots
            if len(references) > 4:
                raise AppError(409, 'character_reference_budget', '参考图超过已实现适配器的4图限制，未派发')
            actual_references = []
            reference_files = []
            for item in references:
                path = safe_media_path(settings.media_root, item['relative_path'], must_exist=True)
                data = path.read_bytes()
                if hashlib.sha256(data).hexdigest() != item['sha256']:
                    raise AppError(409, 'reference_changed', '锁定参考图哈希已变化，拒绝提交')
                reference_files.append((path.name, data, item.get('mime_type', 'image/png')))
                actual_references.append({key: item.get(key) for key in ('id','sha256','purpose','order','project_id','character_id','source')})
            image_data = None
            if snapshot['provider'] == 'comfyui':
                await service_request(settings, snapshot, 'POST', '/free', secret, json={'unload_models': True, 'free_memory': True})
                uploaded = []
                for index, image_file in enumerate(reference_files):
                    upload = await service_request(settings, snapshot, 'POST', '/upload/image', secret, files={'image': image_file}, data={'subfolder': 'character_factory', 'type': 'input', 'overwrite': 'false'})
                    upload.raise_for_status(); entry = upload.json(); filename = '/'.join(filter(None, [entry.get('subfolder'), entry['name']]))
                    uploaded.append(filename)
                    actual_references[index].update(uploaded_image=filename, node_id='1000' if index == 0 else 'character_ref_' + str(index))
                graph = native_graph(job, uploaded)
                job.snapshot_json={**job.snapshot_json,'actual_workflow_version':character_workflow_version(job.snapshot_json),
                                   'actual_workflow_sha256':hashlib.sha256(json.dumps(graph,sort_keys=True,ensure_ascii=False).encode()).hexdigest(),
                                   'actual_reference_inputs':actual_references,
                                   'negative_prompt_applied':bool(job.negative_prompt) and snapshot['model_id']!='flux-2-klein-4b-fp8.safetensors'}
                (logdir / 'prompt.json').write_text(json.dumps(graph, ensure_ascii=False, indent=2), encoding='utf-8')
                session.commit()  # exact submitted graph metadata survives process interruption
                response = await service_request(settings, snapshot, 'POST', '/prompt', secret, json={'prompt': graph, 'client_id': job.id})
                (logdir / 'submission.json').write_text(json.dumps(redact(response.json(), secret), ensure_ascii=False, indent=2), encoding='utf-8')
                if response.status_code >= 400:
                    raise AppError(422, 'comfy_validation_failed', 'ComfyUI 拒绝工作流；请查看此任务脱敏日志')
                job.provider_job_id, job.status = response.json()['prompt_id'], 'submitted'
            else:
                body = {'model': snapshot['model_id'], 'prompt': job.prompt, 'n': 1, 'size': f"{job.snapshot_json['parameters']['width']}x{job.snapshot_json['parameters']['height']}"}
                qwen_format = snapshot['settings'].get('image_api_format') == 'qwen_generations'
                qwen_body = qwen_image_body(body, reference_files) if qwen_format else None
                job.snapshot_json = {**job.snapshot_json, 'actual_reference_inputs': actual_references}
                (logdir / 'cloud_request.json').write_text(json.dumps({'endpoint':'/images/edits' if reference_files and not qwen_format else '/images/generations',
                    'body':body,'reference_inputs':actual_references,'reference_count':len(reference_files)},ensure_ascii=False,indent=2),encoding='utf-8')
                session.commit()
                if qwen_format:
                    response = await service_request(settings, snapshot, 'POST', '/images/generations', secret, json=qwen_body)
                elif reference_files:
                    if snapshot['settings'].get('image_api_format') == 'json':
                        response = await service_request(settings,snapshot,'POST','/images/edits',secret,json={**body,'images':[{'image_url':'data:'+image_file[2]+';base64,'+base64.b64encode(image_file[1]).decode()} for image_file in reference_files]})
                    else:
                        files = {'image': reference_files[0]} if len(reference_files) == 1 else [('image[]', image_file) for image_file in reference_files]
                        response = await service_request(settings, snapshot, 'POST', '/images/edits', secret, data={key: str(value) for key, value in body.items()}, files=files)
                else:
                    response = await service_request(settings, snapshot, 'POST', '/images/generations', secret, json=body)
                job.provider_job_id = response.headers.get('x-request-id')
                if qwen_format and (response.status_code == 408 or response.status_code >= 500):
                    cloud_post_accepted = True  # Submission outcome is uncertain; never automatically retry.
                response.raise_for_status()
                cloud_post_accepted = True
                # Preserve an actual request ID before decoding/downloading.
                # An accepted but malformed result must never trigger another
                # paid POST through automatic or ordinary failed-task retries.
                job.snapshot_json = {**job.snapshot_json, 'cloud_post_accepted': True,
                                     'cloud_response_status': response.status_code}
                session.commit()
                result = response.json(); item = result['data'][0]
                if item.get('b64_json'):
                    image_data = base64.b64decode(item['b64_json'], validate=True)
                else:
                    from urllib.parse import urlsplit
                    url = item['url']; parsed = urlsplit(url)
                    if parsed.scheme != 'https':
                        raise AppError(422, 'unsafe_result_url', '云端图片下载必须为公开 HTTPS')
                    download = await service_request(settings, {**snapshot, 'base_url': url}, 'GET', '')
                    download.raise_for_status(); image_data = download.content
                store_image(settings, session, job, image_data)
            session.commit()
        except Exception as exc:
            import httpx
            uncertain = snapshot['provider'] != 'comfyui' and (cloud_post_accepted or isinstance(exc,httpx.TransportError))
            job.status = 'unknown' if uncertain else 'failed'
            diagnostic = exc.message if isinstance(exc, AppError) else f'{type(exc).__name__}'+(f' HTTP {exc.response.status_code}' if isinstance(exc,httpx.HTTPStatusError) else '')+'：真实生成请求失败，请检查配置及任务日志'
            job.error_message = '云端提交结果未知，未自动重试；请先到供应商核对原请求，避免重复计费' if uncertain else redact(diagnostic, secret if 'secret' in locals() else '')[:2000]
            session.commit()
            maximum = job.snapshot_json['parameters'].get('max_retries', 0)
            if job.status == 'failed' and job.snapshot_json.get('attempt', 0) < maximum:
                try:
                    enqueue_retry(session, job, automatic=True); session.commit()
                except AppError:
                    pass


async def character_worker(settings, session_factory):
    while True:
        try:
            await process_character_jobs(settings, session_factory)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Never log HTTP request objects, authentication headers or remote bodies.
            import logging
            logging.getLogger(__name__).warning('Character queue poll: %s', type(exc).__name__)
        await asyncio.sleep(2)
