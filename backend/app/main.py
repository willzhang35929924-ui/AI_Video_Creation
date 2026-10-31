from __future__ import annotations

from contextlib import asynccontextmanager
import asyncio
import contextlib
import logging
from uuid import uuid4

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router
from app.api.diagnostics import router as diagnostics_router
from app.api.ai_settings import router as ai_settings_router
from app.api.character_factory import router as character_factory_router
from app.api.assemblies import router as assemblies_router
from app.api.workspace import router as workspace_router
from app.api.cinematic import router as cinematic_router
from app.api.studio import router as studio_router
from app.api.studio_import import router as studio_import_router
from app.api.studio_review import router as studio_review_router
from app.api.studio_voice import router as studio_voice_router
from app.api.quality_settings import router as quality_router
from app.services.video_quality import quality_worker
from app.api.model_lab import router as model_lab_router
from app.api.model_services import router as model_services_router
from app.api.screenplay_generation import router as screenplay_generation_router
from app.api.character_reference import router as character_reference_router
from app.services.screenplay_generation import screenplay_worker
from app.api.product_library import router as product_library_router
from app.api.voice_library import router as voice_library_router
from app.api.reference_video_library import router as reference_video_router
from app.services.reference_video_analysis import reference_video_worker
from app.services.media_processing import media_worker
from app.services.model_lab import model_lab_worker
from app.services.studio_pipeline import studio_worker
from app.services.cinematic_visuals import visual_worker
from app.services.production_orchestrator import production_worker
from app.services.assembly import assembly_worker
from app.services.character_factory import character_worker
from app.services.ai_services import seed_local_services
from app.core.config import Settings, get_settings
from app.core.db import Base, build_engine, build_session_factory
from app.core.errors import AppError, app_error_handler
from app.services.seed import seed_demo
from app.services.jobs import worker
from app.models import GenerationWorkflow
import hashlib
import json
import os
from starlette.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)
from app.core.config import PROJECT_ROOT


