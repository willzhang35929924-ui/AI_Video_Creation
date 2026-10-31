"""Resolve text revisions from native generation inputs, never upscale metadata."""
from app.core.errors import AppError
from app.models import GenerationJob
from app.schemas import GenerationRequest


def revision_generation_request(session, shot, current, payload):
    previous = session.get(GenerationJob, current.generation_job_id)
    if previous is None:
        raise AppError(404, 'revision_source_job_missing', '原视频的生成任务不存在，无法读取修订参数')
    original = previous.parameters_json or {}
    snapshot = original.get('service_snapshot') or {}
    # Copy only native sampler settings. Old Studio IDs, reference manifests and
    # screenplay snapshots must be rebuilt for the new task by generate_for_shot.
    parameters = {k: original[k] for k in ('width', 'height', 'steps') if k in original}
    if not any(k in payload.parameters for k in ('width', 'height')):
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
        model_id = snapshot.get('model_id')
    return GenerationRequest(seed=payload.seed if payload.seed is not None else previous.seed,
                             parameters=parameters, service_id=service_id, model_id=model_id, prompt_override=payload.prompt_override)


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
