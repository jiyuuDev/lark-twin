import pytest
from starlette.testclient import TestClient

from larkx.web import app as web


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(web, 'TOKEN_PATH', tmp_path / 'web_token')
    monkeypatch.setattr(web, '_login_state', lambda: {'ok': True, 'message': '', 'user_id': '100'})
    return TestClient(web.create_app())


def test_login_url_keeps_token_out_of_query(tmp_path, monkeypatch):
    monkeypatch.setattr(web, 'TOKEN_PATH', tmp_path / 'web_token')
    url = web.login_url({'web_url': 'http://h:18340/', 'web_port': 18341})
    assert url == f'http://h:18340/#login={web.get_token()}'
    assert (tmp_path / 'web_token').stat().st_mode & 0o777 == 0o600


def test_home_is_console(client):
    r = client.get('/')
    assert r.status_code == 200 and r.headers['cache-control'] == 'no-cache'
    assert '/static/app.js' in r.text and '/static/style.css' in r.text and 'messenger' not in r.text
    # 控制台以前在 /console: 旧书签跳回首页(# 后面的部分由浏览器带过去)
    r = client.get('/console', follow_redirects=False)
    assert r.status_code == 302 and r.headers['location'] == '/'
    assert client.get('/console?embed=1').text == client.get('/').text
    # 网页版飞书已经去掉
    for path in ('/static/messenger.js', '/static/messenger.css', '/static/messenger.html', '/api/im/feed', '/api/im/me'):
        assert client.get(path).status_code == 404, path
    assert client.post('/api/im/chat/1/send', json={'text': 'x'}, headers={'X-Larkx': '1'}).status_code == 404


def test_api_needs_login(client):
    r = client.get('/static/feishu.svg')
    assert r.status_code == 200 and r.headers['content-type'].startswith('image/svg') and r.headers['cache-control'] == 'no-cache'
    assert client.get('/api/tasks').status_code == 401
    assert client.post('/login', json={'token': 'wrong'}).status_code == 401
    assert client.get('/login').status_code == 405
    r = client.post('/login', json={'token': web.get_token()})
    assert r.status_code == 200 and 'httponly' in r.headers['set-cookie'].lower()
    assert client.get('/api/tasks').json() == []


def test_writes_need_header(client):
    client.post('/login', json={'token': web.get_token()})
    body = {'chat_id': 'c1', 'prompt': '提醒我喝水', 'in': '10m'}
    assert client.post('/api/tasks', json=body).status_code == 403
    r = client.post('/api/tasks', json=body, headers={'X-Larkx': '1'})
    assert r.status_code == 200, r.text
    assert [t['prompt'] for t in client.get('/api/tasks').json()] == ['提醒我喝水']


def test_bearer_token(client):
    assert client.get('/api/tasks', headers={'Authorization': f'Bearer {web.get_token()}'}).status_code == 200


def test_activity_marks_recalled(client):
    from larkx.storage import Storage
    st = Storage()
    st.log_action(actor='agent', action='send', chat_id='c1', msg_id='11', content='发错了')
    st.log_action(actor='agent', action='send', chat_id='c1', msg_id='12', content='留着')
    st.log_action(actor='human', action='recall', chat_id='c1', msg_id='12', content='撤回失败', ok=0, error='x')
    st.log_action(actor='human', action='recall', chat_id='c1', msg_id='11', content='撤回 11')
    rows = client.get('/api/activity', headers={'Authorization': f'Bearer {web.get_token()}'}).json()
    assert {r['msg_id']: r['recalled'] for r in rows if r['action'] == 'send'} == {'11': True, '12': False}


