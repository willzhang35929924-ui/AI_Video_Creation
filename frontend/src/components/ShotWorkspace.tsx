import { useCallback, useEffect, useRef, useState } from 'react';
import { request, api } from '../lib/api';
import { useToast } from './ToastProvider';
import { FormField } from './ui';
import { AudioReferencePicker } from './AudioReferencePicker';

type Row = Record<string, any>;
const send = <T,>(url: string, method: string, data?: unknown) => request<T>(url, { method, ...(data === undefined ? {} : { body: JSON.stringify(data) }) });
const performanceGroups: [string, [string, string][]][] = [
  ['表演与站位', [['action','动作'],['emotion','情绪'],['position','画面位置'],['wardrobe','当前服装']]],
  ['表演细节', [['scene_name','场景称谓'],['action_order','动作顺序'],['posture','身体姿态'],['route','移动路线'],['interaction','人物互动'],['expression','表情'],['emotion_change','情绪变化'],['eyeline','视线方向'],['orientation','人物朝向'],['depth','前后景关系']]],
  ['声音与造型', [['voice_profile','声音档案或音色'],['speed','语速'],['pitch','音高'],['vocal_state','发声状态'],['hair','当前发型'],['makeup','妆容'],['props','道具'],['continuity','连续性']]],
];

function AudioEditor({scope, id, items, reload}: {scope: string; id: string; items: Row[]; reload: () => Promise<void>}) {
  const {notify}=useToast();
  const [busy,setBusy]=useState(false);
  const run=async (work:()=>Promise<unknown>)=>{setBusy(true);try {await work();await reload();}catch(e){notify(String(e),'error');}finally{setBusy(false);}};
  return <details className="workspace-details"><summary>{scope==='scenes'?'场景持续声音':'本镜音频事件'} · {items.length}</summary>
    <p className="muted">环境声、音效、音乐独立保存。人物声音在各人物卡片中编辑。</p>
    {items.map(item=><form key={item.id} className="audio-editor-row" onSubmit={e=>{e.preventDefault();const data=new FormData(e.currentTarget);void run(()=>send('/api/audio-events/'+item.id,'PATCH',{kind:data.get('kind'),description:data.get('description'),start_seconds:Number(data.get('start')),end_seconds:data.get('end')===''?null:Number(data.get('end')),volume:Number(data.get('volume'))}));}}>
      <select aria-label="音频类型" name="kind" defaultValue={item.kind}>{[['ambience','环境声'],['sfx','音效'],['music','音乐'],['voice','声音提示']].map(([v,l])=><option key={v} value={v}>{l}</option>)}</select>
      <input aria-label="声音描述" name="description" defaultValue={item.description}/>
      <input aria-label="开始秒" name="start" type="number" min="0" step="0.1" defaultValue={item.start_seconds}/>
      <input aria-label="结束秒" name="end" type="number" min="0" step="0.1" defaultValue={item.end_seconds??''}/>
      <input aria-label="音量" name="volume" type="number" min="0" max="4" step="0.1" defaultValue={item.volume}/>
      <button className="button button-secondary button-small" disabled={busy}>保存音频</button>
      {item.relative_path?<audio controls src={api.mediaUrl(item.relative_path)}/>:null}
      <label className="button button-ghost button-small">关联音频<input aria-label="上传音频" type="file" accept="audio/*" hidden onChange={e=>{const file=e.target.files?.[0];if(file){const data=new FormData();data.append('file',file);void run(()=>request('/api/audio-events/'+item.id+'/file',{method:'POST',body:data}));}}}/></label>
    </form>)}
    <button className="button button-secondary button-small" disabled={busy} onClick={()=>void run(()=>send('/api/'+scope+'/'+id+'/audio-events','POST',{kind:'ambience',description:''}))}>添加音频事件</button>
    <a className="button button-ghost button-small" href="/content?resource=audio">编辑与删除音频记录</a>
  </details>;
}

