import argparse
import asyncio
import json
import sys
import time

import pytest

from larkx import cli, notify, state
from larkx.storage import Storage


def turn(kind='message', chat='c1', owner=False):
    return {'kind': kind, 'chat_id': chat, 'anchor': '', 'is_owner': owner, 'ref': 'm1'}


def test_should_mirror():
    assert notify.should_mirror('g1', turn(chat='g1'), 'self')                 # 别人在群里叫它
    assert not notify.should_mirror('g1', turn(chat='g1', owner=True), 'self')  # 本人亲自叫的, 本人看得到
    assert notify.should_mirror('g2', turn(chat='self', owner=True), 'self')    # 本人让它发到别处
    assert not notify.should_mirror('self', turn(kind='say', chat=''), 'self')


def test_alert_command_and_cooldown(tmp_path, fake_client):
    out = tmp_path / 'alert.txt'
    tpl = f'{sys.executable} -c "import sys; open({str(out)!r}, \'a\').write(sys.argv[1])" {{text}}'
    assert notify.build_alert_command(tpl, 'x y')[-1] == 'x y'
    n = notify.Notifier({'alert_cmd': tpl}, client=None)
    assert n.fallback_alert('登录失效', key='auth', cooldown=3600)
    assert not n.fallback_alert('登录失效', key='auth', cooldown=3600)  # 冷却中不重复
    assert out.read_text(encoding='utf-8') == '⚠️ lark-twin：登录失效'
    n2 = notify.Notifier({}, client=fake_client)
    assert n2.alert('出错了', key='k')
    assert fake_client.sent == [('self', '⚠️ lark-twin：出错了')]


def _send_args(chat, text):
    return argparse.Namespace(text=text, json=True, chat_id=chat, root=None)


def _agent_cli(monkeypatch, tmp_path, fake_client, current_turn):
    st = Storage(f'sqlite:///{tmp_path}/t.db')
    monkeypatch.setattr(cli, 'Storage', lambda: st)
    monkeypatch.setattr(cli, 'LarkClient', lambda *a, **k: fake_client)
    monkeypatch.setattr(cli, 'LarkAuth', lambda *a, **k: None)
    monkeypatch.setattr(state, 'read_turn', lambda: current_turn)
    monkeypatch.setenv('LARKX_ACTOR', 'agent')
    monkeypatch.setenv('LARKX_REPLY_SIGNATURE', '🤖 sig')
    return st


def test_agent_send_mirrors_to_owner(tmp_path, monkeypatch, fake_client):
    st = _agent_cli(monkeypatch, tmp_path, fake_client, turn(chat='g1'))
    st.update_chat_name('g1', '项目群')
    assert cli.cmd_send(_send_args('g1', ['收到'])) == 0
    assert fake_client.sent[0] == ('g1', '收到\n\n—— 🤖 sig')
    assert fake_client.sent[1][0] == 'self' and '项目群' in fake_client.sent[1][1]
    fake_client.sent.clear()
    monkeypatch.setattr(state, 'read_turn', lambda: turn(chat='g1', owner=True))
    assert cli.cmd_send(_send_args('g1', ['好的'])) == 0
    assert [c for c, _ in fake_client.sent] == ['g1']


def test_agent_send_rate_limit(tmp_path, monkeypatch, fake_client):
    st = _agent_cli(monkeypatch, tmp_path, fake_client, turn(chat='g1'))
    monkeypatch.setenv('LARKX_SEND_RATE_LIMIT', '2')
    monkeypatch.setenv('LARKX_MIRROR', 'false')
    assert cli.cmd_send(_send_args('g1', ['1'])) == 0
    assert cli.cmd_send(_send_args('g1', ['2'])) == 0
    assert cli.cmd_send(_send_args('g1', ['3'])) == 1
    assert [c for c, _ in fake_client.sent] == ['g1', 'g1', 'self']  # 第三条被拦, 给本人发告警
    assert '上限' in st.list_actions()[-1].error


