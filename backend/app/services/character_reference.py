"""Explicit project/role photo drafts, bindings, and immutable generation inputs."""
import hashlib
import io
from pathlib import Path

from PIL import Image, UnidentifiedImageError
from sqlalchemy import select, or_, update
from sqlalchemy.exc import IntegrityError
from app import models as m
from app.core.errors import AppError
from app.core.paths import safe_media_path
from app.models.character_reference import CharacterReferenceAsset, CharacterReferenceSet

MAX_IMAGES = 4
MAX_UPLOAD_BYTES = 15 * 1024 * 1024
FORMATS = {'PNG': ('image/png', {'.png'}), 'JPEG': ('image/jpeg', {'.jpg', '.jpeg'}), 'WEBP': ('image/webp', {'.webp'})}


def project_characters(session, project_id):
    if not session.get(m.Project, project_id):
        raise AppError(404, 'project_missing', '项目不存在')
    joined = select(m.ShotCharacter.character_id).join(m.StoryboardShot, m.StoryboardShot.id == m.ShotCharacter.shot_id).where(m.StoryboardShot.project_id == project_id, m.StoryboardShot.status != 'deleted')
    primary = select(m.StoryboardShot.character_id).where(m.StoryboardShot.project_id == project_id, m.StoryboardShot.status != 'deleted')
    return session.scalars(select(m.Character).where(or_(m.Character.id.in_(joined), m.Character.id.in_(primary))).order_by(m.Character.display_name, m.Character.id)).all()


def project_character(session, project_id, character_id):
    role = next((c for c in project_characters(session, project_id) if c.id == character_id), None)
    if role is None:
        raise AppError(422, 'character_reference_ownership', '人物不属于当前项目的有效分镜')
    return role


def _set(session, project_id, character_id):
    return session.scalar(select(CharacterReferenceSet).where(CharacterReferenceSet.project_id == project_id, CharacterReferenceSet.character_id == character_id))


def role_reference_limit(session,project_id,character_id):
    versions=select(m.ShotCharacter.character_version_id).join(m.StoryboardShot,m.StoryboardShot.id==m.ShotCharacter.shot_id).where(
        m.StoryboardShot.project_id==project_id,m.ShotCharacter.character_id==character_id,m.StoryboardShot.status!='deleted')
    role=session.get(m.Character,character_id)
    for version in session.scalars(select(m.CharacterVersion).where(m.CharacterVersion.character_id==character_id,
            or_(m.CharacterVersion.id.in_(versions),m.CharacterVersion.id==(role.current_approved_version_id if role else None)))):
        face=session.get(m.CharacterAsset,version.standard_face_asset_id) if version.standard_face_asset_id else None
        if face and face.status=='approved':return MAX_IMAGES-1
    return MAX_IMAGES


def public_asset(asset):
    return {'id': asset.id, 'project_id': asset.project_id, 'character_id': asset.character_id,
            'state': asset.state, 'purpose': asset.purpose, 'order': asset.order,
            'sha256': asset.sha256, 'width': asset.width, 'height': asset.height,
            'mime_type': asset.mime_type, 'bytes': asset.byte_size, 'source_note': asset.source_note,
            'media_url': '/api/media/' + asset.relative_path}


