// Page/controller contracts only. HTTP responses are controlled; no project or GPU is used.
import {act,cleanup,render,screen,waitFor} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {MemoryRouter} from 'react-router-dom';
import {afterEach,beforeEach,expect,it,vi} from 'vitest';
import {ApiError,request} from '../lib/api';
import {useStudio} from '../lib/studio-ui';
import {StudioPage} from './StudioPage';

vi.mock('../lib/api',async original=>({...await original<typeof import('../lib/api')>(),request:vi.fn()}));
vi.mock('../lib/studio-ui',()=>({useStudio:vi.fn(),recordText:(value:unknown)=>String(value||''),shotTime:()=>'',studioAssetPath:()=>'',studioVideoDuration:()=>0,
  StudioStatus:({status}:{status?:string})=><span>{status}</span>,StudioProjectPicker:({disabled}:{disabled:boolean})=><select aria-label="当前项目" disabled={disabled}><option>测试项目</option></select>}));
vi.mock('../components/ProjectVoicePreparation',async()=>{const React=await import('react');return {ProjectVoicePreparation:({onStateChange}:{onStateChange:(value:any)=>void})=>{React.useEffect(()=>onStateChange({ready:true,loading:false,busy:false,dirty:false,blockingReasons:[]}),[onStateChange]);return null;}};});
vi.mock('../components/ProjectProductionModels',()=>({ProjectProductionModels:()=>null}));
vi.mock('../components/ProjectAudioPolicy',()=>({ProjectAudioPolicy:()=>null}));
vi.mock('../components/ProjectCharacterReferences',()=>({ProjectCharacterReferences:()=>null}));
vi.mock('../components/ProjectProductPreparation',()=>({ProjectProductPreparation:()=>null}));
vi.mock('../components/SeedVrPreparation',()=>({SeedVrPreparation:()=>null}));
const mocked=vi.mocked(request),studio=vi.mocked(useStudio);
let run:any,requiresNewRun:boolean;
const framing=()=>({project_id:'p1',requires_new_run:requiresNewRun,supported_aspect_ratios:['16:9','9:16','1:1'],shots:[{id:'s1',order:1,title:'产品展示',aspect_ratio:'16:9',revision:4,editable:run?.status!=='waiting_review',block_reason:null}]});
beforeEach(()=>{
  run=null;requiresNewRun=false;
  studio.mockImplementation(()=>({projectId:'p1',projects:[{id:'p1',name:'测试项目'}],loading:false,error:'',listLoading:false,
    data:{project:{id:'p1',name:'测试项目'},characters:[],scenes:[],shots:[{id:'s1',order:1,title:'产品展示',cast:[],duration_seconds:5,status:'draft',decision:{}}],run},
    refreshList:vi.fn(),refresh:vi.fn(),refreshAfterChange:vi.fn(),selectProject:vi.fn()} as any));
  mocked.mockImplementation(async(url,init)=>{
    if(url==='/api/studio/projects/p1/shot-framing'){
      if(init?.method==='PUT'){const ratio=JSON.parse(String(init.body)).aspect_ratio;return {...framing(),shots:framing().shots.map(shot=>({...shot,aspect_ratio:ratio,revision:5})),updated_shot_ids:['s1']};}
      return framing();
    }
    if(url==='/api/quality/capabilities')return {};
    if(url.startsWith('/api/quality/profiles/')){const pieces=url.split('/');return {scope:pieces[4],target_id:pieces[5],revision:init?.method==='PUT'?1:0,settings:init?.method==='PUT'?JSON.parse(String(init.body)).settings:{resolution:'1080p',mode:'standard',aspect_ratio:'16:9',processing:'lanczos'},plan:{method:'lanczos',supported:true}};}
    if(url.includes('/voices?'))return {ready_to_start:true,blockers:[]};
    if(url.endsWith('/start')||url.endsWith('/pause'))return {};
    throw new Error(url);
  });
});
afterEach(()=>{cleanup();mocked.mockReset();studio.mockReset();});