def test_agent_recall_only_own_messages(tmp_path, monkeypatch, fake_client):
    st = _agent_cli(monkeypatch, tmp_path, fake_client, turn(chat='g1'))
    st.log_action(actor='agent', action='send', chat_id='g1', msg_id='m-own', content='x')
    st.log_action(actor='agent', action='send', chat_id='g2', msg_id='m-other-chat', content='x')
    args = lambda mid: argparse.Namespace(msg_id=mid, json=True)
    assert cli.cmd_recall(args('m-human')) == 1
    assert cli.cmd_recall(args('m-other-chat')) == 1
    assert cli.cmd_recall(args('m-own')) == 0
    assert fake_client.api_calls == [('messages.DeleteMessageRequest', {'id': 'm-own'})]
    monkeypatch.setattr(state, 'read_turn', lambda: turn(chat='self', owner=True))
    assert cli.cmd_recall(args('m-other-chat')) == 0


class FakeSession:
    def __init__(self):
        self.prompts, self.events = [], []

    async def ask(self, prompt):
        from larkx.agent import TurnResult
        self.prompts.append(prompt)
        return TurnResult('success', False, '', 0.1)

    async def context_pressure(self):
        return None


def _daemon(tmp_path, fake_client, monkeypatch):
    from larkx import daemon as d
    from larkx.config import DEFAULTS
    obj = d.Daemon.__new__(d.Daemon)
    obj.cfg = json.loads(json.dumps(DEFAULTS))
    obj.cfg['triggers']['prefix'] = '/ai'
    obj.cfg['triggers']['owner_only'] = False      # 默认开; 这里测别人发的 /ai 能进收件箱
    obj.storage = Storage(f'sqlite:///{tmp_path}/d.db')
    from larkx.event_rules import RuleStore
    obj.rule_store = RuleStore(obj.storage)
    obj.session, obj.client = FakeSession(), fake_client
    obj.notifier = notify.Notifier(obj.cfg, fake_client)
    obj.auth = fake_client.auth
    obj.prefix, obj.home, obj._disconnects = '/ai', tmp_path, []
    obj._ws_down_at, obj._ws_gaps, obj._catchup_wake = None, [], asyncio.Event()
    obj.inbox = asyncio.Queue()
    obj._ack_tasks = set()
    from larkx.daemon.maintenance import Maintenance
    obj.maintenance = Maintenance(obj)
    monkeypatch.setattr(d, 'commit_agent_home', lambda *a: False)
    return obj


def test_inbox_survives_restart(tmp_path, monkeypatch, fake_client):
    obj = _daemon(tmp_path, fake_client, monkeypatch)
    now = int(time.time())
    # 群里别人发的要 @ 本人才触发(group_at_only 默认开)
    msg = {'msg_id': 'x1', 'chat_id': 'g1', 'chat_type': 2, 'from_id': '200', 'content': '/ai 在吗', 'create_time': now,
           'at_me': True}

    async def receive_then_crash():
        await obj.on_message(msg, '项目群', '张三')
        await obj.on_message(msg, '项目群', '张三')  # 重复推送不会再排一次
        return obj.inbox.qsize()

    assert asyncio.run(receive_then_crash()) == 1
    old = dict(msg, msg_id='x0', create_time=now - 3 * 3600)
    obj.storage.inbox_add('x0', 'g1', {'msg': old, 'body': '很久以前', 'is_owner': False})
    # "重启": 新的内存队列, 从库里恢复
    obj.inbox = asyncio.Queue()
    obj._restore_inbox()
    assert obj.inbox.qsize() == 1
    ev = obj.inbox.get_nowait()
    assert ev.ref == 'x1' and ev.note
    asyncio.run(obj._handle(ev))
    assert '重启前' in obj.session.prompts[0] and '在吗' in obj.session.prompts[0]
    assert obj.storage.inbox_unfinished() == []


def test_catch_up_feeds_missed_messages(tmp_path, monkeypatch, fake_client):
    obj = _daemon(tmp_path, fake_client, monkeypatch)
    obj.storage.save_message({'msg_id': 'a', 'chat_id': 'g1', 'position': 10, 'create_time': 1, 'content': '旧'})
    fed = []

    class FakeListener:
        async def on_message(self, m):
            fed.append(m['msg_id'])
            return True

    obj.listener = FakeListener()
    fake_client.api = lambda t, d=None: {'chats': {'g1': {'lastMessagePosition': 12}}}
    fake_client.pull_history = lambda cid, positions: [{'msg_id': f'p{p}', 'from_id': '200', 'chat_id': cid} for p in positions]
    assert asyncio.run(obj.catch_up()) == ({'g1': 2}, 0) and fed == ['p11', 'p12']


