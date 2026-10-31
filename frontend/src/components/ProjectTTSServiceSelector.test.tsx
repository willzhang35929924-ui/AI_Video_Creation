// Isolated UI/HTTP contracts, not evidence of actual speech generation.
import {act,cleanup,render,screen,waitFor} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {afterEach,beforeEach,expect,it,vi} from 'vitest';
import {ApiError,request} from '../lib/api';
import type {RouteProfile} from '../lib/model-services-contract';
import type {AIService} from '../pages/AIServicesPage';
import {ProjectTTSServiceSelector} from './ProjectTTSServiceSelector';
import type {ProjectTTSServiceState} from './ProjectTTSServiceSelector';

vi.mock('../lib/api',async original=>({...await original<typeof import('../lib/api')>(),request:vi.fn()}));
const mocked=vi.mocked(request),routeUrl='/api/model-services/routes/project/p1/dialogue_generation';
const profile=(revision=4):RouteProfile=>({scope:'project',target_id:'p1',slot:'dialogue_generation',revision,settings:{mode:'local_only',choices:[{service_id:'cosy',model_id:'cosyvoice3-0.5b-2512'}],parameters:{speed:1.1},allow_fallback:false}});
const service=(id:string,family:string,model:string):AIService=>({id,module:'dialogue_generation',provider:'local_runtime',name:id==='index'?'IndexTTS 2.5':'CosyVoice3',default_model:model,enabled:true,is_default:id==='cosy',settings:{runtime_family:family},base_url:'',models:[],capabilities:{},has_api_key:false,api_key_mask:'',revision:1,last_success_at:null,last_test:{}});
let data:RouteProfile;
let items:AIService[];
const puts=()=>mocked.mock.calls.filter(([,init])=>init?.method==='PUT');
beforeEach(()=>{
  data=profile();items=[service('cosy','cosyvoice3','cosyvoice3-0.5b-2512'),service('index','indextts25','indextts-2.5')];
  mocked.mockImplementation(async(url,init)=>{
    if(url==='/api/settings/ai-services')return structuredClone(items);
    if(url===routeUrl&&!init?.method)return structuredClone(data);
    if(url===routeUrl&&init?.method==='PUT'){
      const body=JSON.parse(String(init.body));data={...data,revision:data.revision+1,settings:body.settings};return structuredClone(data);
    }
    throw new Error('Unexpected selector contract URL: '+url);
  });
});
afterEach(()=>{cleanup();mocked.mockReset();vi.restoreAllMocks();});

it('switches the project between its registered local models only after an explicit save',async()=>{
  items.push({...service('disabled','indextts25','indextts-2.5'),enabled:false});
  items.push({...service('cloud','cosyvoice3','cloud-model'),provider:'openai_tts'});
  const user=userEvent.setup(),states:ProjectTTSServiceState[]=[],saved=vi.fn();
  render(<ProjectTTSServiceSelector projectId="p1" onStateChange={s=>states.push(s)} onSaved={saved}/>);
  const select=await screen.findByLabelText('本项目配音模型');
  await waitFor(()=>expect(select).toHaveValue('cosy'));
  expect(screen.getAllByRole('option')).toHaveLength(3);
  await user.selectOptions(select,'index');
  expect(states.at(-1)?.dirty).toBe(true);expect(puts()).toHaveLength(0);
  await user.click(screen.getByRole('button',{name:'保存配音模型'}));
  await waitFor(()=>expect(saved).toHaveBeenCalledTimes(1));
  expect(puts()).toHaveLength(1);
  expect(puts()[0][0]).toBe(routeUrl);
  expect(JSON.parse(String(puts()[0][1]?.body))).toEqual({expected_revision:4,settings:{mode:'local_only',choices:[{service_id:'index',model_id:'indextts-2.5'}],parameters:{},quality_mode:'standard',allow_fallback:false,allow_paid_fallback:false,max_retries:0}});
  expect(states.at(-1)).toEqual({dirty:false,busy:false});
  expect(mocked.mock.calls.some(([url])=>url.includes('/set-default'))).toBe(false);
  expect(screen.getByText(/镜头单独指定的配音服务继续生效/)).toBeVisible();
});

