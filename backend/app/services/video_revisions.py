"""Resolve text revisions from native generation inputs, never upscale metadata."""
from app.core.errors import AppError
from app.models import GenerationJob
from app.schemas import GenerationRequest


def revision_source_job(session, shot, current):
    previous = session.get(GenerationJob, current.generation_job_id)
    if previous is None:
        raise AppError(404, 'revision_source_job_missing', '原视频的生成任务不存在，无法读取修订参数')
    if (current.project_id != shot.project_id or current.shot_id != shot.id
            or previous.project_id != shot.project_id or previous.shot_id != shot.id):
        raise AppError(409, 'revision_source_scope', '原视频及生成任务必须属于当前项目和分镜')
    return previous


def revision_generation_request(session, shot, current, payload):
    previous = revision_source_job(session, shot, current)
    original = previous.parameters_json or {}
    snapshot = original.get('service_snapshot') or {}
    seed = payload.seed if payload.seed is not None else previous.seed
    if payload.use_project_video_service:
        if (payload.service_id or payload.model_id or payload.parameters.get('service_id')
                or payload.parameters.get('model_id')):
            raise AppError(422, 'revision_service_conflict', '请在沿用项目制作设置与指定服务之间选择一种')
        # The selected H3 workflow supplies native dimensions and sampler steps.
        return GenerationRequest(seed=seed, parameters=payload.parameters, prompt_override=payload.prompt_override)
    # Copy only native sampler settings. Old Studio IDs, reference manifests and
    # screenplay snapshots must be rebuilt for the new task by generate_for_shot.
    parameters = {k: original[k] for k in ('width', 'height', 'steps') if k in original}
    if not snapshot.get('h3_workflow') and not any(k in payload.parameters for k in ('width', 'height')):
        width, height = parameters.get('width'), parameters.get('height')
        ratio = {'16:9': 16/9, '9:16': 9/16, '1:1': 1}[shot.aspect_ratio]
        if (not isinstance(width, int) or not isinstance(height, int)
                or width <= 0 or height <= 0 or abs(width / height - ratio) > .025):
            from app.services.video_quality import plan
            native = plan({'aspect_ratio': shot.aspect_ratio})['generation']
            parameters.update(width=native['width'], height=native['height'])
    parameters.update(payload.parameters)
    # Explicit provider/model selection wins; changing services must not silently
    # force that service to load the previous service's unrelated model.
    service_id = payload.service_id or parameters.get('service_id') or snapshot.get('service_id')
    model_id = payload.model_id or parameters.get('model_id')
    if not model_id and service_id == snapshot.get('service_id'):
        model_id = snapshot.get('requested_model_id') or snapshot.get('model_id')
    return GenerationRequest(seed=seed,
                             parameters=parameters, service_id=service_id, model_id=model_id, prompt_override=payload.prompt_override)


def prepare_context_revision(session, settings, shot, current, payload):
    """Use the same service and scoped Context-IR inputs for preview and submission."""
    from app.api.routes import script_for_shot
    from app.services.context_production_inputs import collect_context_inputs, quality_for_shot
    from app.services.studio_audio_policy import get_audio_policy
    from app.services.task_routing import freeze_route, route_is_explicit, selected_route
    from app.services.video_services import select_video_service

    previous = revision_source_job(session, shot, current)
    script = script_for_shot(session, shot)
    if not script or script.project_id != shot.project_id or script.status != 'approved':
        raise AppError(409, 'script_not_approved', '脚本尚未审核通过')
    if shot.status != 'approved':
        raise AppError(409, 'shot_not_approved', '分镜尚未审核通过')
    generation = revision_generation_request(session, shot, current, payload)
    service_id, model_id = generation.service_id, generation.model_id
    if payload.use_project_video_service:
        # Evaluate the real preference order. The Context selector below rejects
        # a cloud choice before any paid submission could take place.
        route = freeze_route(session, settings, shot.project_id, shot.id, 'video_generation', allow_cloud_costs=True)
        if route_is_explicit(route):
            selected = selected_route(route)
            service_id, model_id = selected['service_id'], selected['model_id']
    elif previous.provider.startswith('cloud_') and not service_id:
        raise AppError(409, 'revision_cloud_service_missing', '原视频缺少可用的云端服务快照，请明确改用当前项目的本地视频服务')
    quality = quality_for_shot(session, shot)
    _, service, _ = select_video_service(session, settings, shot.project_id, service_id, model_id,
                                        quality_mode=quality['mode'])
    original_context = (previous.parameters_json or {}).get('context_inputs') or {}
    audio_policy = original_context.get('audio_policy')
    context = collect_context_inputs(session, settings, shot, prompt_override=generation.prompt_override,
        instruction=payload.instruction, image_ids=original_context.get('explicit_visual_ids'),
        motion_values=original_context.get('explicit_motion'),
        audio_policy=audio_policy if audio_policy is not None else get_audio_policy(session, shot.project_id))
    return generation, service, quality, context


def apply_revision_instruction(prompt, instruction, *, structured):
    if not instruction:
        return prompt
    if structured:
        heading = 'detailed_description:\n'
        return (prompt.replace(heading, heading+'Reviewed repair direction for this shot: '+instruction+'\n', 1)
                if heading in prompt else prompt+'\nRevision instruction: '+instruction)
    return prompt


def editable_generation_prompt(prompt, instruction, *, structured, prompt_override=None):
    """Use exactly the edited text while retaining a separately entered revision.

    Legacy build_prompt already includes the revision. Structured prompts insert
    it here; an override replaces the compiled body in either path.
    """
    if prompt_override is not None:
        if not prompt_override.strip():
            raise AppError(422, 'prompt_override_empty', '自定义提示词不能为空')
        prompt = prompt_override
        if instruction and not structured:
            prompt += '\nRevision instruction: ' + instruction
    return apply_revision_instruction(prompt, instruction, structured=structured)