def test_daemon_skips_relay_but_handles_forwarded_copy(tmp_path, monkeypatch, fake_client):
    from larkx.daemon.listener import Listener
    obj = _daemon(tmp_path, fake_client, monkeypatch)
    listener = Listener(fake_client, obj.storage, obj.cfg, '100', on_stored=obj.on_message)
    state.mark_relay('relaycid', 'self')
    now = int(time.time())
    msg = {'msg_id': 'r1', 'chat_id': 'self', 'chat_type': 1, 'from_id': '100', 'cid': 'relaycid', 'position': 5,
           'content': '/ai 帮我看看', 'create_time': now}

    async def run():
        # 本人私聊里的中转消息(以及撤回后再推一次)都跳过: 不入库, 不触发
        assert await listener.on_message(msg) is False
        assert await listener.on_message(dict(msg, content='')) is False
        assert obj.storage.get_messages('self') == [] and obj.inbox.qsize() == 0
        # 转发到目标会话的那份 cid 不同, 照常入库、照常判断触发
        assert await listener.on_message(dict(msg, msg_id='f1', chat_id='c1', cid='other')) is True
        # 本人在私聊里自己发的 /ai 指令照常触发
        assert await listener.on_message(dict(msg, msg_id='own', cid='mine')) is True
        return obj.inbox.qsize()

    assert asyncio.run(run()) == 2
    assert [m.msg_id for m in obj.storage.get_messages('c1')] == ['f1']


def test_reconnect_storm_alerts_once(tmp_path, monkeypatch, fake_client):
    obj = _daemon(tmp_path, fake_client, monkeypatch)
    for _ in range(5):
        obj._on_disconnect(RuntimeError('boom'))
    alerts = [t for c, t in fake_client.sent if '长连接' in t]
    assert len(alerts) == 1


def test_parse_takeover():
    from larkx.daemon.triggers import parse_takeover
    assert parse_takeover('2h 帮我约下周二开会', 3600) == (7200, '帮我约下周二开会')
    assert parse_takeover('帮我约时间', 3600) == (3600, '帮我约时间')
    assert parse_takeover('', 3600) == (3600, '')


def test_takeover_flow(tmp_path, monkeypatch, fake_client):
    obj = _daemon(tmp_path, fake_client, monkeypatch)
    me = fake_client.auth.user_id
    now = int(time.time())
    base = {'chat_id': 'p1', 'chat_type': 1, 'create_time': now}

    async def run():
        await obj.on_message(dict(base, msg_id='c1', from_id=me, content='/ai /takeover 2h 帮我约下周二开会'), '张三', '我')
        await obj.on_message(dict(base, msg_id='c2', from_id='200', content='周二下午可以吗'), '张三', '张三')
        await obj.on_message(dict(base, msg_id='c3', from_id=me, content='我自己插一句'), '张三', '我')
        n_during = obj.inbox.qsize()
        await obj.on_message(dict(base, msg_id='c4', from_id=me, content='/ai /release'), '张三', '我')
        return n_during

    assert asyncio.run(run()) == 1                       # 只有对方那条进了队列, 本人插话不触发
    ev = obj.inbox.get_nowait()
    assert ev.ref == 'c2' and '接管' in ev.note and '下周二' in ev.note and not ev.is_owner
    summary = obj.inbox.get_nowait()                     # 结束后让数字人写一份汇报, 只能发到本人私聊
    assert summary.kind == 'report' and summary.chat_id == 'self' and not summary.is_owner and 'p1' in summary.prompt
    assert '本人结束' in summary.prompt
    assert any('已接管' in t for c, t in fake_client.sent if c == 'self')  # 确认只发到本人私聊
    assert all(c == 'self' for c, _ in fake_client.sent)
    assert obj.storage.takeover_get('p1', time.time()) is None


def test_takeover_in_group_ignores_group_at_only(tmp_path, monkeypatch, fake_client):
    """群里接管: 本人发 /ai /takeover 不用 @ 自己; 接管期间别人不 @ 本人的话也交给数字人。"""
    obj = _daemon(tmp_path, fake_client, monkeypatch)
    assert obj.cfg['triggers']['group_at_only'] is True
    me = fake_client.auth.user_id
    base = {'chat_id': 'g1', 'chat_type': 2, 'create_time': int(time.time())}

    async def run():
        await obj.on_message(dict(base, msg_id='g0', from_id='200', content='/ai 在吗'), '项目群', '张三')
        before = obj.inbox.qsize()
        await obj.on_message(dict(base, msg_id='g1', from_id=me, content='/ai /takeover 1h'), '项目群', '我')
        await obj.on_message(dict(base, msg_id='g2', from_id='200', content='周二下午可以吗'), '项目群', '张三')
        return before, obj.inbox.qsize()

    assert asyncio.run(run()) == (0, 1)                  # 接管前不 @ 不触发; 接管后对方的话照样进队列
    assert obj.storage.takeover_get('g1', time.time()) is not None
    assert obj.inbox.get_nowait().ref == 'g2'


