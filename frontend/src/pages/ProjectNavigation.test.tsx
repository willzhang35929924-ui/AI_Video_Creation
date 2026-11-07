// UI/controller regression only: controlled HTTP results, no real model or media execution.
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, useLocation, useNavigate } from 'react-router-dom';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import App from '../App';
import { api, ApiError, request } from '../lib/api';
import type { Project, StoryboardShot, VideoRevision } from '../lib/types';
import { ToastProvider } from '../components/ToastProvider';

vi.mock('../lib/api', async original => ({...await original<typeof import('../lib/api')>(),request:vi.fn()}));
vi.mock('../components/ShotWorkspace', () => ({ ShotWorkspace: () => null }));
const mocked=vi.mocked(request);
const projects=[{id:'prj_new',name:'最新项目'},{id:'prj_older',name:'较早项目'}] as Project[];
const video=(id:string):VideoRevision=>({id:`${id}-video`,projectId:id,shotId:`${id}-shot`,revision:1,
  relativePath:`${id}.mp4`,reviewStatus:'approved',metadata:{width:1080,height:1920,duration_seconds:5}} as VideoRevision);
const shot=(id:string):StoryboardShot=>({id:`${id}-shot`,projectId:id,order:1,sceneTitle:`${id} 分镜`,status:'draft',revision:1,
  aspectRatio:'9:16',durationSeconds:5,visualPrompt:'测试描述',narration:'',dialogue:'',negativePrompt:'',action:'',cameraMove:'',location:'',lighting:'',audioDirection:''} as StoryboardShot);
function Probe(){const location=useLocation(),navigate=useNavigate();return <><output aria-label="当前地址">{location.pathname}{location.search}{location.hash}</output><button onClick={()=>navigate(-1)}>浏览器后退</button><button onClick={()=>navigate(1)}>浏览器前进</button></>;}
function mount(url:string,entries=[url]){return render(<MemoryRouter initialEntries={entries} initialIndex={0}><ToastProvider><App/><Probe/></ToastProvider></MemoryRouter>);}
beforeEach(()=>{
  vi.spyOn(api,'getContextProviderHealth').mockResolvedValue({ok:true,connected:true,configured:true,provider:'comfyui_h3',mode:'real'});
  vi.spyOn(api,'listProjects').mockResolvedValue(projects);
  vi.spyOn(api,'listVideos').mockImplementation(async id=>{if(!id)throw new Error('Expected project-scoped video request');return [video(id)];});
  vi.spyOn(api,'getVideo').mockImplementation(async id=>video(id.replace(/-video$/,'')));
  vi.spyOn(api,'getScript').mockRejectedValue(new ApiError('尚无剧本记录',404));
  vi.spyOn(api,'listShots').mockImplementation(async id=>[shot(id)]);
  vi.spyOn(api,'listCharacters').mockResolvedValue([]);
  mocked.mockImplementation(async url=>{
    if(url==='/api/settings/ai-services?module=final_assembly'||/^\/api\/projects\/[^/]+\/assemblies$/.test(url))return [];
    throw new Error(`Unexpected non-model request ${url}`);
  });
});
afterEach(()=>{cleanup();mocked.mockReset();});