def test_forward_counts_as_sent_message(client):
    """larkx forward 转发出去的消息和发出的消息一样: 动态里能看出撤回没有, 能从网页撤回, 算进数字人今天发出的条数。"""
    from larkx.storage import Storage
    st = Storage()
    st.log_action(actor='agent', action='forward', chat_id='c1', msg_id='21', content='[转发 1] x')
    st.log_action(actor='agent', action='send', chat_id='c1', msg_id='22', content='好')
    st.log_action(actor='human', action='recall', chat_id='c1', msg_id='21', content='撤回 21')
    auth = {'Authorization': f'Bearer {web.get_token()}'}
    rows = client.get('/api/activity', headers=auth).json()
    assert [r['recalled'] for r in rows if r['action'] == 'forward'] == [True]
    assert web._hourly(st)[-1]['sent'] == 2
    assert st.find_action('agent', ('send', 'forward'), '21').chat_id == 'c1'


def test_non_json_body_is_ignored(client):
    client.post('/login', json={'token': web.get_token()})
    # 不是 JSON(或不是 UTF-8)的请求体不会让中间件出错, 接口照常校验
    r = client.post('/api/say', content=b'\xff\xfe\x00', headers={'Content-Type': 'application/json', 'X-Larkx': '1'})
    assert r.status_code == 400 and r.json()['error'] == '内容为空'
    r = client.post('/api/say', content=b'text=hi', headers={'Content-Type': 'application/x-www-form-urlencoded', 'X-Larkx': '1'})
    assert r.status_code == 400


def test_ack_reaction_is_not_a_sent_message(client):
    """触发时 daemon 在那条消息上加的表情回复只记进动态: 不算数字人发出的消息, 不能撤回, 也不当成那一轮的回复。"""
    from datetime import datetime
    from larkx.storage import Storage
    st = Storage()
    st.inbox_add('31', 'c1', {'msg': {'msg_id': '31', 'content': '/ai 在吗'}, 'body': '在吗'})
    st.log_action(actor='daemon', action='ack_reaction', chat_id='c1', msg_id='31', content='OK', turn_kind='message', turn_ref='31')
    st.log_action(actor='daemon', action='ack_reaction', chat_id='c1', msg_id='32', content='OK', ok=0, error='x',
                  turn_kind='message', turn_ref='32')
    auth = {'Authorization': f'Bearer {web.get_token()}'}
    rows = client.get('/api/activity', headers=auth).json()
    assert [(r['actor'], r['action'], r['recalled']) for r in rows] == [('daemon', 'ack_reaction', False)] * 2
    assert [c['replies'] for c in client.get('/api/conversations', headers=auth).json()] == [[]]
    assert web._hourly(st)[-1]['sent'] == 0 and st.count_actions('agent', ('send', 'forward'), datetime(2000, 1, 1)) == 0


def test_overview_with_untrusted_turn(client):
    """升级窗口里旧 daemon 写的 turn.json 没有签名: 总览照常打开, 标出"不可信"。"""
    import json as _json
    import os
    from larkx import state
    state.STATE_DIR.mkdir(parents=True, exist_ok=True)
    state.TURN_PATH.write_text(_json.dumps({'kind': 'message', 'chat_id': 'c1', 'is_owner': False, 'ref': 'm1',
                                            'pid': os.getpid(), 'started_at': 1}), encoding='utf-8')
    client.post('/login', json={'token': web.get_token()})
    r = client.get('/api/overview')
    assert r.status_code == 200
    t = r.json()['turn']
    assert t['untrusted'] and t['elapsed'] is None
    assert 'sig' not in t and 'locked' not in t


def test_overview_hides_turn_sig_and_locked(client):
    """签名过的正常轮次: 总览也不回显 sig 和锁定的配置。"""
    import os
    from larkx import state
    state.write_turn('message', 'c1', '', False, 'm1', locked={'organize': True})
    data = state.TURN_PATH.read_text(encoding='utf-8')
    assert '"sig"' in data
    client.post('/login', json={'token': web.get_token()})
    t = client.get('/api/overview').json()['turn']
    assert t and not t.get('untrusted') and t['chat_id'] == 'c1'
    assert 'sig' not in t and 'locked' not in t
