import json
from io import BytesIO
from pathlib import Path
from docx import Document
from sqlalchemy import select
from app import models as m


def people(client):
    for i in range(3):
        response=client.post('/api/character-factory/characters',json={'id':f'person_{i}','name':f'人物{i}','profile':{'age':30+i,'description':f'原创成年人物{i}'}})
        assert response.status_code==201,response.text
    return ['person_0','person_1','person_2']


def new_shot(client):
    p=client.post('/api/projects',json={'name':'多人测试','source_text':'原文保留'}).json()
    s=client.post(f"/api/projects/{p['id']}/shots",json={'order':1,'scene_title':'控制室','visual_prompt':'三名成年人在工作桌旁'}).json()
    return p,s


def test_three_participants_independent_dialogue_persistence_and_removal(client):
    ids=people(client);project,shot=new_shot(client)
    url=f"/api/shots/{shot['id']}"
    response=client.post(url+'/participants',json={'character_ids':ids});assert response.status_code==200,response.text
    participants=response.json()['participants'];assert len(participants)==3
    client.post(url+'/participants',json={'character_ids':ids})
    assert len(client.get(url+'/workspace').json()['participants'])==3
    for i,p in enumerate(participants):
        r=client.patch('/api/participants/'+p['id'],json={'performance':{'action':f'动作{i}','position':f'位置{i}','emotion':f'情绪{i}','wardrobe':f'服装{i}'}})
        assert r.status_code==200,r.text
        for n in range(2):
            r=client.post(url+'/dialogues',json={'participant_id':p['id'],'text':f'{i}的第{n}句','order':i*2+n+1,'delivery':{'tone':'低声','pause_seconds':0.5}})
            assert r.status_code==201,r.text
    after=client.get(url+'/workspace').json();assert len(after['dialogues'])==6
    client.patch('/api/participants/'+participants[0]['id'],json={'performance':{'action':'仅修改第一人'}})
    after=client.get(url+'/workspace').json();assert after['participants'][1]['performance_json']['action']=='动作1'
    assert client.delete('/api/participants/'+participants[0]['id']).status_code==409
    assert client.delete('/api/participants/'+participants[0]['id']+'?remove_dialogues=true').status_code==200
    assert client.get('/api/characters/'+participants[0]['character_id']).status_code==200
    after=client.get(url+'/workspace').json();assert len(after['participants'])==2 and len(after['dialogues'])==4
    _,other=new_shot(client)
    foreign=client.post(f"/api/shots/{other['id']}/participants",json={'character_ids':[ids[0]]}).json()['participants'][0]
    assert client.post(url+'/dialogues',json={'participant_id':foreign['id'],'text':'错误说话人'}).status_code==422


def document_bytes():
    doc=Document();doc.add_heading('剧本示例',0);doc.add_heading('项目基本信息',1)
    doc.add_paragraph('项目名称：多人夜班');doc.add_paragraph('画幅：16:9');doc.add_heading('人物设定',1)
    for i,name in enumerate(['人物0','新人物甲','新人物乙']):
        doc.add_heading('人物：'+name,2)
        table=doc.add_table(rows=0,cols=2)
        for key,value in [('年龄',str(30+i)),('身份','海岸站工程师'),('外观','普通成年人，自然肤质'),('声音','低沉而清晰')]:
            cells=table.add_row().cells;cells[0].text=key;cells[1].text=value
    doc.add_heading('正文剧情',1)
    for i in range(2):
        doc.add_heading(f'场景 {i+1} 控制室{i+1}',1);doc.add_paragraph('地点：真实值班室');doc.add_paragraph('环境声：雨声')
        doc.add_heading(f'分镜 {i+1} 三人讨论',2);doc.add_paragraph('出场人物：人物0、新人物甲、新人物乙');doc.add_paragraph('画面：三人站在设备桌旁')
        table=doc.add_table(rows=1,cols=4)
        for c,v in zip(table.rows[0].cells,['人物','动作','情绪','位置']):c.text=v
        for name in ['人物0','新人物甲','新人物乙']:
            for c,v in zip(table.add_row().cells,[name,'看向桌面','克制','画面左侧']):c.text=v
        doc.add_paragraph('对白：人物0：开始核对。');doc.add_paragraph('对白：新人物甲（低声）：收到。');doc.add_paragraph('音效：按钮按压声')
    out=BytesIO();doc.save(out);return out.getvalue()