it.each([['/storyboard','脚本与分镜'],['/review','成片审核'],['/final','合成成片']])('%s keeps picker, refresh and sidebar on the selected older project',async(path)=>{
  const user=userEvent.setup();
  const fixture=mount(`${path}?project=prj_older&shot=old-shot&video=old-video&job=old-job&quality_render=old-render&qualityrender=old-render-2&view=compact`);
  await waitFor(()=>expect(screen.getByLabelText('当前项目')).toHaveValue('prj_older'));
  const reads=path==='/storyboard'?vi.mocked(api.listShots):vi.mocked(api.listVideos);
  await waitFor(()=>expect(reads.mock.calls.filter(([id])=>id==='prj_older')).toHaveLength(1));
  await user.selectOptions(screen.getByLabelText('当前项目'),'prj_new');
  await waitFor(()=>expect(screen.getByLabelText('当前项目')).toHaveValue('prj_new'));
  const changed=new URL(screen.getByLabelText('当前地址').textContent!,'http://localhost');
  expect(changed.searchParams.get('project')).toBe('prj_new');
  for(const key of ['shot','video','job','quality_render','qualityrender'])expect(changed.searchParams.has(key)).toBe(false);
  expect(changed.searchParams.get('view')).toBe('compact');
  await waitFor(()=>expect(reads.mock.calls.filter(([id])=>id==='prj_new')).toHaveLength(1));
  await user.selectOptions(screen.getByLabelText('当前项目'),'prj_older');
  await waitFor(()=>expect(screen.getByLabelText('当前项目')).toHaveValue('prj_older'));
  await waitFor(()=>expect(reads.mock.calls.filter(([id])=>id==='prj_older')).toHaveLength(2));
  for(const label of ['脚本与分镜','成片审核','合成成片']){
    const nav=within(screen.getByRole('navigation',{name:'主导航'}));
    expect(nav.getByRole('link',{name:label})).toHaveAttribute('href',expect.stringContaining('project=prj_older'));
    await user.click(nav.getByRole('link',{name:label}));
    await waitFor(()=>expect(screen.getByLabelText('当前项目')).toHaveValue('prj_older'));
  }
  const refreshUrl=screen.getByLabelText('当前地址').textContent!;
  fixture.unmount();
  mount(refreshUrl);
  await waitFor(()=>expect(screen.getByLabelText('当前项目')).toHaveValue('prj_older'));
  expect(screen.getByLabelText('当前地址')).toHaveTextContent('/final?project=prj_older');
});

it('does not restore an older pending Review response after the picker changes URL project',async()=>{
  let finish!:(videos:VideoRevision[])=>void;
  const old=new Promise<VideoRevision[]>(resolve=>{finish=resolve;});
  vi.mocked(api.listVideos).mockImplementation(async id=>{if(!id)throw new Error('Expected project-scoped video request');return id==='prj_older'?old:[video(id)];});
  const user=userEvent.setup();
  mount('/review?project=prj_older');
  await waitFor(()=>expect(screen.getByLabelText('当前项目')).toHaveValue('prj_older'));
  await user.selectOptions(screen.getByLabelText('当前项目'),'prj_new');
  await waitFor(()=>expect(screen.getByLabelText('当前项目')).toHaveValue('prj_new'));
  expect(await screen.findByLabelText('镜头版本 1 视频播放器')).toHaveAttribute('src',expect.stringContaining('prj_new.mp4'));
  await act(async()=>{finish([video('prj_older')]);await old;});
  expect(screen.getByLabelText('当前项目')).toHaveValue('prj_new');
  expect(screen.getByLabelText('镜头版本 1 视频播放器')).toHaveAttribute('src',expect.stringContaining('prj_new.mp4'));
  expect(within(screen.getByRole('navigation',{name:'主导航'})).getByRole('link',{name:'合成成片'})).toHaveAttribute('href','/final?project=prj_new');
});

it('does not restore older Storyboard details when a later selected project finishes first',async()=>{
  let finish!:(shots:StoryboardShot[])=>void;
  const old=new Promise<StoryboardShot[]>(resolve=>{finish=resolve;});
  vi.mocked(api.listShots).mockImplementation(async id=>id==='prj_new'?old:[shot(id)]);
  const user=userEvent.setup();mount('/storyboard?project=prj_older');
  await waitFor(()=>expect(screen.getByLabelText('场景标题')).toHaveValue('prj_older 分镜'));
  await user.selectOptions(screen.getByLabelText('当前项目'),'prj_new');
  await waitFor(()=>expect(screen.getByLabelText('当前项目')).toHaveValue('prj_new'));
  await user.selectOptions(screen.getByLabelText('当前项目'),'prj_older');
  await waitFor(()=>expect(screen.getByLabelText('场景标题')).toHaveValue('prj_older 分镜'));
  await act(async()=>{finish([shot('prj_new')]);await old;});
  expect(screen.getByLabelText('当前项目')).toHaveValue('prj_older');
  expect(screen.getByLabelText('场景标题')).toHaveValue('prj_older 分镜');
  expect(vi.mocked(api.listShots).mock.calls.filter(([id])=>id==='prj_new')).toHaveLength(1);
});

