import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import * as transport from '../lib/api';
import { DeleteRecordsDialog } from './DeleteRecordsDialog';

afterEach(()=>vi.restoreAllMocks());

it('ignores old deletion previews after the user changes file mode',async()=>{
  const previews:((value:unknown)=>void)[]=[];
  const request=vi.spyOn(transport,'request').mockImplementation(()=>new Promise(resolve=>previews.push(resolve)));
  const ids=['isolated-file'];
  render(<DeleteRecordsDialog resource="files" ids={ids} onClose={()=>{}} onComplete={()=>{}}/>);
  await waitFor(()=>expect(previews).toHaveLength(1));
  fireEvent.change(screen.getByLabelText('文件删除模式'),{target:{value:'permanent'}});
  fireEvent.click(screen.getByLabelText('确认永久删除'));
  await waitFor(()=>expect(previews).toHaveLength(2));
  const plan=(mode:string)=>({mode,cascade:false,fingerprint:mode,record_count:1,file_count:0,total_bytes:0,exclusive_bytes:0,records:[],dependencies:[],blockers:[],files:[],space_note:''});
  await act(async()=>{previews[1](plan('permanent'));});
  await waitFor(()=>expect(screen.getByRole('button',{name:'确认执行删除'})).toBeEnabled());
  await act(async()=>{previews[0](plan('keep'));});
  request.mockResolvedValueOnce({id:'isolated-delete',status:'completed',result:{}});
  fireEvent.click(screen.getByRole('button',{name:'确认执行删除'}));
  await screen.findByRole('status');
  const sent=request.mock.calls.find(([url])=>url==='/api/deletions/execute');
  expect(JSON.parse(String(sent?.[1]?.body))).toMatchObject({mode:'permanent',fingerprint:'permanent'});
});
