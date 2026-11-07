import { useState } from 'react';
import { api, request } from '../lib/api';
import type { ReferenceVideoAsset, ReferenceVideoOverview } from '../lib/asset-library-contract';
import type { StudioShot } from '../lib/studio-contract';
import { FormField } from './ui';

type ImageReference = { id: string; name: string; relative_path: string };
type VideoReferenceRole = 'structure' | 'action';
type VideoReference = { asset: ReferenceVideoAsset; start: string; end: string; role: VideoReferenceRole };
export type ShotVideoInputDraft = { prompt: string; images: ImageReference[]; imageSelectionExplicit: boolean; videos: VideoReference[] };
export const emptyShotVideoInputs: ShotVideoInputDraft = { prompt:'', images:[], imageSelectionExplicit:false, videos:[] };
type VisualResponse = { id: string; project_id: string; shot_id: string; status: string; relative_path: string };
type VideoImport = { items?: ReferenceVideoAsset[]; duplicate_ids?: string[]; errors?: {error:string|{message?:string}}[] };

export function motionInputError(video: VideoReference) {
  const start=Number(video.start), end=Number(video.end);
  if(!video.start.trim()||!video.end.trim()||!Number.isFinite(start)||!Number.isFinite(end)||start<0||end-start<2||end-start>15){
    return '每条参考视频必须填写 2–15 秒的有效截取范围。';
  }
  if(end>Number(video.asset.metadata.duration_seconds))return '截取结束时间超过原视频时长。';
  return '';
}

export function selectedShotInputOverrides(drafts: Record<string, ShotVideoInputDraft>, shotIds: string[]) {
  const prompt_overrides:Record<string,string>={},reference_image_ids:Record<string,string[]>={};
  const motion_reference_overrides:Record<string,{video_id:string;expected_revision:number;source_sha256:string;trim_start_seconds:number;trim_end_seconds:number;authorized_for_motion:boolean;reference_role:VideoReferenceRole}[]>={};
  for(const id of shotIds){
    const draft=drafts[id];if(!draft)continue;
    if(draft.prompt.trim())prompt_overrides[id]=draft.prompt.trim();
    if(draft.imageSelectionExplicit)reference_image_ids[id]=draft.images.map(image=>image.id);
    if(draft.videos.length){
      motion_reference_overrides[id]=draft.videos.map(video=>{
        const error=motionInputError(video);if(error)throw new Error(error);
        return {video_id:video.asset.id,expected_revision:video.asset.revision,source_sha256:video.asset.sha256,
          trim_start_seconds:Number(video.start),trim_end_seconds:Number(video.end),authorized_for_motion:true,reference_role:video.role};
      });
    }
  }
  return {
    ...(Object.keys(prompt_overrides).length?{prompt_overrides}:{}),
    ...(Object.keys(reference_image_ids).length?{reference_image_ids}:{}),
    ...(Object.keys(motion_reference_overrides).length?{motion_reference_overrides}:{}),
  };
}

