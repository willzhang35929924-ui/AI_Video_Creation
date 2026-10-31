// UI publication-boundary regression. Fixtures do not approve any real identity.
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { request } from '../lib/api';
import { emptyProfile } from '../components/CharacterProfileFields';
import { CharacterFactoryPage } from './CharacterFactoryPage';

vi.mock('../lib/api',async importOriginal=>({...await importOriginal<typeof import('../lib/api')>(),request:vi.fn()}));
const mockedRequest=vi.mocked(request);
describe('factory technical acceptance and formal publication',()=>{
  let detail:Record<string,any>;
  beforeEach(()=>{
    detail={id:'test_actor',display_name:'测试成年人物',current_approved_version_id:null,versions:[{
      id:'technical_v1',version:1,approval_state:'approved',standard_face_asset_id:'anchor_1',anchor_locked_at:'2026-10-23T12:00:00Z',
      identity_json:{profile:{...emptyProfile(),age:38,description:'明确成年原创男性'},studio_acceptance:{actor:'automation'},human_identity_approved:false},
      assets:[{id:'anchor_1',asset_type:'front_face',status:'approved',relative_path:'test/face.png',is_primary_reference:true}],
    }]};
    mockedRequest.mockImplementation(async url=>{
      if(url==='/api/character-factory/characters')return [{id:'test_actor',display_name:'测试成年人物',factory_status:'technical_accepted',approved_assets:1,pending_assets:0,asset_count:1,identity_version:1,version_id:'technical_v1',current_approved_version_id:detail.current_approved_version_id,avatar_url:'/api/media/test/face.png',created_at:'2026-10-23T12:00:00Z'}];
      if(url==='/api/characters/test_actor')return detail;
      if(url==='/api/characters/test_actor/batches')return [];
      throw new Error('Unexpected request: '+url);
    });
  });
  it('labels frozen automatic assets truthfully, permits explicit publication dialog, and keeps batch append disabled',async()=>{
    const user=userEvent.setup();
    render(<MemoryRouter initialEntries={['/characters/test_actor']}><Routes><Route path="/characters/:characterId" element={<CharacterFactoryPage/>}/></Routes></MemoryRouter>);
    expect(await screen.findByText('自动技术采纳，未做人脸人审')).toBeInTheDocument();
    expect(screen.queryByText('正式版本已锁定')).not.toBeInTheDocument();
    expect(screen.getByLabelText('人物描述')).toBeDisabled();
    await user.click(screen.getByRole('button',{name:'批量生成'}));
    expect(screen.getByRole('button',{name:'批量生成素材'})).toBeDisabled();
    expect(screen.getByText('已冻结版本不能追加素材，请创建独立新版本。')).toBeInTheDocument();
    await user.click(screen.getByRole('button',{name:'锁定正式版本'}));
    const dialog=screen.getByRole('alertdialog');
    expect(dialog).toHaveTextContent('当前仅为自动技术采纳，尚未做人脸人审');
    expect(within(dialog).getByRole('button',{name:'确认锁定正式版本'})).toBeEnabled();
    await user.click(within(dialog).getByRole('button',{name:'取消'}));
    expect(mockedRequest.mock.calls.filter(([,init])=>init?.method&&init.method!=='GET')).toHaveLength(0);
  });
  it('marks a version formal only when the current published pointer selects it',async()=>{
    detail.current_approved_version_id='technical_v1';
    render(<MemoryRouter initialEntries={['/characters/test_actor']}><Routes><Route path="/characters/:characterId" element={<CharacterFactoryPage/>}/></Routes></MemoryRouter>);
    expect(await screen.findByText('正式版本已锁定')).toBeInTheDocument();
    expect(screen.queryByText('自动技术采纳，未做人脸人审')).not.toBeInTheDocument();
    expect(screen.queryByRole('button',{name:'锁定正式版本'})).not.toBeInTheDocument();
  });
  it('does not call the technically accepted overview a formally approved face',async()=>{
    render(<MemoryRouter><CharacterFactoryPage/></MemoryRouter>);
    const card=await screen.findByRole('button',{name:/测试成年人物.*自动技术采纳/});
    expect(within(card).getByText('自动技术采纳，未做人脸人审')).toBeInTheDocument();
    expect(within(card).getByText('可用素材')).toBeInTheDocument();
    expect(within(card).queryByText('已批准')).not.toBeInTheDocument();
    expect(within(card).getByRole('img')).toHaveAttribute('alt',expect.stringContaining('自动技术采纳'));
  });
  it('retains the originating project through the manual candidate wizard preview',async()=>{
    detail.versions[0]={...detail.versions[0],approval_state:'draft',standard_face_asset_id:null,anchor_locked_at:null,assets:[]};
    const original=mockedRequest.getMockImplementation()!;
    mockedRequest.mockImplementation((url,init)=>{
      if(url==='/api/settings/ai-services?module=character_generation')return Promise.resolve([{id:'image',name:'图像服务',provider:'comfyui',default_model:'klein',enabled:true,is_default:true,models:[],settings:{candidate_model:'klein'}}]);
      if(url==='/api/characters/test_actor/batch-preview')return Promise.resolve({jobs:[],service_snapshot:{name:'图像服务',model_id:'klein'},preview_hash:'preview'});
      return original(url,init);
    });
    const user=userEvent.setup();render(<MemoryRouter initialEntries={['/characters/test_actor?project=project-origin']}><Routes><Route path="/characters/:characterId" element={<CharacterFactoryPage/>}/></Routes></MemoryRouter>);
    expect(await screen.findByRole('link',{name:'返回项目工作台'})).toHaveAttribute('href','/?project=project-origin');
    await user.click(screen.getByRole('button',{name:'标准脸'}));await user.click(screen.getByRole('button',{name:'生成标准脸候选'}));await user.click(screen.getByRole('button',{name:'下一步'}));await user.click(screen.getByRole('button',{name:'下一步'}));await screen.findByRole('option',{name:/图像服务/});await user.click(screen.getByRole('button',{name:'生成提交预览'}));
    const preview=mockedRequest.mock.calls.find(([url])=>url.endsWith('/batch-preview'))!;
    expect(JSON.parse(String(preview[1]?.body))).toMatchObject({project_id:'project-origin',version_id:'technical_v1',kind:'candidates'});
  });
});
