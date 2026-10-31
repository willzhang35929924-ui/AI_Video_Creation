// Isolated form/ownership contracts; no model generation or real image is claimed.
import {cleanup,render,screen,waitFor,within} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {afterEach,beforeEach,expect,it,vi} from 'vitest';
import {ApiError,request} from '../lib/api';
import {ProjectCharacterReferences} from './ProjectCharacterReferences';
vi.mock('../lib/api',async original=>({...await original<typeof import('../lib/api')>(),request:vi.fn()}));
const mocked=vi.mocked(request),endpoint='/api/studio/projects/p1/character-references';
const asset={id:'ref-a',project_id:'p1',character_id:'a',state:'draft',purpose:'identity_front',order:1,sha256:'a'.repeat(64),width:832,height:1024,mime_type:'image/png',bytes:1234,media_url:'/api/media/refs/a.png',source_note:'原创人物'};
let data:any,conflict:boolean;
beforeEach(()=>{conflict=false;data={project_id:'p1',characters:[{character_id:'a',name:'人物甲',description:'成年记录员',revision:1,references:[],drafts:[],state:'no_reference',last_application:null},{character_id:'b',name:'人物乙',description:'成年工程师',revision:1,references:[],drafts:[],state:'no_reference',last_application:null}],limits:{max_images:4,max_upload_bytes:20*1024**2}};
  mocked.mockImplementation(async(url,init)=>{
    if(url===endpoint&&!init?.method)return structuredClone(data);
    if(url===endpoint+'/a/uploads'){data.characters[0].drafts=[asset];return structuredClone(asset);}
    if(url===endpoint+'/a'&&init?.method==='PUT'){
      if(conflict)throw new ApiError('参考已在另一窗口改变',409,'reference_revision_changed');
      const body=JSON.parse(String(init.body));data.characters[0]={...data.characters[0],revision:2,references:body.references.length?[{...asset,state:'saved'}]:[],drafts:[],state:body.references.length?'saved':'no_reference'};return structuredClone(data);
    }
    throw new Error('Unexpected reference URL '+url);
  });});
afterEach(()=>{cleanup();mocked.mockReset();});
async function mount(){const user=userEvent.setup();render(<ProjectCharacterReferences projectId="p1"/>);expect(mocked).not.toHaveBeenCalled();await user.click(screen.getByText('人物角色参考图（可选）'));await screen.findByRole('article',{name:'人物甲人物参考图'});return user;}

it('uploads only to the chosen character and requires a separate explicit save',async()=>{
  const user=await mount(),card=screen.getByRole('article',{name:'人物甲人物参考图'});
  await user.upload(within(card).getByLabelText('上传人物甲人物参考图'),new File(['unit png'],'face.png',{type:'image/png'}));
  await user.type(within(card).getByLabelText('人物甲图片来源说明'),'原创人物');await user.click(within(card).getByRole('button',{name:'上传并加入人物草稿'}));
  expect(await within(card).findByAltText('人物甲参考图 1')).toHaveAttribute('src','/api/media/refs/a.png');
  expect(mocked.mock.calls.filter(([,init])=>init?.method==='PUT')).toHaveLength(0);
  expect(within(screen.getByRole('article',{name:'人物乙人物参考图'})).queryByRole('img')).not.toBeInTheDocument();
  await user.click(within(card).getByRole('button',{name:'保存人物甲人物参考'}));
  await within(card).findByText('参考图已保存，仅用于此人物后续新任务。');
  const save=mocked.mock.calls.find(([,init])=>init?.method==='PUT')!;expect(save[0]).toBe(endpoint+'/a');expect(JSON.parse(String(save[1]?.body))).toEqual({expected_revision:1,references:[{asset_id:'ref-a',purpose:'identity_front',order:1}]});
  expect(mocked.mock.calls.some(([url])=>url.includes('/b/')||url.includes('/tasks'))).toBe(false);
});

it('preserves the draft after a revision conflict instead of overwriting newer references',async()=>{
  const user=await mount(),card=screen.getByRole('article',{name:'人物甲人物参考图'});conflict=true;
  await user.upload(within(card).getByLabelText('上传人物甲人物参考图'),new File(['unit png'],'face.png',{type:'image/png'}));await user.click(within(card).getByRole('button',{name:'上传并加入人物草稿'}));await within(card).findByAltText('人物甲参考图 1');
  await user.click(within(card).getByRole('button',{name:'保存人物甲人物参考'}));await within(card).findByRole('alert');
  expect(within(card).getByAltText('人物甲参考图 1')).toBeInTheDocument();expect(within(card).getByRole('button',{name:'保存人物甲人物参考'})).toBeDisabled();
  await user.click(within(card).getByRole('button',{name:'放弃未保存修改'}));
  await waitFor(()=>expect(within(card).queryByAltText('人物甲参考图 1')).not.toBeInTheDocument());
  expect(mocked.mock.calls.filter(([,init])=>init?.method==='PUT')).toHaveLength(1);
});

it('uploads several selected angles only to their stable character ID and saves their explicit order',async()=>{
  const original=mocked.getMockImplementation()!;let count=0;
  mocked.mockImplementation(async(url,init)=>{
    if(url===endpoint+'/a/uploads'){count++;const item={...asset,id:`ref-${count}`};data.characters[0].drafts.push(item);return item;}
    if(url===endpoint+'/a'&&init?.method==='PUT'){const body=JSON.parse(String(init.body));data.characters[0].references=body.references.map((reference:any)=>({...asset,id:reference.asset_id,order:reference.order,state:'saved'}));return structuredClone(data);}
    return original(url,init);
  });
  const user=await mount(),card=screen.getByRole('article',{name:'人物甲人物参考图'});await user.upload(within(card).getByLabelText('上传人物甲人物参考图'),[new File(['front'],'front.png',{type:'image/png'}),new File(['side'],'side.png',{type:'image/png'})]);await user.click(within(card).getByRole('button',{name:'上传并加入人物草稿'}));await within(card).findByAltText('人物甲参考图 2');await user.click(within(card).getByRole('button',{name:'保存人物甲人物参考'}));
  const calls=mocked.mock.calls.filter(([url])=>url.endsWith('/uploads'));expect(calls.map(([url])=>url)).toEqual([endpoint+'/a/uploads',endpoint+'/a/uploads']);const save=mocked.mock.calls.find(([,init])=>init?.method==='PUT')!;expect(JSON.parse(String(save[1]?.body)).references).toEqual([{asset_id:'ref-1',purpose:'identity_front',order:1},{asset_id:'ref-2',purpose:'identity_front',order:2}]);expect(within(screen.getByRole('article',{name:'人物乙人物参考图'})).queryByRole('img')).not.toBeInTheDocument();
});
