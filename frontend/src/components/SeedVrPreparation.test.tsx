// Isolated UI fixtures; the test transport never invokes inference.
import { useState } from 'react';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { request } from '../lib/api';
import type { QualitySettings } from '../lib/quality-contract';
import { QualitySettingsFields } from './QualitySettings';
import { SeedVrPreparation } from './SeedVrPreparation';

vi.mock('../lib/api',async original=>({...await original<typeof import('../lib/api')>(),request:vi.fn()}));
const mocked=vi.mocked(request);
const base={seedvr2:{state:'verified',verified_targets:[{resolution:'720p',aspect_ratio:'16:9'}],verified_resolutions:['720p'],verified_aspect_ratios:['16:9']}};
const verified={seedvr2:{...base.seedvr2,verified_targets:[...base.seedvr2.verified_targets,{resolution:'1080p',aspect_ratio:'9:16'}]}};
const settings:Required<QualitySettings>={resolution:'1080p',mode:'high',aspect_ratio:'9:16',processing:'lanczos'};
const shots=[{id:'s1',order:1,title:'第一镜'},{id:'s4',order:4,title:'开盒'}];
const raw=(id='raw4')=>({id,shot_id:'s4',generation_job_id:'original-job',relative_path:'actual-source.mp4',revision:2,metadata_json:{width:576,height:1024,playable_video:true}});
function Harness({projectId='p1'}:{projectId?:string}){
  const [q,setQ]=useState(settings),[cap,setCap]=useState(base);
  const refresh=async()=>setCap(await request<typeof base>('/api/quality/capabilities'));
  return <><QualitySettingsFields value={q} onChange={setQ} capabilities={cap}/><SeedVrPreparation projectId={projectId} settings={q} capabilities={cap} shots={shots} onVerified={refresh}/></>;
}
afterEach(()=>{cleanup();mocked.mockReset();});