def test_owner_only_keeps_takeover_tasks_and_say(tmp_path, monkeypatch, fake_client):
    """只有本人能唤起: 别人的 /ai 不触发; 本人主动接管的会话、定时任务、larkx say 照常进队列。"""
    from larkx import daemon as d
    obj = _daemon(tmp_path, fake_client, monkeypatch)
    obj.cfg['triggers']['owner_only'] = True
    obj.cfg['memory_review_hour'] = -1
    me = fake_client.auth.user_id
    p2p = {'chat_id': 'p1', 'chat_type': 1, 'create_time': int(time.time())}

    async def run():
        await obj.on_message(dict(p2p, msg_id='o1', from_id='200', content='/ai 在吗'), '张三', '张三')
        await obj.on_message(dict(p2p, msg_id='o2', chat_id='g1', chat_type=2, from_id='200', content='/ai 在吗',
                                  at_me=True), '项目群', '张三')
        before = obj.inbox.qsize()
        await obj.on_message(dict(p2p, msg_id='o3', from_id=me, content='/ai /takeover 1h'), '张三', '我')
        await obj.on_message(dict(p2p, msg_id='o4', from_id='200', content='周二下午可以吗'), '张三', '张三')
        return before, obj.inbox.qsize()

    assert asyncio.run(run()) == (0, 1)                  # 别人私聊/群里 @ 本人都不触发; 接管后对方的话照常进队列
    assert obj.inbox.get_nowait().ref == 'o4'

    obj.storage.add_task(kind='task', due_at=1, prompt='提醒喝水', chat_id='p1', by_owner=1)
    obj.storage.add_task(kind='say', due_at=1, prompt='看下今天的消息')
    monkeypatch.setattr(d, 'SCHEDULER_INTERVAL', 0)

    async def one_round():
        task = asyncio.create_task(obj._schedule())
        evs = [await asyncio.wait_for(obj.inbox.get(), 2) for _ in range(2)]
        task.cancel()
        return evs

    assert sorted(ev.kind for ev in asyncio.run(one_round())) == ['say', 'task']


def test_whitelist_takes_effect_without_restart(tmp_path, monkeypatch, fake_client):
    """唤起白名单改了立即生效(daemon 每条消息现查); 白名单里的人照旧按访客处理, 群里要 @ 本人。"""
    from larkx.daemon.triggers import GUEST_NOTE
    obj = _daemon(tmp_path, fake_client, monkeypatch)
    obj.cfg['triggers']['owner_only'] = True
    now = int(time.time())
    p2p = {'chat_id': 'p1', 'chat_type': 1, 'from_id': '200', 'create_time': now, 'content': '/ai 在吗'}
    group = dict(p2p, chat_id='g1', chat_type=2)
    other_db = Storage(obj.storage.url)  # 命令行、网页是另一个进程, 用自己的连接改

    async def run():
        seen = []
        for step, m in (('empty', dict(p2p, msg_id='w1')),
                        ('add', None),
                        ('p2p', dict(p2p, msg_id='w2')),
                        ('group-no-at', dict(group, msg_id='w3')),
                        ('group-at', dict(group, msg_id='w4', at_me=True)),
                        ('not-listed', dict(p2p, msg_id='w5', from_id='201')),
                        ('remove', None),
                        ('removed', dict(p2p, msg_id='w6'))):
            if step == 'add':
                other_db.whitelist_add('200', '张三', '飞书-IM', added_by='cli')
            elif step == 'remove':
                other_db.whitelist_remove('200')
            else:
                await obj.on_message(m, '张三', '张三')
                seen.append((step, obj.inbox.qsize()))
        return seen

    assert asyncio.run(run()) == [('empty', 0), ('p2p', 1), ('group-no-at', 1), ('group-at', 2), ('not-listed', 2), ('removed', 2)]
    evs = [obj.inbox.get_nowait() for _ in range(2)]
    assert [e.ref for e in evs] == ['w2', 'w4'] and not any(e.is_owner for e in evs)
    asyncio.run(obj._handle(evs[0]))
    assert 'owner="false"' in obj.session.prompts[0] and GUEST_NOTE in obj.session.prompts[0]


