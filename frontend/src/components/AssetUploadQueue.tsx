import {useEffect,useRef,useState} from 'react';
import {Upload} from 'lucide-react';
import {API_BASE_URL,ApiError} from '../lib/api';

export type UploadQueueItem={id:string;file:File;status:'waiting'|'uploading'|'succeeded'|'failed';progress:number;error?:string;targetId:string};
export type UploadResponse={id?:string;duplicate?:boolean;[key:string]:unknown};

/** Progress measures bytes sent; completion waits for the server's persisted response. */
export function uploadForm<T>(path:string,form:FormData,onProgress:(percent:number)=>void,signal?:AbortSignal):Promise<T>{
  return new Promise((resolve,reject)=>{
    const xhr=new XMLHttpRequest();
    xhr.open('POST',`${API_BASE_URL}${path}`);xhr.withCredentials=true;xhr.setRequestHeader('Accept','application/json');
    const abort=()=>xhr.abort();signal?.addEventListener('abort',abort,{once:true});
    const finish=()=>signal?.removeEventListener('abort',abort);
    xhr.upload.onprogress=event=>{if(event.lengthComputable)onProgress(Math.round(event.loaded/event.total*100));};
    xhr.onload=()=>{finish();let body:unknown;try{body=JSON.parse(xhr.responseText);}catch{body=null;}
      if(xhr.status>=200&&xhr.status<300)resolve(body as T);
      else{const value=body as {detail?:unknown;message?:string;error?:{message?:string;code?:string}}|null;reject(new ApiError(value?.error?.message||(typeof value?.detail==='string'?value.detail:value?.message)||`上传失败（HTTP ${xhr.status}）`,xhr.status,value?.error?.code,body));}};
    xhr.onerror=()=>{finish();reject(new ApiError('上传连接中断。请刷新图库确认是否已保存，再重试；服务器会按文件内容检测重复。',0));};
    xhr.onabort=()=>{finish();reject(new DOMException('上传已取消','AbortError'));};
    if(signal?.aborted){finish();reject(new DOMException('上传已取消','AbortError'));return;}xhr.send(form);
  });
}

