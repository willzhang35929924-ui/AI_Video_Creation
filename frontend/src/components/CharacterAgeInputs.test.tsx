// UI payload contracts. No model generation or real identity approval occurs.
import {useState} from 'react';
import {cleanup,render,screen,waitFor,within} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {MemoryRouter,Route,Routes} from 'react-router-dom';
import {afterEach,expect,it,vi} from 'vitest';
import {request} from '../lib/api';
import {CharacterProfileFields,emptyProfile,readProfile} from './CharacterProfileFields';
import {CharacterFactoryPage} from '../pages/CharacterFactoryPage';
import {WordImportPanel} from './WordImportPanel';
import {ShotWorkspace} from './ShotWorkspace';
import {ToastProvider} from './ToastProvider';

vi.mock('../lib/api',async original=>({...await original<typeof import('../lib/api')>(),request:vi.fn()}));
const mocked=vi.mocked(request);
afterEach(()=>{cleanup();mocked.mockReset();vi.restoreAllMocks();});

it('keeps unknown age blank and permits ordinary age values without an adult confirmation',async()=>{
  const changed=vi.fn();
  function Fields(){const [profile,setProfile]=useState(emptyProfile());return <CharacterProfileFields value={profile} onChange={value=>{setProfile(value);changed(value);}}/>;}
  render(<Fields/>);const age=screen.getByLabelText('年龄（可选）');
  expect(age).toHaveValue(null);expect(age).not.toBeRequired();expect(age).not.toHaveAttribute('max');expect(screen.queryByRole('checkbox')).not.toBeInTheDocument();expect(screen.getByLabelText('人物描述')).not.toBeRequired();
  const user=userEvent.setup();await user.type(age,'9');expect(changed.mock.calls.at(-1)?.[0].age).toBe(9);
  await user.clear(age);expect(changed.mock.calls.at(-1)?.[0].age).toBeNull();await user.type(age,'160');expect(changed.mock.calls.at(-1)?.[0].age).toBe(160);
  expect(emptyProfile().age).toBeNull();expect(emptyProfile().original_adult).toBeNull();
  const partial=readProfile({profile:{description:'剧本只写明记录员'}});expect(partial.age).toBeNull();expect(partial.description).toBe('剧本只写明记录员');expect(partial.fixed_traits).toEqual([]);
  const preferred=readProfile({profile:{age:9,hair:'已保存发型'},source_profile:{age:160,hair:'旧原文发型'}});expect(preferred.age).toBe(9);expect(preferred.hair).toBe('已保存发型');
  const flat=readProfile({profile:{},hairstyle:'旧版顶层发型',facial_features:'旧版顶层面容'});expect(flat.hair).toBe('旧版顶层发型');expect(flat.facial_features).toBe('旧版顶层面容');
});

it('creates a library character with just ID and name without inventing age or requiring a description',async()=>{
  mocked.mockImplementation(async(url,init)=>{
    if(url==='/api/character-factory/characters')return init?.method==='POST'?{character:{id:'new_record_keeper'}}:[];
    throw new Error('Unexpected URL '+url);
  });
  const user=userEvent.setup();render(<MemoryRouter><CharacterFactoryPage/></MemoryRouter>);
  await user.click(await screen.findByRole('button',{name:'新建人物'}));
  const dialog=screen.getByRole('dialog',{name:'创建人物'});await user.type(within(dialog).getByLabelText('稳定人物 ID'),'new_record_keeper');await user.type(within(dialog).getByLabelText('人物名称'),'记录员');
  expect(within(dialog).queryByRole('checkbox')).not.toBeInTheDocument();
  await user.click(within(dialog).getByRole('button',{name:'创建人物与 v1 档案'}));
  await waitFor(()=>expect(mocked.mock.calls.some(([,init])=>init?.method==='POST')).toBe(true));
  const sent=mocked.mock.calls.find(([,init])=>init?.method==='POST')!;
  expect(JSON.parse(String(sent[1]?.body))).toMatchObject({id:'new_record_keeper',name:'记录员',profile:{age:null,description:'',original_adult:null}});
});