it('preserves a multi-model policy until the user chooses a replacement project default',async()=>{
  data={...profile(),settings:{mode:'local_first',choices:[{service_id:'cosy'},{service_id:'index'}],parameters:{speed:.9},allow_fallback:true}};
  const user=userEvent.setup();render(<ProjectTTSServiceSelector projectId="p1"/>);
  const select=await screen.findByLabelText('本项目配音模型');
  await waitFor(()=>expect(select).toBeEnabled());
  expect(select).toHaveValue('');expect(screen.getByRole('option',{name:'保留当前配音配置／策略'})).toBeInTheDocument();
  expect(screen.getByRole('button',{name:'保存配音模型'})).toBeDisabled();expect(puts()).toHaveLength(0);
  await user.selectOptions(select,'index');await user.click(screen.getByRole('button',{name:'取消切换'}));
  expect(select).toHaveValue('');expect(puts()).toHaveLength(0);
});

it('uses the originally read revision and requires an explicit refresh after a conflict',async()=>{
  const original=mocked.getMockImplementation()!;
  mocked.mockImplementation(async(url,init)=>{if(init?.method==='PUT')throw new ApiError('changed',409,'route_revision_changed');return original(url,init);});
  const user=userEvent.setup();render(<ProjectTTSServiceSelector projectId="p1"/>);
  await waitFor(()=>expect(screen.getByLabelText('本项目配音模型')).toHaveValue('cosy'));
  await user.selectOptions(screen.getByLabelText('本项目配音模型'),'index');
  data={...profile(9),settings:{choices:[{service_id:'index',model_id:'indextts-2.5'}]}};
  const readsBefore=mocked.mock.calls.filter(([url,init])=>url===routeUrl&&!init?.method).length;
  await user.click(screen.getByRole('button',{name:'保存配音模型'}));
  expect(await screen.findByRole('alert')).toHaveTextContent('已在其他窗口变化');
  expect(JSON.parse(String(puts()[0][1]?.body)).expected_revision).toBe(4);
  expect(mocked.mock.calls.filter(([url,init])=>url===routeUrl&&!init?.method)).toHaveLength(readsBefore);
  expect(screen.getByLabelText('本项目配音模型')).toHaveValue('index');
  expect(screen.getByRole('button',{name:'保存配音模型'})).toBeDisabled();
  await user.click(screen.getByRole('button',{name:'放弃本次切换并刷新'}));
  await waitFor(()=>expect(screen.getByLabelText('本项目配音模型')).toHaveValue('index'));
  expect(puts()).toHaveLength(1);
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
});

it('does not call its parent when a saved response arrives after unmount',async()=>{
  let finish!:(value:RouteProfile)=>void;
  const pending=new Promise<RouteProfile>(resolve=>{finish=resolve;}),original=mocked.getMockImplementation()!;
  mocked.mockImplementation(async(url,init)=>init?.method==='PUT'?pending:original(url,init));
  const user=userEvent.setup(),saved=vi.fn();const view=render(<ProjectTTSServiceSelector projectId="p1" onSaved={saved}/>);
  await waitFor(()=>expect(screen.getByLabelText('本项目配音模型')).toHaveValue('cosy'));
  await user.selectOptions(screen.getByLabelText('本项目配音模型'),'index');await user.click(screen.getByRole('button',{name:'保存配音模型'}));
  expect(screen.getByLabelText('本项目配音模型')).toBeDisabled();view.unmount();
  await act(async()=>{finish({...profile(5),settings:{choices:[{service_id:'index'}]}});await pending;});
  expect(saved).not.toHaveBeenCalled();
});

it('shows a read failure without issuing a save or generation request',async()=>{
  mocked.mockRejectedValue(new Error('offline'));
  render(<ProjectTTSServiceSelector projectId="p1"/>);
  expect(await screen.findByRole('alert')).toHaveTextContent('offline');
  expect(screen.getByLabelText('本项目配音模型')).toBeDisabled();
  expect(puts()).toHaveLength(0);
  expect(mocked.mock.calls.every(([,init])=>!init?.method)).toBe(true);
});