export function ShotVideoInputs({projectId,shot,draft,onChange,onBusyChange,disabled}: {
  projectId:string;shot:StudioShot;draft:ShotVideoInputDraft;onChange:(value:ShotVideoInputDraft)=>void;
  onBusyChange:(value:boolean)=>void;disabled:boolean;
}) {
  const [imageFile,setImageFile]=useState<File|null>(null),[videoFile,setVideoFile]=useState<File|null>(null);
  const [imageConfirmed,setImageConfirmed]=useState(false),[videoConfirmed,setVideoConfirmed]=useState(false);
  const [busy,setBusy]=useState(false),[error,setError]=useState('');
  const [imageVersion,setImageVersion]=useState(0),[videoVersion,setVideoVersion]=useState(0);
  const locked=busy||disabled;

  async function upload(work:()=>Promise<void>) {
    setBusy(true);onBusyChange(true);setError('');
    try{await work();}catch(cause){setError(String(cause));}
    finally{setBusy(false);onBusyChange(false);}
  }

  async function addImage(file:File) {
    const form=new FormData();form.set('file',file);
    form.set('payload',JSON.stringify({kind:'keyframe',shot_id:shot.id}));
    const url=`/api/projects/${encodeURIComponent(projectId)}/production-visuals/upload`;
    const image=await request<VisualResponse>(url,{method:'POST',body:form});
    if(!image.id||image.project_id!==projectId||image.shot_id!==shot.id)throw new Error('上传响应与当前项目或镜头不一致。');
    const approved=await request<VisualResponse>(`/api/production-visuals/${encodeURIComponent(image.id)}/review`,{
      method:'POST',body:JSON.stringify({status:'approved',category:'scene',reason:'用户已明确批准作为当前镜头的上传参考图',operator:'local-user'}),
    });
    if(approved.status!=='approved'||approved.id!==image.id||approved.project_id!==projectId||approved.shot_id!==shot.id||!approved.relative_path){
      throw new Error('参考图审核没有返回可用的本镜素材。');
    }
    onChange({...draft,imageSelectionExplicit:true,images:[...draft.images,{id:approved.id,name:file.name,relative_path:approved.relative_path}]});
    setImageFile(null);setImageConfirmed(false);setImageVersion(value=>value+1);
  }

  async function addVideo(file:File) {
    const form=new FormData();form.append('files',file);
    form.set('usage_scope','用户已确认可供当前镜头动作参考');
    const result=await request<VideoImport>('/api/reference-videos',{method:'POST',body:form});
    const uploadError=result.errors?.[0]?.error;
    if(uploadError)throw new Error(typeof uploadError==='string'?uploadError:uploadError.message||'参考视频导入失败。');
    let asset=result.items?.[0];
    if(!asset&&result.duplicate_ids?.length){
      const library=await request<ReferenceVideoOverview>('/api/reference-videos');
      asset=library.items.find(item=>item.id===result.duplicate_ids?.[0]);
    }
    const duration=asset?.metadata.duration_seconds;
    if(!asset||asset.state!=='active'||typeof duration!=='number'||!Number.isFinite(duration)||duration<2){
      throw new Error('没有可绑定的参考视频，动作片段至少需要 2 秒。');
    }
    if(draft.videos.some(video=>video.asset.id===asset.id))throw new Error('当前镜头已选择此视频。');
    const end=Math.min(duration,15,Math.max(2,shot.duration_seconds));
    onChange({...draft,videos:[...draft.videos,{asset,start:'0',end:String(end),role:'structure'}]});
    setVideoFile(null);setVideoConfirmed(false);setVideoVersion(value=>value+1);
  }

  return <section aria-label="本镜生成要求与可选素材" className="studio-panel">
    <FormField label="本镜生成要求（可选）" hint="留空使用已保存的镜头提示词。填写后作为本次镜头生成提示词，随新任务保存。">
      <textarea rows={4} value={draft.prompt} disabled={locked} onChange={e=>onChange({...draft,prompt:e.target.value})}/>
    </FormField>
    <fieldset>
      <legend>可选上传图片</legend>
      <p>图片保存为此镜头的项目素材。本次所选图片覆盖镜头关键画面，人物和产品继续使用已绑定的参考。</p>
      {draft.imageSelectionExplicit&&!draft.images.length?<p>本次已取消所选图片，将跳过镜头关键画面。</p>:null}
      {draft.images.length?<div className="reference-preview">{draft.images.map(image=><figure key={image.id}>
        <img src={api.mediaUrl(image.relative_path)} alt={image.name}/><figcaption>{image.name}</figcaption>
        <button type="button" className="button button-ghost button-small" disabled={locked} onClick={()=>onChange({...draft,images:draft.images.filter(item=>item.id!==image.id)})}>从本次选择移除</button>
      </figure>)}</div>:null}
      <FormField label="本镜参考图片"><input key={imageVersion} type="file" accept="image/png,image/jpeg,image/webp" disabled={locked||draft.images.length>=9} onChange={e=>{setImageFile(e.target.files?.[0]||null);setImageConfirmed(false);}}/></FormField>
      <label className="studio-check"><input type="checkbox" checked={imageConfirmed} disabled={locked||!imageFile} onChange={e=>setImageConfirmed(e.target.checked)}/>我已核对并批准使用这张图片作为本镜参考</label>
      <button type="button" className="button button-secondary button-small" disabled={locked||!imageFile||!imageConfirmed||draft.images.length>=9} onClick={()=>{if(imageFile&&imageConfirmed)void upload(()=>addImage(imageFile));}}>上传并绑定图片</button>
    </fieldset>
    <fieldset>
      <legend>可选上传动作参考视频</legend>
      <p>视频原件保存到参考视频库。逐条选择用途和 2–15 秒片段，仅提交所选片段的画面。人物身份沿用本镜独立绑定的参考素材。</p>
      {draft.videos.map((video,index)=><div key={video.asset.id} className="workspace-grid">
        <p>{video.asset.name}</p>
        <FormField label={`参考 ${index+1} 视频用途`}>
          <select value={video.role} disabled={locked} onChange={e=>{
            const role=e.target.value as VideoReferenceRole;
            onChange({...draft,videos:draft.videos.map((item,i)=>i===index?{...item,role}:item)});
          }}>
            <option value="structure">运镜与节奏</option>
            <option value="action">动作与姿态</option>
          </select>
        </FormField>
        {(['start','end'] as const).map((key)=><FormField key={key} label={`参考 ${index+1} ${key==='start'?'开始':'结束'}秒`}>
          <input type="number" min="0" step="0.001" value={video[key]} disabled={locked} onChange={e=>onChange({...draft,videos:draft.videos.map((item,i)=>i===index?{...item,[key]:e.target.value}:item)})}/>
        </FormField>)}
        {motionInputError(video)?<p role="alert">{motionInputError(video)}</p>:null}
        <button type="button" className="button button-ghost button-small" disabled={locked} onClick={()=>onChange({...draft,videos:draft.videos.filter(item=>item.asset.id!==video.asset.id)})}>从本次选择移除</button>
      </div>)}
      <FormField label="本镜动作参考视频文件"><input key={videoVersion} type="file" accept=".mp4,.mov,.mkv,.webm,video/*" disabled={locked||draft.videos.length>=3} onChange={e=>{setVideoFile(e.target.files?.[0]||null);setVideoConfirmed(false);}}/></FormField>
      <label className="studio-check"><input type="checkbox" checked={videoConfirmed} disabled={locked||!videoFile} onChange={e=>setVideoConfirmed(e.target.checked)}/>我确认有权将此视频用于本镜的动作参考</label>
      <button type="button" className="button button-secondary button-small" disabled={locked||!videoFile||!videoConfirmed||draft.videos.length>=3} onClick={()=>{if(videoFile&&videoConfirmed)void upload(()=>addVideo(videoFile));}}>上传并绑定参考视频</button>
    </fieldset>
    {busy?<p role="status">正在保存本镜参考素材…</p>:null}{error?<p role="alert">{error}</p>:null}
  </section>;
}
