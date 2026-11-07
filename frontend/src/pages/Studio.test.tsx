// UI contract tests only. Fixtures below are not evidence of real media generation.
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react';
import { StrictMode } from 'react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError, request } from '../lib/api';
import type { StudioOverview } from '../lib/studio-contract';
import { StudioPage } from './StudioPage';

vi.mock('../lib/api', async importOriginal => {
  const original = await importOriginal<typeof import('../lib/api')>();
  return {...original, request: vi.fn()};
});
const mockRequest = vi.mocked(request);
function overview(): StudioOverview {
  return {project:{id:'p1',name:'测试项目'},characters:[],scenes:[],run:null,exceptions:[],renders:[],shots:[
    {id:'s1',order:1,title:'检查记录',scene_name:'控制室',cast:[{character_id:'c1',name:'角色甲'}],duration_seconds:5,status:'generated',video:{id:'v1',relative_path:'test/shot1.mp4',duration_seconds:5},decision:{action:'pending',revision:2,position:1}},
    {id:'s2',order:2,title:'观察雨棚',scene_name:'门口',cast:[{character_id:'c2',name:'角色乙'}],duration_seconds:5,status:'generated',video:{id:'v2',relative_path:'test/shot2.mp4',duration_seconds:5},decision:{action:'exclude',revision:3,position:2}},
  ]};
}
describe('Studio UI contracts', () => {
  let data: StudioOverview;
  beforeEach(() => {
    data = overview();
    vi.spyOn(window,'confirm').mockReturnValue(true);
    vi.spyOn(HTMLMediaElement.prototype,'pause').mockImplementation(()=>{});
    mockRequest.mockImplementation(async (url,init) => {
    if(url.endsWith('/shot-framing'))return {project_id:url.split('/')[4],shots:[{id:'s1',order:1,title:'镜头',aspect_ratio:'16:9',revision:1,editable:true,block_reason:null}],supported_aspect_ratios:['16:9','9:16','1:1'],requires_new_run:false};
      if(url==='/api/settings/ai-services')return [];
      if(url.startsWith('/api/model-services/routes/project/'))return {scope:'project',target_id:'p1',slot:'dialogue_generation',revision:0,inherited_from:null,settings:{mode:'local_only',choices:[],parameters:{}}};
      if(url.includes('/voices?'))return {project_id:'p1',groups:[],services:[],tts_enabled:false,ready_to_start:true,blockers:[],summary:'本次沿用原声'};
      if(url.startsWith('/api/quality/profiles/')) return {scope:url.split('/')[4],target_id:url.split('/')[5],settings:{resolution:'720p',mode:'standard',aspect_ratio:'16:9',processing:'lanczos'},revision:0,inherited_from:'built_in',plan:{}};
      if(url==='/api/quality/capabilities')return {};
      if(url==='/api/studio/projects') return [{id:'p1',name:'测试项目',status:'completed',shot_count:2}];
      if(url==='/api/studio/projects/p1/repair')return {project_id:'p1',run_id:'run1',run_status:'running',gpu_gate:{state:'idle',reason:'health_unreachable_and_no_managed_runtime_process'},actions:[{code:'visual_dispatched',status:'applied',message:'已把已有视觉任务派发给本机ComfyUI；原任务ID与输入快照保留。',visual_id:'visual1',provider_job_id:'prompt1'}],blockers:[{node_id:'character:c1',kind:'character',status:'exception',code:'capability_unverified',message:'图片模型或参考图工作流尚无匹配的真实输出验证；不会伪造验证结果。'}]};
      if(url==='/api/studio/import') return {project_id:'p1',status:'imported'};
      if(url==='/api/projects/p1/videos')return data.shots!.filter(shot=>shot.video).map(shot=>({...shot.video,shot_id:shot.id}));
      if(url==='/api/shots/s1/prompt-preview')return {prompt:'原始生成提示词',reference_order:['<Picture 1>']};
      if(url==='/api/studio/projects/p1') return structuredClone(data);
      throw new Error('Unexpected contract URL: '+url);
    });
  });
  afterEach(() => { cleanup();vi.useRealTimers();vi.restoreAllMocks(); mockRequest.mockReset(); });

  it('orders preparation as saved Word, production settings and role settings without requiring another import',async()=>{
    data.script={id:'saved-script',status:'approved',revision:1};data.project.source_filename='已审核剧本.docx';
    render(<MemoryRouter initialEntries={['/?project=p1']}><StudioPage/></MemoryRouter>);await screen.findByText('已加载保存的剧本');
    const word=screen.getByRole('region',{name:'第一步 导入 Word，准备制作'}),settings=screen.getByRole('region',{name:'第二步 制作设置'}),roles=screen.getByRole('region',{name:'第三步 角色设置'});
    expect(word.compareDocumentPosition(settings)&Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();expect(settings.compareDocumentPosition(roles)&Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();expect(word).toHaveTextContent('已审核剧本.docx');expect(word).toHaveTextContent('无需重新导入同一份 Word');expect(within(roles).getByText('人物角色参考图（可选）')).toBeInTheDocument();const characterReferences=within(roles).getByText('人物角色参考图（可选）'),productReferences=within(roles).getByText('固定产品／道具参考图（可选）');expect(characterReferences.compareDocumentPosition(productReferences)&Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();expect(within(roles).getByRole('heading',{name:'角色配音（开始制作前配置）'})).toBeInTheDocument();expect(mockRequest.mock.calls.some(([url])=>url==='/api/studio/import')).toBe(false);
  });

  it('shows automatic character adoption as pending video review without claiming formal identity approval',async()=>{
    data.characters=[{id:'c_new',display_name:'新成年人物',version_id:'technical_version',current_approved_version_id:null,acceptance:{actor:'automation'}}];
    render(<MemoryRouter initialEntries={['/?project=p1']}><StudioPage/></MemoryRouter>);
    expect(await screen.findByText('自动技术采纳，待视频审片')).toBeInTheDocument();
    expect(screen.queryByText('正式版本已锁定')).not.toBeInTheDocument();
  });

  it('offers a new incremental run after product inputs changed while paused',async()=>{
    data.run={id:'paused-run',status:'paused',nodes_json:[],snapshot_json:{product_references_changed:true}};
    render(<MemoryRouter initialEntries={['/?project=p1']}><StudioPage/></MemoryRouter>);
    expect(await screen.findByRole('button',{name:'开始／增量续作'})).toBeInTheDocument();
    expect(screen.queryByRole('button',{name:'继续制作'})).not.toBeInTheDocument();
    expect(screen.getByText(/产品参考已更改。点击/)).toBeInTheDocument();
  });

  it('renders slow real-world responses without polling starvation or duplicate requests in StrictMode', async () => {
    vi.useFakeTimers();
    let resolveDetail!:(value:StudioOverview)=>void;
    const detail=new Promise<StudioOverview>(resolve=>{resolveDetail=resolve;});
    mockRequest.mockImplementation(async url=>url==='/api/studio/projects/p1'?detail:[{id:'p1',name:'测试项目'}]);
    render(<StrictMode><MemoryRouter initialEntries={['/?project=p1']}><StudioPage/></MemoryRouter></StrictMode>);
    await act(async()=>{await vi.advanceTimersByTimeAsync(12_000);});
    expect(mockRequest.mock.calls.filter(([url])=>url==='/api/studio/projects/p1')).toHaveLength(1);
    await act(async()=>{resolveDetail(data);await detail;});
    expect(screen.getByRole('heading',{name:'人物与复用身份'})).toBeInTheDocument();
  });

  it('places one-click repair beside pause and reports real diagnosis and repair evidence',async()=>{
    const user=userEvent.setup();
    data.run={id:'run1',status:'running',nodes_json:[]};
    render(<MemoryRouter initialEntries={['/?project=p1']}><StudioPage/></MemoryRouter>);
    const repair=await screen.findByRole('button',{name:'一键修复'});
    const actionGroup=repair.closest<HTMLElement>('.studio-actions');
    expect(actionGroup).not.toBeNull();
    expect(within(actionGroup!).getByRole('button',{name:'暂停后续任务'})).toBeInTheDocument();
    await user.click(repair);
    await waitFor(()=>expect(mockRequest.mock.calls.some(([url,init])=>url==='/api/studio/projects/p1/repair'&&init?.method==='POST')).toBe(true));
    expect(await screen.findByText('一键修复诊断结果')).toBeInTheDocument();
    const report=screen.getByText('一键修复诊断结果').closest('[role="status"]');
    expect(report).toHaveTextContent('prompt1');
    expect(report).toHaveTextContent('capability_unverified');
  });

  it('handles audio overrun at its exception with measured evidence and explicit application rather than blind retry',async()=>{
    const user=userEvent.setup();
    data.run={id:'run1',status:'exceptions',nodes_json:[]};
    data.exceptions=[{node_id:'audio_mix:s1',kind:'audio_mix',status:'exception',target_id:'s1',code:'audio_longer_than_video',message:'对白音频超过原片长度，未静默截断'}];
    const base=mockRequest.getMockImplementation()!;
    const context={project_id:'p1',run_id:'run1',node_id:'audio_mix:s1',source_fingerprint:'frozen-source',shot:{id:'s1',title:'检查记录',order:1,revision:2},video:{id:'v1',duration_seconds:3},audio_duration_seconds:5,dialogues:[{id:'line1',text:'Look! A basket!',original_text:'Look! A basket!',duration_seconds:5,language:'ZH',speed:1}],capabilities:{extend_video:{available:true,recommended_duration_seconds:6},shorten_dialogue:{available:true},adjust_speed:{available:true,min:.5,max:2,languages:['ZH','EN'],recommended_speed:1.8}}};
    let finish!:(value:unknown)=>void;const applying=new Promise(resolve=>{finish=resolve;});
    mockRequest.mockImplementation(async(url,init)=>{
      if(url.endsWith('/audio-duration/audio_mix%3As1'))return context;
      if(url.endsWith('/audio-duration/audio_mix%3As1/preview'))return {preview_token:'checked',action:'extend_video',summary:'重新处理本镜视频及混音',affected_nodes:['video:s1','audio_mix:s1'],warnings:[],parameters:{}};
      if(url.endsWith('/audio-duration/audio_mix%3As1/apply'))return applying;
      return base(url,init);
    });
    render(<MemoryRouter initialEntries={['/?project=p1']}><StudioPage/></MemoryRouter>);
    const region=await screen.findByRole('region',{name:'音频超长处理'});
    expect(region).toHaveTextContent('实际原片 3.000 秒，配音 5.000 秒');expect(screen.queryByRole('button',{name:'重试此任务'})).toBeNull();
    await user.click(await within(region).findByRole('button',{name:'延长镜头'}));await user.click(within(region).getByRole('button',{name:'预览处理影响'}));
    expect(mockRequest.mock.calls.some(([url])=>url.endsWith('/apply'))).toBe(false);
    await user.click(await within(region).findByRole('button',{name:'确认并开始重新处理'}));
    expect(screen.getByLabelText('当前项目')).toBeDisabled();expect(screen.getByRole('button',{name:'一键修复'})).toBeDisabled();
    const detailReads=mockRequest.mock.calls.filter(([url])=>url==='/api/studio/projects/p1').length;
    data.exceptions=[];
    await act(async()=>{finish({run_id:'run1',node_id:'audio_mix:s1',status:'running',affected_nodes:['video:s1','audio_mix:s1'],replayed:false,notice:'已提交'});await applying;});
    await waitFor(()=>expect(mockRequest.mock.calls.filter(([url])=>url==='/api/studio/projects/p1').length).toBeGreaterThan(detailReads));
    expect(mockRequest.mock.calls.some(([url])=>url==='/api/studio/projects/p1/retry')).toBe(false);
  });

  it('saves quality before explicitly applying its returned revision to undispatched work',async()=>{
    const user=userEvent.setup();
    data.run={id:'current-run',status:'running',nodes_json:[]};
    const base=mockRequest.getMockImplementation()!;
    mockRequest.mockImplementation(async(url,init)=>{
      if(url==='/api/quality/profiles/project/p1'&&init?.method==='PUT')return {scope:'project',target_id:'p1',revision:3,settings:JSON.parse(String(init.body)).settings,plan:{}};
      if(url==='/api/studio/projects/p1/quality/apply')return {applied:['s1'],skipped:[{shot_id:'s2',reason:'已接收任务保留原快照'}],run_status:'running'};
      return base(url,init);
    });
    render(<MemoryRouter initialEntries={['/?project=p1']}><StudioPage/></MemoryRouter>);
    const apply=await screen.findByRole('button',{name:'保存并应用到未派发画质任务'});
    await waitFor(()=>expect(apply).toBeEnabled());await user.click(apply);
    await waitFor(()=>expect(mockRequest.mock.calls.some(([url])=>url==='/api/studio/projects/p1/quality/apply')).toBe(true));
    const calls=mockRequest.mock.calls;
    const savedIndex=calls.findIndex(([url,init])=>url==='/api/quality/profiles/project/p1'&&init?.method==='PUT');
    const appliedIndex=calls.findIndex(([url])=>url==='/api/studio/projects/p1/quality/apply');
    expect(savedIndex).toBeGreaterThan(-1);expect(appliedIndex).toBeGreaterThan(savedIndex);
    expect(JSON.parse(String(calls[appliedIndex][1]?.body))).toEqual({expected_profile_revision:3});
    expect(await screen.findByText(/已将保存的画质应用到 1 个未派发镜头/)).toHaveTextContent('已接收任务与原视频保留');
    expect(calls.some(([url])=>url==='/api/studio/projects/p1/start'||url==='/api/studio/projects/p1/retry')).toBe(false);
  });

  it('reports every keyframe reference without claiming preflight is completed inference',async()=>{
    const user=userEvent.setup();
    data.run={id:'run1',status:'running',nodes_json:[]};
    const base=mockRequest.getMockImplementation()!;
    mockRequest.mockImplementation(async(url,init)=>url==='/api/studio/projects/p1/repair'
      ? {run_status:'running',gpu_gate:{state:'idle',reason:'healthy_idle'},actions:[{code:'keyframe_reference_nodes_requeued',status:'applied',message:'已恢复参考预算误拦截的关键画面',references:[{shot_id:'s2',reference_count:5,roles:{identity:3,scene:1,product:1}}]}],blockers:[]}
      : base(url,init));
    render(<MemoryRouter initialEntries={['/?project=p1']}><StudioPage/></MemoryRouter>);
    await user.click(await screen.findByRole('button',{name:'一键修复'}));
    const report=(await screen.findByText('一键修复诊断结果')).closest('[role="status"]');
    expect(report).toHaveTextContent('第 2 镜：5 张原图已完整编译（人物 3、场景 1、产品 1）');
    expect(report).toHaveTextContent('未删除参考图。生成进度以对应关键画面节点为准。');
  });

  it('shows a missing repair endpoint beside the controls so the click never appears to do nothing',async()=>{
    const user=userEvent.setup();
    data.run={id:'run1',status:'running',nodes_json:[]};
    const base=mockRequest.getMockImplementation()!;
    mockRequest.mockImplementation(async(url,init)=>{
      if(url==='/api/studio/projects/p1/repair')throw new ApiError('Not Found',404);
      if(url==='/api/studio/projects/p1/production-preferences')return {project_id:'p1',revision:0,dialogue_audio_policy:'native'};
      return base(url,init);
    });
    render(<MemoryRouter initialEntries={['/?project=p1']}><StudioPage/></MemoryRouter>);
    const repair=await screen.findByRole('button',{name:'一键修复'});
    await user.click(repair);
    const error=await screen.findByText(/当前本地后端尚未加载一键修复接口/);
    expect(error).toHaveTextContent('后端尚未加载一键修复接口（HTTP 404）');
    expect(error).toHaveTextContent('同一 Windows 用户上下文中重启后端');
    expect(repair).toBeEnabled();
  });

  it('uploads Word through unified import with cloud costs disabled until explicitly selected', async () => {
    const user=userEvent.setup();
    render(<MemoryRouter><StudioPage/></MemoryRouter>);
    const input=screen.getByLabelText('剧本文档（.docx）');
    await user.upload(input,new File(['contract fixture'],'scene.docx',{type:'application/vnd.openxmlformats-officedocument.wordprocessingml.document'}));
    await user.click(screen.getByRole('button',{name:'导入并配置制作'}));
    await waitFor(()=>expect(mockRequest.mock.calls.some(([path])=>path==='/api/studio/import')).toBe(true));
    const call=mockRequest.mock.calls.find(([path])=>path==='/api/studio/import')!;
    const form=call[1]?.body as FormData;
    expect(form.get('allow_cloud_costs')).toBe('false');expect(form.get('auto_start')).toBe('false');
    expect(form.get('file')).toBeInstanceOf(File);expect(JSON.parse(String(form.get('quality_json')))).toEqual({resolution:'720p',mode:'standard',aspect_ratio:'16:9',processing:'lanczos'});
    expect(await screen.findByText('Word 已导入，尚未启动制作。请先在「角色配音」上传并保存参考音频，核对画质后点击「开始／增量续作」。')).toBeInTheDocument();
    expect(mockRequest.mock.calls.some(([url])=>url.endsWith('/start'))).toBe(false);
  });

});
