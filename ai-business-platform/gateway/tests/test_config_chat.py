import pytest
from app import configuration, main as gateway
from test_configuration import source, BOT, EDITOR
CONTROLLER={**BOT,'X-Config-Controller-Token':'z'*64}
BASE='/v1/config/chat'

@pytest.fixture(autouse=True)
def setup(client,monkeypatch):
    monkeypatch.setenv('CONFIG_ADMIN_TOKEN','c'*64)
    monkeypatch.setenv('CONFIG_ADMIN_ACTOR','operator')
    monkeypatch.setenv('CONFIG_CONTROLLER_TOKEN','z'*64)
    monkeypatch.setattr(configuration,'fetch_inventory',lambda:{'documents':[source()]})
    with gateway.pool.connection() as db:db.execute('TRUNCATE config_source_names,config_chat_sessions,config_chat_turns CASCADE')

def create(c,key='create-test'):
    return c.post(BASE+'/sessions',headers={**EDITOR,'Idempotency-Key':key},json={'source_id':source()['id'],'base_sha256':source()['sha256']})

def send(c,row,text='明確な報告手順にしてください',key='send-test'):
    return c.post(BASE+'/sessions/'+row['id']+'/messages',headers={**EDITOR,'Idempotency-Key':key},json={'expected_revision':row['revision'],'message':text})

def report(c,turn,**extra):
    return c.post(BASE+'/controller/turns/'+turn['id']+'/report',headers=CONTROLLER,json={'request_sha256':turn['request_sha256'],'state':'COMPLETED','reply':'変更案です','proposal':'# Policy\nKeep evidence.\nReport tests.\n',**extra})

def detail(c,row):return c.get(BASE+'/sessions/'+row['id'],headers=BOT).json()

def accept(c,row):
    row=detail(c,row)
    return c.post(BASE+'/sessions/'+row['id']+'/accept',headers=EDITOR,json={'expected_revision':row['revision'],'turn_id':row['turns'][-1]['id']})

def test_auth_idempotency_and_serial_turns(client):
    assert client.get(BASE+'/sessions').status_code==401
    assert client.get(BASE+'/controller/work',headers=BOT).status_code==401
    session=create(client).json();assert create(client).json()['id']==session['id']
    assert client.post(BASE+'/sessions/'+session['id']+'/messages',headers={**BOT,'Idempotency-Key':'abcdefgh'},json={'expected_revision':1,'message':'相談'}).status_code==401
    row=send(client,session).json()
    assert send(client,session).json()['turns'][0]['id']==row['turns'][0]['id']
    assert send(client,session,text='different').status_code==409
    assert send(client,row,key='another-message').status_code==409

def test_model_cannot_save_or_release_without_acceptance(client):
    row=send(client,create(client).json()).json()
    work=client.get(BASE+'/controller/work',headers=CONTROLLER).json()['turns'][0]
    assert work['context']['content']==source()['content']
    assert report(client,work).status_code==200
    row=detail(client,row);assert '+Report tests.' in row['turns'][0]['diff']
    assert 'context' not in row['turns'][0]
    with gateway.pool.connection() as db:
        assert db.execute('SELECT count(*) n FROM config_drafts WHERE idempotency_key=%s',('chat-'+work['id'],)).fetchone()['n']==0
    draft=accept(client,row).json()
    assert draft['state']=='VALIDATED' and draft['applied'] is False
    assert accept(client,row).json()['id']==draft['id']
    result=client.post('/v1/config/drafts/'+draft['id']+'/release',headers=EDITOR,json={'expected_revision':draft['revision'],'auto_promote':False})
    assert result.status_code==201 and result.json()['state']=='QUEUED'

def test_followup_uses_proposal_and_history(client):
    row=send(client,create(client).json()).json();report(client,row['turns'][0]);row=detail(client,row)
    second=send(client,row,text='説明だけしてください',key='second-message').json()
    work=client.get(BASE+'/controller/work',headers=CONTROLLER).json()['turns'][0]
    assert work['context']['content'].endswith('Report tests.\n')
    assert work['context']['history'][0]['assistant']=='変更案です'
    assert accept(client,second).status_code==409
    report(client,work,proposal=None,reply='説明です');assert accept(client,second).status_code==409

def test_stale_source_and_revision(client,monkeypatch):
    row=send(client,create(client).json()).json();report(client,row['turns'][0]);row=detail(client,row)
    assert client.post(BASE+'/sessions/'+row['id']+'/accept',headers=EDITOR,json={'expected_revision':1,'turn_id':row['turns'][0]['id']}).status_code==409
    monkeypatch.setattr(configuration,'fetch_inventory',lambda:{'documents':[source('New source')]})
    assert accept(client,row).status_code==409

def test_invalid_output_identity_and_terminal_replay(client):
    row=send(client,create(client).json()).json();turn=row['turns'][0]
    assert report(client,turn,request_sha256='f'*64).status_code==409
    assert report(client,turn,reply='').status_code==422
    assert report(client,turn,proposal='文'*30000).status_code==422
    assert report(client,turn,state='FAILED',proposal=None,reply=None,failure='Unavailable').status_code==200
    assert report(client,turn).json()['state']=='FAILED'
    assert accept(client,row).status_code==409

def test_capacity_and_expiry(client):
    for i in range(4):assert send(client,create(client,key='session-'+str(i)).json()).status_code==202
    last=create(client,key='session-last').json();assert send(client,last).status_code==429
    with gateway.pool.connection() as db:db.execute("UPDATE config_chat_turns SET created_at=created_at-INTERVAL '20 minutes'")
    assert client.get(BASE+'/controller/work',headers=CONTROLLER).json()['turns']==[]
    assert send(client,last).status_code==202


def test_existing_conversation_uses_current_logical_name(client):
    row = create(client).json()
    path = '/v1/config/sources/' + source()['id'] + '/name'
    assert client.post(path, headers=EDITOR, json={'logical_name': '共通ルール', 'expected_revision': 0}).status_code == 200
    updated = detail(client, row)
    assert updated['source']['logical_name'] == '共通ルール'
    assert updated['revision'] == row['revision'] and updated['content'] == source()['content']
    assert client.get(BASE+'/sessions', headers=BOT).json()['sessions'][0]['source']['logical_name'] == '共通ルール'
