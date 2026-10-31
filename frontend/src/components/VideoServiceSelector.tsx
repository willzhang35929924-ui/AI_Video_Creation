import {useEffect,useState} from 'react';
import {request} from '../lib/api';
import {FormField} from './ui';
import type {AIService} from '../pages/AIServicesPage';

export function VideoServiceSelector({projectId,onChange,onDirtyChange}:{projectId:string;onChange:(id:string,model:string,service:AIService|undefined)=>void;onDirtyChange?:(dirty:boolean)=>void}){
  const [items,setItems]=useState<AIService[]>([]),[id,setId]=useState(''),[model,setModel]=useState(''),[notice,setNotice]=useState(''),[busy,setBusy]=useState(false),[error,setError]=useState('');
  useEffect(()=>{let alive=true;if(!projectId)return;setBusy(true);setError('');onDirtyChange?.(false);
    void Promise.all([request<AIService[]>('/api/settings/ai-services?module=video_generation'),request<any[]>(`/api/projects/${projectId}/ai-services`)]).then(([services,bindings])=>{
      if(!alive)return;setItems(services.filter(service=>service.enabled));const binding=bindings.find(item=>item.module==='video_generation'),service=services.find(item=>item.id===binding?.service_id)||services.find(item=>item.is_default&&item.enabled),nextModel=binding?.model_id||service?.default_model||'';
      setId(service?.id||'');setModel(nextModel);onChange(service?.id||'',nextModel,service);setNotice(binding?'已读取项目覆盖':'使用视频模块默认');
    }).catch(cause=>{if(alive)setError(String(cause));}).finally(()=>{if(alive)setBusy(false);});return()=>{alive=false;};
  },[projectId]);
  async function save(clear=false){setBusy(true);setError('');try{
    await request(`/api/projects/${projectId}/ai-services/video_generation`,clear?{method:'DELETE'}:{method:'PUT',body:JSON.stringify({service_id:id,model_id:model})});
    if(clear){const service=items.find(item=>item.is_default);setId(service?.id||'');setModel(service?.default_model||'');onChange(service?.id||'',service?.default_model||'',service);}
    onDirtyChange?.(false);setNotice(clear?'已恢复模块默认':'已保存项目覆盖；全局默认未更改');
  }catch(cause){setError(String(cause));}finally{setBusy(false);}}
  return <section className="card assembly-output"><div className="section-heading"><h2>视频服务与模型</h2><button type="button" className="button button-ghost" disabled={busy} onClick={()=>void save(true)}>恢复模块默认</button><button type="button" className="button button-secondary" disabled={busy||!id||!model} onClick={()=>void save()}>保存为项目覆盖</button></div>
    {error?<p role="alert">{error}</p>:null}<div className="editor-form-grid"><FormField label="项目视频服务"><select disabled={busy} value={id} onChange={event=>{const service=items.find(item=>item.id===event.target.value);setId(event.target.value);setModel(service?.default_model||'');onChange(event.target.value,service?.default_model||'',service);onDirtyChange?.(true);}}><option value="">尚无已启用服务</option>{items.map(service=><option key={service.id} value={service.id}>{service.name} · {service.provider==='comfyui'?'本地工作流':'云端视频 API'}</option>)}</select></FormField><FormField label="项目视频模型"><input disabled={busy} list="video-model-overrides" value={model} onChange={event=>{setModel(event.target.value);onChange(id,event.target.value,items.find(item=>item.id===id));onDirtyChange?.(true);}}/><datalist id="video-model-overrides">{items.find(item=>item.id===id)?.models.map(item=><option key={item.id} value={item.id}/>)}</datalist></FormField></div><p className="form-hint">{notice}。修改选择后请保存项目覆盖才会用于统一编排。每个任务记录实际服务、模型与工作流快照；目录中出现不等于模型运行已验证。</p>
  </section>;
}
