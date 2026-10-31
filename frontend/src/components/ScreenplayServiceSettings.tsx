import {useEffect,useRef,useState} from 'react';
import {Link} from 'react-router-dom';
import {ApiError,request} from '../lib/api';
import type {AIService} from '../pages/AIServicesPage';
import type {RouteSettings} from '../lib/model-services-contract';
import type {ScreenplayMode,ScreenplayOptions,ScreenplaySlot} from '../lib/screenplay-generation-contract';
import {Badge,FormField} from './ui';
import '../screenplay-generation.css';

type Profile={revision:number;scope:string;target_id:string;slot:string;settings:RouteSettings};
const modes:{mode:ScreenplayMode;slot:ScreenplaySlot;name:string}[]=[{mode:'text',slot:'screenplay_from_text',name:'文生剧本'},{mode:'image',slot:'screenplay_from_image',name:'图片生剧本'},{mode:'video',slot:'screenplay_from_video',name:'视频生剧本'}];
function Slot({mode,slot,name,services,options,disabled,onBusy}:{mode:ScreenplayMode;slot:ScreenplaySlot;name:string;services:AIService[];options:ScreenplayOptions;disabled:boolean;onBusy:(busy:boolean)=>void}){
  const [profile,setProfile]=useState<Profile|null>(null),[primary,setPrimary]=useState(''),[fallback,setFallback]=useState(''),[allowFallback,setAllowFallback]=useState(false),[allowPaid,setAllowPaid]=useState(false),[loading,setLoading]=useState(true),[saving,setSaving]=useState(false),[error,setError]=useState(''),[notice,setNotice]=useState(''),[conflict,setConflict]=useState(false),[reload,setReload]=useState(0);
  const [primaryModel,setPrimaryModel]=useState(''),[fallbackModel,setFallbackModel]=useState('');
  const alive=useRef(true);useEffect(()=>{alive.current=true;return()=>{alive.current=false;};},[]);
  const endpoint=`/api/model-services/routes/system/default/${slot}`;
  useEffect(()=>{let current=true;setLoading(true);setError('');setConflict(false);void request<Profile>(endpoint).then(value=>{if(current){if(value.scope!=='system'||value.target_id!=='default'||value.slot!==slot)throw new Error('返回的模型设置不属于当前剧本模式');setProfile(value);setPrimary(value.settings.choices?.[0]?.service_id||'');setPrimaryModel(value.settings.choices?.[0]?.model_id||'');setFallback(value.settings.choices?.[1]?.service_id||'');setFallbackModel(value.settings.choices?.[1]?.model_id||'');setAllowFallback(Boolean(value.settings.allow_fallback));setAllowPaid(Boolean(value.settings.allow_paid_fallback));}}).catch(e=>{if(current)setError(String(e));}).finally(()=>{if(current)setLoading(false);});return()=>{current=false;};},[endpoint,reload,slot]);
  const first=services.find(item=>item.id===primary),second=services.find(item=>item.id===fallback);
  const firstModel=primaryModel||first?.default_model||'',secondModel=fallbackModel||second?.default_model||'';
  const savedFirst=profile?.settings.choices?.[0],savedSecond=profile?.settings.choices?.[1];
  const savedFirstModel=savedFirst?.model_id||services.find(item=>item.id===savedFirst?.service_id)?.default_model||'';
  const savedSecondModel=savedSecond?.model_id||services.find(item=>item.id===savedSecond?.service_id)?.default_model||'';
  const dirty=Boolean(profile)&&(primary!==(savedFirst?.service_id||'')||fallback!==(savedSecond?.service_id||'')||firstModel!==savedFirstModel||secondModel!==savedSecondModel||allowFallback!==Boolean(profile!.settings.allow_fallback)||allowPaid!==Boolean(profile!.settings.allow_paid_fallback));
  const candidate=options.modes.find(item=>item.input_mode===mode)?.candidates.find(item=>item.service_id===primary&&item.model_id===firstModel);
  const models=(service:AIService|undefined,current:string)=>[...new Set([service?.default_model,...(service?.models||[]).map(item=>item.id),current].filter((id):id is string=>Boolean(id)))];
  async function save(){
    if(disabled||!profile||saving||!primary||!dirty||conflict)return;
    if(allowFallback&&(!second||fallback===primary&&firstModel===secondModel)){setError('请选择不同的回退服务或模型版本。');return;}
    if(!first)return;
    const isCloud=first.provider!=='local_llm';
    setSaving(true);onBusy(true);setError('');setNotice('');
    try{const updated=await request<Profile>(endpoint,{method:'PUT',body:JSON.stringify({expected_revision:profile.revision,settings:{mode:allowFallback?(isCloud?'cloud_first':'local_first'):'manual',choices:[{service_id:first.id,model_id:firstModel},...(allowFallback&&second?[{service_id:second.id,model_id:secondModel}]:[])],parameters:profile.settings.parameters||{},quality_mode:profile.settings.quality_mode||'standard',allow_fallback:allowFallback,allow_paid_fallback:allowFallback&&allowPaid,max_retries:0}})});if(alive.current){setProfile(updated);setNotice('默认剧本模型已保存。仅影响后续任务。');}}
    catch(e){if(alive.current){setConflict(e instanceof ApiError&&e.status===409);setError(String(e));}}
    finally{if(alive.current)setSaving(false);onBusy(false);}
  }
  return <div className="screenplay-slot-row" role="group" aria-label={`${name}模型配置`}><strong>{name}</strong><FormField label={`${name}默认模型`}><select value={primary} disabled={disabled||loading||saving||conflict} onChange={e=>{setPrimary(e.target.value);setPrimaryModel('');setNotice('');}}><option value="">请选择已登记服务</option>{services.filter(item=>item.enabled).map(item=><option value={item.id} key={item.id}>{item.name} · {item.default_model}</option>)}</select></FormField><button type="button" className="button button-secondary button-small" disabled={disabled||loading||saving||!dirty||!primary||conflict} onClick={()=>void save()}>{saving?'保存中…':`保存${name}模型`}</button>
    {models(first,firstModel).length>1?<FormField label={`${name}模型版本`}><select value={firstModel} disabled={disabled||loading||saving||conflict} onChange={e=>setPrimaryModel(e.target.value)}>{models(first,firstModel).map(id=><option value={id} key={id}>{id}</option>)}</select></FormField>:null}
    <p className="form-hint"><Badge tone={candidate?.eligible?'success':'neutral'}>{candidate?.eligible?'本模式已实测':'本模式未验证'}</Badge> <Link to={`/screenplay-generation?mode=${mode}${primary?'&service='+encodeURIComponent(primary)+'&model='+encodeURIComponent(firstModel):''}`}>进行真实生成测试</Link></p>
    <details><summary>失败后的可选回退</summary><label><input type="checkbox" checked={allowFallback} disabled={disabled||saving||conflict} onChange={e=>{setAllowFallback(e.target.checked);if(!e.target.checked)setAllowPaid(false);}}/>允许回退到另一已登记服务</label>{allowFallback?<><FormField label={`${name}回退服务`}><select value={fallback} disabled={disabled||saving||conflict} onChange={e=>{setFallback(e.target.value);setFallbackModel('');}}><option value="">请选择</option>{services.filter(item=>item.enabled).map(item=><option value={item.id} key={item.id}>{item.name}</option>)}</select></FormField>{models(second,secondModel).length>1?<FormField label={`${name}回退模型版本`}><select value={secondModel} disabled={disabled||saving||conflict} onChange={e=>setFallbackModel(e.target.value)}>{models(second,secondModel).map(id=><option value={id} key={id}>{id}</option>)}</select></FormField>:null}<label><input type="checkbox" checked={allowPaid} disabled={disabled||saving||conflict} onChange={e=>setAllowPaid(e.target.checked)}/>允许付费云端回退（生成时还需再次授权）</label></>:null}</details>
    {error?<p className="form-hint" role="alert">{error}{conflict?' 此配置已被其他窗口修改，请保留当前选择或明确刷新后重新填写。':''}<button type="button" className="button button-ghost button-small" disabled={saving} onClick={()=>{setReload(value=>value+1);setNotice('');}}>放弃本次修改并刷新</button></p>:null}{notice?<p role="status" className="form-hint">{notice}</p>:null}
  </div>;
}
export function ScreenplayServiceSettings({services}:{services:AIService[]}){
  const [options,setOptions]=useState<ScreenplayOptions|null>(null),[error,setError]=useState(''),[busy,setBusy]=useState(false);
  const signature=services.map(item=>`${item.id}:${item.revision}`).join('|');
  useEffect(()=>{let current=true;void request<ScreenplayOptions>('/api/screenplay/options').then(value=>{if(current){setOptions(value);setError('');}}).catch(e=>{if(current)setError(String(e));});return()=>{current=false;};},[signature]);
  return <section className="screenplay-slot-settings" aria-label="剧本生成模型设置"><h3>剧本生成模型</h3><p className="form-hint">三个模式复用上方语言与剧本服务档案，不重复保存密钥。连接检测通过后仍需分别验证生成能力。</p>{error?<p role="alert">{error}</p>:null}{options?modes.map(item=><Slot key={item.mode} {...item} services={services} options={options} disabled={busy} onBusy={setBusy}/>):!error?<p role="status">读取剧本模型设置…</p>:null}</section>;
}