export function AssetUploadQueue({accept,label,targets,targetId,disabled=false,upload,onCompleted,onBusyChange}:{accept:string;label:string;targets:{id:string;name:string}[];targetId?:string;disabled?:boolean;upload:(file:File,targetId:string,onProgress:(percent:number)=>void,signal:AbortSignal)=>Promise<unknown>;onCompleted?:()=>void|Promise<void>;onBusyChange?:(busy:boolean)=>void}){
  const [items,setItems]=useState<UploadQueueItem[]>([]),[dragging,setDragging]=useState(false),[running,setRunning]=useState(false),[notice,setNotice]=useState('');
  const identity=useRef(0),alive=useRef(true),controller=useRef<AbortController|null>(null),runningRef=useRef(false),itemsRef=useRef(items);itemsRef.current=items;
  const uploadRef=useRef(upload),completeRef=useRef(onCompleted);uploadRef.current=upload;completeRef.current=onCompleted;
  useEffect(()=>{alive.current=true;return()=>{alive.current=false;controller.current?.abort();};},[]);
  useEffect(()=>{onBusyChange?.(running);},[running,onBusyChange]);
  function append(files:FileList|File[]){const additions=Array.from(files).map(file=>({id:`upload-${++identity.current}`,file,status:'waiting' as const,progress:0,targetId:targetId||''}));setItems(previous=>[...previous,...additions]);setNotice('请核对每个文件的归属，再开始上传。');}
  function update(id:string,patch:Partial<UploadQueueItem>){if(alive.current)setItems(previous=>previous.map(item=>item.id===id?{...item,...patch}:item));}
  async function run(failedOnly=false){
    if(runningRef.current||disabled)return;const selected=itemsRef.current.filter(item=>(failedOnly?item.status==='failed':item.status==='waiting'||item.status==='failed'));
    if(selected.some(item=>!item.targetId)){setNotice('请先为队列中的每个文件选择归属。');return;}
    if(selected.some(item=>!targets.some(target=>target.id===item.targetId))){setNotice('部分文件的归属已不在当前图库，请为这些文件重新选择归属后上传。');return;}
    runningRef.current=true;setRunning(true);setNotice('');controller.current=new AbortController();let succeeded=0;
    try{for(const item of selected){if(controller.current.signal.aborted)break;update(item.id,{status:'uploading',progress:0,error:undefined});try{await uploadRef.current(item.file,item.targetId,percent=>update(item.id,{progress:percent}),controller.current.signal);update(item.id,{status:'succeeded',progress:100});succeeded++;}catch(error){if(alive.current)update(item.id,{status:'failed',error:String(error)});}}
      if(succeeded&&alive.current)try{await completeRef.current?.();}catch(error){if(alive.current)setNotice(`文件已保存，但图库刷新失败：${String(error)}。请刷新图库核对。`);}
    }finally{runningRef.current=false;if(alive.current){setRunning(false);setNotice(`${succeeded} 个文件已由服务器确认保存；失败文件可单独重试。`);}}
  }
  const pending=items.some(item=>['waiting','failed'].includes(item.status));
  return <section className="asset-upload" aria-label={`${label}上传队列`}>
    <label className={`asset-dropzone${dragging?' is-dragging':''}`} onDragOver={event=>{event.preventDefault();if(!disabled&&!running)setDragging(true);}} onDragLeave={()=>setDragging(false)} onDrop={event=>{event.preventDefault();setDragging(false);if(!disabled&&!running)append(event.dataTransfer.files);}}>
      <Upload size={24} aria-hidden="true"/><strong>{label}</strong><span>拖放多个文件到这里，或选择文件</span><input type="file" multiple accept={accept} aria-label={label} disabled={disabled||running} onChange={event=>{if(event.target.files)append(event.target.files);event.target.value='';}}/>
    </label>
    {items.length?<><div className="asset-upload-list">{items.map(item=><article key={item.id} className="asset-upload-row"><div><strong>{item.file.name}</strong><small>{(item.file.size/1024/1024).toFixed(2)} MB</small></div><label><span>归属</span><select aria-label={`${item.file.name}归属`} value={item.targetId} disabled={disabled||running||item.status==='succeeded'} onChange={event=>update(item.id,{targetId:event.target.value})}><option value="">请选择归属</option>{targets.map(target=><option key={target.id} value={target.id}>{target.name}</option>)}</select></label><div><span>{item.status==='waiting'?'等待上传':item.status==='uploading'?(item.progress===100?'服务器正在校验保存…':`正在上传 ${item.progress}%`):item.status==='succeeded'?'已保存':'上传失败'}</span>{item.status==='uploading'?<progress max={100} value={item.progress} aria-label={`${item.file.name}上传进度`}/>:null}{item.error?<p role="alert">{item.error}</p>:null}</div>{item.status!=='uploading'?<button className="button button-ghost button-small" type="button" disabled={running} onClick={()=>setItems(previous=>previous.filter(value=>value.id!==item.id))}>移出队列</button>:null}</article>)}</div><div className="asset-actions"><button type="button" className="button button-primary" disabled={disabled||running||!pending} onClick={()=>void run()}>{running?'正在逐项上传…':'开始上传队列'}</button>{items.some(item=>item.status==='failed')?<button type="button" className="button button-secondary" disabled={disabled||running} onClick={()=>void run(true)}>重试失败文件</button>:null}<button type="button" className="button button-ghost" disabled={running} onClick={()=>setItems(previous=>previous.filter(item=>item.status!=='succeeded'))}>清除已完成队列</button></div></>:null}
    {notice?<p className="form-hint" role="status">{notice}</p>:null}
  </section>;
}