def test_docx_preview_tables_sources_atomic_commit_and_repeat(client):
    people(client)
    source_bytes=document_bytes()
    result=client.post('/api/word-imports',files={'file':('example.docx',source_bytes)})
    assert result.status_code==201,result.text
    batch=result.json();assert batch['status']=='pending_review'
    assert batch['summary']['characters']==3 and batch['summary']['scenes']==2 and batch['summary']['dialogues']==4
    assert any(b['kind']=='table' for b in batch['blocks_json'])
    assert batch['summary']['suggestions']==4
    assert batch['preview_json']['characters'][0]['match_character_id']=='person_0'
    assert len(client.get('/api/projects').json())==0
    response=client.post(f"/api/word-imports/{batch['id']}/confirm",json={'revision':batch['revision'],'duplicate_strategy':'new_project'})
    assert response.status_code==200,response.text
    imported=response.json();assert len(imported['new_characters'])==2
    bundle=client.get('/api/shots/'+imported['shot_ids'][0]+'/workspace').json()
    assert len(bundle['participants'])==3 and len(bundle['dialogues'])==2
    assert bundle['participants'][0]['performance_json']['action']=='看向桌面'
    assert len(client.get('/api/projects/'+imported['project_id']+'/scenes').json())==2
    again=client.post(f"/api/word-imports/{batch['id']}/confirm",json={'revision':batch['revision'],'duplicate_strategy':'new_project'})
    assert again.json()['project_id']==imported['project_id']
    duplicate=client.post('/api/word-imports',files={'file':('example.docx',source_bytes)}).json()
    assert duplicate['duplicates']
    bad=client.post('/api/word-imports',files={'file':('old.doc',b'old binary')}).json()
    assert bad['status']=='failed' and '.docx' in bad['error_message']


def delete(client,resource,ids,mode='keep',cascade=False):
    payload={'resource':resource,'ids':ids,'mode':mode,'cascade':cascade}
    p=client.post('/api/deletions/preview',json=payload);assert p.status_code==200,p.text
    return p.json(),client.post('/api/deletions/execute',json={**payload,'fingerprint':p.json()['fingerprint'],'confirm':True,'confirm_permanent':mode=='permanent'})


def test_delete_modes_real_files_shared_retained_trash_restore_and_purge(client,settings):
    settings.media_root.mkdir(exist_ok=True)
    with client.app.state.session_factory() as s:
        items=[]
        for i in range(3):
            relative=f'exports/file{i}.txt';(settings.media_root/'exports').mkdir(exist_ok=True)
            (settings.media_root/relative).write_bytes(b'real output file')
            f=m.ManagedFile(relative_path=relative,state='retained',metadata_json={'verified_owned':True});s.add(f);s.flush();items.append(f.id)
        s.commit()
    plan,response=delete(client,'files',[items[0]],'keep');assert response.status_code==200,response.text
    assert response.json()['status']=='completed' and (settings.media_root/'exports/file0.txt').exists()
    _,response=delete(client,'files',[items[1]],'trash');assert response.json()['status']=='completed',response.text
    task=response.json()['id'];assert not (settings.media_root/'exports/file1.txt').exists()
    assert (settings.media_root/'.recycle'/task/'exports/file1.txt').exists()
    restored=client.post('/api/deletions/'+task+'/restore');assert restored.status_code==200,restored.text
    assert (settings.media_root/'exports/file1.txt').exists()
    _,response=delete(client,'files',[items[2]],'permanent');assert response.json()['status']=='completed',response.text
    assert not (settings.media_root/'exports/file2.txt').exists()
    assert response.json()['result']['verified_released_bytes']==16


def test_delete_dependency_and_path_protection(client,settings,tmp_path):
    people(client);_,shot=new_shot(client)
    client.post('/api/shots/'+shot['id']+'/participants',json={'character_ids':['person_0']})
    plan,response=delete(client,'characters',['person_0'],cascade=True)
    assert response.status_code==409 and plan['blockers']
    outside=tmp_path/'outside.txt';outside.write_text('must survive')
    with client.app.state.session_factory() as s:
        f=m.ManagedFile(relative_path='../outside.txt',state='retained',metadata_json={'verified_owned':True});s.add(f);s.commit();identifier=f.id
    plan,response=delete(client,'files',[identifier],'permanent')
    assert response.json()['status']=='completed' and plan['files'][0]['protected']
    assert outside.read_text()=='must survive'


def test_masked_secret_is_not_saved_and_service_is_physically_removed(client):
    services=client.get('/api/settings/ai-services').json()
    service=services[0]
    r=client.patch('/api/settings/ai-services/'+service['id'],json={'api_key':'***'})
    assert r.status_code==422
    plan,response=delete(client,'services',[service['id']])
    assert response.status_code==200 and response.json()['status']=='completed',response.text
    assert all(x['id']!=service['id'] for x in client.get('/api/settings/ai-services').json())
    assert 'encrypted_secret' not in json.dumps(response.json())
