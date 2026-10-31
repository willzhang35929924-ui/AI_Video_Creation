import { useEffect, useState } from 'react';
import { request, ApiError } from '../lib/api';
type Row=Record<string,any>;
const bytes=(n:number)=>n>=1048576?(n/1048576).toFixed(2)+' MiB':n+' B';
export function DeleteRecordsDialog({resource,ids,onClose,onComplete}: {resource:string;ids:string[];onClose:()=>void;onComplete:()=>void}) {
  const [mode,setMode]=useState('keep'),[cascade,setCascade]=useState(false),[plan,setPlan]=useState<Row|null>(null),[error,setError]=useState(''),[busy,setBusy]=useState(false),[permanent,setPermanent]=useState(false),[result,setResult]=useState<Row|null>(null);
  const [previewRevision,setPreviewRevision]=useState(0);
  useEffect(()=>{let current=true;setPlan(null);setError('');void request<Row>('/api/deletions/preview',{method:'POST',body:JSON.stringify({resource,ids,mode,cascade})}).then(value=>{if(current)setPlan(value);}).catch(e=>{if(current)setError(String(e));});return()=>{current=false;};},[resource,ids,mode,cascade,previewRevision]);
  async function execute(){setBusy(true);setError('');try{const r=await request<Row>('/api/deletions/execute',{method:'POST',body:JSON.stringify({resource,ids,mode,cascade,fingerprint:plan!.fingerprint,confirm:true,confirm_permanent:permanent})});setResult(r);if(r.status==='completed')onComplete();}catch(e){setError(String(e)+(e instanceof ApiError?' '+JSON.stringify(e.details||''):''));}finally{setBusy(false);}}
  return <div className="modal-backdrop" role="presentation"><section className="modal content-delete-modal" role="dialog" aria-modal="true" aria-label="删除影响范围">
    <h2>删除影响范围</h2><p>所选 {ids.length} 条记录。先核对依赖与文件，再确认执行。</p>
    <select aria-label="文件删除模式" value={mode} onChange={e=>{setMode(e.target.value);setPermanent(false);}} disabled={busy||!!result}><option value="keep">只删除记录 保留磁盘文件</option><option value="trash">删除记录 专属文件移入回收站</option><option value="permanent">永久删除记录及专属文件</option></select>
    <label><input type="checkbox" checked={cascade} disabled={busy||!!result} onChange={e=>setCascade(e.target.checked)}/>明确包含所选记录拥有的子记录</label>
    {plan?<><p>{plan.record_count} 条业务记录 · {plan.file_count} 个关联文件 · {bytes(plan.total_bytes)} · 可处理专属文件 {bytes(plan.exclusive_bytes)}</p><p>{plan.space_note}</p>
      <details><summary>业务记录与关联关系</summary>{plan.records.map((x:Row)=><p key={x.id}>{x.resource} · {x.label} · {x.id}</p>)}{plan.dependencies.map((x:Row,i:number)=><p key={i}>{x.label||x.id} · {x.reason||x.resource}</p>)}</details>
      {plan.blockers.length?<div className="inline-notice warning" role="alert"><strong>当前不能删除</strong>{plan.blockers.map((b:Row,i:number)=><p key={i}>{b.reason} · {b.label||b.id}</p>)}</div>:null}
      <details open><summary>文件清单与共享保护</summary>{plan.files.map((f:Row)=><p key={f.path}>{f.path} · {bytes(f.bytes)} · {f.protected?'保护保留：'+f.reason:!f.exists?'文件已不存在':mode==='keep'?'保留':mode==='trash'?'移入回收站':'永久删除'}</p>)}</details>
    </>:<p>正在读取实际依赖和磁盘文件…</p>}
    {mode==='permanent'?<label><input aria-label="确认永久删除" type="checkbox" checked={permanent} onChange={e=>setPermanent(e.target.checked)} disabled={busy}/>我已核对范围并确认永久删除专属文件</label>:null}
    {error?<p role="alert">{error}<button className="button button-secondary button-small" disabled={busy} onClick={()=>{setPermanent(false);setPreviewRevision(v=>v+1);}}>重新读取删除预览</button></p>:null}{result?<p role="status">任务 {result.id} · {result.status} · 已核验释放 {bytes(result.result.verified_released_bytes||0)} {result.error}</p>:null}
    <div className="toolbar"><button className="button button-secondary" disabled={busy} onClick={onClose}>关闭</button><button className="button button-danger" disabled={busy||!plan||plan.mode!==mode||plan.cascade!==cascade||plan.blockers.length>0||(mode==='permanent'&&!permanent)||!!result} onClick={()=>void execute()}>{busy?'正在执行实际删除…':'确认执行删除'}</button></div>
  </section></div>;
}
