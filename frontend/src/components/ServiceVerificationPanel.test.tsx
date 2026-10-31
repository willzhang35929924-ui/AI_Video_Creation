// HTTP contract fixtures exercise UI boundaries; no model or cloud request is executed.
import {act,fireEvent,render,screen,waitFor} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {MemoryRouter} from 'react-router-dom';
import {afterEach,describe,expect,it,vi} from 'vitest';
import {ApiError,request} from '../lib/api';
import type {ModelServicesOverview,UnifiedTask} from '../lib/model-services-contract';
import {ServiceVerificationPanel} from './ServiceVerificationPanel';
import {ModelServicesPage} from '../pages/ModelServicesPage';

vi.mock('../lib/api',async original=>({...await original<typeof import('../lib/api')>(),request:vi.fn()}));
const mocked=vi.mocked(request);
const overview:ModelServicesOverview={slots:[{slot:'dialogue_generation',name:'角色配音',allowed_modules:['dialogue_generation'],
  default_route:{scope:'system',target_id:'default',slot:'dialogue_generation',revision:1,settings:{mode:'local_only'}},
  candidates:[{service_id:'cloud/tts',model_id:'tts-model',name:'未验证的TTS',provider:'openai_tts',module:'dialogue_generation',is_cloud:true,enabled:true,eligible:false,verification:'not_tested'}]}]};
const task=(status:UnifiedTask['status']):UnifiedTask=>({id:'service:test/one',source_type:'service_test',source_id:'one',project_id:'p1',slot:'dialogue_generation',status,provider:'openai_tts',model_id:'tts-model',snapshot:{inputs:{text:'记录已核对。'}},output:status==='succeeded'?{relative_path:'audio/real-task-result.wav'}:{},error:status==='unknown'?{code:'PROVIDER_OUTCOME_UNKNOWN',message:'请核对供应商'}:null});
function mount(initial='/settings/model-services',data=overview){return render(<MemoryRouter initialEntries={[initial]}><ServiceVerificationPanel overview={data} projects={[{id:'p1',name:'实际项目'}]} projectId="p1" onProjectChange={vi.fn()}/></MemoryRouter>);}
const posts=()=>mocked.mock.calls.filter(([,init])=>init?.method==='POST');
afterEach(()=>{mocked.mockReset();vi.useRealTimers();});

