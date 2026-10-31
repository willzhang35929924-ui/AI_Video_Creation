import {useEffect,useMemo,useRef,useState} from 'react';
import {ApiError,request} from '../lib/api';
import type {RouteProfile,RouteSave} from '../lib/model-services-contract';
import type {AIService} from '../pages/AIServicesPage';
import {FormField} from './ui';

export type ProjectTTSServiceState={dirty:boolean;busy:boolean;refreshPending?:boolean};
export type ProjectTTSServiceSelectorProps={projectId:string;disabled?:boolean;refreshKey?:string|number;onStateChange?:(state:ProjectTTSServiceState)=>void;onSaved?:()=>void|Promise<void>};
const families=new Set(['cosyvoice3','indextts25']);

export function ProjectTTSServiceSelector({projectId,disabled=false,refreshKey,onStateChange,onSaved}:ProjectTTSServiceSelectorProps){
  const [services,setServices]=useState<AIService[]>([]),[profile,setProfile]=useState<RouteProfile|null>(null),[loading,setLoading]=useState(true),[busy,setBusy]=useState(false),[draft,setDraft]=useState<string|null>(null),[error,setError]=useState(''),[conflict,setConflict]=useState(false),[reload,setReload]=useState(0);
  // Refresh signals do not change the destination of a draft or an in-flight PUT.
  const identity=projectId,current=useRef(identity);current.current=identity;
  const [appliedRefresh,setAppliedRefresh]=useState({identity,key:refreshKey}),[requestedRefresh,setRequestedRefresh]=useState(false);
  const pendingRefresh=requestedRefresh||(appliedRefresh.identity===identity&&appliedRefresh.key!==refreshKey);
  const alive=useRef(true),callbacks=useRef({onStateChange,onSaved});callbacks.current={onStateChange,onSaved};
  useEffect(()=>{alive.current=true;return()=>{alive.current=false;callbacks.current.onStateChange?.({dirty:false,busy:false});};},[]);
  const endpoint=`/api/model-services/routes/project/${encodeURIComponent(projectId)}/dialogue_generation`;
  useEffect(()=>{
    const controller=new AbortController(),destination=identity;
    setLoading(true);setError('');setConflict(false);setDraft(null);setProfile(null);setServices([]);setBusy(false);
    void Promise.all([request<AIService[]>('/api/settings/ai-services',{signal:controller.signal}),request<RouteProfile>(endpoint,{signal:controller.signal})]).then(([items,route])=>{
      if(!alive.current||controller.signal.aborted||current.current!==destination)return;
      if(route.scope!=='project'||route.target_id!==projectId||route.slot!=='dialogue_generation')throw new Error('返回的配音模型设置不属于当前项目');
      setServices(items.filter(item=>item.module==='dialogue_generation'&&item.enabled&&item.provider==='local_runtime'&&families.has(String(item.settings.runtime_family))));
      setProfile(route);
    }).catch(e=>{if(alive.current&&!controller.signal.aborted&&current.current===destination)setError(String(e));}).finally(()=>{if(alive.current&&!controller.signal.aborted&&current.current===destination)setLoading(false);});
    return()=>controller.abort();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  },[identity,reload]);
  const saved=useMemo(()=>{
    const choices=profile?.settings.choices||[];
    if(choices.length!==1)return '';
    const choice=choices[0];
    return services.find(service=>service.id===choice.service_id&&(!choice.model_id||service.default_model===choice.model_id))?.id||'';
  },[profile,services]);
  const selected=draft??saved,dirty=draft!==null&&draft!==saved;
  useEffect(()=>{
    if(appliedRefresh.identity!==identity){setAppliedRefresh({identity,key:refreshKey});setRequestedRefresh(false);return;}
    if(!pendingRefresh||dirty||busy||disabled)return;
    setAppliedRefresh({identity,key:refreshKey});setRequestedRefresh(false);setReload(value=>value+1);
  },[identity,refreshKey,appliedRefresh.identity,pendingRefresh,dirty,busy,disabled]);
  const state=useMemo(()=>({dirty,busy,...(pendingRefresh||loading?{refreshPending:true}:{})}),[dirty,pendingRefresh,loading,busy]);
  useEffect(()=>{callbacks.current.onStateChange?.(state);},[state]);
  async function save(){
    if(disabled||busy||loading||conflict||!profile||!dirty)return;
    const service=services.find(item=>item.id===selected);if(!service)return;
    const destination=identity;
    const body:RouteSave={expected_revision:profile.revision,settings:{mode:'local_only',choices:[{service_id:service.id,model_id:service.default_model}],parameters:{},quality_mode:'standard',allow_fallback:false,allow_paid_fallback:false,max_retries:0}};
    setBusy(true);setError('');
    try{
      const next=await request<RouteProfile>(endpoint,{method:'PUT',body:JSON.stringify(body)});
      if(!alive.current||current.current!==destination)return;
      if(next.scope!=='project'||next.target_id!==projectId||next.slot!=='dialogue_generation')throw new Error('保存结果不属于当前项目');
      setProfile(next);setDraft(null);
      await callbacks.current.onSaved?.();
    }catch(e){if(alive.current&&current.current===destination){setConflict(e instanceof ApiError&&e.status===409);setError(e instanceof ApiError&&e.status===409?'配音模型设置已在其他窗口变化，请放弃本次切换并刷新后重选。':String(e));}}
    finally{if(alive.current&&current.current===destination)setBusy(false);}
  }
  function cancel(){setDraft(null);setError('');if(conflict){setConflict(false);setRequestedRefresh(true);}}
  const complex=Boolean(profile&&(profile.settings.choices?.length!==1||!saved));
  return <div className="project-tts-service-selector" aria-label="项目配音模型切换">
    <FormField label="本项目配音模型"><select disabled={disabled||loading||busy||!profile} value={selected} onChange={event=>{setDraft(event.target.value);if(!conflict)setError('');}}>
      <option value="" disabled>{loading?'读取配音模型…':complex?'保留当前配音配置／策略':'请选择配音模型'}</option>
      {services.map(service=><option value={service.id} key={service.id}>{service.name} · {service.default_model}</option>)}
    </select></FormField>
    <p className="studio-muted">切换后使用新模型的默认配音参数，仅用于本项目后续任务；已接收任务保留原模型。镜头单独指定的配音服务继续生效。</p>
    {!loading&&!services.length?<p>尚未登记可切换的本地配音服务，请先在系统设置中配置 CosyVoice3 或 IndexTTS 2.5。</p>:null}
    <div className="studio-actions"><button type="button" className="button button-secondary button-small" disabled={disabled||loading||busy||!dirty||conflict} onClick={()=>void save()}>{busy?'正在保存配音模型…':'保存配音模型'}</button>
      {dirty?<button type="button" className="button button-ghost button-small" disabled={disabled||busy} onClick={cancel}>{conflict||pendingRefresh?'放弃本次切换并刷新':'取消切换'}</button>:null}
      {error&&!dirty?<button type="button" className="button button-secondary button-small" disabled={disabled||loading||busy} onClick={()=>setRequestedRefresh(true)}>重新读取配音模型</button>:null}</div>
    {dirty?<p className="studio-muted">配音模型选择尚未保存。</p>:null}
    {pendingRefresh?<p role="status" className="inline-notice warning">项目配置已变化。当前配音模型选择和保存操作已保留，保存或明确放弃后再刷新；刷新前不能开始制作。</p>:null}
    {error?<p role="alert" className="inline-notice warning">{error}</p>:null}
  </div>;
}
