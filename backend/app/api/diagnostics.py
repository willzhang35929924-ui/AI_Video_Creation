from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from PIL import Image

import httpx
from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.routes import audit, build_prompt, required, row_dict
from app.core.config import PROJECT_ROOT
from app.core.db import get_db
from app.core.errors import AppError
from app.core.paths import safe_media_path
from app.models import CharacterAsset, CharacterVersion, FinalComposition, Project, PublishingAccount, StoryboardShot, VideoRevision
from app.schemas import CompositionRequest, FinalDryRunRequest, RevisionRequest
from app.services.quality import inspect_media

router = APIRouter(prefix="/api")


@router.get("/system/status")
async def system_status(request: Request):
    settings = request.app.state.settings
    try:
        result = await asyncio.to_thread(subprocess.run, ["nvidia-smi", "--query-gpu=name,memory.total,memory.used,utilization.gpu,temperature.gpu", "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10)
        gpu = {"status": "measured", "raw": result.stdout.strip(), "exit_code": result.returncode}
    except (OSError, subprocess.TimeoutExpired) as error:
        gpu = {"status": "unavailable", "error": str(error)}
    try:
        async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
            response = await client.get(settings.comfyui_base_url + "/system_stats")
            response.raise_for_status()
            comfy = {"status": "connected", "stats": response.json()}
    except (httpx.HTTPError, ValueError) as error:
        comfy = {"status": "disconnected", "error": str(error)}
    download = PROJECT_ROOT / "reports/model_download_state.json"
    try:
        downloads = json.loads(download.read_text(encoding="utf-8")) if download.exists() else []
    except (json.JSONDecodeError, OSError):
        downloads = []
    return {"gpu": gpu, "comfyui": comfy, "downloads": downloads, "real_publish_enabled": False}


@router.get("/shots/{shot_id}/prompt-preview")
def prompt_preview(shot_id: str, request: Request, session: Session = Depends(get_db)):
    shot = required(session, StoryboardShot, shot_id, "分镜")
    from app.models import ShotCharacter
    from app.services.screenplay import generation_inputs, uses_structured_screenplay
    from app.core.config import get_settings
    if uses_structured_screenplay(session,shot):
        version, references, prompt, bundle = generation_inputs(session,shot,request.app.state.settings)
        return {'character_id':version.character_id if version else None,'character_version_id':version.id if version else None,'character_version':version.version if version else None,'participants':bundle['participants'],'cinematic':bundle.get('cinematic'),'prompt':prompt,'negative_prompt':'','negative_prompt_supported':False,
                'assets':[{**{k:v for k,v in r.items() if k!='absolute_path'},'media_url':'/api/media/'+r['relative_path']} for r in references],
                'reference_order':[f'<Picture {i+1}>' for i in range(len(references))],'editing_semantics':'所有人物按独立参考标签重新生成；不承诺像素级编辑'}
    if not shot.character_asset_version_id:
        raise AppError(409, "character_not_assigned", "请先绑定已审核角色版本")
    version = required(session, CharacterVersion, shot.character_asset_version_id, "角色版本")
    prompt, negative = build_prompt(shot, version, None)
    assets = [required(session, CharacterAsset, identifier, "素材") for identifier in shot.reference_asset_ids_json]
    return {"character_id": shot.character_id, "character_version_id": version.id, "character_version": version.version, "prompt": prompt, "negative_prompt": negative, "assets": [{**row_dict(asset), "media_url": f"/api/media/{asset.relative_path}"} for asset in assets], "reference_order": ["<Picture 1>"], "editing_semantics": "H3 通过参考引导重新生成，不承诺像素级局部编辑"}


@router.post("/projects/{project_id}/compositions", status_code=202)
def create_composition(project_id: str, payload: CompositionRequest, session: Session = Depends(get_db)):
    required(session, Project, project_id, "项目")
    if len(set(payload.video_ids)) != len(payload.video_ids):
        raise AppError(422, "duplicate_clip", "不能重复同一片段充数")
    for identifier in payload.video_ids:
        video = required(session, VideoRevision, identifier, "视频版本")
        if video.project_id != project_id or video.review_status != "approved":
            raise AppError(409, "approved_clip_required", "成片只能使用本项目已批准的片段")
    item = FinalComposition(project_id=project_id, video_ids_json=payload.video_ids, status="queued")
    session.add(item)
    session.flush()
    audit(session, "final_composition", item.id, "queued", {"video_ids": payload.video_ids})
    session.commit()
    return row_dict(item)


@router.post("/videos/{video_id}/revision-preview")
def revision_preview(video_id: str, payload: RevisionRequest, request: Request, session: Session = Depends(get_db)):
    video = required(session, VideoRevision, video_id, "视频版本")
    shot = required(session, StoryboardShot, video.shot_id, "分镜")
    from app.services.screenplay import uses_structured_screenplay, generation_inputs
    from app.services.video_revisions import editable_generation_prompt
    structured = uses_structured_screenplay(session, shot)
    if structured:
        version, references, prompt, snapshot = generation_inputs(session, shot, request.app.state.settings)
        negative = snapshot.get('model_lab_negative_prompt', '')
        asset_ids = [ref['asset_id'] for ref in references]
    else:
        version = required(session, CharacterVersion, shot.character_asset_version_id, "角色版本")
        prompt, negative = build_prompt(shot, version, payload.instruction)
        asset_ids = shot.reference_asset_ids_json
    prompt = editable_generation_prompt(prompt, payload.instruction, structured=structured, prompt_override=payload.prompt_override)
    return {"prompt": prompt, "negative_prompt": negative,
            "character_id": version.character_id if version else None,
            "character_version_id": version.id if version else None,
            "asset_ids": asset_ids, "instruction": payload.instruction, "based_on": video.id}


@router.get("/projects/{project_id}/compositions")
def list_compositions(project_id: str, session: Session = Depends(get_db)):
    return [{**row_dict(item), "media_url": f"/api/media/{item.relative_path}" if item.relative_path else None} for item in session.scalars(select(FinalComposition).where(FinalComposition.project_id == project_id).order_by(FinalComposition.created_at.desc()))]


@router.post("/compositions/{composition_id}/dry-run")
async def final_dry_run(request: Request, composition_id: str, payload: FinalDryRunRequest, session: Session = Depends(get_db)):
    item = required(session, FinalComposition, composition_id, "成片")
    if item.status != "generated" or not item.relative_path:
        raise AppError(409, "final_not_generated", "需要真实已合成的成片")
    path = safe_media_path(request.app.state.settings.media_root, item.relative_path, must_exist=True)
    checks = await asyncio.to_thread(inspect_media, path, request.app.state.settings)
    valid = not any(check["status"] == "failed" for check in checks) and any(c["check_type"] == "media_decode" and c["status"] == "passed" for c in checks)
    cover_valid = False
    cover_relative = item.metadata_json.get("cover_relative_path")
    if cover_relative:
        try:
            cover = safe_media_path(request.app.state.settings.media_root, cover_relative, must_exist=True)
            with Image.open(cover) as image:
                image.verify()
            cover_valid = True
        except (OSError, ValueError, AppError):
            pass
    results = []
    for platform in ("douyin", "kuaishou", "xiaohongshu", "wechat_channels"):
        reasons = []
        candidates = session.scalars(select(PublishingAccount).where(PublishingAccount.platform == platform, PublishingAccount.auth_status != "removed").order_by(PublishingAccount.created_at.desc())).all()
        account = next((entry for entry in candidates if entry.account_metadata_json.get("simulated") is True), None)
        if account is None:
            reasons.append("数据库中尚未配置该平台的模拟账号")
        elif account.account_metadata_json.get("dry_run_enabled") is not True:
            reasons.append("该模拟账号的 dry-run 配置未启用")
        elif account.credential_reference:
            reasons.append("本次模拟账号必须不关联真实凭据")
        if not valid:
            reasons.append("成片文件/解码校验失败")
        if not cover_valid:
            reasons.append("缺少可解码的真实成片封面")
        if not 1 <= len(payload.title.strip()) <= 80:
            reasons.append("本地预检要求标题1..80字符")
        if len(payload.description) > 1000:
            reasons.append("本地预检要求描述不超过1000字符")
        if len(payload.hashtags) > 10 or any(not tag.strip() or len(tag) > 30 for tag in payload.hashtags):
            reasons.append("本地预检要求话题最多10项、每项1..30字符")
        account_check = {"status": "simulated" if account else "not_configured", "id": account.id if account else None, "display_name": account.display_name if account else None, "authorization_status": account.auth_status if account else "not_configured", "dry_run_enabled": bool(account and account.account_metadata_json.get("dry_run_enabled") is True), "credentials_stored": bool(account and account.credential_reference)}
        results.append({"platform": platform, "status": "FAIL" if reasons else "PASS", "notice": "预览完成/模拟检查通过" if not reasons else "模拟检查未通过", "failures": reasons, "account": account_check, "media_checks": checks, "cover": {"status": "PASS" if cover_valid else "FAIL", "media_url": f"/api/media/{cover_relative}" if cover_relative else None}, "fields": payload.model_dump(), "external_requests": 0, "policy": "仅本地保守字段预检，不代表平台审核/授权"})
    audit(session, "final_composition", item.id, "publish_dry_run", {"results": results, "external_requests": 0})
    session.commit()
    return {"results": results, "external_requests": 0, "real_publish_enabled": False}


@router.get("/diagnostics/reports")
def reports_index():
    return [{"name": p.name, "bytes": p.stat().st_size} for p in (PROJECT_ROOT / "reports").iterdir() if p.is_file() and p.suffix in {".json", ".md"}]
