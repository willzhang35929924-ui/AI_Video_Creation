"""Default studio upload, with the existing detailed preview as exception UI."""
import asyncio
import json
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import AppError
from app.services.studio_import import import_word

router=APIRouter(prefix='/api/studio',tags=['studio'])
# One process owns the studio worker. Serializing uploads prevents two tabs from
# creating the same project while an optional language request is in flight.
_import_lock=asyncio.Lock()


@router.post('/import')
async def upload(request:Request,file:UploadFile=File(...),project_id:str|None=Form(None),
        auto_start:bool=Form(False),allow_cloud_costs:bool=Form(False),allow_paid_fallback:bool=Form(False),new_revision:bool=Form(False),
        quality_json:str|None=Form(None),
        session:Session=Depends(get_db)):
    settings=request.app.state.settings
    data=await file.read(settings.import_max_bytes+1)
    if len(data)>settings.import_max_bytes:raise AppError(413,'document_too_large','文档超过大小限制')
    filename=Path((file.filename or 'document.docx').replace('\\','/')).name[:240]
    quality=None
    if quality_json:
        from app.schemas_quality import QualitySettings
        try:quality=QualitySettings.model_validate(json.loads(quality_json)).model_dump()
        except (ValueError,TypeError):raise AppError(422,'invalid_quality_settings','制作画质配置无效')
    async with _import_lock:
        return await import_word(session,settings,filename,data,project_id=project_id,
            auto_start=auto_start,allow_cloud_costs=allow_cloud_costs,allow_paid_fallback=allow_paid_fallback,new_revision=new_revision,quality=quality)
