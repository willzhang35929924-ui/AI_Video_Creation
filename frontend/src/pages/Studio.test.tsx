// UI contract tests only. Fixtures below are not evidence of real media generation.
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { StrictMode } from 'react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError, request } from '../lib/api';
import type { StudioOverview } from '../lib/studio-contract';
import { StudioPage } from './StudioPage';
import { StudioReviewPage } from './StudioReviewPage';

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
      if(url.endsWith('/decision')) {
        const body=JSON.parse(String(init?.body));
        const shot=data.shots!.find(item=>url.includes('/'+item.id+'/'))!;
        shot.decision={...shot.decision,...body,revision:(shot.decision.revision||0)+1};
        return shot.decision;
      }
      if(url.endsWith('/compose')) return {assembly_id:'a1',render_id:'r1',status:'queued'};
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

  it('saves review decision, video identity, trim, position and concurrency revision before allowing compose', async () => {
    const user=userEvent.setup();
    for(const shot of data.shots||[])if(shot.video)shot.video.metadata_json={...shot.video.metadata_json,width:1080,height:1920};
    render(<MemoryRouter initialEntries={['/studio-review?project=p1']}><StudioReviewPage/></MemoryRouter>);
    expect(await screen.findByTestId('studio-review-video')).toHaveAttribute('src',expect.stringContaining('test/shot1.mp4'));
    expect(screen.getByRole('button',{name:'按审核结果合成成片'})).toBeDisabled();
    await user.clear(screen.getByLabelText('裁剪起点（秒）'));await user.type(screen.getByLabelText('裁剪起点（秒）'),'0.5');
    await user.click(screen.getByRole('button',{name:'保留／保存裁剪'}));
    await waitFor(()=>expect(mockRequest.mock.calls.some(([url])=>url.endsWith('/decision'))).toBe(true));
    const call=mockRequest.mock.calls.find(([url])=>url.endsWith('/decision'))!;
    expect(JSON.parse(String(call[1]?.body))).toEqual(expect.objectContaining({action:'keep',video_id:'v1',trim_start:.5,trim_end:null,position:1,expected_revision:2}));
    const confirmation=screen.getByLabelText('我已审核所有镜头，并确认上述顺序、裁剪和输出设置。');
    await waitFor(()=>expect(confirmation).toBeEnabled());await user.click(confirmation);
    await user.click(screen.getByRole('button',{name:'按审核结果合成成片'}));
    await waitFor(()=>expect(mockRequest.mock.calls.some(([url])=>url.endsWith('/compose'))).toBe(true));
    const compose=mockRequest.mock.calls.find(([url])=>url.endsWith('/compose'))!;
    expect(JSON.parse(String(compose[1]?.body))).toEqual({transition:'cut',transition_duration:.5,width:1080,height:1920,resolution_mode:'source',allow_downscale:false,fps:24,audio_policy:'preserve',subtitles:false,expected_decisions:{s1:3,s2:3}});
  });

  it('lets review edit and submit an over-7000-character prompt without truncating it',async()=>{
    const user=userEvent.setup();
    render(<MemoryRouter initialEntries={['/studio-review?project=p1']}><StudioReviewPage/></MemoryRouter>);
    await screen.findByTestId('studio-review-video');
    await user.click(screen.getByText('修改此镜头／重生成'));
    await user.click(screen.getByRole('button',{name:'编辑本次提示词'}));
    const prompt='电影提示词：'+'景'.repeat(8000);
    fireEvent.change(await screen.findByLabelText('本次生成提示词'),{target:{value:prompt}});
    fireEvent.change(screen.getByLabelText('希望修改的内容'),{target:{value:'保留本镜表演和环境'}});
    expect(screen.getAllByRole('status').find(element=>element.textContent?.includes('提示词 8006 字'))).toBeTruthy();
    await user.click(screen.getByRole('button',{name:'提交修改并重生成此镜'}));
    await waitFor(()=>expect(mockRequest.mock.calls.some(([url,init])=>url==='/api/studio/projects/p1/shots/s1/decision'&&JSON.parse(String(init?.body)).prompt_override===prompt)).toBe(true));
    expect(mockRequest.mock.calls.some(([url,init])=>url==='/api/studio/projects/p1/shots/s1/decision'&&JSON.parse(String(init?.body)).action==='modify')).toBe(true);
  });

  it('discards late prompt responses after switching shots and confirms before replacing edits',async()=>{
    const user=userEvent.setup();
    let resolvePrompt!:(value:{prompt:string})=>void;
    const pending=new Promise<{prompt:string}>(resolve=>{resolvePrompt=resolve;});
    const base=mockRequest.getMockImplementation()!;
    mockRequest.mockImplementation(async(url,init)=>url==='/api/shots/s1/prompt-preview'?pending:url==='/api/shots/s2/prompt-preview'?{prompt:'镜头二的提示词'}:base(url,init));
    render(<MemoryRouter initialEntries={['/studio-review?project=p1']}><StudioReviewPage/></MemoryRouter>);
    await screen.findByTestId('studio-review-video');
    await user.click(screen.getByText('修改此镜头／重生成'));
    await user.click(screen.getByRole('button',{name:'编辑本次提示词'}));
    await user.click(screen.getByRole('button',{name:'下一个镜头'}));
    await act(async()=>{resolvePrompt({prompt:'镜头一迟到的内容'});await pending;});
    expect(screen.queryByLabelText('本次生成提示词')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button',{name:'编辑本次提示词'}));
    expect(await screen.findByLabelText('本次生成提示词')).toHaveValue('镜头二的提示词');
    fireEvent.change(screen.getByLabelText('本次生成提示词'),{target:{value:'镜头二已编辑文本'}});
    vi.mocked(window.confirm).mockReturnValue(false);
    await user.click(screen.getByRole('button',{name:'重新读取原始提示词'}));
    expect(screen.getByLabelText('本次生成提示词')).toHaveValue('镜头二已编辑文本');
    expect(mockRequest.mock.calls.filter(([url])=>url==='/api/shots/s2/prompt-preview')).toHaveLength(1);
  });

  it('keeps pending decisions when the trim is invalid and navigates actual shot sources with keyboard', async () => {
    const user=userEvent.setup();
    render(<MemoryRouter initialEntries={['/studio-review?project=p1']}><StudioReviewPage/></MemoryRouter>);
    await screen.findByTestId('studio-review-video');
    await user.clear(screen.getByLabelText('裁剪起点（秒）'));await user.type(screen.getByLabelText('裁剪起点（秒）'),'8');
    await user.click(screen.getByRole('button',{name:'保留／保存裁剪'}));
    expect(await screen.findByRole('alert')).toHaveTextContent('裁剪范围应在实际视频内');
    expect(mockRequest.mock.calls.filter(([url])=>url.endsWith('/decision'))).toHaveLength(0);
    await user.keyboard('{ArrowRight}');
    await waitFor(()=>expect(screen.getByTestId('studio-review-video')).toHaveAttribute('src',expect.stringContaining('test/shot2.mp4')));
    expect(within(screen.getByRole('complementary',{name:'分镜视频列表'})).getByRole('button',{current:true})).toHaveTextContent('观察雨棚');
  });

  it('previews a retained quality version without writing until keep, resets trim, and invalidates compose confirmation',async()=>{
    const user=userEvent.setup();
    data.shots![0].decision={action:'keep',video_id:'v1',revision:2,position:1,trim_start:1,trim_end:4};
    const revised={id:'v3',shot_id:'s1',relative_path:'test/shot1-4k.mp4',duration_seconds:2,revision:3,metadata_json:{width:3840,height:2160,quality_render_id:'q3'}};
    const base=mockRequest.getMockImplementation()!;
    mockRequest.mockImplementation(async(url,init)=>{
      if(url==='/api/projects/p1/videos')return [...data.shots!.filter(shot=>shot.video).map(shot=>({...shot.video,shot_id:shot.id})),revised];
      if(url.endsWith('/decision')&&JSON.parse(String(init?.body)).video_id==='v3')data.shots![0].video=revised;
      return base(url,init);
    });
    render(<MemoryRouter initialEntries={['/studio-review?project=p1']}><StudioReviewPage/></MemoryRouter>);
    await screen.findByTestId('studio-review-video');
    const confirmation=screen.getByLabelText('我已审核所有镜头，并确认上述顺序、裁剪和输出设置。');
    await waitFor(()=>expect(confirmation).toBeEnabled());await user.click(confirmation);
    expect(screen.getByRole('button',{name:'按审核结果合成成片'})).toBeEnabled();
    await user.selectOptions(screen.getByLabelText('本镜预览与采用版本'),'v3');
    expect(screen.getByTestId('studio-review-video')).toHaveAttribute('src','/api/media/test/shot1-4k.mp4');
    expect(screen.getByLabelText('裁剪起点（秒）')).toHaveValue(0);expect(screen.getByLabelText('裁剪终点（秒）')).toHaveValue(null);
    expect(confirmation).not.toBeChecked();expect(screen.getByRole('button',{name:'按审核结果合成成片'})).toBeDisabled();
    expect(mockRequest.mock.calls.some(([url,init])=>url.endsWith('/decision')&&init?.method==='POST')).toBe(false);
    await user.click(screen.getByRole('button',{name:'保留／保存裁剪'}));
    await waitFor(()=>expect(mockRequest.mock.calls.some(([url])=>url.endsWith('/decision'))).toBe(true));
    const call=mockRequest.mock.calls.find(([url])=>url.endsWith('/decision'))!;
    expect(JSON.parse(String(call[1]?.body))).toEqual(expect.objectContaining({action:'keep',video_id:'v3',trim_start:0,trim_end:null,expected_revision:2}));
  });
  it('protects an unsaved review draft from timeline moves and allows cancelling a shot switch',async()=>{
    const user=userEvent.setup();vi.mocked(window.confirm).mockReturnValue(false);
    render(<MemoryRouter initialEntries={['/studio-review?project=p1']}><StudioReviewPage/></MemoryRouter>);
    await screen.findByTestId('studio-review-video');
    await user.clear(screen.getByLabelText('裁剪起点（秒）'));await user.type(screen.getByLabelText('裁剪起点（秒）'),'0.5');
    expect(screen.getByRole('button',{name:'下移镜头 1'})).toBeDisabled();
    await user.click(screen.getByRole('button',{name:'下一个镜头'}));
    expect(window.confirm).toHaveBeenCalled();
    expect(screen.getByTestId('studio-review-video')).toHaveAttribute('src','/api/media/test/shot1.mp4');
    expect(screen.getByLabelText('裁剪起点（秒）')).toHaveValue(.5);
    expect(mockRequest.mock.calls.some(([url,init])=>url.endsWith('/decision')&&init?.method==='POST')).toBe(false);
  });
  it('seeds custom output from the project profile if source dimensions are missing and preserves a manual edit',async()=>{
    const user=userEvent.setup();data.shots![0].decision.action='keep';
    const base=mockRequest.getMockImplementation()!;
    mockRequest.mockImplementation(async(url,init)=>url==='/api/quality/profiles/project/p1'?{scope:'project',target_id:'p1',revision:1,settings:{resolution:'4k',mode:'standard',aspect_ratio:'16:9',processing:'lanczos'},plan:{requested:{width:3840,height:2160}}}:base(url,init));
    render(<MemoryRouter initialEntries={['/studio-review?project=p1']}><StudioReviewPage/></MemoryRouter>);
    await screen.findByTestId('studio-review-video');
    await user.selectOptions(screen.getByLabelText('输出尺寸模式'),'custom');
    await waitFor(()=>expect(screen.getByLabelText('输出宽度')).toHaveValue(3840));
    expect(screen.getByLabelText('输出高度')).toHaveValue(2160);
    await user.clear(screen.getByLabelText('输出宽度'));await user.type(screen.getByLabelText('输出宽度'),'1920');
    await user.clear(screen.getByLabelText('输出高度'));await user.type(screen.getByLabelText('输出高度'),'1080');
    await user.click(screen.getByRole('button',{name:'刷新实际状态'}));
    expect(screen.getByLabelText('输出宽度')).toHaveValue(1920);expect(screen.getByLabelText('输出高度')).toHaveValue(1080);
    await user.click(screen.getByLabelText('我已审核所有镜头，并确认上述顺序、裁剪和输出设置。'));
    await user.click(screen.getByRole('button',{name:'按审核结果合成成片'}));
    await waitFor(()=>expect(mockRequest.mock.calls.some(([url])=>url.endsWith('/compose'))).toBe(true));
    const call=mockRequest.mock.calls.find(([url])=>url.endsWith('/compose'))!;
    expect(JSON.parse(String(call[1]?.body))).toEqual(expect.objectContaining({width:1920,height:1080}));
  });
});
