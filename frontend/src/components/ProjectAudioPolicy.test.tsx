import {cleanup,render,screen,waitFor} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {afterEach,expect,it,vi} from 'vitest';
import {request} from '../lib/api';
import {ProjectAudioPolicy} from './ProjectAudioPolicy';
vi.mock('../lib/api',async original=>({...await original<typeof import('../lib/api')>(),request:vi.fn()}));
afterEach(()=>{cleanup();vi.mocked(request).mockReset();});
it('keeps the default automatic check and only persists explicit native or skip choices with the project revision',async()=>{
  const mocked=vi.mocked(request),saved=vi.fn(),states:unknown[]=[];mocked.mockImplementation(async(_url,init)=>init?.method==='PUT'?{project_id:'p1',revision:1,dialogue_audio_policy:'native'}:{project_id:'p1',revision:0,dialogue_audio_policy:'auto'});
  const user=userEvent.setup();render(<ProjectAudioPolicy projectId="p1" disabled={false} onSaved={saved} onStateChange={state=>states.push(state)}/>);const select=await screen.findByLabelText('对白声音方式');await waitFor(()=>expect(select).toBeEnabled());expect(select).toHaveValue('auto');expect(mocked.mock.calls.some(([,init])=>init?.method==='PUT')).toBe(false);await user.selectOptions(select,'native');expect(states.at(-1)).toEqual({busy:false,dirty:true});await user.click(screen.getByRole('button',{name:'保存项目声音方式'}));await waitFor(()=>expect(saved).toHaveBeenCalledOnce());
  const sent=mocked.mock.calls.find(([,init])=>init?.method==='PUT')!;expect(sent[0]).toBe('/api/studio/projects/p1/production-preferences');expect(JSON.parse(String(sent[1]?.body))).toEqual({expected_revision:0,dialogue_audio_policy:'native'});expect(states.at(-1)).toEqual({busy:false,dirty:false});
});
