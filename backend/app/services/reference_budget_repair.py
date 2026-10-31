"""Preflight repairs for definitive, pre-submission keyframe budget failures."""
import copy
from collections import Counter

from app import models as m
from app.core.errors import AppError


REPAIRABLE_CODES={'product_visual_reference_limit','visual_reference_limit'}


def inspect_reference_budget_repairs(session,settings,run,nodes):
    from app.services.studio_pipeline import _service,_visual
    from app.services.cinematic_visuals import native_visual_reference_limit,visual_graph
    from app.services.identity_references import verified_image
    ready=[];diagnostics=[];blocked=[]
    for item in nodes:
        if (item.get('kind')!='keyframe' or item.get('code') not in REPAIRABLE_CODES
                or item.get('status') not in {'failed','exception'}):
            continue
        if item.get('child_id'):
            blocked.append({'node_id':item['id'],'kind':'keyframe','status':item['status'],
                'code':item['code'],'message':'该节点已有真实子任务；本次不重建或重复提交，请先核对原子任务。'})
            continue
        try:
            service,snapshot=_service(session,run,'image','keyframe_image',item['target_id'])
            limit=native_visual_reference_limit(snapshot,service.capabilities_json)
            if snapshot['provider']!='comfyui' or snapshot.get('is_cloud') or limit<=4:
                raise AppError(409,'reference_budget_adapter_unverified','当前服务／模型没有匹配的原生多参考图链能力，未更换服务或丢弃图片。')
            probe=copy.deepcopy(item)
            probe.update(status='pending',error=None,code=None)
            # Compile the very same path as the pipeline, then roll it back.
            # No orphan visual, approval, audit or provider request is persisted.
            with session.begin_nested() as transaction:
                _visual(session,settings,run,probe,True)
                visual=session.get(m.ProductionVisual,probe.get('child_id')) if probe.get('child_id') else None
                if not visual or probe.get('status') not in {'running','succeeded'}:
                    raise AppError(409,'reference_budget_original_task_unresolved','已有任务的结果或当前素材尚未确定，未重复派发。')
                if visual.status in {'submitting','submitted','running','unknown'}:
                    raise AppError(409,'reference_budget_original_task_unresolved','已发现相同输入的原服务任务，先核对原任务，不重复提交。')
                actual=visual.snapshot_json.get('service_snapshot',{})
                if any(actual.get(key)!=snapshot.get(key) for key in ('service_id','service_revision','model_id')):
                    raise AppError(409,'reference_budget_service_changed','编译服务与当前任务冻结配置不一致，未自动换用其他模型。')
                refs=visual.snapshot_json.get('references',[])
                for reference in refs:verified_image(settings,reference)
                graph=visual_graph(visual,[str(reference['asset_id'])+'.png' for reference in refs])
                if sum(node['class_type']=='LoadImage' for node in graph.values())!=len(refs):
                    raise AppError(409,'reference_budget_graph_mismatch','实际工作流未包含全部所选参考图，未重排。')
                info={'node_id':item['id'],'shot_id':item['target_id'],'reference_count':len(refs),
                    'reference_ids':[ref['asset_id'] for ref in refs],
                    'roles':dict(Counter(ref.get('role','unspecified') for ref in refs)),
                    'service_id':snapshot['service_id'],'model_id':snapshot['model_id'],'budget':limit,
                    'inference_verified':False,'selection_policy':'保留全部人物、场景、产品参考及原件SHA；不拼图、不裁减'}
                transaction.rollback()
            ready.append(item);diagnostics.append(info)
        except AppError as exc:
            blocked.append({'node_id':item['id'],'kind':'keyframe','status':item['status'],
                'code':exc.code,'message':exc.message})
        except (OSError,ValueError,KeyError,TypeError) as exc:
            blocked.append({'node_id':item['id'],'kind':'keyframe','status':item['status'],
                'code':'reference_budget_preflight_failed','message':'参考图／工作流预检未通过（'+type(exc).__name__+'）；原节点与原件保留。'})
    return ready,diagnostics,blocked