def test_takeover_in_owner_chat_gives_hint(tmp_path, monkeypatch, fake_client):
    obj = _daemon(tmp_path, fake_client, monkeypatch)
    msg = {'chat_id': 'self', 'chat_type': 1, 'create_time': int(time.time()), 'msg_id': 'h1',
           'from_id': fake_client.auth.user_id, 'content': '/ai /takeover 1h'}
    obj.cfg['allow_self'] = True
    asyncio.run(obj.on_message(msg, '', '我'))
    assert obj.storage.takeover_list() == [] and '要接管哪个会话' in fake_client.sent[-1][1]


def test_mentions_query(tmp_path):
    st = Storage(f'sqlite:///{tmp_path}/m.db')
    t = int(time.time())
    st.save_message({'msg_id': 'a', 'chat_id': 'p1', 'chat_type': 1, 'from_id': '200', 'content': '在吗', 'create_time': t - 100}, sender_name='张三')
    st.save_message({'msg_id': 'b', 'chat_id': 'g1', 'chat_type': 2, 'from_id': '300', 'content': '@耿宇贺 看下工单', 'create_time': t - 90}, sender_name='李四')
    st.save_message({'msg_id': 'c', 'chat_id': 'g1', 'chat_type': 2, 'from_id': '300', 'content': '无关的话', 'create_time': t - 80}, sender_name='李四')
    st.save_message({'msg_id': 'd', 'chat_id': 'g1', 'chat_type': 2, 'from_id': '100', 'content': '好的', 'create_time': t - 50}, sender_name='耿宇贺', direction='out')
    rows = st.mentions('100', st.owner_name('100'), t - 3600)
    assert [(r['msg_id'], r['kind'], r['replied']) for r in rows] == [('a', 'p2p', False), ('b', 'at', True)]


def test_mentions_structured_at_only(tmp_path):
    """群里结构化的 @(不带名字的富文本 @)也算; 合并转发的记录里带着本人 id 的不算。"""
    st = Storage(f'sqlite:///{tmp_path}/m2.db')
    t = int(time.time())
    st.save_message({'msg_id': 'e', 'chat_id': 'g1', 'chat_type': 2, 'from_id': '300', 'content': '看下这个',
                     'content_data': {'_larkx_mention_ids': ['100']}, 'create_time': t - 70}, sender_name='李四')
    st.save_message({'msg_id': 'f', 'chat_id': 'g1', 'chat_type': 2, 'from_id': '300', 'content': '[合并转发]',
                     'content_data': {'messages': [{'fromId': '100', 'content': '之前说的'}]}, 'create_time': t - 60},
                    sender_name='李四')
    assert [r['msg_id'] for r in st.mentions('100', '', t - 3600)] == ['e']
    assert [r['msg_id'] for r in st.mentions('100', '', t - 3600, limit=1)] == ['e']


def test_listener_leaves_messages_unread_by_default(tmp_path, fake_client):
    from larkx.config import DEFAULTS
    from larkx.daemon.listener import Listener
    marked = []
    fake_client.mark_read = lambda *a, **k: marked.append(a)
    fake_client.get_user_name = lambda uid, cid: '张三'
    fake_client.get_group_name = lambda cid: '项目群'
    assert DEFAULTS['mark_read'] is False
    cfg = {'mark_read': DEFAULTS['mark_read']}
    lst = Listener(fake_client, Storage(f'sqlite:///{tmp_path}/t.db'), cfg, '100')
    msg = {'msg_id': 'x1', 'chat_id': 'g1', 'chat_type': 2, 'from_id': '200', 'content': '@你 /ai 在吗', 'position': 9}
    assert asyncio.run(lst.on_message(msg)) and marked == []
    cfg['mark_read'] = True
    assert asyncio.run(lst.on_message({**msg, 'msg_id': 'x2', 'position': 10})) and len(marked) == 1