export function ShotWorkspace({shotId,projectId,disabled=false,onSaved,onDirtyChange}: {shotId:string;projectId:string;disabled?:boolean;onSaved:()=>Promise<void>;onDirtyChange?:(dirty:boolean)=>void}) {
  const loadSequence=useRef(0);
  const dirtyPeople=useRef(new Set<string>()),dirtyLines=useRef(new Set<string>());
  const dirtyDetails=useRef({environment:false,photography:false});
  const [bundle,setBundle]=useState<Row|null>(null),[characters,setCharacters]=useState<Row[]>([]),[scenes,setScenes]=useState<Row[]>([]);
  const [selected,setSelected]=useState<string[]>([]),[search,setSearch]=useState(''),[busy,setBusy]=useState(false),[error,setError]=useState('');
  const [performance,setPerformance]=useState<Record<string,Row>>({}),[lines,setLinesState]=useState<Row[]>([]),[newLine,setNewLine]=useState({participant_id:'',text:''});
  const [environment,setEnvironmentState]=useState<Row>({}),[photography,setPhotographyState]=useState<Row>({}),[sceneAudio,setSceneAudio]=useState<Row[]>([]),[sceneId,setSceneId]=useState('');
  function setLines(update:(value:Row[])=>Row[]){setLinesState(current=>{const next=update(current);for(const line of next)if(current.find(x=>x.id===line.id)!==line)dirtyLines.current.add(line.id);return next;});}
  function setEnvironment(update:Row|((value:Row)=>Row)){dirtyDetails.current.environment=true;setEnvironmentState(update);}
  function setPhotography(update:Row|((value:Row)=>Row)){dirtyDetails.current.photography=true;setPhotographyState(update);}
  const {notify}=useToast();
  const load=useCallback(async()=>{
    const sequence=++loadSequence.current;
    const [b,c,s]=await Promise.all([request<Row>('/api/shots/'+shotId+'/workspace'),request<Row[]>('/api/characters').then(rows=>Promise.all(rows.map(x=>request<Row>('/api/characters/'+x.id)))),request<Row[]>('/api/projects/'+projectId+'/scenes')]);
    const sounds=b.scene?await request<Row[]>('/api/scenes/'+b.scene.id+'/audio-events'):[];
    if(sequence!==loadSequence.current)return;
    for(const id of dirtyPeople.current)if(!b.participants.some((p:Row)=>p.id===id))dirtyPeople.current.delete(id);
    for(const id of dirtyLines.current)if(!b.dialogues.some((l:Row)=>l.id===id))dirtyLines.current.delete(id);
    setBundle(b);setCharacters(c);setScenes(s);setLinesState(current=>b.dialogues.map((line:Row)=>dirtyLines.current.has(line.id)?current.find(l=>l.id===line.id)||line:line));setPerformance(current=>Object.fromEntries(b.participants.map((x:Row)=>[x.id,dirtyPeople.current.has(x.id)?current[x.id]||x.performance_json:x.performance_json])));
    if(!dirtyDetails.current.environment){setSceneId(b.scene?.id||'');setEnvironmentState(b.scene?.environment_json||{});}
    if(!dirtyDetails.current.photography)setPhotographyState(b.detail?.photography_json||{});
    setSceneAudio(sounds);setError('');
  },[shotId,projectId]);
  useEffect(()=>{dirtyPeople.current.clear();dirtyLines.current.clear();dirtyDetails.current={environment:false,photography:false};setBundle(null);void load().catch(e=>setError(String(e)));},[load]);
  useEffect(()=>{onDirtyChange?.(dirtyPeople.current.size>0||dirtyLines.current.size>0||dirtyDetails.current.environment||dirtyDetails.current.photography||!!newLine.text);},[performance,lines,newLine,environment,photography,onDirtyChange]);
  useEffect(()=>{const protect=(event:BeforeUnloadEvent)=>{if(dirtyPeople.current.size||dirtyLines.current.size||dirtyDetails.current.environment||dirtyDetails.current.photography){event.preventDefault();event.returnValue='';}};window.addEventListener('beforeunload',protect);return()=>window.removeEventListener('beforeunload',protect);},[]);
  async function run(work:()=>Promise<unknown>,message='已保存') {setBusy(true);try{await work();await load();await onSaved();notify(message,'success');}catch(e){notify(String(e),'error');}finally{setBusy(false);}}
  const setPerf=(id:string,key:string,value:string)=>{dirtyPeople.current.add(id);setPerformance(p=>({...p,[id]:{...p[id],[key]:value}}));};
  if(error)return <div role="alert" className="inline-notice warning">{error}<button onClick={()=>void load()}>重试</button></div>;
  if(!bundle)return <p>正在加载人物与场景…</p>;
  const locked=busy||disabled;
  return <section className="shot-workspace" aria-label="多人物与场景工作区">
    <div className="section-heading"><div><h3>人物参与</h3><p>每个人物独立保存动作、造型与声音，身份版本明确绑定。</p></div><span>{bundle.participants.length} 人</span></div>
    {disabled?<p className="inline-notice warning">请先保存上方分镜修改，再编辑人物和场景。</p>:null}
    <details className="workspace-details"><summary>添加人物或新建人物档案</summary>
      <input aria-label="搜索人物素材库" placeholder="搜索名称、身份ID" value={search} onChange={e=>setSearch(e.target.value)}/>
      <div className="people-picker">{characters.filter(c=>(c.display_name+' '+c.id).includes(search)).map(c=>{
        const added=bundle.participants.some((p:Row)=>p.character_id===c.id);const versions=c.versions||[];const assets=versions.flatMap((v:Row)=>v.assets||[]);const face=assets.find((a:Row)=>a.status==='approved');
        return <label key={c.id}><input type="checkbox" disabled={added||locked} checked={added||selected.includes(c.id)} onChange={e=>setSelected(x=>e.target.checked?[...x,c.id]:x.filter(k=>k!==c.id))}/>{face?<img src={api.mediaUrl(face.relative_path)} alt=""/>:<span className="person-placeholder">人</span>}<span><strong>{c.display_name}</strong><small>{c.id} · {c.current_approved_version_id?'有正式身份':'待审核素材'}</small></span></label>;
      })}</div>
      <button className="button button-primary button-small" disabled={locked||!selected.length} onClick={()=>void run(async()=>{await send('/api/shots/'+shotId+'/participants','POST',{character_ids:selected});setSelected([]);},'人物已加入当前分镜')}>添加所选人物</button>
      <form className="new-person-form" onSubmit={e=>{e.preventDefault();const form=new FormData(e.currentTarget);void run(async()=>{const id='char_'+crypto.randomUUID().replaceAll('-','');await send('/api/character-factory/characters','POST',{id,name:form.get('name'),profile:{age:form.get('age')===''?null:Number(form.get('age')),description:form.get('description')}});await send('/api/shots/'+shotId+'/participants','POST',{character_ids:[id]});},'待审核人物档案已建立并加入分镜');}}>
        <h4>从剧本新建人物</h4><input aria-label="新人物名称" name="name" placeholder="人物名称" required/><input aria-label="新人物年龄" name="age" type="number" min="0" step="1" placeholder="年龄（可选）"/><input aria-label="新人物身份" name="description" placeholder="人物身份与外观（可选）"/><button className="button button-secondary button-small" disabled={locked}>新建并加入</button>
      </form>
    </details>
    {bundle.participants.map((p:Row,index:number)=>{
      const c=characters.find(x=>x.id===p.character_id);const versions=c?.versions||[];
      return <details key={p.id} className="participant-card" open={bundle.participants.length===1} data-testid={'participant-'+p.character_id}>
        <summary><strong>{p.name}</strong><span>{p.character_id} · {p.character_version_id?('v'+(versions.find((v:Row)=>v.id===p.character_version_id)?.version??'?')):'未选版本'}</span><small>{p.warnings.join(' · ')||'已审核参考可用'}</small></summary>
        <div className="participant-toolbar"><FormField label="身份版本"><select aria-label={p.name+'身份版本'} value={p.character_version_id||''} disabled={locked} onChange={e=>void run(()=>send('/api/participants/'+p.id,'PATCH',{character_version_id:e.target.value||null,reference_asset_ids:[]}))}><option value="">待确认</option>{versions.map((v:Row)=><option key={v.id} value={v.id}>v{v.version} · {v.approval_state} · {v.id}</option>)}</select></FormField>
          <button className="button button-ghost button-small" disabled={locked||index===0} onClick={()=>void run(async()=>{await send('/api/participants/'+p.id,'PATCH',{order:index});await send('/api/participants/'+bundle.participants[index-1].id,'PATCH',{order:index+1});})}>上移</button>
          <button className="button button-ghost-danger button-small" disabled={locked} onClick={()=>{const count=lines.filter(x=>x.participant_id===p.id).length;if(window.confirm(`仅移除此镜中的 ${p.name}${count?` 及其 ${count} 条对白`:''}，人物素材库保留。`))void run(()=>send('/api/participants/'+p.id+'?remove_dialogues=true','DELETE'),'已移除分镜人物关系');}}>移除参与</button>
        </div>
        {performanceGroups.map(([title,fields],n)=>{
          const content=<div className="workspace-grid">{fields.map(([key,name])=><FormField label={name} key={key}><input aria-label={p.name+name} value={performance[p.id]?.[key]||''} onChange={e=>setPerf(p.id,key,e.target.value)} disabled={locked}/></FormField>)}</div>;
          return n===0?<div key={title}>{content}</div>:<details key={title} className="workspace-details"><summary>{title}</summary>{content}</details>;
        })}
        <AudioReferencePicker key={`${p.id}:${p.character_version_id}`} projectId={projectId} versionId={p.character_version_id||''} name={p.name} value={performance[p.id]?.reference_audio_asset_id||''} onChange={id=>setPerf(p.id,'reference_audio_asset_id',id)} disabled={locked}/>
        <details className="workspace-details"><summary>已审核参考素材 · {p.assets.length}</summary><div className="reference-picker">{p.assets.map((a:Row)=><label key={a.id}><img src={api.mediaUrl(a.relative_path)} alt={a.asset_type}/><input type="checkbox" aria-label={p.name+'参考素材'+a.id} checked={p.reference_asset_ids_json.includes(a.id)} disabled={locked} onChange={e=>void run(()=>send('/api/participants/'+p.id,'PATCH',{reference_asset_ids:e.target.checked?[...p.reference_asset_ids_json,a.id]:p.reference_asset_ids_json.filter((id:string)=>id!==a.id)}))}/>{a.asset_type}</label>)}</div>{!p.assets.length?<a href={'/characters/'+p.character_id}>素材待补齐，前往人物工厂生成与审核</a>:null}</details>
        <button className="button button-primary button-small" disabled={locked} onClick={()=>void run(async()=>{await send('/api/participants/'+p.id,'PATCH',{performance:performance[p.id],revision:bundle.shot.revision});dirtyPeople.current.delete(p.id);},'此人物表演已独立保存')}>保存 {p.name} 表演</button>
      </details>;
    })}
    <details className="workspace-details" open><summary>对白与说话顺序 · {lines.length} 条</summary>
      {lines.map((line,index)=><div className="dialogue-editor" key={line.id} data-testid="dialogue-line">
        <input aria-label="对白顺序" type="number" min="1" value={line.order} onChange={e=>setLines(x=>x.map((l,i)=>i===index?{...l,order:Number(e.target.value)}:l))}/>
        <select aria-label="对白说话人" value={line.participant_id} onChange={e=>setLines(x=>x.map((l,i)=>i===index?{...l,participant_id:e.target.value}:l))}>{bundle.participants.map((p:Row)=><option key={p.id} value={p.id}>{p.name}</option>)}</select>
        <textarea aria-label="对白内容" value={line.text} onChange={e=>setLines(x=>x.map((l,i)=>i===index?{...l,text:e.target.value}:l))}/>
        <details><summary>语气 情绪 时间</summary>{[['tone','语气'],['emotion','对白情绪'],['pause_seconds','停顿秒'],['start_seconds','开始秒'],['end_seconds','结束秒'],['dubbing','配音要求']].map(([key,title])=><FormField key={key} label={title}><input value={line.delivery_json?.[key]??''} onChange={e=>setLines(x=>x.map((l,i)=>i===index?{...l,delivery_json:{...l.delivery_json,[key]:e.target.value}}:l))}/></FormField>)}</details>
        <button className="button button-secondary button-small" disabled={locked} onClick={()=>void run(async()=>{await send('/api/dialogues/'+line.id,'PATCH',{participant_id:line.participant_id,text:line.text,order:line.order,delivery:line.delivery_json});dirtyLines.current.delete(line.id);})}>保存对白</button>
        <button className="button button-ghost-danger button-small" disabled={locked} onClick={()=>{if(window.confirm('移除此条对白？'))void run(async()=>{await send('/api/dialogues/'+line.id,'DELETE');dirtyLines.current.delete(line.id);});}}>移除对白</button>
      </div>)}
      <form className="dialogue-editor" onSubmit={e=>{e.preventDefault();void run(async()=>{await send('/api/shots/'+shotId+'/dialogues','POST',{...newLine,order:lines.length+1});setNewLine({participant_id:'',text:''});});}}><select aria-label="新对白说话人" value={newLine.participant_id} required onChange={e=>setNewLine(x=>({...x,participant_id:e.target.value}))}><option value="">选择说话人</option>{bundle.participants.map((p:Row)=><option key={p.id} value={p.id}>{p.name}</option>)}</select><textarea aria-label="新对白内容" value={newLine.text} required onChange={e=>setNewLine(x=>({...x,text:e.target.value}))}/><button className="button button-secondary button-small" disabled={locked||!newLine.participant_id}>添加对白</button></form>
    </details>
    <details className="workspace-details"><summary>场景环境与摄影</summary>
      <FormField label="所属场景"><select value={sceneId} onChange={e=>{setSceneId(e.target.value);setEnvironment(scenes.find(s=>s.id===e.target.value)?.environment_json||{});}}><option value="">未绑定场景</option>{scenes.map(s=><option key={s.id} value={s.id}>{s.order} · {s.name}</option>)}</select></FormField>
      <button className="button button-secondary button-small" disabled={locked} onClick={()=>{const name=window.prompt('新场景名称');if(name)void run(async()=>{const s=await send<Row>('/api/projects/'+projectId+'/scenes','POST',{name,order:scenes.length+1,script_revision_id:bundle.scene?.script_revision_id});await send('/api/shots/'+shotId+'/details','PUT',{scene_id:s.id,photography});});}}>新增场景并绑定</button>
      <div className="workspace-grid">{[['location','地点'],['time','时间'],['weather','天气'],['interior_exterior','内外景'],['layout','空间布局'],['lighting','真实光源'],['palette','主色调'],['atmosphere','氛围'],['props','关键道具'],['continuity','场景连续性']].map(([key,title])=><FormField label={title} key={key}><input value={environment[key]||''} onChange={e=>setEnvironment(v=>({...v,[key]:e.target.value}))}/></FormField>)}</div>
      <button className="button button-secondary button-small" disabled={locked||!sceneId} onClick={()=>void run(async()=>{const scene=scenes.find(x=>x.id===sceneId)!;await send('/api/scenes/'+sceneId,'PATCH',{name:scene.name,order:scene.order,environment});await send('/api/shots/'+shotId+'/details','PUT',{scene_id:sceneId,photography});dirtyDetails.current={environment:false,photography:false};},'场景与本镜绑定已保存')}>保存场景环境</button>
      <div className="workspace-grid">{[['shot_size','景别'],['camera_position','机位'],['angle','角度'],['composition','构图'],['lens','焦段意图'],['start_state','开始状态'],['end_state','结束状态'],['continuity','相邻镜头衔接'],['transition','转场意图']].map(([key,title])=><FormField label={title} key={key}><input value={photography[key]||''} onChange={e=>setPhotography(v=>({...v,[key]:e.target.value}))}/></FormField>)}</div>
      <button className="button button-secondary button-small" disabled={locked} onClick={()=>void run(async()=>{await send('/api/shots/'+shotId+'/details','PUT',{scene_id:sceneId||null,photography});dirtyDetails.current.photography=false;},'本镜摄影信息已保存')}>保存本镜摄影</button>
      {bundle.scene?<AudioEditor scope="scenes" id={bundle.scene.id} items={sceneAudio} reload={async()=>{await load();await onSaved();}}/>:null}
    </details>
    <AudioEditor scope="shots" id={shotId} items={bundle.audio_events} reload={async()=>{await load();await onSaved();}}/>
  </section>;
}
