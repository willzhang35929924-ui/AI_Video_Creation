import {useEffect,useMemo,useRef,useState,type FormEvent} from 'react';
import {Link,useSearchParams} from 'react-router-dom';
import {ArrowRight,Download,RefreshCw,Upload,FileText} from 'lucide-react';
import {api,request} from '../lib/api';
import {Badge,FormField,LoadingState,PageHeader} from '../components/ui';
import type {ScreenplayInputAsset,ScreenplayIRPreview,ScreenplayJob,ScreenplayMode,ScreenplayOptions,ScreenplayReferenceTarget,ScreenplayScope} from '../lib/screenplay-generation-contract';
import {ProductLibraryPicker,ReferenceVideoPicker,type ProductSelection} from '../components/ScreenplayLibraryInputs';
import '../screenplay-generation.css';

const labels:Record<string,string>={queued:'等待执行',running:'正在处理',preprocessing:'正在读取素材',generating:'模型正在生成',validating:'正在校验剧本结构',exporting:'正在生成 Word',awaiting_review:'等待内容审核',approved:'审核已通过',failed:'执行失败',unknown:'请求未返回 · 等待已结束',blocked:'条件未满足',export_failed:'Word 导出失败',cancelled:'已中止'};
const modeLabels:Record<ScreenplayMode,string>={text:'文生剧本',image:'图片生剧本',video:'视频生剧本'};
const fieldLabels:Record<string,string>={description:'描述',age:'年龄',identity:'身份',facial_features:'面容',hair:'发型',eyes:'眼睛',skin:'肤质',identifying_marks:'识别特征',body:'体态',style:'视觉风格',fixed_traits:'固定特征',variable_attributes:'可变特征',wardrobes:'服装',voice:'声音',voice_profile:'声音',location:'地点',time:'时间',weather:'天气',lighting:'灯光',layout:'空间布局',continuity:'连续性',action:'动作',action_order:'动作顺序',position:'站位',orientation:'朝向',expression:'表情',emotion:'情绪',eyeline:'视线',wardrobe:'服装',props:'道具',shot_size:'景别',camera_position:'机位',composition:'构图',lens:'焦段',start_state:'开始状态',end_state:'结束状态',transition:'转场',kind:'类型',text:'内容',name:'名称',volume:'音量',start_seconds:'开始秒',end_seconds:'结束秒',synopsis:'梗概',genre:'题材',target_duration_seconds:'目标时长（秒）',target_duration:'目标时长',shot_total_duration_seconds:'分镜合计时长（秒）',duration_conflict:'时长需核对',relationships:'人物关系',notes:'备注',title:'标题',beats:'动作节拍',wardrobe_states:'服装状态',prop_states:'道具状态',scene_state_key:'场景状态',state_change_reason:'状态变化原因',require_visual_review:'需要画面审核'};
Object.assign(fieldLabels,{hook:'开场吸引点',target_audience:'目标观众',display_sequence:'产品展示顺序',call_to_action:'行动号召',evidence_sources:'卖点证据来源',product_id:'产品 ID',visible_state:'产品可见状态',display_features:'展示特征',on_screen_text:'屏幕文字',logo_review_required:'Logo或包装文字需人工复核',story_body:'剧情正文',aspect_ratio:'画幅',immutable_traits:'固定身份特征',angle:'角度',materials:'主要材质',speaker:'说话人',state:'状态',character_key:'所属人物标识',scene_key:'所属场景标识',key:'引用编号',spec:'制作设定',wardrobe_key:'服装资料编号',prop_key:'道具资料编号',production_rules_version:'制作规则版本',screenplay_schema_version:'剧本结构版本',template_version:'模板版本',parser_version:'解析器版本',video_prompt_compiler_version:'视频提示词版本'});
function valueText(value:unknown):string{
  if(value===null||value===undefined)return '';
  if(typeof value==='boolean')return value?'是':'否';
  if(Array.isArray(value))return value.map(valueText).filter(Boolean).join('；');
  if(typeof value==='object')return Object.entries(value as Record<string,unknown>).filter(([key])=>!['source','source_ids','provenance'].includes(key)).map(([key,item])=>`${fieldLabels[key]||key}：${valueText(item)}`).join('；');
  return String(value);
}
function Fields({value}:{value?:Record<string,unknown>}){return <div className="screenplay-fields">{Object.entries(value||{}).filter(([key,item])=>item!==null&&item!==undefined&&valueText(item)&&!['character_id','source','provenance','requested_controls','visual_ids'].includes(key)).map(([key,item])=><p key={key}><strong>{fieldLabels[key]||key}：</strong>{valueText(item)}</p>)}</div>;}
function Preview({ir}:{ir:ScreenplayIRPreview}){
  const person=(key:string)=>ir.characters.find(item=>item.key===key)?.name||key;
  return <div className="screenplay-outline" aria-label="结构化剧本预览">
    <details open><summary>项目概述</summary><Fields value={ir.project}/></details>{ir.marketing?<details open><summary>产品营销方案与卖点依据</summary><Fields value={ir.marketing}/></details>:null}
    {ir.source_transcript?.text?<details open><summary>视频音轨转写</summary><p>{ir.source_transcript.text}</p><p className="form-hint">这是实际音轨转写，文本准确性与说话人归属仍需核对。</p>{ir.source_transcript.segments?.map((segment,index)=><p key={index}><small>{segment.start??'—'}–{segment.end??'—'} 秒 · {segment.speaker==='unknown'||!segment.speaker?'说话人待确认':segment.speaker}</small> {segment.text}</p>)}</details>:null}
    <details open><summary>人物设定 · {ir.characters.length} 人</summary>{ir.characters.map(item=><article key={item.key}><h4>{item.name}</h4><Fields value={item.profile}/><small>人物标识：{item.key}</small></article>)}</details>
    <details><summary>场景与环境 · {ir.scenes.length} 场</summary>{ir.scenes.map(item=><article key={item.key}><h4>{item.order||''} {item.name}</h4><Fields value={item.environment}/>{item.audio_events?.length?<p><strong>场景声音：</strong>{valueText(item.audio_events)}</p>:null}<small>场景标识：{item.key}</small></article>)}</details>
    {ir.shots.map((shot,index)=><details open key={shot.key}><summary>分镜 {index+1} · {shot.title} · {shot.duration_seconds} 秒</summary><p>场景：{ir.scenes.find(scene=>scene.key===shot.scene_key)?.name||shot.scene_key}</p><p>{shot.visual_prompt}</p>{shot.action?<p><strong>镜头动作：</strong>{shot.action}</p>:null}{shot.camera_move?<p><strong>运镜：</strong>{shot.camera_move}</p>:null}<Fields value={shot.photography}/>{shot.participants.map(participant=><div className="screenplay-person-performance" key={participant.character_key}><strong>{person(participant.character_key)}</strong><Fields value={participant.performance}/></div>)}{shot.dialogues.length?<div><h4>独立对白</h4><ol>{shot.dialogues.map((line,n)=><li key={n}><strong>{person(line.character_key)}：</strong>{line.text}{line.delivery&&Object.keys(line.delivery).length?<small> · {valueText(line.delivery)}</small>:null}</li>)}</ol></div>:<p>本镜无对白。</p>}{shot.products?.length?<div><h4>产品展示与外观连续性</h4>{shot.products.map((product,index)=><Fields key={index} value={product}/>)}</div>:null}{shot.audio_events?.length?<p><strong>环境与声音：</strong>{valueText(shot.audio_events)}</p>:null}<Fields value={shot.production}/><small>分镜标识：{shot.key}</small></details>)}
    {ir.designs?.length?<details><summary>服装 道具与连续性资料</summary>{ir.designs.map((item,index)=><Fields key={index} value={item}/>)}</details>:null}
  </div>;
}
function newKey(){return globalThis.crypto?.randomUUID?.()||`screenplay-${Date.now()}-${Math.random().toString(36).slice(2)}`;}
const active=(job:ScreenplayJob|null)=>Boolean(job&&['queued','running'].includes(job.status));