class FrontendFiles(StaticFiles):
    async def get_response(self, path, scope):
        normalized = path.replace('\\', '/').lstrip('/')
        if normalized == 'api' or normalized.startswith('api/'):
            raise StarletteHTTPException(404)
        try:
            response = await super().get_response(path, scope)
            if response.status_code != 404:
                return response
        except StarletteHTTPException as exc:
            if exc.status_code != 404:
                raise
        if normalized.startswith('assets/') and normalized not in {'assets/products','assets/videos','assets/voices'}:
            raise StarletteHTTPException(404)
        return await super().get_response('index.html', scope)


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or get_settings()
    engine = build_engine(resolved_settings.database_url)
    session_factory = build_session_factory(engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        resolved_settings.media_root.mkdir(parents=True, exist_ok=True)
        if resolved_settings.container_mode:
            from app.core.deployment import validate_container_startup
            app.state.container_startup=validate_container_startup(resolved_settings)
        from app.core.migrations import upgrade_database
        upgrade_database(resolved_settings, engine)
        Base.metadata.create_all(engine)
        with session_factory() as session:
            seed_local_services(session, resolved_settings)
            from sqlalchemy import select
            from app.models import GenerationJob
            for pending in session.scalars(select(GenerationJob).where(GenerationJob.status=='submitting',GenerationJob.provider_job_id.is_(None))):
                pending.status='unknown';pending.error_code='submission_outcome_unknown';pending.error_message='服务在提交期间中断，结果未知；核对供应商原请求后处理，不自动重复提交'
            session.commit()
        if resolved_settings.h3_workflow_api_json and resolved_settings.h3_workflow_api_json.exists():
            data = resolved_settings.h3_workflow_api_json.read_bytes()
            graph = json.loads(data)
            digest = hashlib.sha256(data).hexdigest()
            identifier = digest[:32]
            with session_factory() as session:
                if session.get(GenerationWorkflow, identifier) is None:
                    session.add(GenerationWorkflow(id=identifier, source_url=graph.get("_video_factory", {}).get("upstream_url", ""), sha256=digest, graph_json=graph))
                    session.commit()
        if resolved_settings.seed_demo_data:
            with session_factory() as session:
                seed_demo(session, resolved_settings.media_root.parent.parent)
        task = asyncio.create_task(worker(resolved_settings, session_factory)) if not resolved_settings.h3_demo_mode else None
        image_task = asyncio.create_task(character_worker(resolved_settings, session_factory)) if not resolved_settings.h3_demo_mode else None
        assembly_task = asyncio.create_task(assembly_worker(resolved_settings, session_factory)) if not resolved_settings.h3_demo_mode else None
        cinematic_visual_task=asyncio.create_task(visual_worker(resolved_settings,session_factory)) if not resolved_settings.h3_demo_mode else None
        production_task=asyncio.create_task(production_worker(app,resolved_settings,session_factory)) if not resolved_settings.h3_demo_mode else None
        studio_task=asyncio.create_task(studio_worker(app,resolved_settings,session_factory)) if not resolved_settings.h3_demo_mode else None
        quality_task=asyncio.create_task(quality_worker(resolved_settings,session_factory)) if not resolved_settings.h3_demo_mode else None
        model_lab_task=asyncio.create_task(model_lab_worker(app,resolved_settings,session_factory)) if not resolved_settings.h3_demo_mode else None
        media_task=asyncio.create_task(media_worker(resolved_settings,session_factory)) if not resolved_settings.h3_demo_mode else None
        screenplay_task=asyncio.create_task(screenplay_worker(resolved_settings,session_factory)) if not resolved_settings.h3_demo_mode else None
        reference_video_task=asyncio.create_task(reference_video_worker(resolved_settings,session_factory)) if not resolved_settings.h3_demo_mode else None
        app.state.worker_tasks={name:task for name,task in {
            'jobs':task,'character':image_task,'assembly':assembly_task,'visual':cinematic_visual_task,
            'production':production_task,'studio':studio_task,'quality':quality_task,'model_lab':model_lab_task,
            'media':media_task,'screenplay':screenplay_task,'reference_video':reference_video_task}.items() if task is not None}
        yield
        for cinematic_task in (cinematic_visual_task,production_task,studio_task,quality_task,model_lab_task,media_task,screenplay_task,reference_video_task):
            if cinematic_task:
                cinematic_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):await cinematic_task
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if image_task:
            image_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await image_task
        if assembly_task:
            assembly_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await assembly_task
        engine.dispose()

    application = FastAPI(
        title="人物一致性 AI 视频工厂 API",
        version="0.1.0",
        description="本地优先工作流。真实 H3 状态只在验证当前 ComfyUI 与工作流后报告。",
        lifespan=lifespan,
    )
    application.state.settings = resolved_settings
    application.state.engine = engine
    application.state.session_factory = session_factory
    application.state.worker_tasks = {}
    @application.get('/api/health/runtime')
    def runtime_status():
        from app.core.deployment import runtime_health
        result=runtime_health(application)
        return JSONResponse(status_code=200 if result['application_ready'] else 503,content=result)
    application.add_exception_handler(AppError, app_error_handler)

    @application.exception_handler(Exception)
    async def unhandled_exception_handler(request, exc):
        # Keep the traceback in the backend log and give the caller a stable
        # correlation token. Never expose exception text or secrets in the API.
        request_id = getattr(request.state, 'request_id', None) or uuid4().hex
        logger.error(
            'Unhandled request exception request_id=%s method=%s path=%s',
            request_id, request.method, request.url.path,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        return JSONResponse(
            status_code=500,
            content={'error': {
                'code': 'internal_error',
                'message': '服务器内部错误，请将请求编号提供给维护人员。',
                'request_id': request_id,
            }},
            headers={'X-Request-ID': request_id},
        )
    @application.exception_handler(RequestValidationError)
    async def safe_validation_error(_request, exc):
        # Pydantic normally includes the rejected input. Never reflect key fields.
        return JSONResponse(status_code=422, content={'error': {'code': 'validation_error', 'message': '输入参数不符合要求', 'details': {'fields': [{'loc': list(item['loc']), 'type': item['type'], 'message': item['msg']} for item in exc.errors()]}}})
    application.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:5173",
            "http://localhost:5173",
            "http://127.0.0.1:3000",
            "http://localhost:3000",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    application.include_router(router)
    application.include_router(diagnostics_router)
    application.include_router(ai_settings_router)
    application.include_router(character_factory_router)
    application.include_router(assemblies_router)
    application.include_router(workspace_router)
    application.include_router(cinematic_router)
    application.include_router(studio_router)
    application.include_router(studio_import_router)
    application.include_router(studio_review_router)
    application.include_router(studio_voice_router)
    application.include_router(quality_router)
    application.include_router(model_lab_router)
    application.include_router(model_services_router)
    application.include_router(screenplay_generation_router)
    application.include_router(character_reference_router)
    application.include_router(product_library_router)
    application.include_router(voice_library_router)
    application.include_router(reference_video_router)
    frontend = PROJECT_ROOT / 'frontend' / 'dist'
    if os.getenv('VIDEO_FACTORY_PORTABLE') == '1' and (frontend / 'index.html').exists():
        @application.get('/api/portable/status')
        def portable_status():
            return {'instance': os.getenv('VIDEO_FACTORY_INSTANCE_TOKEN', ''), 'root': str(PROJECT_ROOT)}
        application.mount('/', FrontendFiles(directory=str(frontend), html=True), name='frontend')
    return application


app = create_app()

