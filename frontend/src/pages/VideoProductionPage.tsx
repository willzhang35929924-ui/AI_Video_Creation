import { useEffect, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { request } from '../lib/api';
import { useStudio, StudioProjectPicker, StudioStatus } from '../lib/studio-ui';
import { PageHeader, EmptyState, LoadingState } from '../components/ui';
import { QualitySettingsFields, QualityPlan, QualityProfileEditor, useQualityProfile } from '../components/QualitySettings';
import { VideoServiceSelector } from '../components/VideoServiceSelector';
import { VideoTaskPanel } from '../components/VideoTaskPanel';
import { StudioProductPlanPicker } from '../components/StudioProductPlanPicker';
import { ProductionMaterialsPanel } from './ProductionPage';
import type { PipelineStart,StudioProductReferencePlan } from '../lib/studio-contract';
import '../studio.css';

export function VideoProductionPage(){
  const studio=useStudio(),{projectId,data,projects}=studio;
  const quality=useQualityProfile(projectId?'project':'system',projectId||'default');
  const [params,setParams]=useSearchParams();
  const [selection,setSelection]=useState<string[]>([]),[allowCosts,setAllowCosts]=useState(false),[paidFallback,setPaidFallback]=useState(false),[confirmed,setConfirmed]=useState(false),[busy,setBusy]=useState(''),[error,setError]=useState(''),[notice,setNotice]=useState(''),[bindingDirty,setBindingDirty]=useState(false);
  const [materialsDirty,setMaterialsDirty]=useState(false),[shotDirty,setShotDirty]=useState(false);
  const [automaticDraft,setAutomaticDraft]=useState(false);
  const [productPlanEnabled,setProductPlanEnabled]=useState(false),[productPlan,setProductPlan]=useState<StudioProductReferencePlan>({product_id:'',image_ids:[],selection_reason:''});
  const productPlanIncomplete=productPlanEnabled&&(!productPlan.product_id||!productPlan.image_ids.length||!productPlan.selection_reason.trim());
  const shots=data?.shots||[];
  const shotKey=shots.map(shot=>shot.id).join('|');
  useEffect(()=>{setSelection(shots.map(shot=>shot.id));setConfirmed(false);},[projectId,shotKey]);
  useEffect(()=>{setProductPlanEnabled(false);setProductPlan({product_id:'',image_ids:[],selection_reason:''});},[projectId]);
  useEffect(()=>{setAutomaticDraft(Boolean(data?.run?.snapshot_json?.options?.automatic_draft_assembly));},[projectId,data?.run?.id]);
  useEffect(()=>{setConfirmed(false);},[quality.settings,selection,bindingDirty,materialsDirty,shotDirty,allowCosts,paidFallback,productPlanEnabled,productPlan,automaticDraft]);
  async function control(action:'start'|'pause'|'resume'){
    setBusy(action);setError('');setNotice('');
    try{if(action==='start'&&!quality.supported)throw new Error('当前处理方式尚无对应画幅和档位的实测能力。');if(action==='start'&&quality.dirty)await quality.save();
      const payload:PipelineStart=action==='start'?{quality:quality.settings,shot_ids:selection,allow_cloud_costs:allowCosts,allow_paid_fallback:paidFallback,automatic_draft_assembly:automaticDraft,...(productPlanEnabled?{product_reference_plan:{...productPlan,selection_reason:productPlan.selection_reason.trim()}}:{})}:{};
      if(action==='resume'&&data?.run?.id)await request(`/api/studio/projects/${projectId}/automatic-draft`,{method:'POST',body:JSON.stringify({expected_run_id:data.run.id,enabled:automaticDraft})});
      await request(`/api/studio/projects/${projectId}/${action}`,{method:'POST',body:JSON.stringify(payload)});await studio.refreshAfterChange();setConfirmed(false);setNotice('请求已提交，状态由后台实际任务更新。');
    }catch(e){setError(String(e));}finally{setBusy('');}
  }
  const active=data?.run&&!['completed','cancelled','failed','completed_with_errors','waiting_review'].includes(data.run.status);
  const selected=shots.find(shot=>shot.id===params.get('shot'))||shots[0];
  return <div className="studio-workspace"><PageHeader title="视频制作" description="制作设置、逐镜输入、任务与高级资料集中于同一处。新任务使用统一自动生产链，生成结果进入审片与成片。" actions={<Link className="button button-secondary" to={projectId?`/?project=${projectId}`:'/'}>项目工作台</Link>}/>
    <div className="studio-toolbar"><StudioProjectPicker projectId={projectId} projects={projects} selectedName={data?.project.name} disabled={Boolean(busy)||quality.saving||quality.loading} onChange={id=>{if(busy)return;if((materialsDirty||shotDirty||quality.dirty||bindingDirty)&&!window.confirm('有未保存的制作设置或资料，仍要切换项目吗？'))return;studio.selectProject(id);}}/>{projectId?<Link className="button button-primary" to={`/studio-review?project=${projectId}`}>审片与成片</Link>:null}</div>
    {(error||studio.error)?<p role="alert" className="inline-notice warning">{error||studio.error}</p>:null}{notice?<p role="status">{notice}</p>:null}
    {!projectId?<EmptyState title="选择制作项目" description="工作台导入 Word 后，人物、场景和分镜会自动建立。" action={<Link className="button button-primary" to="/">上传剧本</Link>}/>:null}
    {studio.loading?<LoadingState label="读取真实制作数据…"/>:null}
    {projectId&&data?<><section className="card studio-panel"><h2>项目制作画质与生成范围</h2>{quality.error?<p role="alert">{quality.error}</p>:null}{quality.loading?<p>读取制作设置…</p>:null}{quality.profile?<><p>配置来源：{quality.profile.inherited_from||'项目独立设置'} · 修订 {quality.profile.revision}</p><QualitySettingsFields value={quality.settings} onChange={quality.setSettings} capabilities={quality.capabilities} capabilityError={quality.capabilityError} onRefreshCapabilities={quality.refreshCapabilities} disabled={Boolean(busy)||quality.saving||quality.loading}/><button type="button" className="button button-secondary" disabled={Boolean(busy)||quality.saving||quality.loading||!quality.supported} onClick={()=>void quality.save().then(()=>setNotice('项目画质设置已保存。')).catch(()=>{})}>保存项目制作设置</button>{quality.dirty?<p>有未保存设置；下方是上次保存方案，开始时会先保存。</p>:null}<QualityPlan plan={quality.profile.plan}/></>:null}
      <StudioProductPlanPicker enabled={productPlanEnabled} onEnabled={setProductPlanEnabled} value={productPlan} onChange={setProductPlan} disabled={Boolean(busy)}/><fieldset><legend>本次制作镜头</legend>{shots.map(shot=><label className="studio-check" key={shot.id}><input type="checkbox" checked={selection.includes(shot.id)} disabled={Boolean(busy)} onChange={e=>setSelection(old=>e.target.checked?[...old,shot.id]:old.filter(id=>id!==shot.id))}/><span>#{shot.order} · {shot.title} · {shot.cast?.map(person=>person.name).join('、')}</span></label>)}</fieldset>
      <label className="studio-check"><input type="checkbox" checked={automaticDraft} disabled={Boolean(busy)||Boolean(data.run&&data.run.status!=='paused')} onChange={e=>setAutomaticDraft(e.target.checked)}/><span>镜头技术处理完成后自动硬切生成完整音画草稿；草稿和角色、对白仍需人工审核。</span></label>
      <label className="studio-check"><input type="checkbox" checked={allowCosts} disabled={Boolean(busy)} onChange={e=>{setAllowCosts(e.target.checked);if(!e.target.checked)setPaidFallback(false);}}/><span>允许本次范围使用已配置的云服务，并承担供应商实际费用；未勾选仅本地任务。</span></label>
      <label className="studio-check"><input type="checkbox" checked={paidFallback} disabled={Boolean(busy)||!allowCosts} onChange={e=>setPaidFallback(e.target.checked)}/><span>允许本次制作使用已保存的付费云端回退策略。</span></label><Link to="/settings">自定义模型与服务</Link><label className="studio-check"><input type="checkbox" checked={confirmed} disabled={Boolean(busy)||bindingDirty||materialsDirty||shotDirty||productPlanIncomplete} onChange={e=>setConfirmed(e.target.checked)}/><span>已核对镜头范围、服务绑定与画质配置，开始缺失任务或增量续作。</span></label>
      {bindingDirty?<p role="status">服务／模型有未保存选择，请先保存项目覆盖或恢复模块默认。</p>:null}{materialsDirty||shotDirty?<p role="status">有未保存的镜头画质、连续性或制作资料，请先保存再启动。</p>:null}
      <div className="studio-actions">{!active?<button type="button" className="button button-primary" disabled={Boolean(busy)||!confirmed||!selection.length||!quality.profile||quality.loading||quality.saving||!quality.supported||bindingDirty||materialsDirty||shotDirty||productPlanIncomplete} onClick={()=>void control('start')}>开始统一视频制作</button>:data.run?.status==='paused'?<><button type="button" className="button button-secondary" disabled={Boolean(busy)||productPlanEnabled} onClick={()=>void control('resume')}>继续当前编排</button><button type="button" className="button button-primary" disabled={Boolean(busy)||!confirmed||!selection.length||!quality.profile||quality.loading||quality.saving||!quality.supported||bindingDirty||materialsDirty||shotDirty||productPlanIncomplete} onClick={()=>void control('start')}>按当前设置新建编排</button></>:<button type="button" className="button button-secondary" disabled={Boolean(busy)} onClick={()=>void control('pause')}>暂停后续编排</button>}<StudioStatus status={data.run?.status}/></div><p className="studio-muted">暂停编排不等于取消已被模型接收的任务；底层任务可在下方按实际状态取消。画质改变不会覆盖旧视频。产品原图选择仅用于新建编排，继续当前编排会沿用原快照。</p>
    </section><VideoServiceSelector projectId={projectId} onChange={()=>{}} onDirtyChange={setBindingDirty}/>
    <section className="card studio-panel"><h2>逐镜继承与覆盖</h2><select aria-label="画质覆盖镜头" value={selected?.id||''} onChange={e=>{if(shotDirty&&!window.confirm('此镜画质设置尚未保存，仍要切换吗？'))return;setParams(current=>{const next=new URLSearchParams(current);next.set('shot',e.target.value);return next;},{replace:true});}}>{shots.map(shot=><option value={shot.id} key={shot.id}>#{shot.order} · {shot.title}</option>)}</select><p>项目默认适用于没有独立覆盖的镜头；覆盖只影响此镜后续生成。</p>{selected?<QualityProfileEditor key={selected.id} scope="shot" targetId={selected.id} title={`#${selected.order} 镜头画质`} onDirtyChange={setShotDirty}/>:null}</section>
    <VideoTaskPanel projectId={projectId} shots={shots}/><details className="workspace-details"><summary>高级制作资料、参考与旧编排历史</summary><ProductionMaterialsPanel key={projectId} projectId={projectId} onDirtyChange={setMaterialsDirty}/></details></>:null}
  </div>;
}
