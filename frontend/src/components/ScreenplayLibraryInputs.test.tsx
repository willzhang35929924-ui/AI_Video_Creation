import {cleanup,render,screen} from '@testing-library/react';
import {MemoryRouter} from 'react-router-dom';
import {afterEach,expect,it,vi} from 'vitest';
import {request} from '../lib/api';
import {ReferenceVideoPicker} from './ScreenplayLibraryInputs';
vi.mock('../lib/api',async original=>({...await original<typeof import('../lib/api')>(),request:vi.fn()}));
afterEach(()=>{cleanup();vi.mocked(request).mockReset();});
it('explains a historical style profile above the model source limit without silently truncating its IDs',async()=>{
  const ids=['v1','v2','v3','v4','v5'],onIds=vi.fn();vi.mocked(request).mockImplementation(async url=>url==='/api/reference-videos'?{items:[],limits:{}}:{items:[{id:'over-limit',name:'五视频来源',video_ids:ids,version:1}]});render(<MemoryRouter><ReferenceVideoPicker ids={[]} onIds={onIds} primary="" onPrimary={vi.fn()} styleId="" onStyle={vi.fn()} disabled={false} initialStyleId="over-limit"/></MemoryRouter>);expect(await screen.findByRole('alert')).toHaveTextContent('不会自动截断原档案');expect(screen.getByRole('option',{name:/五视频来源.*超出本次4条限制/})).toBeDisabled();expect(onIds).not.toHaveBeenCalled();
});
