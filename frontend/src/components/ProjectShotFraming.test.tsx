// Isolated UI contracts only: no production project is edited and no model is called.
import {act,cleanup,render,screen,waitFor} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {afterEach,expect,it,vi} from 'vitest';
import {ApiError,request} from '../lib/api';
import {ProjectShotFraming} from './ProjectShotFraming';

vi.mock('../lib/api',async original=>({...await original<typeof import('../lib/api')>(),request:vi.fn()}));
const mocked=vi.mocked(request),path='/api/studio/projects/p1/shot-framing';
afterEach(()=>{cleanup();mocked.mockReset();});
const initial=()=>({project_id:'p1',supported_aspect_ratios:['16:9','9:16','1:1'],shots:[
  {id:'s1',order:1,title:'产品全景',aspect_ratio:'16:9',revision:3,editable:true,block_reason:null},
  {id:'s2',order:2,title:'产品细节',aspect_ratio:'16:9',revision:8,editable:true,block_reason:null},
]});

it('saves 9:16 with precise selected revisions and reports dirty/busy without submitting generation',async()=>{
  const data=initial(),saved=vi.fn(),state=vi.fn();let finish!:(value:any)=>void;
  const saving=new Promise(resolve=>{finish=resolve;});
  mocked.mockImplementation(async(_url,init)=>init?.method==='PUT'?saving:data);
  const user=userEvent.setup();render(<ProjectShotFraming projectId="p1" onSaved={saved} onStateChange={state}/>);
  const ratio=await screen.findByLabelText('选中镜头的实际画幅');
  expect(ratio).toHaveValue('16:9');expect(screen.getByLabelText('选择全部可编辑镜头')).toBeChecked();
  await user.selectOptions(ratio,'9:16');
  expect(state.mock.lastCall?.[0]).toMatchObject({busy:false,dirty:true,requiresNewRun:false});
  await user.click(screen.getByRole('button',{name:'保存选中镜头画幅'}));
  expect(state.mock.lastCall?.[0]).toMatchObject({busy:true,dirty:true,requiresNewRun:false});
  expect(ratio).toBeDisabled();
  const sent=mocked.mock.calls.find(([,init])=>init?.method==='PUT')!;
  expect(sent[0]).toBe(path);expect(JSON.parse(String(sent[1]?.body))).toEqual({aspect_ratio:'9:16',shots:[{id:'s1',expected_revision:3},{id:'s2',expected_revision:8}]});
  await act(async()=>{finish({...data,shots:data.shots.map(shot=>({...shot,aspect_ratio:'9:16',revision:shot.revision+1})),updated_shot_ids:['s1','s2']});await saving;});
  await waitFor(()=>expect(saved).toHaveBeenCalledWith('9:16',true));
  expect(state.mock.lastCall?.[0]).toMatchObject({busy:false,dirty:false,requiresNewRun:false});
  expect(screen.getByText(/实际镜头画幅已保存为 9:16/)).toHaveTextContent('未启动生成');
  expect(mocked.mock.calls.every(([url])=>url===path)).toBe(true);
});

it('only changes selected editable shots and preserves the target after a failed save',async()=>{
  const data=initial(),saved=vi.fn(),state=vi.fn();
  data.shots.push({id:'active',order:3,title:'正在生成',aspect_ratio:'16:9',revision:10,editable:false,block_reason:'该镜已有活动任务'} as any);
  mocked.mockImplementation(async(_url,init)=>{if(init?.method==='PUT')throw new ApiError('网络失败',503);return data;});
  const user=userEvent.setup();render(<ProjectShotFraming projectId="p1" onSaved={saved} onStateChange={state}/>);
  const ratio=await screen.findByLabelText('选中镜头的实际画幅');
  expect(screen.getByLabelText('选择镜头 #3 · 正在生成')).toBeDisabled();
  await user.click(screen.getByLabelText('选择全部可编辑镜头'));
  expect(screen.getByRole('button',{name:'保存选中镜头画幅'})).toBeDisabled();
  await user.click(screen.getByLabelText('选择镜头 #2 · 产品细节'));
  await user.selectOptions(ratio,'1:1');await user.click(screen.getByRole('button',{name:'保存选中镜头画幅'}));
  expect(await screen.findByRole('alert')).toHaveTextContent('网络失败');
  const sent=mocked.mock.calls.find(([,init])=>init?.method==='PUT')!;
  expect(JSON.parse(String(sent[1]?.body))).toEqual({aspect_ratio:'1:1',shots:[{id:'s2',expected_revision:8}]});
  expect(ratio).toHaveValue('1:1');expect(screen.getByLabelText('选择镜头 #2 · 产品细节')).toBeChecked();
  expect(state.mock.lastCall?.[0]).toMatchObject({busy:false,dirty:true,requiresNewRun:false});expect(saved).not.toHaveBeenCalled();
});

it('reports mixed framing after saving a subset so the page does not overwrite global output framing',async()=>{
  const data=initial(),saved=vi.fn();
  mocked.mockImplementation(async(_url,init)=>init?.method==='PUT'?{...data,shots:[data.shots[0],{...data.shots[1],aspect_ratio:'9:16',revision:9}],updated_shot_ids:['s2']}:data);
  const user=userEvent.setup();render(<ProjectShotFraming projectId="p1" onSaved={saved}/>);
  await screen.findByLabelText('选中镜头的实际画幅');
  await user.click(screen.getByLabelText('选择全部可编辑镜头'));
  await user.click(screen.getByLabelText('选择镜头 #2 · 产品细节'));
  await user.selectOptions(screen.getByLabelText('选中镜头的实际画幅'),'9:16');
  await user.click(screen.getByRole('button',{name:'保存选中镜头画幅'}));
  await waitFor(()=>expect(saved).toHaveBeenCalledWith('9:16',false));
  expect(screen.getByText('#1 · 产品全景 · 当前 16:9')).toBeVisible();
});