describe('explicit cloud service generation verification',()=>{
  it('requires per-test costs, accepts an unverified enabled TTS, and submits only the selected service',async()=>{
    mocked.mockResolvedValue(task('failed'));
    const user=userEvent.setup();mount();
    const submit=screen.getByRole('button',{name:'提交本次云端TTS测试'});
    expect(submit).toBeDisabled();expect(posts()).toHaveLength(0);
    expect(screen.getByRole('option',{name:/未验证的TTS/})).toBeEnabled();
    await user.click(screen.getByRole('checkbox',{name:'我授权本次云端TTS测试产生供应商费用'}));
    expect(submit).toBeEnabled();await user.click(submit);
    await waitFor(()=>expect(posts()).toHaveLength(1));
    expect(posts()[0][0]).toBe('/api/model-services/services/cloud%2Ftts/generation-test');
    expect(JSON.parse(String(posts()[0][1]?.body))).toEqual({slot:'dialogue_generation',project_id:'p1',model_id:'tts-model',inputs:{text:'记录已核对。'},parameters:{},allow_cloud_costs:true});
    await waitFor(()=>expect(submit).toBeDisabled());
    expect(screen.getByRole('checkbox')).not.toBeChecked();
    expect(mocked.mock.calls.some(([url])=>String(url).includes('/routes/'))).toBe(false);
  });

  it('keeps an unknown accepted task locked and manual refresh only GETs its encoded ID',async()=>{
    mocked.mockResolvedValue(task('unknown'));const user=userEvent.setup();mount();
    await user.click(screen.getByRole('checkbox'));await user.click(screen.getByRole('button',{name:'提交本次云端TTS测试'}));
    expect(await screen.findByText(/结果未知：仅查询现有任务/)).toBeInTheDocument();
    expect(screen.getByRole('checkbox')).toBeDisabled();
    expect(screen.getByRole('button',{name:'提交本次云端TTS测试'})).toBeDisabled();
    await user.click(screen.getByRole('button',{name:'刷新本次任务状态（只读）'}));
    await waitFor(()=>expect(mocked).toHaveBeenCalledWith('/api/model-services/tasks/service%3Atest%2Fone'));
    expect(posts()).toHaveLength(1);
  });

  it('polls a queued task by GET and exposes only the returned succeeded output',async()=>{
    vi.useFakeTimers();mocked.mockImplementation(async(_url,init)=>task(init?.method==='POST'?'queued':'succeeded'));
    mount();fireEvent.click(screen.getByRole('checkbox'));fireEvent.click(screen.getByRole('button',{name:'提交本次云端TTS测试'}));
    await act(async()=>{await Promise.resolve();});
    expect(screen.getByText(/等待实际执行/)).toBeInTheDocument();
    await act(async()=>{await vi.advanceTimersByTimeAsync(3000);});
    expect(screen.getByTestId('service-verification-audio')).toHaveAttribute('src',expect.stringContaining('audio/real-task-result.wav'));
    expect(posts()).toHaveLength(1);
    expect(mocked.mock.calls.filter(([,init])=>!init?.method)).toHaveLength(1);
    await act(async()=>{await vi.advanceTimersByTimeAsync(12000);});
    expect(mocked).toHaveBeenCalledTimes(2);
  });

  it('opens the task tab and restores an unknown task after page refresh without resubmission',async()=>{
    mocked.mockImplementation(async url=>url==='/api/model-services'?overview:url==='/api/studio/projects'?[{id:'p1',name:'实际项目'}]:task('unknown'));
    render(<MemoryRouter initialEntries={['/settings/model-services?service_test_task=service%3Atest%2Fone']}><ModelServicesPage/></MemoryRouter>);
    expect(await screen.findByText(/结果未知：仅查询现有任务/)).toBeInTheDocument();
    expect(screen.getByRole('button',{name:'统一任务记录'})).toHaveClass('active');
    expect(mocked).toHaveBeenCalledWith('/api/model-services/tasks/service%3Atest%2Fone');
    expect(posts()).toHaveLength(0);
    expect(screen.getByRole('button',{name:'提交本次云端TTS测试'})).toBeDisabled();
  });

  it('does not allow a network-uncertain submission to be repeated',async()=>{
    mocked.mockRejectedValue(new ApiError('连接中断',0,'NETWORK_ERROR'));
    const user=userEvent.setup();mount();await user.click(screen.getByRole('checkbox'));await user.click(screen.getByRole('button',{name:'提交本次云端TTS测试'}));
    expect(await screen.findByText(/提交结果待核对，未取得可信任务结果/)).toBeInTheDocument();
    expect(screen.getByRole('checkbox')).toBeDisabled();
    fireEvent.click(screen.getByRole('button',{name:'提交本次云端TTS测试'}));expect(posts()).toHaveLength(1);
  });

  it('explains missing supported profiles and never starts automatically',()=>{
    mount('/settings/model-services',{slots:[]});
    expect(screen.getByText(/尚无已启用且已选择模型的云端TTS档案/)).toBeInTheDocument();
    expect(screen.getByRole('link',{name:'服务档案'})).toHaveAttribute('href','/settings/ai-services');
    expect(screen.queryByRole('button',{name:'提交本次云端TTS测试'})).not.toBeInTheDocument();expect(mocked).not.toHaveBeenCalled();
  });

  it('offers an actual in-page profile navigation callback when none is configured',async()=>{
    const configure=vi.fn();const user=userEvent.setup();
    render(<MemoryRouter><ServiceVerificationPanel overview={{slots:[]}} projects={[]} projectId="" onProjectChange={vi.fn()} onConfigure={configure}/></MemoryRouter>);
    await user.click(screen.getByRole('button',{name:'打开服务档案'}));expect(configure).toHaveBeenCalledTimes(1);expect(mocked).not.toHaveBeenCalled();
  });
});