def upload_reference(session, settings, project_id, character_id, filename, data, purpose, source_note):
    project_character(session, project_id, character_id)
    limit = min(settings.import_max_bytes, MAX_UPLOAD_BYTES)
    if not data:
        raise AppError(422, 'reference_empty', '请选择人物参考图片')
    if len(data) > limit:
        raise AppError(413, 'reference_too_large', '人物参考图片超过大小限制')
    try:
        with Image.open(io.BytesIO(data)) as image:
            image_format = image.format
            width, height = image.size
            if image_format not in FORMATS or Path(filename).suffix.lower() not in FORMATS[image_format][1]:
                raise AppError(415, 'reference_image_format', '仅支持文件格式与内容一致的 PNG、JPEG、WebP 图片')
            if min(width, height) < 32 or width * height > 40_000_000:
                raise AppError(422, 'reference_image_dimensions', '参考图片边长至少32像素，且不超过4000万像素')
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            image.load()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise AppError(422, 'reference_image_decode', '图片无法完整解码') from exc
    asset = CharacterReferenceAsset(project_id=project_id, character_id=character_id,
        relative_path='', sha256=hashlib.sha256(data).hexdigest(), mime_type=FORMATS[image_format][0],
        width=width, height=height, byte_size=len(data), purpose=purpose, source_note=source_note,
        state='draft', order=1)
    session.add(asset); session.flush()
    extension = '.jpg' if image_format == 'JPEG' else '.' + image_format.lower()
    asset.relative_path = f'character_references/{project_id}/{character_id}/{asset.id}{extension}'
    destination = safe_media_path(settings.media_root, asset.relative_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open('xb') as stream:
        stream.write(data)
    session.add(m.ManagedFile(relative_path=asset.relative_path, state='active',
        ownership_json=[{'table': 'character_reference_assets', 'id': asset.id}, {'table': 'projects', 'id': project_id}],
        metadata_json={'verified_owned': True, 'sha256': asset.sha256, 'kind': 'project_character_reference', 'draft': True}))
    session.add(m.AuditEvent(object_type='character_reference_asset', object_id=asset.id, action='reference_draft_uploaded',
        details_json={'project_id': project_id, 'character_id': character_id, 'sha256': asset.sha256}))
    return asset


def save_references(session, settings, project_id, character_id, payload):
    # Serialize every role's reference save in this project. This also protects
    # the cross-role same-content check, including callers outside HTTP (review
    # creation). A no-op UPDATE takes a database write/row lock on SQLite/PG.
    session.execute(update(m.Project).where(m.Project.id == project_id).values(name=m.Project.name).execution_options(synchronize_session=False))
    project_character(session, project_id, character_id)
    maximum=role_reference_limit(session,project_id,character_id)
    if len(payload.references)>maximum:
        raise AppError(422,'character_reference_budget',f'已批准标准脸占用1张模型参考额度，此人物当前最多保存{maximum}张项目参考图，请先调整选择')
    binding = _set(session, project_id, character_id)
    if payload.expected_revision != (binding.revision if binding else 0):
        raise AppError(409, 'character_reference_revision_changed', '人物参考图已在其他窗口修改，请刷新后重新保存')
    chosen = []
    for item in payload.references:
        asset = session.get(CharacterReferenceAsset, item.asset_id)
        if not asset or asset.project_id != project_id or asset.character_id != character_id:
            raise AppError(422, 'character_reference_ownership', '参考图片不属于本项目的指定人物')
        path = safe_media_path(settings.media_root, asset.relative_path, must_exist=True)
        if hashlib.sha256(path.read_bytes()).hexdigest() != asset.sha256:
            raise AppError(409, 'character_reference_changed', '参考图片内容已变化，未保存')
        duplicate = session.scalar(select(CharacterReferenceAsset.id).where(
            CharacterReferenceAsset.project_id == project_id, CharacterReferenceAsset.character_id != character_id,
            CharacterReferenceAsset.state == 'saved', CharacterReferenceAsset.sha256 == asset.sha256))
        if duplicate:
            raise AppError(409, 'character_reference_identity_conflict', '本项目其他人物已使用相同图片，请为不同人物选择独立身份参考')
        chosen.append((asset, item))
    if not binding:
        binding = CharacterReferenceSet(project_id=project_id, character_id=character_id, revision=0)
        try:
            with session.begin_nested():
                session.add(binding); session.flush()
        except IntegrityError as exc:
            raise AppError(409, 'character_reference_revision_changed', '人物参考图已被其他窗口创建，请刷新后重新保存') from exc
    changed = session.execute(update(CharacterReferenceSet).where(CharacterReferenceSet.id == binding.id,
        CharacterReferenceSet.revision == payload.expected_revision).values(revision=payload.expected_revision + 1).execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        raise AppError(409, 'character_reference_revision_changed', '人物参考图已在其他窗口修改，请刷新后重新保存')
    session.refresh(binding)
    ids = {asset.id for asset, _ in chosen}
    for asset in session.scalars(select(CharacterReferenceAsset).where(CharacterReferenceAsset.project_id == project_id,
            CharacterReferenceAsset.character_id == character_id, CharacterReferenceAsset.state == 'saved')):
        if asset.id not in ids:
            asset.state = 'unbound'
    for asset, item in chosen:
        asset.state, asset.purpose, asset.order = 'saved', item.purpose, item.order
        registry = session.scalar(select(m.ManagedFile).where(m.ManagedFile.relative_path == asset.relative_path))
        if registry:
            registry.metadata_json = {**registry.metadata_json, 'draft': False, 'binding_state': 'saved'}
    session.flush()
    session.add(m.AuditEvent(object_type='character_reference_set', object_id=binding.id, action='project_references_saved',
        details_json={'project_id': project_id, 'character_id': character_id, 'revision': binding.revision, 'asset_ids': sorted(ids), 'files_deleted': False}))
    return reference_overview(session, settings, project_id)


def adopt_source_reference(session, settings, project_id, character_id, relative_path, sha256,
                           purpose='identity_front', source_note=''):
    """Explicit review transfer of an owned creation image, in caller's transaction.

    The caller verifies its creation-asset ownership. We independently enforce
    media-root confinement, bytes/hash/format and target project/role ownership,
    then create a separate role asset; removing that relation never removes the
    shared original creation image. No network call or implicit project creation.
    """
    from app.schemas_character_reference import CharacterReferenceSave, ReferenceSelection
    if purpose not in {'identity_front', 'identity_side', 'full_body', 'wardrobe'}:
        raise AppError(422, 'character_reference_purpose', '人物参考用途无效')
    project_character(session, project_id, character_id)
    source = safe_media_path(settings.media_root, relative_path, must_exist=True)
    data = source.read_bytes()
    if hashlib.sha256(data).hexdigest() != sha256:
        raise AppError(409, 'character_reference_changed', '创作原图内容发生变化，未转入人物参考')
    saved, revision = saved_reference_snapshot(session, settings, project_id, character_id)
    existing = next((r for r in saved if r['sha256'] == sha256), None)
    if existing:
        return session.get(CharacterReferenceAsset, existing['id'])
    if len(saved) >= MAX_IMAGES:
        raise AppError(409, 'character_reference_budget', '此人物已保存4张参考图，请先调整后再选入创作原图')
    asset = upload_reference(session, settings, project_id, character_id, source.name, data, purpose, str(source_note)[:2000])
    order = max((r['order'] for r in saved), default=0) + 1
    if order > MAX_IMAGES:
        # Existing order values may have gaps. Preserve their relative order,
        # then use a compact deterministic ordering for this explicit addition.
        selections = [ReferenceSelection(asset_id=r['id'], purpose=r['purpose'], order=i+1) for i,r in enumerate(saved)]
        order = len(selections) + 1
    else:
        selections = [ReferenceSelection(asset_id=r['id'], purpose=r['purpose'], order=r['order']) for r in saved]
    selections.append(ReferenceSelection(asset_id=asset.id, purpose=purpose, order=order))
    save_references(session, settings, project_id, character_id,
        CharacterReferenceSave(expected_revision=revision, references=selections))
    session.flush()
    return asset


def saved_reference_snapshot(session, settings, project_id, character_id):
    project_character(session, project_id, character_id)
    refs = session.scalars(select(CharacterReferenceAsset).where(CharacterReferenceAsset.project_id == project_id,
        CharacterReferenceAsset.character_id == character_id, CharacterReferenceAsset.state == 'saved').order_by(CharacterReferenceAsset.order, CharacterReferenceAsset.id)).all()
    result = []
    for asset in refs:
        path = safe_media_path(settings.media_root, asset.relative_path, must_exist=True)
        if hashlib.sha256(path.read_bytes()).hexdigest() != asset.sha256:
            raise AppError(409, 'character_reference_changed', '已保存的人物参考图片内容变化，未派发')
        result.append({'id': asset.id, 'asset_id': asset.id, 'sha256': asset.sha256, 'relative_path': asset.relative_path,
            'mime_type': asset.mime_type, 'purpose': asset.purpose, 'order': asset.order,
            'project_id': project_id, 'character_id': character_id, 'source': 'project_character_reference'})
    binding = _set(session, project_id, character_id)
    return result, binding.revision if binding else 0


def reference_overview(session, settings, project_id):
    groups = []
    for role in project_characters(session, project_id):
        assets = session.scalars(select(CharacterReferenceAsset).where(CharacterReferenceAsset.project_id == project_id,
            CharacterReferenceAsset.character_id == role.id).order_by(CharacterReferenceAsset.order, CharacterReferenceAsset.created_at)).all()
        saved = [a for a in assets if a.state == 'saved']
        current = {a.id: a.sha256 for a in saved}
        latest = None
        for job in session.scalars(select(m.CharacterGenerationJob).join(m.CharacterBatch).where(
                m.CharacterBatch.character_id == role.id).order_by(m.CharacterGenerationJob.created_at.desc()).limit(40)):
            snapshot = job.snapshot_json
            if snapshot.get('project_id') != project_id:
                continue
            refs = snapshot.get('project_references', [])
            if not refs or {r['id']: r['sha256'] for r in refs} != current:
                continue
            latest = {'job_id': job.id, 'status': job.status, 'result_asset_id': job.result_asset_id,
                'error_message': job.error_message, 'reference_ids': [r['id'] for r in refs]}
            break
        state = 'saved' if saved else 'none'
        if latest:
            actual = {r.get('id'): r.get('sha256') for r in job.snapshot_json.get('actual_reference_inputs', [])}
            applied = all(actual.get(identifier) == sha for identifier, sha in current.items())
            state = 'generated' if applied and latest['status'] in {'needs_review', 'approved', 'generated'} else 'failed' if latest['status'] in {'failed', 'cancelled'} else 'applied' if applied else 'saved'
        binding = _set(session, project_id, role.id)
        maximum=role_reference_limit(session,project_id,role.id)
        if len(saved)>maximum:state='reference_limit'
        groups.append({'character_id': role.id, 'name': role.display_name, 'description': role.description,
            'revision': binding.revision if binding else 0, 'references': [public_asset(a) for a in saved],
            'drafts': [public_asset(a) for a in assets if a.state == 'draft'], 'state': state, 'last_application': latest,
            'max_images':maximum,'limit_reason':'模型总计支持4张独立参考，已批准标准脸占用1张，最多再保存3张项目参考。' if maximum<MAX_IMAGES else '最多4张项目参考；后续锁定标准脸后，请为标准脸保留1张模型参考额度。'})
    return {'project_id': project_id, 'characters': groups, 'limits': {'max_images': MAX_IMAGES, 'max_upload_bytes': min(settings.import_max_bytes, MAX_UPLOAD_BYTES)}}