export function ScreenplayGenerationPage(){
  const [search,setSearch]=useSearchParams();
  const selectedId=search.get('job')||'';
  const initialMode=search.get('mode');
  const [mode,setMode]=useState<ScreenplayMode>(initialMode==='image'||initialMode==='video'?initialMode:'text');
  const [options,setOptions]=useState<ScreenplayOptions|null>(null),[history,setHistory]=useState<ScreenplayJob[]>([]),[job,setJob]=useState<ScreenplayJob|null>(null);
  const [projectName,setProjectName]=useState(''),[creativePrompt,setCreativePrompt]=useState(''),[story,setStory]=useState(''),[serviceId,setServiceId]=useState(search.get('service')||'');
  const [modelId,setModelId]=useState(search.get('model')||'');
  const [files,setFiles]=useState<Partial<Record<ScreenplayMode,File>>>({}),[assets,setAssets]=useState<Partial<Record<ScreenplayMode,ScreenplayInputAsset>>>({}),[sourceNote,setSourceNote]=useState('');
  const [imageSource,setImageSource]=useState<'upload'|'products'>(search.get('product')?'products':'upload'),[videoSource,setVideoSource]=useState<'upload'|'library'>(search.get('video')||search.get('style')?'library':'upload');
  const [productSelections,setProductSelections]=useState<ProductSelection[]>([]),[productMode,setProductMode]=useState<'separate'|'combined'>('separate'),[marketingTemplate,setMarketingTemplate]=useState('showcase');
  const [videoIds,setVideoIds]=useState<string[]>([]),[primaryVideo,setPrimaryVideo]=useState(''),[styleId,setStyleId]=useState(''),[videoCreationMode,setVideoCreationMode]=useState<'adaptation'|'style'>('adaptation'),[newTheme,setNewTheme]=useState('');
  const [pendingImages,setPendingImages]=useState<File[]>([]),[imageAssets,setImageAssets]=useState<ScreenplayInputAsset[]>([]),[uploadRows,setUploadRows]=useState<{name:string;status:string;error?:string}[]>([]);
  const [cloudCosts,setCloudCosts]=useState(false),[paidFallback,setPaidFallback]=useState(false),[cloudMedia,setCloudMedia]=useState(false);
  const [loading,setLoading]=useState(true),[busy,setBusy]=useState(''),[error,setError]=useState(''),[notice,setNotice]=useState(''),[jobLoading,setJobLoading]=useState(false);
  const [selectedIds,setSelectedIds]=useState<Set<string>>(new Set());
  const [documentId,setDocumentId]=useState(''),[reviewed,setReviewed]=useState(false),[createProject,setCreateProject]=useState(false);
  const [referenceUse,setReferenceUse]=useState<'none'|'character'|'scene'>('none'),[referenceTarget,setReferenceTarget]=useState('');
  const [revisionScope,setRevisionScope]=useState<ScreenplayScope>('all'),[revisionTarget,setRevisionTarget]=useState(''),[revisionInstruction,setRevisionInstruction]=useState('');
  const [retryCosts,setRetryCosts]=useState(false),[retryPaid,setRetryPaid]=useState(false),[confirmUnknownRetry,setConfirmUnknownRetry]=useState(false);
  const [resumeCosts,setResumeCosts]=useState(false),[resumePaid,setResumePaid]=useState(false);
  const retrySubmission=useRef<{fingerprint:string;key:string}|null>(null);
  const resumeSubmission=useRef<{fingerprint:string;key:string}|null>(null);
  const alive=useRef(true),identity=useRef(selectedId),submission=useRef<{fingerprint:string;key:string}|null>(null),revisionSubmission=useRef<{fingerprint:string;key:string}|null>(null),busyRef=useRef(false);
  identity.current=selectedId;
  useEffect(()=>{alive.current=true;return()=>{alive.current=false;};},[]);
  async function refresh(){const [nextOptions,nextHistory]=await Promise.all([request<ScreenplayOptions>('/api/screenplay/options'),request<ScreenplayJob[]>('/api/screenplay/jobs')]);if(alive.current){setOptions(nextOptions);setHistory(nextHistory);}}
  useEffect(()=>{let current=true;void refresh().catch(e=>{if(current)setError(String(e));}).finally(()=>{if(current)setLoading(false);});return()=>{current=false;};},[]);
  useEffect(()=>{
    const controller=new AbortController();let timer:ReturnType<typeof setTimeout>|undefined;
    setJob(null);setDocumentId('');setReviewed(false);setCreateProject(false);setReferenceUse('none');setReferenceTarget('');setRevisionInstruction('');setRevisionTarget('');setRevisionScope('all');setRetryCosts(false);setRetryPaid(false);setConfirmUnknownRetry(false);
    if(!selectedId){setJobLoading(false);return()=>controller.abort();}
    const destination=selectedId;setJobLoading(true);
    async function fetchJob(){try{
      const next=await request<ScreenplayJob>(`/api/screenplay/jobs/${encodeURIComponent(destination)}`,{signal:controller.signal});
      if(!alive.current||controller.signal.aborted||identity.current!==destination)return;
      setJob(next);setDocumentId(current=>current||next.latest_document_id||next.documents.at(-1)?.id||'');setJobLoading(false);
      setHistory(items=>[next,...items.filter(item=>item.id!==next.id)]);
      if(active(next))timer=setTimeout(()=>void fetchJob(),4000);
      else void request<ScreenplayOptions>('/api/screenplay/options').then(value=>{if(!controller.signal.aborted&&alive.current)setOptions(value);}).catch(()=>{});
    }catch(e){if(!controller.signal.aborted&&alive.current&&identity.current===destination){setError(String(e));setJobLoading(false);}}}
    void fetchJob();return()=>{controller.abort();if(timer)clearTimeout(timer);};
  },[selectedId]);
  const modeOptions=options?.modes.find(item=>item.input_mode===mode);
  const effectiveService=serviceId||modeOptions?.default_service_id||'';
  const service=options?.services.find(item=>item.id===effectiveService);
  const effectiveModel=(serviceId?modelId:modeOptions?.default_model_id)||service?.default_model||'';
  const candidate=modeOptions?.candidates.find(item=>item.service_id===effectiveService&&item.model_id===effectiveModel);
  const serviceModels=[...new Set([service?.default_model,...(service?.models||[]).map(item=>item.id)].filter((value):value is string=>Boolean(value)))];
  const cloud=Boolean(candidate?.is_cloud||service&&service.provider!=='local_llm');
  const hasCloudServices=Boolean(options?.services.some(item=>item.enabled&&item.provider!=='local_llm'));
  const selectedAsset=assets[mode],selectedFile=files[mode];
  const selectedImageCount=productSelections.reduce((count,item)=>count+item.image_ids.length,0);
  const sourceValid=mode==='text'?Boolean(story.trim()):mode==='image'&&imageSource==='products'?productSelections.length>0&&productSelections.length<=4&&productSelections.every(item=>item.image_ids.length>0&&item.image_ids.length<=8)&&(productMode==='separate'||selectedImageCount<=8):mode==='video'&&videoSource==='library'?videoIds.length>0&&videoIds.length<=4&&(videoCreationMode!=='style'||Boolean(newTheme.trim())):Boolean(selectedAsset)&&(mode!=='image'||imageAssets.length<=8);
  const cloudMediaRequired=cloud&&mode!=='text';
  useEffect(()=>setCloudMedia(false),[serviceId,modelId,mode,imageSource,videoSource,JSON.stringify(productSelections),imageAssets.map(item=>item.id).join(','),videoIds.join(','),styleId,primaryVideo,newTheme,videoCreationMode]);
  const validInput=Boolean(projectName.trim()&&projectName.length<=200&&creativePrompt.trim()&&sourceValid);
  const generating=active(job),canGenerate=validInput&&Boolean(candidate?.eligible)&&(!cloud||cloudCosts)&&(!cloudMediaRequired||cloudMedia)&&!busy&&!generating;
  const selectedDocument=job?.documents.find(item=>item.id===documentId);
  const selectedReferenceOptions=referenceUse==='character'?job?.screenplay?.characters:job?.screenplay?.scenes;
  const revisionOptions=revisionScope==='scene'?job?.screenplay?.scenes:job?.screenplay?.shots;
  const inputLimits=modeOptions?.limits;
  const pollLabel=job?labels[job.stage]||labels[job.status]||job.status:'';
  const errorText=job?.error?valueText(job.error).replace(/[：:]\s*$/,''):'';
  const requestMayHaveBeenSubmitted=Boolean(job?.receipt?.provider_request_submitted);
  const attempts=job?.receipt?.attempt_history;
  const latestAttempt=Array.isArray(attempts)?attempts.at(-1) as Record<string,unknown>|undefined:undefined;
  const snapshotSettings=(job?.service_snapshot?.settings||{}) as Record<string,unknown>;
  const unknownReadTimeoutSeconds=latestAttempt?.timeout_seconds===null?null:Number(latestAttempt?.timeout_seconds||snapshotSettings.timeout||0);
  const unknownTotalTimeoutSeconds=latestAttempt?.total_timeout_seconds===null?null:Number(latestAttempt?.total_timeout_seconds||0);
  const unknownElapsedSeconds=Number(job?.receipt?.elapsed_seconds||0);
  const historySorted=useMemo(()=>[...history].sort((a,b)=>(b.created_at||'').localeCompare(a.created_at||'')),[history]);
  function selectJob(id:string){if(busyRef.current)return;setSearch(current=>{const next=new URLSearchParams(current);next.set('job',id);return next;});}
  async function upload(){
    if(mode==='text'||!selectedFile||busyRef.current)return;
    const destination=mode,chosen=destination==='image'&&pendingImages.length?pendingImages:[selectedFile];
    if(destination==='image'&&chosen.length+imageAssets.length>8){setError('每次创作最多使用 8 张图片，请在产品图库中分组或减少本次选择。');return;}
    busyRef.current=true;setBusy('upload');setError('');setUploadRows(chosen.map(file=>({name:file.name,status:'等待上传'})));
    const uploaded:ScreenplayInputAsset[]=[],failed:File[]=[];
    try{for(let index=0;index<chosen.length;index++){const file=chosen[index];setUploadRows(rows=>rows.map((row,n)=>n===index?{...row,status:'正在上传并实际校验'}:row));
      try{if(inputLimits?.max_bytes&&file.size>inputLimits.max_bytes)throw new Error(`素材超过 ${Math.round(inputLimits.max_bytes/1024**2)} MB 限制`);const form=new FormData();form.set('file',file);form.set('input_mode',destination);form.set('source_note',sourceNote);const result=await request<ScreenplayInputAsset>('/api/screenplay/assets',{method:'POST',body:form});uploaded.push(result);if(alive.current)setUploadRows(rows=>rows.map((row,n)=>n===index?{...row,status:'服务器已确认保存'}:row));}
      catch(error){failed.push(file);if(alive.current)setUploadRows(rows=>rows.map((row,n)=>n===index?{...row,status:'上传失败',error:String(error)}:row));}}
      if(alive.current){if(uploaded.length){setAssets(value=>({...value,[destination]:uploaded[0]}));if(destination==='image')setImageAssets(value=>[...value,...uploaded.filter(asset=>!value.some(item=>item.id===asset.id))]);}setFiles(value=>({...value,[destination]:failed[0]}));if(destination==='image')setPendingImages(failed);setNotice(failed.length?'部分文件未保存，点击上传按钮可重试失败文件。':'素材已保存。当前生成只使用所选模式的素材。');}
    }finally{busyRef.current=false;if(alive.current)setBusy('');}
  }
  async function generate(test=false){
    if(busyRef.current||!validInput||generating||(!test&&!canGenerate)||test&&!effectiveService)return;
    const destination=selectedId;
    const libraryProducts=mode==='image'&&imageSource==='products',libraryVideos=mode==='video'&&videoSource==='library',batch=libraryProducts&&productSelections.length>1&&productMode==='separate'&&!test;
    const sourcePayload=mode==='text'?{story_text:story}:libraryProducts?{product_selections:productSelections,product_mode:productSelections.length>1?productMode:'single',marketing_template:marketingTemplate}:libraryVideos?{reference_video_ids:videoIds,video_creation_mode:videoCreationMode,new_theme:newTheme,...(styleId?{style_profile_id:styleId}:{}),...(primaryVideo?{primary_reference_video_id:primaryVideo}:{})}:mode==='image'&&imageAssets.length>1?{asset_ids:imageAssets.map(item=>item.id)}:{asset_id:selectedAsset!.id};
    const payload={project_name:projectName,creative_prompt:creativePrompt,input_mode:mode,...sourcePayload,...((test||serviceId)&&effectiveService?{service_id:effectiveService,...(candidate?.model_id?{model_id:candidate.model_id}:{})}:{}),allow_cloud_costs:cloudCosts,allow_cloud_media_upload:cloudMediaRequired&&cloudMedia,allow_paid_fallback:test?false:paidFallback};
    const fingerprint=JSON.stringify({test,...payload});if(submission.current?.fingerprint!==fingerprint)submission.current={fingerprint,key:newKey()};
    busyRef.current=true;setBusy(test?'test':'generate');setError('');setNotice('');
    try{const response=await request<ScreenplayJob|{jobs:ScreenplayJob[];errors?:{product_id:string;error:string}[]}>(test?'/api/screenplay/capability-tests':batch?'/api/screenplay/batches':'/api/screenplay/jobs',{method:'POST',body:JSON.stringify({...payload,idempotency_key:submission.current.key})});
      const createdJobs='jobs' in response?response.jobs:[response],created=createdJobs[0];if(!created)throw new Error('服务器未返回剧本任务');if('jobs' in response&&response.errors?.length)setError('部分产品尚未创建任务：'+response.errors.map(item=>`${item.product_id}：${item.error}`).join('；'));
      if(alive.current){setHistory(items=>[...createdJobs,...items.filter(item=>!createdJobs.some(created=>created.id===item.id))]);if(identity.current===destination){setSearch(current=>{const next=new URLSearchParams(current);next.set('job',created.id);return next;});setNotice(test?'真实能力测试任务已提交。完成后仍需审核，不会自动创建项目。':batch?`已为 ${createdJobs.length} 个产品分别建立独立剧本任务。各产品素材不会混为同一商品。`:'剧本任务已提交。生成 Word 后由你审核，当前不会创建项目。');}}
    }catch(e){if(alive.current)setError(String(e)+'；如请求结果不明，请先刷新任务列表核对，重复提交会保留本次请求标识。');}finally{busyRef.current=false;if(alive.current)setBusy('');}
  }
  async function approve(){
    if(!job||!reviewed||!selectedDocument||busyRef.current||generating)return;
    if(referenceUse!=='none'&&createProject&&!referenceTarget){setError('请选择输入图片要绑定的具体人物或场景。');return;}
    const destination=job.id,creatingProject=createProject&&!job.project_id,referenceTargets:ScreenplayReferenceTarget[]=creatingProject&&referenceUse!=='none'&&job.input_asset?[{asset_id:job.input_asset.id,target_kind:referenceUse,target_key:referenceTarget,purpose:referenceUse==='scene'?'scene_reference':'identity_front'}]:[];
    busyRef.current=true;setBusy('approve');setError('');
    try{const result=await request<ScreenplayJob>(`/api/screenplay/jobs/${encodeURIComponent(destination)}/approve`,{method:'POST',body:JSON.stringify({expected_revision:job.revision,document_id:selectedDocument.id,approve:true,create_project:creatingProject,reference_targets:referenceTargets})});
      if(alive.current&&identity.current===destination){setJob(result);setHistory(items=>[result,...items.filter(item=>item.id!==result.id)]);setReviewed(false);setCreateProject(false);setNotice(result.project_id?'项目已创建，可进入项目工作台配置人物参考与配音。':'审核已保存，仅归档剧本和 Word；尚未创建项目。');}
    }catch(e){if(alive.current&&identity.current===destination)setError(String(e)+'；剧本和 Word 会保留，请核对后重试建项。');}finally{busyRef.current=false;if(alive.current)setBusy('');}
  }
  async function revise(e:FormEvent){
    e.preventDefault();if(!job||busyRef.current||!revisionInstruction.trim()||revisionScope!=='all'&&!revisionTarget)return;
    const destination=job.id,payload={expected_revision:job.revision,instruction:revisionInstruction,scope:revisionScope,...(revisionScope!=='all'?{target_key:revisionTarget}:{}),allow_cloud_costs:cloudCosts,allow_paid_fallback:paidFallback};
    const fingerprint=JSON.stringify({job_id:destination,...payload});if(revisionSubmission.current?.fingerprint!==fingerprint)revisionSubmission.current={fingerprint,key:newKey()};
    busyRef.current=true;setBusy('revise');setError('');
    try{const result=await request<ScreenplayJob>(`/api/screenplay/jobs/${encodeURIComponent(destination)}/revise`,{method:'POST',body:JSON.stringify({...payload,idempotency_key:revisionSubmission.current.key})});
      if(alive.current&&identity.current===destination){setSearch(current=>{const next=new URLSearchParams(current);next.set('job',result.id);return next;});setNotice('已建立独立修订版本；历史 Word 和已审核内容保留。');}
    }catch(e){if(alive.current&&identity.current===destination)setError(String(e));}finally{busyRef.current=false;if(alive.current)setBusy('');}
  }
  async function retryExport(newVersion=false){if(!job||busyRef.current)return;const destination=job.id;busyRef.current=true;setBusy('export');setError('');try{const next=await request<ScreenplayJob>(`/api/screenplay/jobs/${encodeURIComponent(destination)}/${newVersion?'export-new-version':'export-retry'}`,{method:'POST',body:JSON.stringify({expected_revision:job.revision})});if(alive.current&&identity.current===destination){setJob(next);setDocumentId(next.latest_document_id||next.documents.at(-1)?.id||'');setReviewed(false);setCreateProject(false);setNotice('已使用保存的结构化剧本导出独立 Word 版本，没有重新调用创作模型。');}}catch(e){if(alive.current)setError(String(e));}finally{busyRef.current=false;if(alive.current)setBusy('');}}
  async function retryGeneration(){
    if(!job||busyRef.current||!['failed','blocked','unknown'].includes(job.status)||job.status==='unknown'&&!confirmUnknownRetry)return;
    const destination=job.id,payload={expected_revision:job.revision,allow_cloud_costs:retryCosts,allow_paid_fallback:retryPaid,confirm_unknown_retry:job.status==='unknown'&&confirmUnknownRetry};
    const fingerprint=JSON.stringify({job_id:destination,...payload});if(retrySubmission.current?.fingerprint!==fingerprint)retrySubmission.current={fingerprint,key:newKey()};
    busyRef.current=true;setBusy('retry');setError('');
    try{const next=await request<ScreenplayJob>(`/api/screenplay/jobs/${encodeURIComponent(destination)}/retry`,{method:'POST',body:JSON.stringify({...payload,idempotency_key:retrySubmission.current.key})});if(alive.current&&identity.current===destination){setHistory(items=>[next,...items.filter(item=>item.id!==next.id)]);setSearch(current=>{const value=new URLSearchParams(current);value.set('job',next.id);return value;});setNotice(job.status==='unknown'?'已建立独立重试任务；原结果未知记录保留，请核对供应商记录并留意可能的重复费用。':'已建立独立重试任务，失败记录和已有文档保留。');}}
    catch(e){if(alive.current&&identity.current===destination)setError(String(e));}finally{busyRef.current=false;if(alive.current)setBusy('');}
  }
  async function cancelJob(){
    if(!job||busyRef.current||!['queued','running'].includes(job.status))return;
    const destination=job.id,payload={expected_revision:job.revision};
    busyRef.current=true;setBusy('cancel');setError('');setNotice('');
    try{const next=await request<ScreenplayJob>(`/api/screenplay/jobs/${encodeURIComponent(destination)}/cancel`,{method:'POST',body:JSON.stringify(payload)});
      if(alive.current&&identity.current===destination){setJob(next);setHistory(items=>[next,...items.filter(item=>item.id!==next.id)]);setNotice('已中止当前任务。可在修复后继续执行，或新建重试任务。');}
    }catch(e){if(alive.current&&identity.current===destination)setError(String(e));}finally{busyRef.current=false;if(alive.current)setBusy('');}
  }
  async function resumeJob(){
    if(!job||busyRef.current||job.status!=='cancelled')return;
    const destination=job.id,payload={expected_revision:job.revision,allow_cloud_costs:resumeCosts,allow_paid_fallback:resumePaid,confirm_unknown_retry:requestMayHaveBeenSubmitted&&confirmUnknownRetry};
    if(requestMayHaveBeenSubmitted&&!confirmUnknownRetry)return;
    const fingerprint=JSON.stringify({job_id:destination,...payload});if(resumeSubmission.current?.fingerprint!==fingerprint)resumeSubmission.current={fingerprint,key:newKey()};
    busyRef.current=true;setBusy('resume');setError('');setNotice('');
    try{const next=await request<ScreenplayJob>(`/api/screenplay/jobs/${encodeURIComponent(destination)}/resume`,{method:'POST',body:JSON.stringify({...payload,idempotency_key:resumeSubmission.current.key})});
      if(alive.current&&identity.current===destination){setHistory(items=>[next,...items.filter(item=>item.id!==next.id)]);setSearch(current=>{const value=new URLSearchParams(current);value.set('job',next.id);return value;});setNotice('已基于原输入新建继续执行任务，原中止记录保留。');}
    }catch(e){if(alive.current&&identity.current===destination)setError(String(e));}finally{busyRef.current=false;if(alive.current)setBusy('');}
  }
  function toggleSelected(id:string){setSelectedIds(current=>{const next=new Set(current);if(next.has(id))next.delete(id);else next.add(id);return next;});}
  function toggleSelectAll(){setSelectedIds(current=>{if(current.size===historySorted.length)return new Set();return new Set(historySorted.map(item=>item.id));});}
  function clearSelection(){setSelectedIds(new Set());}
  async function batchCancelJobs(){
    if(busyRef.current||selectedIds.size===0)return;
    busyRef.current=true;setBusy('batch-cancel');setError('');setNotice('');
    try{
      const result=await request<{results:{id:string;ok:boolean;error?:string}[]}>('/api/screenplay/jobs/batch-cancel',{method:'POST',body:JSON.stringify({ids:[...selectedIds]})});
      if(alive.current){
        const ok=result.results.filter(r=>r.ok).map(r=>r.id),failed=result.results.filter(r=>!r.ok);
        await refresh();
        setSelectedIds(new Set());
        setNotice(`已中止 ${ok.length} 个任务${failed.length?`；${failed.length} 个未成功`:''}。`);
      }
    }catch(e){if(alive.current)setError(String(e));}finally{busyRef.current=false;if(alive.current)setBusy('');}
  }
  async function batchDeleteJobs(){
    if(busyRef.current||selectedIds.size===0)return;
    if(!window.confirm(`确认删除所选 ${selectedIds.size} 个任务吗？此操作不可撤销，会同步删除历史记录、Word 文档和磁盘上对应的素材文件夹。`))return;
    busyRef.current=true;setBusy('batch-delete');setError('');setNotice('');
    try{
      const result=await request<{results:{id:string;ok:boolean;error?:string}[]}>('/api/screenplay/jobs/batch',{method:'DELETE',body:JSON.stringify({ids:[...selectedIds]})});
      if(alive.current){
        const ok=result.results.filter(r=>r.ok).map(r=>r.id),failed=result.results.filter(r=>!r.ok);
        if(selectedId&&ok.includes(selectedId)){setSearch(current=>{const next=new URLSearchParams(current);next.delete('job');return next;});}
        await refresh();
        setSelectedIds(new Set());
        setNotice(`已删除 ${ok.length} 个任务及对应的磁盘文件${failed.length?`；${failed.length} 个未成功`:''}。`);
      }
    }catch(e){if(alive.current)setError(String(e));}finally{busyRef.current=false;if(alive.current)setBusy('');}
  }
  async function batchResumeJobs(){
    if(busyRef.current||selectedIds.size===0)return;
    busyRef.current=true;setBusy('batch-resume');setError('');setNotice('');
    try{
      const result=await request<{results:{id:string;ok:boolean;new_id?:string;error?:string}[]}>('/api/screenplay/jobs/batch-resume',{method:'POST',body:JSON.stringify({ids:[...selectedIds],allow_cloud_costs:false,allow_paid_fallback:false})});
      if(alive.current){
        const ok=result.results.filter(r=>r.ok),failed=result.results.filter(r=>!r.ok);
        await refresh();
        setSelectedIds(new Set());
        if(failed.length===0){
          setNotice(`已为 ${ok.length} 个已中止任务建立继续执行任务。`);
        }else if(failed.some(row=>row.error==='screenplay_resume_unknown')){
          setNotice(`已继续 ${ok.length} 个；含已提交模型请求的任务不会批量续跑，请逐项核对结果后明确确认重试。`);
        }else{
          setNotice(`已继续 ${ok.length} 个；${failed.length} 个未成功（仅"已中止"状态可继续）。`);
        }
      }
    }catch(e){if(alive.current)setError(String(e));}finally{busyRef.current=false;if(alive.current)setBusy('');}
  }
  function editInput(){if(!job)return;const source=job.source_selection||{};setProjectName(job.project_name);setCreativePrompt(job.creative_prompt);setMode(job.input_mode);setStory(job.story_text||'');setPendingImages([]);setUploadRows([]);setFiles({});setCloudMedia(false);
    const restoredImages=job.input_assets?.length?job.input_assets:job.input_asset?[job.input_asset]:[];
    if(job.input_mode==='image'){setImageSource(source.product_selections?.length?'products':'upload');setProductSelections(source.product_selections||[]);setProductMode(source.product_mode==='combined'?'combined':'separate');setMarketingTemplate(source.marketing_template||'showcase');setImageAssets(restoredImages);setAssets(current=>({...current,image:restoredImages[0]}));}
    if(job.input_mode==='video'){setVideoSource(source.reference_video_ids?.length?'library':'upload');setVideoIds(source.reference_video_ids||[]);setPrimaryVideo(source.primary_reference_video_id||'');setStyleId(source.style_profile_id||'');setVideoCreationMode(source.video_creation_mode||'adaptation');setNewTheme(source.new_theme||'');setAssets(current=>({...current,video:job.input_asset||restoredImages[0]}));}
    setNotice('已将本版全部已保存的输入带回创作区。修改后生成将建立独立任务，历史版本保留。');document.getElementById('screenplay-creation-form')?.scrollIntoView?.({behavior:'smooth'});}

  return <div className="screenplay-center">
    <PageHeader title="剧本生成" description="选择一种创作素材，生成可审核的 Word 和结构化分镜。审核后再决定是否创建制作项目。" actions={<Link className="button button-secondary" to="/">项目工作台<ArrowRight size={16}/></Link>}/>
    {error?<p className="inline-notice warning" role="alert">{error}</p>:null}{notice?<p className="inline-notice" role="status">{notice}</p>:null}
    {loading?<LoadingState label="读取剧本模型与历史任务…"/>:null}
    <form id="screenplay-creation-form" className="card screenplay-create" onSubmit={e=>{e.preventDefault();void generate();}}>
      <FormField label="项目备注 / 项目名称" required hint="单份剧本原样使用此名称；多产品分别生成时会加「 · 产品名称」区分任务。不会由模型改名；同名冲突会明确提示。"><input required maxLength={200} value={projectName} onChange={e=>setProjectName(e.target.value)} disabled={!!busy}/></FormField>
      <FormField label="创作提示词" required hint="填写题材、目标时长、人物关系、节奏、现实感、对白与摄影要求。系统自动加入统一制作规范。"><textarea required rows={4} value={creativePrompt} onChange={e=>setCreativePrompt(e.target.value)} disabled={!!busy}/></FormField>
      <fieldset className="screenplay-mode-picker"><legend>选择一种创作素材</legend>{(['text','image','video'] as const).map(value=><label key={value}><input type="radio" name="screenplay-input-mode" value={value} checked={mode===value} disabled={!!busy} onChange={()=>{setMode(value);setError('');}}/>{modeLabels[value]}</label>)}</fieldset>
      {mode==='image'?<FormField label="图片素材来源"><select value={imageSource} disabled={!!busy} onChange={event=>setImageSource(event.target.value as typeof imageSource)}><option value="upload">上传新的创作图片</option><option value="products">从产品图库选择产品或部分图片</option></select></FormField>:null}
      {mode==='video'?<FormField label="视频素材来源"><select value={videoSource} disabled={!!busy} onChange={event=>setVideoSource(event.target.value as typeof videoSource)}><option value="upload">上传一条新的创作视频</option><option value="library">从参考视频库选择多条视频</option></select></FormField>:null}
      {mode==='text'?<FormField label="故事梗概 / 创意素材" required><textarea required rows={5} value={story} onChange={e=>setStory(e.target.value)} disabled={!!busy}/></FormField>:mode==='image'&&imageSource==='products'?<>
        <ProductLibraryPicker value={productSelections} onChange={setProductSelections} disabled={!!busy} initialId={search.get('product')||undefined}/>
        {productSelections.length>1?<fieldset className="screenplay-mode-picker"><legend>多个产品的任务归属</legend><label><input type="radio" name="product-mode" checked={productMode==='separate'} disabled={!!busy} onChange={()=>setProductMode('separate')}/>每个产品分别生成一份营销剧本（默认）</label><label><input type="radio" name="product-mode" checked={productMode==='combined'} disabled={!!busy} onChange={()=>setProductMode('combined')}/>将所选产品放入同一条视频组合营销</label></fieldset>:<p className="form-hint">默认一个产品生成一条营销视频剧本。</p>}
        <FormField label="产品营销模板"><select value={marketingTemplate} disabled={!!busy} onChange={event=>setMarketingTemplate(event.target.value)}>{Object.entries({showcase:'产品展示型',usage:'使用场景型',problem_solution:'问题与解决方案型',comparison:'产品对比型',unboxing:'开箱或细节展示型',lifestyle:'生活方式广告型',sales:'短视频带货型',brand:'品牌形象型'}).map(([id,label])=><option key={id} value={id}>{label}</option>)}</select></FormField><p className="form-hint">本次选中 {productSelections.length} 个产品、{selectedImageCount} 张图片。每次最多选择 4 个产品，每份剧本最多使用 8 张图片。价格、功效、认证与规格以产品资料为依据，缺少证据的内容需人工确认。</p>
      </>:mode==='video'&&videoSource==='library'?<>
        <ReferenceVideoPicker ids={videoIds} onIds={setVideoIds} primary={primaryVideo} onPrimary={setPrimaryVideo} styleId={styleId} onStyle={setStyleId} disabled={!!busy} initialVideoId={search.get('video')||undefined} initialStyleId={search.get('style')||undefined}/>
        <fieldset className="screenplay-mode-picker"><legend>参考视频创作模式</legend><label><input type="radio" name="video-creation-mode" checked={videoCreationMode==='adaptation'} disabled={!!busy} onChange={()=>setVideoCreationMode('adaptation')}/>内容改编：参考主题、结构与表达方式</label><label><input type="radio" name="video-creation-mode" checked={videoCreationMode==='style'} disabled={!!busy} onChange={()=>setVideoCreationMode('style')}/>风格迁移式创作：为新的产品、故事或主题创作</label></fieldset>
        <FormField label="新的产品、故事或主题" required={videoCreationMode==='style'}><textarea rows={3} required={videoCreationMode==='style'} value={newTheme} disabled={!!busy} onChange={event=>setNewTheme(event.target.value)} placeholder="填写这次要创作的新内容，不会直接复制参考视频完整台词或品牌标识"/></FormField>
      </>:<div className="screenplay-source">
        <FormField label={mode==='image'?'上传创作图片':'上传创作视频'} required hint={`${inputLimits?.formats?.join('、')|| (mode==='image'?'PNG、JPEG、WebP':'MP4、MOV、MKV、WebM')}${inputLimits?.max_bytes?` · 最大 ${Math.round(inputLimits.max_bytes/1024**2)} MB`:''}${inputLimits?.max_duration_seconds?` · 最长 ${inputLimits.max_duration_seconds} 秒`:''}${mode==='image'?' · 一次可选择多张（最多 8 张）':''}`}><input key={mode} type="file" multiple={mode==='image'} accept={mode==='image'?'.png,.jpg,.jpeg,.webp':'.mp4,.mov,.mkv,.webm'} disabled={!!busy} onChange={event=>{const chosen=Array.from(event.target.files||[]);setFiles(current=>({...current,[mode]:chosen[0]}));setAssets(current=>({...current,[mode]:undefined}));if(mode==='image'){setPendingImages(chosen);setImageAssets([]);}setUploadRows([]);}}/></FormField>
        <FormField label="素材来源说明"><input value={sourceNote} onChange={e=>setSourceNote(e.target.value)} disabled={!!busy}/></FormField><div className="screenplay-actions"><button type="button" className="button button-secondary" disabled={!selectedFile||!!busy} onClick={()=>void upload()}><Upload size={16}/>{busy==='upload'?'正在上传…':uploadRows.some(row=>row.error)?'重试失败创作素材':'上传并保存创作素材'}</button>{selectedFile?<span>{mode==='image'&&pendingImages.length>1?`${pendingImages.length} 张待上传图片`:selectedFile.name}</span>:null}</div>
        {uploadRows.length?<ul>{uploadRows.map((row,index)=><li key={index}>{row.name} · {row.status}{row.error?<span role="alert"> · {row.error}</span>:null}</li>)}</ul>:null}
        {selectedAsset?<><p>已保存：{selectedAsset.filename}{mode==='image'&&imageAssets.length>1?` 等 ${imageAssets.length} 张图片`:''}</p>{mode==='image'?<div className="asset-picker-images">{(imageAssets.length?imageAssets:[selectedAsset]).map((asset,index)=><img key={asset.id} src={api.mediaUrl(asset.media_url)} alt={index===0?'已保存的创作素材':`已保存的创作素材 ${index+1}`}/>)}</div>:<video src={api.mediaUrl(selectedAsset.media_url)} controls preload="metadata"/>}</>:null}
        {mode==='video'?<p className="form-hint">视频会真实抽取 8 帧并提取音轨。已配置的本机服务可自动转写；文本与说话人归属仍需核对。未配置转写服务时会明确标注音轨理解受限。多条视频请切换到参考视频库。</p>:<p className="form-hint">同一产品可使用多个角度。不同产品请使用产品图库分组，明确选择分别生成或组合营销。图片中的可见信息与创作推断分别记录。</p>}
      </div>}
      <FormField label="剧本模型" hint="默认继承系统设置。连接成功和实际生成能力分别验证。"><select value={serviceId} onChange={e=>{setServiceId(e.target.value);setModelId('');setCloudCosts(false);setPaidFallback(false);}} disabled={!!busy}><option value="">继承系统默认{modeOptions?.default_service_id?` · ${options?.services.find(item=>item.id===modeOptions.default_service_id)?.name||''}`:'（尚未配置）'}</option>{options?.services.filter(item=>item.enabled).map(item=><option key={item.id} value={item.id}>{item.name} · {item.default_model}</option>)}</select></FormField>
      {serviceId&&serviceModels.length>1?<FormField label="此服务的剧本模型版本"><select value={effectiveModel} disabled={!!busy} onChange={e=>setModelId(e.target.value)}>{serviceModels.map(id=><option value={id} key={id}>{id}</option>)}</select></FormField>:null}
      {serviceId?<p className="form-hint">本次手动选择只调用此服务。选择「继承系统默认」才会使用系统已保存的可选回退策略。</p>:null}
      {candidate?<p className="form-hint"><Badge tone={candidate.eligible?'success':'warning'}>{candidate.eligible?'本模式真实生成已验证':'本模式尚未验证'}</Badge> {candidate.reasons?.join('；')}</p>:<p className="form-hint">请在系统设置的服务档案中配置语言与剧本服务，并为此模式选择模型。</p>}
      {hasCloudServices||cloud?<label><input type="checkbox" checked={cloudCosts} disabled={!!busy} onChange={e=>{setCloudCosts(e.target.checked);if(!e.target.checked)setPaidFallback(false);}}/>允许本次按所选策略调用付费云端服务</label>:null}
      {cloudMediaRequired?<label><input type="checkbox" checked={cloudMedia} disabled={!!busy} onChange={event=>setCloudMedia(event.target.checked)}/>允许将本次选中的图片、视频或派生参考分析发送到所选云端模型服务。此授权独立于费用授权，默认关闭。</label>:null}
      {cloudCosts?<label><input type="checkbox" checked={paidFallback} disabled={!!busy} onChange={e=>setPaidFallback(e.target.checked)}/>同时允许已明确配置的付费回退</label>:null}
      <div className="screenplay-actions"><button className="button button-primary" disabled={!canGenerate}><FileText size={16}/>{busy==='generate'?'正在提交…':generating?'本次任务仍在执行':'生成剧本'}</button><button type="button" className="button button-secondary" disabled={!validInput||!effectiveService||!!busy||generating||cloud&&!cloudCosts||cloudMediaRequired&&!cloudMedia||service?.provider==='cloud_draft'||mode==='image'&&imageSource==='products'&&productSelections.length>1&&productMode==='separate'} onClick={()=>void generate(true)}>{busy==='test'?'正在提交测试…':'使用当前输入测试此模式能力'}</button><Link to="/settings">配置模型与服务</Link></div>
      {!validInput?<p className="form-hint">请填写项目名称、创作提示词和当前模式的素材；图片或视频需先上传保存。</p>:!candidate?.eligible?<p className="form-hint">此模式尚无有效生成证据。可先明确选择服务并执行一次真实能力测试，成功后再用于日常创作。</p>:null}
    </form>
    <div className="screenplay-two-column"><section className="card"><div className="screenplay-review-header"><h2>剧本与历史版本</h2><button type="button" className="button button-secondary button-small" disabled={!!busy} onClick={()=>{void refresh().catch(e=>setError(String(e)));}}><RefreshCw size={14}/>刷新任务列表</button></div><div className="screenplay-job-list">{historySorted.map(item=><div key={item.id} className="screenplay-job-row"><input type="checkbox" aria-label={`选择任务 ${item.project_name}`} checked={selectedIds.has(item.id)} onChange={()=>toggleSelected(item.id)} disabled={!!busy}/><button type="button" disabled={!!busy} aria-current={item.id===selectedId} onClick={()=>selectJob(item.id)}><strong>{item.project_name}</strong><span>{modeLabels[item.input_mode]} · {labels[item.status]||item.status}{item.parent_job_id?' · 独立修订':''}</span><small>{item.created_at?new Date(item.created_at).toLocaleString():item.id}</small></button></div>)}</div>{historySorted.length>0?<div className="screenplay-batch-bar"><label className="screenplay-batch-select-all"><input type="checkbox" aria-label="全选/取消全选" checked={selectedIds.size===historySorted.length&&historySorted.length>0} onChange={toggleSelectAll} disabled={!!busy}/>全选（{selectedIds.size}/{historySorted.length}）</label>{selectedIds.size>0?<><button type="button" className="button button-secondary button-small" disabled={!!busy} onClick={()=>void batchCancelJobs()}>{busy==='batch-cancel'?`正在中止 ${selectedIds.size} 个…`:`中止所选 ${selectedIds.size} 个`}</button><button type="button" className="button button-secondary button-small" disabled={!!busy} onClick={()=>void batchResumeJobs()}>{busy==='batch-resume'?`正在继续 ${selectedIds.size} 个…`:`继续所选 ${selectedIds.size} 个`}</button><button type="button" className="button button-danger button-small" disabled={!!busy} onClick={()=>void batchDeleteJobs()}>{busy==='batch-delete'?`正在删除 ${selectedIds.size} 个…`:`删除所选 ${selectedIds.size} 个`}</button><button type="button" className="button button-secondary button-small" disabled={!!busy} onClick={clearSelection}>取消选择</button></>:null}</div>:null}{!history.length&&!loading?<p>还没有生成任务。所有版本会在这里保留。</p>:null}</section>
      <section className="card screenplay-review" aria-label="剧本审核">
        {jobLoading?<LoadingState label="读取剧本任务…"/>:!job?<p>选择历史任务，或先生成一份剧本。</p>:<>
          <div className="screenplay-review-header"><div><h2>{job.project_name}</h2><Badge tone={job.status==='approved'?'success':job.status==='failed'||job.status==='unknown'?'warning':'neutral'}>{pollLabel}</Badge></div><button type="button" className="button button-secondary button-small" disabled={!!busy} onClick={editInput}>返回修改创作输入</button></div>
          {job.service_snapshot?.model_id?<p className="form-hint">本次创作模型：{valueText(job.service_snapshot.name)||valueText(job.service_snapshot.provider)} · {valueText(job.service_snapshot.model_id)}</p>:null}
          {job.source_selection?.product_selections?.length?<p className="form-hint">本版产品素材：{job.products?.map(item=>item.name).join('、')||`${job.source_selection.product_selections.length} 个产品`} · {job.source_selection.product_selections.reduce((total,item)=>total+item.image_ids.length,0)} 张已保存图片 · {job.source_selection.product_mode==='combined'?'组合营销':'单产品独立剧本'}</p>:null}
          {job.source_selection?.reference_video_ids?.length?<p className="form-hint">本版参考视频：{job.source_selection.reference_video_ids.length} 条 · {job.source_selection.video_creation_mode==='style'?'新主题风格创作':'内容改编'}{job.source_selection.style_profile_id?` · 风格档案 ${job.source_selection.style_profile_id}`:''}{job.source_selection.primary_reference_video_id?` · 主参考 ${job.source_selection.primary_reference_video_id}`:''}</p>:null}
          {job.input_assets&&job.input_assets.length>1?<p className="form-hint">本版已保存创作图片：{job.input_assets.length} 张，全部图片 ID 随任务归档。</p>:null}
          {job.input_asset?<p className="form-hint">原始创作素材：{job.input_asset.filename} · 来源已随本版归档</p>:null}
          {generating?<p role="status">{pollLabel}。进度来自后台任务，页面会持续读取实际状态。</p>:null}
          {generating?<div className="screenplay-review-actions"><button type="button" className="button button-secondary" disabled={!!busy} onClick={()=>void cancelJob()}>{busy==='cancel'?'正在中止…':'中止当前任务'}</button><p className="form-hint">中止后任务不会自动恢复，可在此页继续执行或新建任务。</p></div>:null}
          {job.status==='cancelled'?<div className="screenplay-review-actions"><p>{requestMayHaveBeenSubmitted?'本地中止不会停止已经提交给模型服务的请求；该请求结果可能未知。核对服务商记录，或确认接受可能重复生成和计费后，才能新建独立任务。':'任务在模型请求提交前已中止。修复服务或条件后，可继续执行（新建独立任务，原记录保留）。'}</p>{requestMayHaveBeenSubmitted?<label><input type="checkbox" checked={confirmUnknownRetry} disabled={!!busy} onChange={e=>setConfirmUnknownRetry(e.target.checked)}/>我已核对服务商结果，或接受原请求可能已执行、重试可能重复生成和计费</label>:null}<label><input type="checkbox" checked={resumeCosts} disabled={!!busy} onChange={e=>{setResumeCosts(e.target.checked);if(!e.target.checked)setResumePaid(false);}}/>允许继续任务使用付费云端服务</label>{resumeCosts?<label><input type="checkbox" checked={resumePaid} disabled={!!busy} onChange={e=>setResumePaid(e.target.checked)}/>同时允许已配置的付费回退</label>:null}<button type="button" className="button button-secondary" disabled={!!busy||Boolean(job.service_snapshot?.is_cloud)&&!resumeCosts||requestMayHaveBeenSubmitted&&!confirmUnknownRetry} onClick={()=>void resumeJob()}>{busy==='resume'?'正在提交继续…':requestMayHaveBeenSubmitted?'确认并新建重试任务':'继续执行'}</button></div>:null}
          {errorText?<p role="alert" className="inline-notice warning">{errorText}</p>:null}
          {job.status==='unknown'?<div className="screenplay-review-actions"><p>本次等待已经结束，页面不会继续轮询，也不会自动重发。{unknownReadTimeoutSeconds===null?'模型响应读取无上限。':unknownReadTimeoutSeconds?`响应读取等待上限为 ${unknownReadTimeoutSeconds} 秒。`:''}{unknownTotalTimeoutSeconds===null?'请求总时限无上限。':unknownTotalTimeoutSeconds?`请求总时限为 ${unknownTotalTimeoutSeconds} 秒。`:''}{unknownElapsedSeconds?`本地记录的总等待时间约 ${unknownElapsedSeconds.toFixed(1)} 秒。`:''}没有收到可用于查询的供应商响应，因此无法确认供应商是否完成生成或计费。请先核对供应商记录；若找不到结果或接受重复生成和费用，请勾选确认后新建独立任务。</p><label><input type="checkbox" checked={confirmUnknownRetry} disabled={!!busy} onChange={e=>setConfirmUnknownRetry(e.target.checked)}/>我已核对服务商结果，或接受原请求可能已执行、重试可能重复生成和计费</label><label><input type="checkbox" checked={retryCosts} disabled={!!busy} onChange={e=>{setRetryCosts(e.target.checked);if(!e.target.checked)setRetryPaid(false);}}/>允许此重试任务使用付费云端服务</label>{retryCosts?<label><input type="checkbox" checked={retryPaid} disabled={!!busy} onChange={e=>setRetryPaid(e.target.checked)}/>同时允许此重试的已配置付费回退</label>:null}<button type="button" className="button button-secondary" disabled={!!busy||!confirmUnknownRetry||Boolean(job.service_snapshot?.is_cloud)&&!retryCosts} onClick={()=>void retryGeneration()}>{busy==='retry'?'正在提交重试…':'确认并新建重试任务'}</button></div>:null}
          {['failed','blocked'].includes(job.status)?<div className="screenplay-review-actions"><p>修复服务或输入条件后，可明确建立一个新的重试任务；旧任务记录保留。</p><label><input type="checkbox" checked={retryCosts} disabled={!!busy} onChange={e=>{setRetryCosts(e.target.checked);if(!e.target.checked)setRetryPaid(false);}}/>允许此重试任务使用付费云端服务</label>{retryCosts?<label><input type="checkbox" checked={retryPaid} disabled={!!busy} onChange={e=>setRetryPaid(e.target.checked)}/>同时允许此重试的已配置付费回退</label>:null}<button type="button" className="button button-secondary" disabled={!!busy||Boolean(job.service_snapshot?.is_cloud)&&!retryCosts} onClick={()=>void retryGeneration()}>{busy==='retry'?'正在提交重试…':'新建重试任务'}</button></div>:null}
          {job.status==='export_failed'?<button type="button" className="button button-secondary" disabled={!!busy} onClick={()=>void retryExport()}>使用已保存剧本重试 Word 导出</button>:null}
          {job.screenplay&&!generating&&job.status!=='export_failed'?<button type="button" className="button button-secondary" disabled={!!busy} onClick={()=>void retryExport(true)}>{busy==='export'?'正在导出…':'使用已有剧本导出新 Word 版本'}</button>:null}
          {job.screenplay?<><div className="screenplay-counts"><span>{job.counts?.characters??job.screenplay.characters.length} 位人物</span><span>{job.counts?.scenes??job.screenplay.scenes.length} 个场景</span><span>{job.counts?.shots??job.screenplay.shots.length} 个分镜</span><span>{job.counts?.dialogues??job.screenplay.shots.reduce((total,shot)=>total+shot.dialogues.length,0)} 条对白</span></div>
            {job.uncertainties.length?<div><h3>需要核对的内容</h3><ul className="screenplay-warning-list">{job.uncertainties.map((item,index)=><li key={index}>{item}</li>)}</ul></div>:null}
            <Preview ir={job.screenplay}/>
          </>:null}
          {job.documents.length?<><div className="screenplay-document-list">{job.documents.map(document=><a key={document.id} href={document.download_url} download><Download size={14}/> Word v{document.version} · {document.review_state==='approved'?'已审核':'待审核'}</a>)}</div>
            <div className="screenplay-review-actions"><FormField label="本次审核的 Word 版本"><select value={documentId} disabled={!!busy} onChange={e=>{setDocumentId(e.target.value);setReviewed(false);}}>{job.documents.map(document=><option key={document.id} value={document.id}>Word v{document.version} · {document.review_state==='approved'?'已审核':'待审核'}</option>)}</select></FormField>
              {job.project_id?<p role="status">项目已创建：<Link to={`/?project=${encodeURIComponent(job.project_id)}`}>{job.project_name} · 进入项目工作台<ArrowRight size={14}/></Link></p>:null}
              {!job.project_id||selectedDocument?.review_state!=='approved'?<><label><input type="checkbox" checked={reviewed} disabled={!!busy||generating} onChange={e=>setReviewed(e.target.checked)}/>剧本内容审核通过</label>{!job.project_id?<label><input type="checkbox" checked={createProject} disabled={!!busy||generating} onChange={e=>setCreateProject(e.target.checked)}/>审核通过后创建项目</label>:null}
                {createProject&&job.input_mode==='image'&&job.input_asset?<><FormField label="输入图片的后续用途"><select value={referenceUse} disabled={!!busy} onChange={e=>{setReferenceUse(e.target.value as typeof referenceUse);setReferenceTarget('');}}><option value="none">仅作为本次创作素材</option><option value="character">保存为指定人物的参考图</option><option value="scene">保存为指定场景的参考图</option></select></FormField>{referenceUse!=='none'?<FormField label={referenceUse==='character'?'参考图对应人物':'参考图对应场景'}><select value={referenceTarget} disabled={!!busy} onChange={e=>setReferenceTarget(e.target.value)}><option value="">请选择明确对象</option>{selectedReferenceOptions?.map(item=><option key={item.key} value={item.key}>{item.name}</option>)}</select></FormField>:null}</>:null}
                <button type="button" className="button button-primary" disabled={!reviewed||!selectedDocument||!!busy||generating||createProject&&referenceUse!=='none'&&!referenceTarget} onClick={()=>void approve()}>{busy==='approve'?'正在保存审核…':createProject?'确认审核并创建项目':'仅保存审核结果'}</button><p className="form-hint">未勾选创建项目时，只保存剧本与 Word。建项使用同一份结构化数据，名称保持原样，不会再次猜测文档。</p>
              </>:null}
            </div>
          </>:null}
          {job.screenplay&&!generating?<details><summary>局部修改或重新生成</summary><form className="screenplay-revision" onSubmit={e=>void revise(e)}><FormField label="修改范围"><select value={revisionScope} disabled={!!busy} onChange={e=>{setRevisionScope(e.target.value as ScreenplayScope);setRevisionTarget('');}}><option value="all">整份剧本</option><option value="scene">指定场景</option><option value="shot">指定分镜</option></select></FormField>{revisionScope!=='all'?<FormField label="要修改的内容"><select required value={revisionTarget} disabled={!!busy} onChange={e=>setRevisionTarget(e.target.value)}><option value="">请选择</option>{revisionOptions?.map(item=><option key={item.key} value={item.key}>{'name' in item?item.name:item.title}</option>)}</select></FormField>:null}<FormField label="本次修改要求" required><textarea rows={3} required value={revisionInstruction} disabled={!!busy} onChange={e=>setRevisionInstruction(e.target.value)}/></FormField><button className="button button-secondary" disabled={!!busy||!revisionInstruction.trim()||revisionScope!=='all'&&!revisionTarget}>生成独立修订版本</button><p className="form-hint">局部修改只提交所选场景或分镜；历史版本与已有项目保留。云端修订同样需要本次费用授权。</p></form></details>:null}
        </>}
      </section>
    </div>
  </div>;
}