describe('SeedVR2 target preparation',()=>{
  it('queues one real-workflow test request and opens the exact target only after returned completion evidence',async()=>{
    mocked.mockImplementation(async(url,init)=>{
      if(url==='/api/projects/p1/videos')return [raw(),{...raw('derived'),metadata_json:{quality_render_id:'processed'}},{...raw('landscape'),metadata_json:{width:1024,height:576}}, {...raw('raw1'),shot_id:'s1',revision:5}];
      if(url==='/api/quality/seedvr2/load-test')return {id:'qr1',source_video_id:'raw4',status:'queued',snapshot:{},result:{}};
      if(url==='/api/quality/renders/qr1')return {id:'qr1',source_video_id:'raw4',status:'generated',snapshot:{},result:{final_actual:{width:1080,height:1920,frames:107}}};
      if(url==='/api/quality/capabilities')return verified;
      throw new Error(`Unexpected isolated request ${url} ${init?.method}`);
    });
    render(<Harness/>);
    expect(screen.getByRole('option',{name:'SeedVR2（当前档位或画幅未验证）'})).toBeDisabled();
    const source=await screen.findByLabelText('SeedVR2 验证源视频');
    await waitFor(()=>expect(source).toHaveValue('raw4'));
    expect(source.querySelectorAll('option')).toHaveLength(2);
    const start=screen.getByRole('button',{name:'验证当前规格的 SeedVR2'});
    fireEvent.click(start);fireEvent.click(start);
    await waitFor(()=>expect(mocked.mock.calls.filter(([url])=>url==='/api/quality/seedvr2/load-test')).toHaveLength(1));
    const submission=mocked.mock.calls.find(([url])=>url==='/api/quality/seedvr2/load-test')!;
    expect(JSON.parse(String(submission[1]?.body))).toEqual({video_id:'raw4',settings:{...settings,processing:'seedvr2'}});
    expect(screen.getByLabelText('制作处理方式')).toHaveValue('lanczos');
    await waitFor(()=>expect(screen.getByRole('option',{name:'1080p输出（SeedVR2时序视频超分）'})).toBeEnabled(),{timeout:4000});
    expect(screen.getByText(/实际输出 1080×1920 · 107 帧/)).toBeInTheDocument();
    expect(screen.getByLabelText('制作处理方式')).toHaveValue('lanczos');
    expect(mocked.mock.calls.some(([,init])=>init?.method==='PUT')).toBe(false);
  });

  it('keeps an unknown provider result visible and prevents another submission',async()=>{
    mocked.mockImplementation(async url=>url==='/api/projects/p1/videos'?[raw()]:{id:'uncertain',source_video_id:'raw4',status:'unknown',snapshot:{},result:{},error_message:'原请求结果待核对'});
    render(<Harness/>);
    const start=screen.getByRole('button',{name:'验证当前规格的 SeedVR2'});
    await waitFor(()=>expect(start).toBeEnabled());fireEvent.click(start);
    await screen.findByText(/请先核对该任务结果，不会自动重提/);
    expect(start).toBeDisabled();fireEvent.click(start);
    expect(mocked.mock.calls.filter(([url])=>url==='/api/quality/seedvr2/load-test')).toHaveLength(1);
    expect(screen.getByRole('option',{name:'SeedVR2（当前档位或画幅未验证）'})).toBeDisabled();
  });

  it('ignores a late create response after changing projects',async()=>{
    let finish!:(value:unknown)=>void;
    const pending=new Promise(resolve=>{finish=resolve;});
    mocked.mockImplementation(async url=>url==='/api/projects/p1/videos'?[raw()]:url==='/api/projects/p2/videos'?[raw('new-raw')]:url==='/api/quality/seedvr2/load-test'?pending:Promise.reject(new Error('No polling of old project is allowed')));
    const view=render(<Harness/>);
    const start=screen.getByRole('button',{name:'验证当前规格的 SeedVR2'});await waitFor(()=>expect(start).toBeEnabled());fireEvent.click(start);
    await waitFor(()=>expect(mocked.mock.calls.some(([url])=>url==='/api/quality/seedvr2/load-test')).toBe(true));
    view.rerender(<Harness projectId="p2"/>);
    finish({id:'old-qr',source_video_id:'raw4',status:'queued',snapshot:{},result:{}});
    await waitFor(()=>expect(screen.getByLabelText('SeedVR2 验证源视频')).toHaveValue('new-raw'));
    expect(screen.queryByText(/old-qr/)).not.toBeInTheDocument();
    expect(mocked.mock.calls.some(([url])=>url==='/api/quality/renders/old-qr')).toBe(false);
  });

  it('continues collection for the same accepted prompt without creating another inference request',async()=>{
    const accepted={id:'accepted-qr',source_video_id:'raw4',status:'failed',snapshot:{native_execution:{inference_submitted:true,prompt_id:'existing-native-prompt',terminal_status:{status_str:'success'}}},result:{},error_message:'原模型已完成，取回路径需要修复'};
    mocked.mockImplementation(async(url,init)=>{
      if(url==='/api/projects/p1/videos')return [raw()];
      if(url==='/api/quality/seedvr2/load-test')return accepted;
      if(url==='/api/quality/renders/accepted-qr/resume-native')return {...accepted,status:'queued',error_message:null};
      if(url==='/api/quality/renders/accepted-qr')return {...accepted,status:'generated',error_message:null,result:{final_actual:{width:1080,height:1920,frames:107}}};
      if(url==='/api/quality/capabilities')return verified;
      throw new Error(`Unexpected isolated resume request ${url} ${init?.method}`);
    });
    render(<Harness/>);
    const start=screen.getByRole('button',{name:'验证当前规格的 SeedVR2'});await waitFor(()=>expect(start).toBeEnabled());fireEvent.click(start);
    const resume=await screen.findByRole('button',{name:'继续取回已提交的 SeedVR2 结果'});
    expect(start).toBeDisabled();fireEvent.click(start);fireEvent.click(resume);fireEvent.click(resume);
    await waitFor(()=>expect(screen.getByRole('option',{name:'1080p输出（SeedVR2时序视频超分）'})).toBeEnabled(),{timeout:4000});
    expect(mocked.mock.calls.filter(([url])=>url==='/api/quality/seedvr2/load-test')).toHaveLength(1);
    expect(mocked.mock.calls.filter(([url])=>url==='/api/quality/renders/accepted-qr/resume-native')).toHaveLength(1);
    expect(mocked.mock.calls.some(([url])=>url==='/api/quality/renders/accepted-qr')).toBe(true);
    expect(screen.getByLabelText('制作处理方式')).toHaveValue('lanczos');
  });

  it.each(['error','failed'])('does not offer collection resume for a definitive native %s result',async terminal=>{
    mocked.mockImplementation(async url=>url==='/api/projects/p1/videos'?[raw()]:{id:'native-failure',source_video_id:'raw4',status:'unknown',snapshot:{native_execution:{inference_submitted:true,prompt_id:'failed-native-prompt',terminal_status:{status_str:terminal}}},result:{}});
    render(<Harness/>);
    const start=screen.getByRole('button',{name:'验证当前规格的 SeedVR2'});await waitFor(()=>expect(start).toBeEnabled());fireEvent.click(start);
    await screen.findByText(/请先核对该任务结果，不会自动重提/);
    expect(screen.queryByRole('button',{name:'继续取回已提交的 SeedVR2 结果'})).not.toBeInTheDocument();
    expect(start).toBeDisabled();
    expect(mocked.mock.calls.some(([url])=>url.includes('/resume-native'))).toBe(false);
  });
});