def test_added_columns_migrated(tmp_path):
    """跑过第一版的库: chat_profiles 已经存在但没有 suggested_at, 启动时补上列。"""
    import sqlite3
    db = tmp_path / 'old.db'
    Storage(f'sqlite:///{db}')
    con = sqlite3.connect(db)
    con.execute('ALTER TABLE chat_profiles DROP COLUMN suggested_at')                # 模拟第一版的表
    con.execute("INSERT INTO chat_profiles (chat_id, name) VALUES ('g1', '项目群')")
    con.commit()
    con.close()
    st = Storage(f'sqlite:///{db}')
    from larkx.storage.models import ChatProfile
    s = st.Session()
    assert s.get(ChatProfile, 'g1').suggested_at is None
    s.close()
    Storage(f'sqlite:///{db}')                                                    # 再启动一次不报错


def test_mentions_name_case_insensitive(tmp_path):
    st = Storage(f'sqlite:///{tmp_path}/c.db')
    t = int(time.time())
    st.save_message({'msg_id': 'a', 'chat_id': 'g1', 'chat_type': 2, 'from_id': '300', 'content': '@alice 看下',
                     'create_time': t - 10}, sender_name='李四')
    assert [r['msg_id'] for r in st.mentions('100', 'Alice', t - 3600)] == ['a']


def test_added_column_race_ignored(tmp_path, monkeypatch):
    """daemon 和 CLI 同时第一次启动: 查的时候没有、加的时候另一边已经加上了。"""
    import sqlite3
    from sqlalchemy import inspect as sa_inspect
    from larkx import storage as storage_mod
    db = tmp_path / 'race.db'
    Storage(f'sqlite:///{db}')
    con = sqlite3.connect(db)
    con.execute('ALTER TABLE chat_profiles DROP COLUMN suggested_at')
    con.commit()
    con.close()
    real = sa_inspect

    def stale(engine):                                   # 让"查列"看到旧表, 但先由"另一边"加上列
        i = real(engine)
        cols = i.get_columns('chat_profiles')
        c2 = sqlite3.connect(db)
        c2.execute('ALTER TABLE chat_profiles ADD COLUMN suggested_at DATETIME')
        c2.commit()
        c2.close()
        i.get_columns = lambda t: cols
        return i
    monkeypatch.setattr('sqlalchemy.inspect', stale)
    Storage(f'sqlite:///{db}')                                                       # 不报 duplicate column


def test_turn_is_signed():
    """数字人直接改 turn.json(把自己改成本人、打开开关)、去掉签名: 按不可信的访客轮次处理, 不当成没有限制。"""
    import json as _json
    from larkx import state
    state.write_turn('system', 'self', ref='job:organize#1', locked={'organize': False})
    assert state.read_turn()['ref'] == 'job:organize#1'
    assert (state.STATE_DIR / 'turn.key').stat().st_mode & 0o777 == 0o600
    data = _json.loads(state.TURN_PATH.read_text(encoding='utf-8'))
    data.update(kind='message', is_owner=True, locked={'organize': True})
    state.TURN_PATH.write_text(_json.dumps(data), encoding='utf-8')
    t = state.read_turn()
    assert t['untrusted'] and state.is_guest(t) and not t['is_owner'] and t['locked'] == {}
    data.pop('sig')                                                          # 升级时旧 daemon 写的: 没有签名
    state.TURN_PATH.write_text(_json.dumps(data), encoding='utf-8')
    assert state.is_guest(state.read_turn())
    state.TURN_PATH.write_text('{坏的', encoding='utf-8')
    assert state.is_guest(state.read_turn())


def test_turn_key_problems_fail_closed():
    import json as _json
    from larkx import state
    state.write_turn('message', 'self', is_owner=True, ref='m1')
    (state.STATE_DIR / 'turn.key').write_bytes(b'')                          # 空密钥: 不能用空 key 验过
    assert state.is_guest(state.read_turn())
    state.write_turn('message', 'self', is_owner=True, ref='m2')            # 下一轮重新生成
    assert len((state.STATE_DIR / 'turn.key').read_bytes()) == 32 and state.read_turn()['ref'] == 'm2'
    data = _json.loads(state.TURN_PATH.read_text(encoding='utf-8'))
    data['sig'] = '签名'                                                      # 非 ASCII: 不抛 TypeError
    state.TURN_PATH.write_text(_json.dumps(data, ensure_ascii=False), encoding='utf-8')
    assert state.is_guest(state.read_turn())


