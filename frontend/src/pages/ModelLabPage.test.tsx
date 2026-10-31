// UI contract fixtures; no generated fixture represents a real model test.
import {render,screen,waitFor} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {MemoryRouter} from 'react-router-dom';
import {afterEach,expect,it,vi} from 'vitest';
import {request} from '../lib/api';
import {ModelLabPage} from './ModelLabPage';
vi.mock('../lib/api',async original=>({...await original<typeof import('../lib/api')>(),request:vi.fn()}));
const mocked=vi.mocked(request);
afterEach(()=>mocked.mockReset());
const model=(id:string,name:string,verified:boolean,kind='video')=>({id,name,kind,path:`E:/actual/${id}.safetensors`,model_id:id,service_id:'svc',fingerprint:id,component_status:'complete',load_status:verified?'verified':'not_tested',eligible_for_comparison:verified,diagnostics:[],components:[],metadata:{},evidence:{}});
it('keeps untested and language models out of normal comparison and never writes production bindings when selecting cases',async()=>{
  const user=userEvent.setup();
  mocked.mockImplementation(async(url,init)=>{
    if(url==='/api/model-lab')return {directories:[],models:[model('a','模型A',true),model('b','模型B',true),model('untested','未验证H3',false),model('language','M3语言组件',false,'language')],experiments:[],limitations:[]};
    if(url==='/api/projects')return [{id:'p1',name:'来源项目'}];
    if(url==='/api/projects/p1/shots')return [{id:'s1',order:1,scene_title:'多人参考镜头'}];
    if(url==='/api/model-lab/experiments'&&init?.method==='POST')return {id:'e1',name:'真实任务待执行',mode:'cross_model',status:'queued',source_shot_id:'s1',test_project_id:'lab',test_shot_id:'lab-shot',cases:[],snapshot:{},results:{},created_at:'2026-10-24'};
    if(url==='/api/model-lab/experiments/e1')return {id:'e1',name:'真实任务待执行',mode:'cross_model',status:'queued',source_shot_id:'s1',test_project_id:'lab',test_shot_id:'lab-shot',cases:[],snapshot:{},results:{},created_at:'2026-10-24'};
    throw new Error(url);
  });
  render(<MemoryRouter><ModelLabPage section="experiments"/></MemoryRouter>);
  await user.selectOptions(await screen.findByLabelText('实验参考来源项目'),'p1');
  await user.selectOptions(await screen.findByLabelText('实验参考来源镜头'),'s1');
  expect(screen.queryByLabelText('未验证H3')).not.toBeInTheDocument();expect(screen.queryByLabelText('M3语言组件')).not.toBeInTheDocument();
  await user.click(screen.getByLabelText('模型A'));await user.click(screen.getByLabelText('模型B'));
  expect(mocked.mock.calls.filter(([,init])=>init?.method==='PUT'||init?.method==='POST')).toHaveLength(0);
  await user.clear(screen.getByLabelText('实验固定 seed'));await user.type(screen.getByLabelText('实验固定 seed'),'17');
  await user.click(screen.getByLabelText('已核对真实模型、参考镜头、尺寸和费用，允许提交上述实际测试任务。'));
  await user.click(screen.getByRole('button',{name:'创建并运行独立对比实验'}));
  await waitFor(()=>expect(mocked.mock.calls.some(([url,init])=>url==='/api/model-lab/experiments'&&init?.method==='POST')).toBe(true));
  const call=mocked.mock.calls.find(([url,init])=>url==='/api/model-lab/experiments'&&init?.method==='POST')!;
  expect(JSON.parse(String(call[1]?.body))).toEqual(expect.objectContaining({shot_id:'s1',mode:'cross_model',seed:17,width:1024,height:576,allow_cloud_costs:false,cases:[{model_id:'a',label:'模型A',parameters:{steps:20}},{model_id:'b',label:'模型B',parameters:{steps:20}}]}));
  expect(mocked.mock.calls.some(([url,init])=>url.includes('/ai-services/')&&init?.method)).toBe(false);
});
it('shows language model purpose without offering a video load action',async()=>{
  mocked.mockImplementation(async url=>url==='/api/model-lab'?{directories:[],models:[model('language','M3语言组件',false,'language')],experiments:[],limitations:[]}:[]);
  render(<MemoryRouter><ModelLabPage section="models"/></MemoryRouter>);
  expect(await screen.findByText('M3语言组件')).toBeInTheDocument();
  expect(screen.getByText('此项用途不是视频生成，不作为视频模型提交。')).toBeInTheDocument();
  expect(screen.queryByRole('button',{name:'单独真实加载测试'})).not.toBeInTheDocument();
});