it('keeps an unsaved choice across same-project refreshes until it is explicitly discarded',async()=>{
  const user=userEvent.setup(),states:ProjectTTSServiceState[]=[];
  const view=render(<ProjectTTSServiceSelector projectId="p1" refreshKey={1} onStateChange={s=>states.push(s)}/>);
  await waitFor(()=>expect(screen.getByLabelText('本项目配音模型')).toHaveValue('cosy'));
  await user.selectOptions(screen.getByLabelText('本项目配音模型'),'index');
  const reads=mocked.mock.calls.filter(([url,init])=>url===routeUrl&&!init?.method).length;
  data=profile(9);
  view.rerender(<ProjectTTSServiceSelector projectId="p1" refreshKey={2} onStateChange={s=>states.push(s)}/>);
  await screen.findByText(/当前配音模型选择和保存操作已保留/);
  expect(screen.getByLabelText('本项目配音模型')).toHaveValue('index');
  expect(states.at(-1)).toEqual({dirty:true,busy:false,refreshPending:true});
  expect(mocked.mock.calls.filter(([url,init])=>url===routeUrl&&!init?.method)).toHaveLength(reads);
  await user.click(screen.getByRole('button',{name:'放弃本次切换并刷新'}));
  await waitFor(()=>expect(screen.getByLabelText('本项目配音模型')).toHaveValue('cosy'));
  await waitFor(()=>expect(states.at(-1)).toEqual({dirty:false,busy:false}));
  expect(mocked.mock.calls.filter(([url,init])=>url===routeUrl&&!init?.method)).toHaveLength(reads+1);
  expect(puts()).toHaveLength(0);
});

it('finishes an in-flight save before reading a same-project refresh',async()=>{
  let finish!:(value:RouteProfile)=>void;
  const pending=new Promise<RouteProfile>(resolve=>{finish=resolve;}),original=mocked.getMockImplementation()!;
  mocked.mockImplementation(async(url,init)=>init?.method==='PUT'?pending:original(url,init));
  const user=userEvent.setup(),states:ProjectTTSServiceState[]=[],saved=vi.fn();
  const view=render(<ProjectTTSServiceSelector projectId="p1" refreshKey={1} onStateChange={s=>states.push(s)} onSaved={saved}/>);
  await waitFor(()=>expect(screen.getByLabelText('本项目配音模型')).toHaveValue('cosy'));
  await user.selectOptions(screen.getByLabelText('本项目配音模型'),'index');
  await user.click(screen.getByRole('button',{name:'保存配音模型'}));
  const reads=mocked.mock.calls.filter(([url,init])=>url===routeUrl&&!init?.method).length;
  view.rerender(<ProjectTTSServiceSelector projectId="p1" refreshKey={2} onStateChange={s=>states.push(s)} onSaved={saved}/>);
  await waitFor(()=>expect(states.at(-1)).toEqual({dirty:true,busy:true,refreshPending:true}));
  expect(screen.getByLabelText('本项目配音模型')).toHaveValue('index');
  expect(screen.getByRole('button',{name:'正在保存配音模型…'})).toBeDisabled();
  expect(mocked.mock.calls.filter(([url,init])=>url===routeUrl&&!init?.method)).toHaveLength(reads);
  data={...profile(5),settings:{choices:[{service_id:'index',model_id:'indextts-2.5'}]}};
  await act(async()=>{finish(structuredClone(data));await pending;});
  await waitFor(()=>expect(saved).toHaveBeenCalledTimes(1));
  await waitFor(()=>expect(states.at(-1)).toEqual({dirty:false,busy:false}));
  expect(screen.getByLabelText('本项目配音模型')).toHaveValue('index');
  expect(mocked.mock.calls.filter(([url,init])=>url===routeUrl&&!init?.method)).toHaveLength(reads+1);
  expect(JSON.parse(String(puts()[0][1]?.body)).expected_revision).toBe(4);
});
