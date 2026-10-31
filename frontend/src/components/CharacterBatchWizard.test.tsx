// Only API propagation contracts; no actual model tasks are submitted.
import {cleanup,render,screen,waitFor} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {afterEach,beforeEach,expect,it,vi} from 'vitest';
import {request} from '../lib/api';
import {CharacterBatchWizard} from './CharacterBatchWizard';
vi.mock('../lib/api',async original=>({...await original<typeof import('../lib/api')>(),request:vi.fn()}));
const mocked=vi.mocked(request);
beforeEach(()=>{mocked.mockImplementation(async(url)=>{
  if(url==='/api/settings/ai-services?module=character_generation')return [{id:'image',name:'图像服务',enabled:true,is_default:true,provider:'comfyui',default_model:'klein',models:[{id:'klein'}],settings:{candidate_model:'klein'}}];
  if(url==='/api/characters/actor-a/batch-preview')return {jobs:[{type:'front_face',seed:12,prompt:'neutral adult portrait'}],service_snapshot:{name:'图像服务',model_id:'klein',service_id:'image',service_revision:1},preview_hash:'pinned-hash',cost_note:'local'};
  if(url==='/api/characters/actor-a/batch-generate')return {id:'batch-a'};
  throw new Error('Unexpected wizard URL '+url);
});});
afterEach(()=>{cleanup();mocked.mockReset();});
for(const projectId of ['project-a',undefined])it(projectId?'keeps project context in both preview and confirmed manual generation':'omits project context for standalone global-library generation',async()=>{
  const user=userEvent.setup(),created=vi.fn();render(<CharacterBatchWizard character={{id:'actor-a',display_name:'角色甲'}} version={{id:'v1',version:1,identity_json:{profile:{wardrobes:[]}}}} anchor={null} kind="candidates" projectId={projectId} onBack={vi.fn()} onCreated={created}/>);
  await user.click(screen.getByRole('button',{name:'下一步'}));await user.click(screen.getByRole('button',{name:'下一步'}));await screen.findByRole('option',{name:/图像服务/});
  await user.click(screen.getByRole('button',{name:'生成提交预览'}));await screen.findByRole('button',{name:'确认提交真实任务'});await user.click(screen.getByRole('button',{name:'确认提交真实任务'}));await waitFor(()=>expect(created).toHaveBeenCalledTimes(1));
  const calls=mocked.mock.calls.filter(([,init])=>init?.method==='POST');expect(calls).toHaveLength(2);
  for(const [,init] of calls){const payload=JSON.parse(String(init?.body));expect(payload.version_id).toBe('v1');expect(payload.negative_prompt).not.toMatch(/\b(?:child|teenager)\b/i);if(projectId)expect(payload.project_id).toBe(projectId);else expect(payload).not.toHaveProperty('project_id');}
  expect(JSON.parse(String(calls[1][1]?.body))).toMatchObject({confirm:true,preview_hash:'pinned-hash'});
});