for(const legacyProfile of [null,{}])for(const age of [undefined,'未知','160岁'])it(`preserves legacy source-only identity traits in the saved profile ${JSON.stringify(legacyProfile)}, age ${age}`,async()=>{
  const source={age,description:'街头记录员',facial_features:'椭圆脸',eyebrows:'平眉',nose:'直鼻',lips:'薄唇',jaw:'下颌清晰',body:'匀称身材',shoulders:'肩宽适中',hands:'手指修长',hair:'短黑发',hairline:'平直发际线',eyes:'深褐色杏眼',skin:'自然肤质',marks:'左脸小痣',immutable_traits:'黑发；左脸小痣',wardrobe:'灰色夹克'};
  const identity={profile:legacyProfile,source_profile:source,needs_confirmation:['旧版年龄提示']},original=structuredClone(identity);
  const detail={id:'legacy_actor',display_name:'记录员',current_approved_version_id:null,versions:[{id:'legacy-v1',version:1,approval_state:'draft',identity_json:identity,assets:[]}]};
  mocked.mockImplementation(async(url,init)=>{
    if(url==='/api/character-factory/characters')return [];
    if(url==='/api/characters/legacy_actor')return detail;
    if(url==='/api/characters/legacy_actor/batches')return [];
    if(url==='/api/characters/legacy_actor/profile'&&init?.method==='PATCH')return {};
    throw new Error('Unexpected URL '+url);
  });
  const user=userEvent.setup();render(<MemoryRouter initialEntries={['/characters/legacy_actor']}><Routes><Route path="/characters/:characterId" element={<CharacterFactoryPage/>}/></Routes></MemoryRouter>);
  const hair=await screen.findByLabelText('发型与发色');await waitFor(()=>expect(hair).toHaveValue('短黑发；平直发际线'));
  expect(screen.getByLabelText('面部特征')).toHaveValue('椭圆脸；平眉；直鼻；薄唇；下颌清晰');expect(screen.getByLabelText('可识别标记')).toHaveValue('左脸小痣');
  expect(screen.getByLabelText('年龄（可选）')).toHaveValue(age==='160岁'?160:null);
  await user.click(screen.getByRole('button',{name:'保存身份档案'}));await waitFor(()=>expect(mocked.mock.calls.some(([,init])=>init?.method==='PATCH')).toBe(true));
  const sent=mocked.mock.calls.find(([,init])=>init?.method==='PATCH')!;const profile=JSON.parse(String(sent[1]?.body)).profile;
  expect(profile).toMatchObject({age:age==='160岁'?160:null,facial_features:'椭圆脸；平眉；直鼻；薄唇；下颌清晰',body:'匀称身材；肩宽适中；手指修长',hair:'短黑发；平直发际线',eyes:'深褐色杏眼',skin:'自然肤质',identifying_marks:'左脸小痣',fixed_traits:['黑发','左脸小痣'],wardrobes:[{id:'default',label:'默认服装',description:'灰色夹克'}]});
  expect(profile.description).toBe(age==='未知'?'街头记录员；原文年龄描述：未知':'街头记录员');expect(identity).toEqual(original);
});

it('clears an imported age by removing the field rather than converting an unknown age to zero',async()=>{
  const batch={id:'import1',status:'pending_review',revision:1,filename:'script.docx',duplicates:[],issues:[],blocks_json:[],summary:{characters:1,scenes:0,shots:0,dialogues:0,suggestions:0,unrecognized:0},preview_json:{project:{name:'家庭日常'},characters:[{key:'p',name:'记录员',aliases:[],source:[],decision:'new',profile:{age:12,description:'记录员'}}],scenes:[],shots:[],unrecognized:[]}};
  mocked.mockImplementation(async(url,init)=>{
    if(url==='/api/characters'||url==='/api/projects')return [];
    if(url==='/api/word-imports/import1')return structuredClone(batch);
    if(url==='/api/word-imports/import1/preview'&&init?.method==='PUT')return {...structuredClone(batch),issues:['本测试只核对编辑请求']};
    throw new Error('Unexpected URL '+url);
  });
  const user=userEvent.setup();render(<WordImportPanel importId="import1"/>);await user.click(await screen.findByText('核对与修正人物资料'));
  const age=screen.getByLabelText('记录员年龄（可选）');expect(age).toHaveValue(12);expect(age).toHaveAttribute('min','0');expect(age).not.toBeRequired();await user.clear(age);await user.click(screen.getByRole('button',{name:'确认预览并导入'}));
  await waitFor(()=>expect(mocked.mock.calls.some(([,init])=>init?.method==='PUT')).toBe(true));
  const sent=mocked.mock.calls.find(([,init])=>init?.method==='PUT')!;const profile=JSON.parse(String(sent[1]?.body)).preview.characters[0].profile;
  expect(profile).toEqual({description:'记录员'});
});

for(const age of [null,8,160])it(`creates a shot participant with optional age ${age} and no adult confirmation`,async()=>{
  mocked.mockImplementation(async(url,init)=>{
    if(url==='/api/shots/shot1/workspace')return {shot:{revision:1},scene:null,detail:{},participants:[],dialogues:[],audio_events:[]};
    if(url==='/api/characters'||url==='/api/projects/project1/scenes')return [];
    if(url==='/api/character-factory/characters'&&init?.method==='POST')return {character:{id:'new'}};
    if(url==='/api/shots/shot1/participants'&&init?.method==='POST')return {};
    throw new Error('Unexpected URL '+url);
  });
  const user=userEvent.setup();render(<ToastProvider><ShotWorkspace shotId="shot1" projectId="project1" onSaved={async()=>{}}/></ToastProvider>);
  await user.click(await screen.findByText('添加人物或新建人物档案'));await user.type(screen.getByLabelText('新人物名称'),'记录员');
  const input=screen.getByLabelText('新人物年龄');expect(input).not.toBeRequired();expect(input).not.toHaveAttribute('max');if(age!==null)await user.type(input,String(age));
  await user.click(screen.getByRole('button',{name:'新建并加入'}));
  await waitFor(()=>expect(mocked.mock.calls.some(([url,init])=>url==='/api/character-factory/characters'&&init?.method==='POST')).toBe(true));
  const sent=mocked.mock.calls.find(([url,init])=>url==='/api/character-factory/characters'&&init?.method==='POST')!;
  expect(JSON.parse(String(sent[1]?.body)).profile).toEqual({age,description:''});
});
