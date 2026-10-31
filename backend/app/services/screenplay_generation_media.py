"""Bounded real source uploads and FFmpeg video observations."""
import base64
import hashlib
from io import BytesIO
import json
from pathlib import Path
import subprocess
from PIL import Image, ImageOps, UnidentifiedImageError
from app.core.errors import AppError
from app.core.paths import safe_media_path
from app.models import ManagedFile
from app.models.entities import new_id
from app.models.screenplay_generation import ScreenplayInputAsset

LIMITS={'text':{'max_characters':40000,'formats':[]},
    'image':{'max_bytes':10*1024**2,'max_pixels':20_000_000,'formats':['.png','.jpg','.jpeg','.webp']},
    'video':{'max_bytes':100*1024**2,'max_duration_seconds':60,'max_frames':8,'formats':['.mp4','.mov','.mkv','.webm']}}

def sha(data):return hashlib.sha256(data).hexdigest()

def run(arguments, timeout=120):
    result=subprocess.run([str(a) for a in arguments],capture_output=True,shell=False,timeout=timeout,
        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if result.returncode:raise AppError(422,'screenplay_media_decode','素材无法完整读取或解码')
    return result.stdout

def image_data(path,max_side=768):
    with Image.open(path) as picture:
        picture.load();picture=ImageOps.exif_transpose(picture).convert('RGB');picture.thumbnail((max_side,max_side))
        out=BytesIO();picture.save(out,format='JPEG',quality=90)
    data=out.getvalue()
    return {'type':'image_url','image_url':{'url':'data:image/jpeg;base64,'+base64.b64encode(data).decode('ascii')}},sha(data)

def upload_asset(session,settings,input_mode,filename,data,source_note='',*,commit=True):
    if input_mode not in {'image','video'}:raise AppError(422,'screenplay_asset_mode','只有图片和视频模式需要上传素材')
    limits=LIMITS[input_mode];extension=Path(filename).suffix.lower()
    if extension not in limits['formats']:raise AppError(415,'screenplay_asset_format','不支持该模式的文件格式')
    if not data or len(data)>limits['max_bytes']:raise AppError(413,'screenplay_asset_size','素材为空或超过当前模式大小限制')
    metadata={'bytes':len(data),'source_note':source_note}
    if input_mode=='image':
        try:
            with Image.open(BytesIO(data)) as picture:
                if picture.format not in {'PNG','JPEG','WEBP'} or picture.width*picture.height>limits['max_pixels'] or min(picture.size)<32:
                    raise ValueError('Image signature/dimensions invalid')
                expected={'PNG':{'.png'},'JPEG':{'.jpg','.jpeg'},'WEBP':{'.webp'}}[picture.format]
                if extension not in expected:raise ValueError('Image extension and actual format differ')
                metadata.update(width=picture.width,height=picture.height,format=picture.format);picture.verify()
            with Image.open(BytesIO(data)) as picture:picture.load()
        except (OSError,ValueError,UnidentifiedImageError,Image.DecompressionBombError) as exc:
            raise AppError(422,'screenplay_image_decode','图片签名、尺寸或内容无效') from exc
    asset=ScreenplayInputAsset(id=new_id('spinput'),input_mode=input_mode,filename=filename,relative_path='',sha256=sha(data),metadata_json=metadata,source_note=source_note)
    asset.relative_path=f'screenplay_sources/{asset.id}/original{extension}'
    path=safe_media_path(settings.media_root,asset.relative_path);path.parent.mkdir(parents=True,exist_ok=True)
    if not commit:session.info.setdefault('product_upload_paths',[]).append(asset.relative_path)
    with path.open('xb') as stream:stream.write(data)
    try:
        if input_mode=='video':
            raw=json.loads(run([settings.ffprobe_path,'-v','error','-show_streams','-show_format','-of','json',path],30))
            video=next((s for s in raw['streams'] if s.get('codec_type')=='video'),None)
            duration=float(raw.get('format',{}).get('duration',0))
            if not video or not 0<duration<=60 or int(video.get('width',0))*int(video.get('height',0))>20_000_000:
                raise AppError(422,'screenplay_video_limits','视频需要有效画面，时长不超过60秒，像素不超过2000万')
            run([settings.ffmpeg_path,'-nostdin','-v','error','-xerror','-i',path,'-f','null','-'],180)
            metadata.update(duration_seconds=duration,width=int(video['width']),height=int(video['height']),
                has_audio=any(s.get('codec_type')=='audio' for s in raw['streams']),full_decode=True)
            asset.metadata_json=metadata
        session.add(asset);session.flush()
        session.add(ManagedFile(relative_path=asset.relative_path,state='active',ownership_json=[{'table':'screenplay_input_assets','id':asset.id}],metadata_json={'sha256':asset.sha256,'verified_owned':True}))
        if commit:session.commit()
    except Exception:
        # A failed upload never enters any inference job. Preserve nonempty source
        # bytes as a local orphan for diagnosis rather than deleting shared data.
        session.rollback();raise
    return asset

def asset_dto(asset):
    return {'id':asset.id,'input_mode':asset.input_mode,'filename':asset.filename,'relative_path':asset.relative_path,
        'media_url':'/api/media/'+asset.relative_path,'sha256':asset.sha256,'metadata':asset.metadata_json,'source_note':asset.source_note}

def prepare_source(settings,job,asset):
    receipt={'input_mode':job.input_mode,'source_asset_id':asset.id if asset else None,'frames':[]}
    content=[]
    if job.input_mode=='text':return content,receipt
    path=safe_media_path(settings.media_root,asset.relative_path,must_exist=True)
    if sha(path.read_bytes())!=asset.sha256:raise AppError(409,'screenplay_source_changed','原始输入素材已发生变化')
    receipt.update(source_sha256=asset.sha256,filename=asset.filename,source_note=asset.source_note)
    if job.input_mode=='image':
        part,digest=image_data(path);content.append(part);receipt['image_request_sha256']=digest
        return content,receipt
    directory=safe_media_path(settings.media_root,f'screenplay_jobs/{job.id}/source_frames');directory.mkdir(parents=True,exist_ok=True)
    duration=float(asset.metadata_json['duration_seconds']);timestamps=[round(max(0,duration-.08)*i/7,3) for i in range(8)]
    for index,timestamp in enumerate(timestamps):
        frame=directory/f'frame_{index:02d}.jpg'
        run([settings.ffmpeg_path,'-nostdin','-v','error','-n','-ss',timestamp,'-i',path,'-frames:v','1','-vf','scale=768:768:force_original_aspect_ratio=decrease',frame])
        if not frame.is_file():raise AppError(422,'screenplay_frame_missing','视频抽帧没有生成实际图像')
        part,digest=image_data(frame)
        content.extend([{'type':'text','text':f'视频时间戳 {timestamp:.3f} 秒'},part])
        receipt['frames'].append({'timestamp_seconds':timestamp,'relative_path':frame.relative_to(settings.media_root).as_posix(),'sha256':sha(frame.read_bytes()),'request_image_sha256':digest})
    if asset.metadata_json.get('has_audio'):
        audio=directory/'source_audio.wav'
        run([settings.ffmpeg_path,'-nostdin','-v','error','-n','-i',path,'-vn','-ar','16000','-ac','1','-c:a','pcm_s16le',audio])
        receipt.update(audio_relative_path=audio.relative_to(settings.media_root).as_posix(),audio_sha256=sha(audio.read_bytes()),audio_transcription='not_configured')
    else:receipt['audio_transcription']='no_audio_stream'
    return content,receipt
