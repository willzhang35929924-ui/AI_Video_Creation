// UI integration fixtures; actual model execution is verified separately.
import {render,screen,waitFor} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {MemoryRouter} from 'react-router-dom';
import {afterEach,expect,it,vi} from 'vitest';
import {request} from '../lib/api';
import {VideoProductionPage} from './VideoProductionPage';
vi.mock('../lib/api',async original=>({...await original<typeof import('../lib/api')>(),request:vi.fn()}));
const mocked=vi.mocked(request);
afterEach(()=>mocked.mockReset());
it('uses one Studio generation entry and saves the chosen quality before starting, retaining frozen task controls',async()=>{
  const user=userEvent.setup();
  const shots=[{id:'s1',order:1,title:'多人核对',duration_seconds:5,status:'approved',cast:[],decision:{}}];
  let settings={resolution:'720p',mode:'standard',aspect_ratio:'16:9',processing:'lanczos'};
  mocked.mockImplementation(async(url,init)=>{
    if(url==='/api/studio/projects')return [{id:'p1',name:'项目'}];
    if(url==='/api/studio/projects/p1')return {project:{id:'p1',name:'项目'},shots,characters:[],scenes:[],run:null};
    if(url.startsWith('/api/quality/profiles/')){if(init?.method==='PUT')settings=JSON.parse(String(init.body)).settings;return {scope:'project',target_id:'p1',revision:0,settings,plan:{method:'lanczos',supported:true}};}
    if(url==='/api/settings/ai-services?module=video_generation')return [];
    if(url==='/api/projects/p1/ai-services')return [];
    if(url==='/api/projects/p1/videos')return [{id:'v1',generation_job_id:'j1',relative_path:'fixtures/original.mp4',revision:1,metadata_json:{width:1024,height:576}},{id:'v2',generation_job_id:'j1',relative_path:'fixtures/postprocessed.mp4',revision:2,metadata_json:{quality_render_id:'q1',width:1280,height:720}}];
    if(url==='/api/projects/p1/jobs')return [{id:'j1',shot_id:'s1',provider:'comfyui_h3',status:'running',seed:17,parameters_json:{steps:20,width:1024,height:576},reference_assets_json:[]}];
    if(url==='/api/cinematic/rules')return {repair_categories:{},versions:{},sections:{}};
    if(url==='/api/projects/p1/production')return {project:{id:'p1'},rules:{},report:{estimate:{},counts:{},findings:[]},shots:[],designs:[],scenes:[],runs:[],visuals:[],videos:[]};
    if(url.endsWith('/start')||url.endsWith('/cancel'))return {};
    throw new Error(url);
  });
  render(<MemoryRouter initialEntries={['/video-production?project=p1&shot=s1']}><VideoProductionPage/></MemoryRouter>);
  await user.selectOptions(await screen.findByLabelText('制作输出规格'),'1080p');
  await user.selectOptions(screen.getByLabelText('制作质量模式'),'high');
  await user.click(screen.getByLabelText('已核对镜头范围、服务绑定与画质配置，开始缺失任务或增量续作。'));
  await user.click(screen.getByRole('button',{name:'开始统一视频制作'}));
  await waitFor(()=>expect(mocked.mock.calls.some(([url])=>url.endsWith('/start'))).toBe(true));
  const saveIndex=mocked.mock.calls.findIndex(([url,init])=>url==='/api/quality/profiles/project/p1'&&init?.method==='PUT');
  const startIndex=mocked.mock.calls.findIndex(([url])=>url.endsWith('/start'));
  expect(saveIndex).toBeGreaterThan(-1);expect(startIndex).toBeGreaterThan(saveIndex);
  expect(JSON.parse(String(mocked.mock.calls[startIndex][1]?.body))).toEqual({quality:{resolution:'1080p',mode:'high',aspect_ratio:'16:9',processing:'lanczos'},shot_ids:['s1'],allow_cloud_costs:false,allow_paid_fallback:false,automatic_draft_assembly:false});
  expect(screen.queryByRole('button',{name:'生成此镜头'})).not.toBeInTheDocument();
  expect(screen.getByText(/冻结 seed 17/)).toHaveTextContent('采样 20 步');
  expect(screen.getAllByTestId('production-history-video').map(node=>node.getAttribute('src'))).toEqual(['/api/media/fixtures/original.mp4','/api/media/fixtures/postprocessed.mp4']);
  expect(screen.getByText('此处仅播放和下载，不更改审片决定，也不自动采用新结果。')).toBeInTheDocument();
  expect(mocked.mock.calls.some(([url,init])=>url.endsWith('/decision')&&init?.method==='POST')).toBe(false);
  await user.click(screen.getByRole('button',{name:'取消此实际任务'}));
  await waitFor(()=>expect(mocked.mock.calls.some(([url])=>url==='/api/jobs/j1/cancel')).toBe(true));
  expect(mocked.mock.calls.filter(([url,init])=>init?.method==='POST'&&(url.includes('/generate')||url.endsWith('/productions')))).toHaveLength(0);
});


