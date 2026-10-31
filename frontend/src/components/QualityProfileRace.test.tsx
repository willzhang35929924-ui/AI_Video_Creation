// Deferred HTTP fixture regressions. No real settings are written.
import {act,renderHook,waitFor} from '@testing-library/react';
import {afterEach,expect,it,vi} from 'vitest';
import {request} from '../lib/api';
import {useQualityProfile} from './QualitySettings';
vi.mock('../lib/api',async original=>({...await original<typeof import('../lib/api')>(),request:vi.fn()}));
const mocked=vi.mocked(request);
const profile=(id:string,resolution='720p')=>({scope:'project',target_id:id,revision:1,settings:{resolution,mode:'standard',aspect_ratio:'16:9',processing:'lanczos'},plan:{}});
afterEach(()=>mocked.mockReset());
it('rejects a late A save without replacing B settings and deduplicates the same in-flight save',async()=>{
  let finish!:(value:any)=>void;const oldSave=new Promise(resolve=>{finish=resolve;});
  mocked.mockImplementation(async(url,init)=>url==='/api/quality/capabilities'?{ai_super_resolution:{state:'not_configured'}}:init?.method==='PUT'?oldSave:profile(url.endsWith('/a')?'a':'b',url.endsWith('/a')?'720p':'1080p'));
  const {result,rerender}=renderHook(({id})=>useQualityProfile('project',id),{initialProps:{id:'a'}});
  await waitFor(()=>expect(result.current.profile?.target_id).toBe('a'));
  let saving!:Promise<unknown>,second!:Promise<unknown>;
  act(()=>{saving=result.current.save().catch(e=>e);second=result.current.save().catch(e=>e);});
  expect(mocked.mock.calls.filter(([,init])=>init?.method==='PUT')).toHaveLength(1);
  rerender({id:'b'});await waitFor(()=>expect(result.current.profile?.target_id).toBe('b'));
  await act(async()=>{finish(profile('a','4k'));await saving;await second;});
  expect(result.current.profile?.target_id).toBe('b');expect(result.current.settings.resolution).toBe('1080p');expect(result.current.saving).toBe(false);
});
it('ignores a late manual A read after the automatic B read',async()=>{
  let count=0,finish!:(value:any)=>void;const oldRead=new Promise(resolve=>{finish=resolve;});
  mocked.mockImplementation(async url=>{if(url==='/api/quality/capabilities')return {};if(url.endsWith('/a'))return ++count===1?profile('a'):oldRead;return profile('b','2k');});
  const {result,rerender}=renderHook(({id})=>useQualityProfile('project',id),{initialProps:{id:'a'}});
  await waitFor(()=>expect(result.current.profile?.target_id).toBe('a'));
  let reading!:Promise<void>;act(()=>{reading=result.current.load();});rerender({id:'b'});
  await waitFor(()=>expect(result.current.profile?.target_id).toBe('b'));
  await act(async()=>{finish(profile('a','4k'));await reading;});
  expect(result.current.profile?.target_id).toBe('b');expect(result.current.settings.resolution).toBe('2k');
});
it('settles loading when a save supersedes an unfinished manual read',async()=>{
  let count=0,finish!:(value:any)=>void;const oldRead=new Promise(resolve=>{finish=resolve;});
  mocked.mockImplementation(async(url,init)=>{if(url==='/api/quality/capabilities')return {};if(init?.method==='PUT')return {...profile('a','1080p'),revision:2};return ++count===1?profile('a'):oldRead;});
  const {result}=renderHook(()=>useQualityProfile('project','a'));
  await waitFor(()=>expect(result.current.profile?.target_id).toBe('a'));
  let read!:Promise<void>;act(()=>{read=result.current.load();});
  expect(result.current.loading).toBe(true);
  await act(async()=>{await result.current.save();});
  expect(result.current.loading).toBe(false);expect(result.current.saving).toBe(false);expect(result.current.settings.resolution).toBe('1080p');
  await act(async()=>{finish(profile('a','720p'));await read;});
  expect(result.current.loading).toBe(false);expect(result.current.settings.resolution).toBe('1080p');
});