it('Studio blocks starting and project switching while framing is dirty, then syncs only the output aspect draft',async()=>{
  const user=userEvent.setup();render(<MemoryRouter><StudioPage/></MemoryRouter>);
  const ratio=await screen.findByLabelText('选中镜头的实际画幅');
  await waitFor(()=>expect(screen.getByRole('button',{name:'开始／增量续作'})).toBeEnabled());
  await user.selectOptions(ratio,'9:16');
  expect(screen.getByRole('button',{name:'开始／增量续作'})).toBeDisabled();
  expect(screen.getByLabelText('当前项目')).toBeDisabled();
  await user.click(screen.getByRole('button',{name:'保存选中镜头画幅'}));
  await waitFor(()=>expect(screen.getByRole('button',{name:'开始／增量续作'})).toBeEnabled());
  expect(mocked.mock.calls.some(([url])=>url.endsWith('/start'))).toBe(false);
  await user.click(screen.getByRole('button',{name:'开始／增量续作'}));
  await waitFor(()=>expect(mocked.mock.calls.some(([url])=>url.endsWith('/start'))).toBe(true));
  const qualitySave=mocked.mock.calls.find(([url,init])=>url==='/api/quality/profiles/project/p1'&&init?.method==='PUT')!;
  expect(JSON.parse(String(qualitySave[1]?.body)).settings).toEqual({resolution:'1080p',mode:'standard',aspect_ratio:'9:16',processing:'lanczos'});
});

it.each([['studio',StudioPage,'继续制作','开始／增量续作','paused'],['studio',StudioPage,'继续制作','开始／增量续作','exceptions']] as const)('%s offers a new run after persisted framing changes instead of resuming an old snapshot (%s)',async(_name,Page,resume,start,status)=>{
  run={id:'old-run',status,nodes_json:[],snapshot_json:{}};requiresNewRun=true;
  render(<MemoryRouter><Page/></MemoryRouter>);
  await screen.findByText(/旧编排不能通过「继续制作」应用新画幅/);
  expect(screen.queryByRole('button',{name:resume})).toBeNull();
  expect(screen.getByRole('button',{name:start})).toBeVisible();
});

it.each([['studio',StudioPage,'暂停后续任务']] as const)('%s can pause a waiting-review run so framing edits are not deadlocked',async(_name,Page,label)=>{
  run={id:'waiting-run',status:'waiting_review',nodes_json:[],snapshot_json:{}};
  const user=userEvent.setup();render(<MemoryRouter><Page/></MemoryRouter>);
  await user.click(await screen.findByRole('button',{name:label}));
  await waitFor(()=>expect(mocked.mock.calls.some(([url])=>url==='/api/studio/projects/p1/pause')).toBe(true));
});

it.each([['studio',StudioPage,'开始／增量续作',false],['studio',StudioPage,'继续制作',true]] as const)('%s blocks %s when framing cannot be read and recovers after refresh',async(name,Page,action,paused)=>{
  if(paused)run={id:'paused-run',status:'paused',nodes_json:[],snapshot_json:{}};
  const original=mocked.getMockImplementation()!;let failed=true;
  mocked.mockImplementation(async(url,init)=>{if(url.endsWith('/shot-framing')&&failed)throw new ApiError('画幅接口暂不可用',503);return original(url,init);});
  const user=userEvent.setup();render(<MemoryRouter><Page/></MemoryRouter>);
  await screen.findByText(/无法读取实际镜头画幅/);
  expect(screen.getByRole('button',{name:action})).toBeDisabled();
  expect(screen.getByRole('button',{name:'重新读取镜头画幅'})).toBeEnabled();
  if(name==='studio')expect(screen.getByLabelText('剧本文档（.docx）')).toBeEnabled();
  failed=false;await user.click(screen.getByRole('button',{name:'重新读取镜头画幅'}));
  await waitFor(()=>expect(screen.queryByText(/无法读取实际镜头画幅/)).toBeNull());
  await waitFor(()=>expect(screen.getByRole('button',{name:action})).toBeEnabled());
  expect(mocked.mock.calls.some(([url])=>url.endsWith('/start')||url.endsWith('/resume'))).toBe(false);
});

it('does not let an old project read make the new project ready',async()=>{
  const original=mocked.getMockImplementation()!,baseStudio=studio.getMockImplementation()!;
  let finish!:(value:any)=>void;const oldRead=new Promise(resolve=>{finish=resolve;});
  mocked.mockImplementation(async(url,init)=>{if(url==='/api/studio/projects/p1/shot-framing')return oldRead;if(url==='/api/studio/projects/p2/shot-framing')throw new ApiError('新项目读取失败',503);return original(url,init);});
  const {rerender}=render(<MemoryRouter><StudioPage/></MemoryRouter>);
  await screen.findByText('正在读取实际镜头画幅…');
  studio.mockImplementation(()=>{const value=baseStudio();return {...value,projectId:'p2',data:{...value.data!,project:{id:'p2',name:'新项目'}}};});
  rerender(<MemoryRouter><StudioPage/></MemoryRouter>);
  await screen.findByText(/新项目读取失败/);
  await act(async()=>{finish(framing());await oldRead;});
  expect(screen.getByRole('button',{name:'开始／增量续作'})).toBeDisabled();
});