it('refreshes on CAS conflict and requires a fresh choice instead of automatically retrying',async()=>{
  const data=initial(),state=vi.fn();let reads=0;
  mocked.mockImplementation(async(_url,init)=>{if(init?.method==='PUT')throw new ApiError('镜头版本已变化',409);reads++;return reads===1?data:{...data,shots:data.shots.map(shot=>({...shot,revision:shot.revision+5}))};});
  const user=userEvent.setup();render(<ProjectShotFraming projectId="p1" onStateChange={state}/>);
  await user.selectOptions(await screen.findByLabelText('选中镜头的实际画幅'),'9:16');
  await user.click(screen.getByRole('button',{name:'保存选中镜头画幅'}));
  await waitFor(()=>expect(reads).toBe(2));
  await waitFor(()=>expect(state.mock.lastCall?.[0]).toMatchObject({busy:false,dirty:false,requiresNewRun:false}));
  expect(screen.getByRole('alert')).toHaveTextContent('请重新选择镜头和画幅');
  expect(screen.getByLabelText('选中镜头的实际画幅')).toHaveValue('16:9');
  expect(mocked.mock.calls.filter(([,init])=>init?.method==='PUT')).toHaveLength(1);
  await user.selectOptions(screen.getByLabelText('选中镜头的实际画幅'),'9:16');
  await user.click(screen.getByRole('button',{name:'保存选中镜头画幅'}));
  const puts=mocked.mock.calls.filter(([,init])=>init?.method==='PUT');
  expect(JSON.parse(String(puts[1][1]?.body)).shots).toEqual([{id:'s1',expected_revision:8},{id:'s2',expected_revision:13}]);
});

it('manual refresh discards the draft and adopts current server ratios and revisions',async()=>{
  const data=initial(),state=vi.fn();let reads=0;
  mocked.mockImplementation(async()=>{reads++;return reads===1?data:{...data,shots:data.shots.map(shot=>({...shot,aspect_ratio:'9:16',revision:shot.revision+1}))};});
  const user=userEvent.setup();render(<ProjectShotFraming projectId="p1" onStateChange={state}/>);
  await user.selectOptions(await screen.findByLabelText('选中镜头的实际画幅'),'1:1');
  expect(state.mock.lastCall?.[0].dirty).toBe(true);
  await user.click(screen.getByRole('button',{name:'重新读取镜头画幅'}));
  await waitFor(()=>expect(screen.getByLabelText('选中镜头的实际画幅')).toHaveValue('9:16'));
  expect(state.mock.lastCall?.[0]).toMatchObject({busy:false,dirty:false,requiresNewRun:false});
  expect(mocked.mock.calls.some(([,init])=>init?.method==='PUT')).toBe(false);
});

it('reports read errors and ignores a late response for a different project',async()=>{
  let finish!:(value:any)=>void;const stale=new Promise(resolve=>{finish=resolve;});
  mocked.mockImplementation(async url=>url===path?stale:Promise.reject(new ApiError('新项目读取失败',503)));
  const {rerender}=render(<ProjectShotFraming projectId="p1"/>);
  expect(screen.getByRole('button',{name:'保存选中镜头画幅'})).toBeDisabled();
  rerender(<ProjectShotFraming projectId="p2"/>);
  expect(await screen.findByRole('alert')).toHaveTextContent('新项目读取失败');
  await act(async()=>{finish(initial());await stale;});
  expect(screen.queryByLabelText('选择镜头 #1 · 产品全景')).toBeNull();
});

it('reports persistent new-run requirements after a fresh read and refreshes that state from the server',async()=>{
  let requiresNewRun=true;const state=vi.fn();
  mocked.mockImplementation(async()=>({...initial(),requires_new_run:requiresNewRun,notice:'旧编排保留原画幅快照'}));
  const user=userEvent.setup();render(<ProjectShotFraming projectId="p1" onStateChange={state}/>);
  expect(await screen.findByText(/旧编排保留原画幅快照/)).toHaveTextContent('旧编排不能通过「继续制作」应用新画幅');
  await waitFor(()=>expect(state.mock.lastCall?.[0]).toMatchObject({busy:false,dirty:false,requiresNewRun:true}));
  requiresNewRun=false;
  await user.click(screen.getByRole('button',{name:'重新读取镜头画幅'}));
  await waitFor(()=>expect(state.mock.lastCall?.[0]).toMatchObject({busy:false,dirty:false,requiresNewRun:false}));
});

it('keeps ready false after a read failure without locking refresh, then reports ready after recovery',async()=>{
  let failed=true;const state=vi.fn();
  mocked.mockImplementation(async()=>{if(failed)throw new ApiError('读取失败',503);return initial();});
  const user=userEvent.setup();render(<ProjectShotFraming projectId="p1" onStateChange={state}/>);
  await screen.findByRole('alert');
  expect(state.mock.lastCall?.[0]).toMatchObject({busy:false,dirty:false,ready:false});
  expect(screen.getByRole('button',{name:'重新读取镜头画幅'})).toBeEnabled();
  failed=false;await user.click(screen.getByRole('button',{name:'重新读取镜头画幅'}));
  await waitFor(()=>expect(state.mock.lastCall?.[0]).toMatchObject({busy:false,dirty:false,ready:true}));
  expect(screen.queryByRole('alert')).toBeNull();
});