it('configures one explicit product plan for a new run without selecting other photos or modifying the paused run',async()=>{
  const user=userEvent.setup();
  const shots=[{id:'s1',order:1,title:'产品展示',duration_seconds:5,status:'draft',cast:[],decision:{}}];
  const settings={resolution:'720p',mode:'standard',aspect_ratio:'16:9',processing:'lanczos'};
  mocked.mockImplementation(async(url,init)=>{
    if(url==='/api/studio/projects')return [{id:'p1',name:'项目'}];
    if(url==='/api/studio/projects/p1')return {project:{id:'p1',name:'项目'},shots,characters:[],scenes:[],run:{status:'paused'}};
    if(url.startsWith('/api/quality/profiles/'))return {scope:'project',target_id:'p1',revision:0,settings,plan:{method:'lanczos',supported:true}};
    if(url==='/api/product-library')return {items:[{id:'p',name:'实际产品',state:'active',images:[
      {id:'en',filename:'english.jpg',media_url:'/api/media/en.jpg',thumbnail_url:'/api/media/en.jpg'},
      {id:'tray',filename:'tray.jpg',media_url:'/api/media/tray.jpg',thumbnail_url:'/api/media/tray.jpg'},
      {id:'other',filename:'other.jpg',media_url:'/api/media/other.jpg',thumbnail_url:'/api/media/other.jpg'}]}]};
    if(url==='/api/settings/ai-services?module=video_generation'||url==='/api/projects/p1/ai-services'||url==='/api/projects/p1/videos'||url==='/api/projects/p1/jobs')return [];
    if(url==='/api/cinematic/rules')return {repair_categories:{},versions:{},sections:{}};
    if(url==='/api/projects/p1/production')return {project:{id:'p1'},rules:{},report:{estimate:{},counts:{},findings:[]},shots:[],designs:[],scenes:[],runs:[],visuals:[],videos:[]};
    if(url.endsWith('/start'))return {};
    throw new Error(url);
  });
  render(<MemoryRouter initialEntries={['/video-production?project=p1&shot=s1']}><VideoProductionPage/></MemoryRouter>);
  await user.click(await screen.findByLabelText('本次按所选产品原图自动配置相关镜头'));
  expect(screen.getByRole('button',{name:'继续当前编排'})).toBeDisabled();
  expect(screen.getByRole('button',{name:'按当前设置新建编排'})).toBeDisabled();
  await user.selectOptions(await screen.findByLabelText('本次产品'),'p');
  expect(screen.getByLabelText('english.jpg')).not.toBeChecked();
  expect(screen.getByLabelText('other.jpg')).not.toBeChecked();
  await user.click(screen.getByLabelText('english.jpg'));
  await user.click(screen.getByLabelText('tray.jpg'));
  await user.type(screen.getByLabelText('选择说明'),'英文盒面与同款托盘');
  await user.click(screen.getByLabelText('已核对镜头范围、服务绑定与画质配置，开始缺失任务或增量续作。'));
  await user.click(screen.getByRole('button',{name:'按当前设置新建编排'}));
  await waitFor(()=>expect(mocked.mock.calls.some(([url])=>url.endsWith('/start'))).toBe(true));
  const sent=mocked.mock.calls.find(([url])=>url.endsWith('/start'))!;
  expect(JSON.parse(String(sent[1]?.body)).product_reference_plan).toEqual({product_id:'p',image_ids:['en','tray'],selection_reason:'英文盒面与同款托盘'});
  expect(mocked.mock.calls.some(([url])=>url.endsWith('/resume'))).toBe(false);
});