def test_turn_replay_rejected():
    """把本人那一轮的 turn.json 存下来, 访客那一轮再拷回去: 每一轮换了新密钥, 验不过。"""
    from larkx import state
    state.write_turn('message', 'self', is_owner=True, ref='owner-turn')
    saved = state.TURN_PATH.read_text(encoding='utf-8')
    state.write_turn('message', 'g1', is_owner=False, ref='guest-turn')
    state.TURN_PATH.write_text(saved, encoding='utf-8')
    assert state.read_turn()['untrusted']


def test_agent_turn_defaults_to_guest():
    from larkx import state
    assert state.read_turn() is None and state.is_guest(state.agent_turn())


def test_cli_treats_untrusted_turn_as_guest(monkeypatch, capsys):
    """数字人去掉 LARKX_ACTOR、turn.json 读不到或被改过: 也按访客检查, 不能读别的会话。"""
    from larkx import cli, guest
    monkeypatch.delenv('LARKX_ACTOR', raising=False)
    monkeypatch.setattr(cli, 'inside_twin', lambda: True)
    seen = []
    monkeypatch.setattr(guest, 'cli_denied', lambda cmd, args, turn, st: seen.append(turn) or '访客不能看别的会话')
    monkeypatch.setattr('larkx.notify.Notifier.alert', lambda *a, **k: None)
    monkeypatch.setattr(cli, 'LarkClient', lambda auth: None)
    monkeypatch.setattr(cli, 'LarkAuth', lambda: None)
    monkeypatch.setattr('sys.argv', ['larkx', 'messages', 'other-chat'])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 3 and seen and seen[0]['untrusted']


def test_hook_fails_closed_on_error(monkeypatch):
    from larkx import state
    from larkx.agent import hooks
    monkeypatch.setattr(state, 'agent_turn', lambda: (_ for _ in ()).throw(RuntimeError('boom')))
    out = asyncio.run(hooks.make_pre_tool_use('/tmp')({'tool_name': 'Bash', 'tool_input': {'command': 'ls'}}, 'x', None))
    assert out['hookSpecificOutput']['permissionDecision'] == 'deny'


def test_owner_user_id_reads_only_credentials(tmp_path):
    import json as _json
    from larkx import config
    assert config.owner_user_id() == ''                                      # 测试里不读本机真实凭证
    config.CREDENTIALS_PATH.write_text(_json.dumps({'user_id': '555', 'cookies': {}}), encoding='utf-8')
    assert config.owner_user_id() == '555'


def test_twin_without_actor_env_still_restricted(monkeypatch, tmp_path, fake_client):
    """维护任务里数字人 env -u LARKX_ACTOR 再调 larkx send: 照样按数字人检查(维护任务不能发飞书消息)。"""
    from larkx import cli as c
    monkeypatch.delenv('LARKX_ACTOR', raising=False)
    monkeypatch.setattr(c, 'inside_twin', lambda: True)
    state.write_turn('system', 'self', ref='job:review#1')
    monkeypatch.setattr(c, 'LarkClient', lambda auth: fake_client)
    monkeypatch.setattr(c, 'LarkAuth', lambda: None)
    monkeypatch.setattr(c, 'Storage', lambda: Storage(f'sqlite:///{tmp_path}/s.db'))
    args = argparse.Namespace(chat_id='c1', text=['hi'], root=None, json=False, markdown=False, mention=[], stdin=False)
    assert c.cmd_send(args) != 0 and not [x for x in fake_client.sent if x[0] == 'c1']


def test_task_add_rejected_when_turn_untrusted(monkeypatch, tmp_path):
    from larkx import cli as c
    monkeypatch.setattr(c, 'inside_twin', lambda: True)
    monkeypatch.setattr(c, 'Storage', lambda: Storage(f'sqlite:///{tmp_path}/t.db'))
    state.STATE_DIR.mkdir(parents=True, exist_ok=True)
    state.TURN_PATH.write_text('{"kind": "message", "chat_id": "", "is_owner": false}', encoding='utf-8')   # 没签名
    args = argparse.Namespace(action='add', text=['提醒'], at=None, every=None, chat=None, json=False, all=False, **{'in': '1h'})
    assert c.cmd_task(args) == 3


def test_forbidden_cmd_without_actor_env(monkeypatch):
    from larkx import cli as c
    monkeypatch.delenv('LARKX_ACTOR', raising=False)
    monkeypatch.setattr(c, 'inside_twin', lambda: True)
    monkeypatch.setattr('sys.argv', ['larkx', 'prune', '--dry-run'])
    with pytest.raises(SystemExit) as e:
        c.main()
    assert e.value.code == 3