it('responds to browser forward and back project URLs without remounting Storyboard',async()=>{
  const old='/storyboard?project=prj_older',next='/storyboard?project=prj_new';
  const user=userEvent.setup();mount(old,[old,next]);
  await waitFor(()=>expect(screen.getByLabelText('场景标题')).toHaveValue('prj_older 分镜'));
  await user.click(screen.getByRole('button',{name:'浏览器前进'}));
  await waitFor(()=>expect(screen.getByLabelText('场景标题')).toHaveValue('prj_new 分镜'));
  expect(screen.getByLabelText('当前项目')).toHaveValue('prj_new');
  await user.click(screen.getByRole('button',{name:'浏览器后退'}));
  await waitFor(()=>expect(screen.getByLabelText('场景标题')).toHaveValue('prj_older 分镜'));
  expect(screen.getByLabelText('当前项目')).toHaveValue('prj_older');
});

it('keeps a project with no timelines empty when a previous project’s version read returns late',async()=>{
  let finish!:(renders:any[])=>void;
  const oldVersions=new Promise<any[]>(resolve=>{finish=resolve;});
  const assembly=(id:string)=>({id,project_id:'prj_older',name:id,revision:1,timeline_json:{clips:[],transitions:[],service_snapshot:{service_id:''},
    output:{width:1280,height:720,resolution_mode:'source',allow_downscale:false,fps:24,codec:'libx264',audio_policy:'preserve',fade_duration:.2}}});
  const fallback=mocked.getMockImplementation()!;
  mocked.mockImplementation(async(url,init)=>{
    if(url==='/api/projects/prj_older/assemblies')return [assembly('a1'),assembly('a2')];
    if(url==='/api/assemblies/a1/versions')return [];
    if(url==='/api/assemblies/a2/versions')return oldVersions;
    return fallback(url,init);
  });
  const user=userEvent.setup();mount('/final?project=prj_older');
  await user.selectOptions(await screen.findByLabelText('时间线'),'a2');
  await waitFor(()=>expect(mocked.mock.calls.some(([url])=>url==='/api/assemblies/a2/versions')).toBe(true));
  await user.selectOptions(screen.getByLabelText('当前项目'),'prj_new');
  await waitFor(()=>expect(screen.getByLabelText('当前项目')).toHaveValue('prj_new'));
  await screen.findByText('尚无渲染版本。');
  await act(async()=>{finish([{id:'private-old-render',version:1,kind:'render',status:'generated',media_url:'/api/media/private-old.mp4',metadata_json:{},snapshot_json:{duration:5}}]);await oldVersions;});
  expect(screen.getByText('尚无渲染版本。')).toBeInTheDocument();
  expect(screen.queryByText('private-old-render')).not.toBeInTheDocument();
  expect(screen.getByLabelText('时间线')).toHaveValue('');
});

it('continues polling a saved timeline with the same assembly ID until its render completes',async()=>{
  const base={id:'same-assembly',project_id:'prj_older',name:'同一时间线',revision:1,timeline_json:{
    clips:[{video_id:'prj_older-video',source:{width:1080,height:1920,duration:5}}],transitions:[],service_snapshot:{service_id:''},
    output:{width:1080,height:1920,resolution_mode:'source',allow_downscale:false,fps:24,codec:'libx264',audio_policy:'preserve',fade_duration:.2}}};
  const queued={id:'render-live',version:1,kind:'render',status:'queued',metadata_json:{progress:0},snapshot_json:{duration:5}};
  let rendered=false;
  const fallback=mocked.getMockImplementation()!;
  mocked.mockImplementation(async(url,init)=>{
    if(url==='/api/projects/prj_older/assemblies')return [base];
    if(url==='/api/assemblies/same-assembly'&&init?.method==='PATCH')return {...base,revision:2};
    if(url==='/api/assemblies/same-assembly/render'){rendered=true;return queued;}
    if(url==='/api/assemblies/same-assembly/versions')return rendered?[{...queued,status:'generated',media_url:'/api/media/current-final.mp4'}]:[];
    return fallback(url,init);
  });
  const user=userEvent.setup();mount('/final?project=prj_older');
  await user.click(await screen.findByRole('button',{name:'保存时间线'}));
  await waitFor(()=>expect(mocked.mock.calls.some(([url,init])=>url==='/api/assemblies/same-assembly'&&init?.method==='PATCH')).toBe(true));
  const render=screen.getByRole('button',{name:/渲染最终 MP4/});
  await waitFor(()=>expect(render).toBeEnabled());await user.click(render);
  await screen.findByText('render-live');
  await waitFor(()=>expect(document.querySelector('[data-render-id="render-live"] video')).toHaveAttribute('src','/api/media/current-final.mp4'),{timeout:5000});
});
