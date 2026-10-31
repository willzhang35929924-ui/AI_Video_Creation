import { render,screen,waitFor,within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach,expect,it,vi } from 'vitest';
import { request } from '../lib/api';
import { ProductionMaterialsPanel } from './ProductionPage';

vi.mock('../lib/api',()=>({request:vi.fn(),api:{mediaUrl:(path:string)=>path}}));
vi.mock('../components/ProductionDesignForm',()=>({ProductionDesignForm:()=>null}));
vi.mock('../components/ProductionVisualUpload',()=>({ProductionVisualUpload:()=>null}));
vi.mock('../components/ProductionShotEditor',()=>({ProductionShotEditor:()=>null}));
const mocked=vi.mocked(request);
afterEach(()=>mocked.mockReset());

it('only queries the original unknown cloud task when its provider ID is known',async()=>{
  const jobs=[{id:'job-known',shot_id:'s1',status:'unknown',provider:'cloud_wan26_i2v',provider_job_id:'remote-id'},
    {id:'job-missing',shot_id:'s2',status:'unknown',provider:'cloud_wan26_i2v',provider_job_id:null}];
  const data={rules:{},shots:[{shot:{id:'s1',scene_title:'第一镜'}},{shot:{id:'s2',scene_title:'第二镜'}}],
    report:{estimate:{},counts:{},findings:[]},designs:[],runs:[],visuals:[],videos:[]};
  mocked.mockImplementation(async(url,init)=>{
    if(url==='/api/cinematic/rules')return {versions:{},sections:{},repair_categories:{}};
    if(url==='/api/projects/p1/production')return data;
    if(url==='/api/projects/p1/jobs')return jobs;
    if(url==='/api/jobs/job-known/reconcile' && init?.method==='POST')return {status:'submitted'};
    throw new Error('Unexpected request '+url);
  });
  render(<ProductionMaterialsPanel projectId="p1"/>);
  const section=(await screen.findByRole('heading',{name:'结果待核对的视频任务'})).parentElement!;
  expect(within(section).getByText(/只重新查询原任务，不会重新提交生成/)).toBeVisible();
  expect(within(section).getByText(/请先到供应商处人工核对/)).toBeVisible();
  await userEvent.setup().click(within(section).getByRole('button',{name:'重新查询原任务'}));
  await waitFor(()=>expect(mocked).toHaveBeenCalledWith('/api/jobs/job-known/reconcile',{method:'POST'}));
  expect(mocked.mock.calls.filter(([url])=>String(url).includes('/reconcile'))).toHaveLength(1);
});

it('requires documented human inspection and risk confirmation before closing an unknown local task',async()=>{
  const jobs=[{id:'job-local',shot_id:'s1',status:'unknown',provider:'comfyui_h3',provider_job_id:null}];
  const data={rules:{},shots:[{shot:{id:'s1',scene_title:'本地镜头'}}],report:{estimate:{},counts:{},findings:[]},designs:[],runs:[],visuals:[],videos:[]};
  mocked.mockImplementation(async(url,init)=>{
    if(url==='/api/cinematic/rules')return {versions:{},sections:{},repair_categories:{}};
    if(url==='/api/projects/p1/production')return data;
    if(url==='/api/projects/p1/jobs')return jobs;
    if(url==='/api/jobs/job-local/resolve-unknown'&&init?.method==='POST')return {status:'closed_unknown'};
    throw new Error('Unexpected request '+url);
  });
  const user=userEvent.setup();render(<ProductionMaterialsPanel projectId="p1"/>);
  await user.click(await screen.findByText('人工核对后处理此未知任务'));
  const button=screen.getByRole('button',{name:'记录核对并关闭未知任务'});expect(button).toBeDisabled();
  await user.type(screen.getByLabelText('人工核对说明'),'已人工检查本地任务队列和历史记录');
  await user.click(screen.getByLabelText('我已检查 ComfyUI 队列和历史'));expect(button).toBeDisabled();
  await user.click(screen.getByLabelText('我理解原任务可能已执行，重新生成可能重复'));expect(button).toBeEnabled();
  await user.click(button);
  await waitFor(()=>expect(mocked).toHaveBeenCalledWith('/api/jobs/job-local/resolve-unknown',expect.objectContaining({method:'POST'})));
});
