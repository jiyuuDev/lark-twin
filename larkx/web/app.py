"""lark-twin 网页控制台: 看数字人在做什么、管理任务/接管/记忆, 必要时打断它。

只监听 127.0.0.1, 由系统 Nginx 反向代理出去。页面会展示聊天内容和记忆, 所以所有数据接口都要令牌:
打开 `larkx web url` 给出的链接后, 页面把 # 后面的令牌 POST 给 /login 换成 HttpOnly cookie
(令牌放在 # 片段里, 不会进 nginx 访问日志); 改动类请求还要带 X-Larkx 请求头, 防跨站伪造。
"""
import hmac
import json
import re
import secrets
import subprocess
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .. import service, state
from .. import whitelist as wl
from ..agent.home import commit_agent_home
from ..config import DATA_DIR, load_config
from ..storage import ACK_REACTION, Storage
from ..storage.models import Action, InboxItem, Message

STATIC = Path(__file__).parent / 'static'
TOKEN_PATH = DATA_DIR / 'web_token'
COOKIE = 'larkx_token'
MEMORY_NAME = re.compile(r'^(archive/)?[\w\-.一-鿿]+\.md$')
_login_cache = {'at': 0, 'value': None}


def get_token(reset: bool = False) -> str:
    if TOKEN_PATH.exists() and not reset:
        return TOKEN_PATH.read_text(encoding='utf-8').strip()
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(24)
    TOKEN_PATH.write_text(token + '\n', encoding='utf-8')
    TOKEN_PATH.chmod(0o600)
    return token


def login_url(cfg: dict) -> str:
    base = (cfg.get('web_url') or f'http://127.0.0.1:{cfg["web_port"]}').rstrip('/')
    return f'{base}/#login={get_token()}'


def _json(data, status: int = 200) -> Response:
    return Response(json.dumps(data, ensure_ascii=False, default=str), status_code=status, media_type='application/json')


def _authorized(request: Request) -> bool:
    token = request.cookies.get(COOKIE) or request.headers.get('authorization', '').removeprefix('Bearer ').strip()
    return bool(token) and hmac.compare_digest(token, get_token())


def api(fn):
    """数据接口统一做鉴权; 改动类请求要求自定义请求头, 浏览器跨站提交带不上它。"""
    def endpoint(request: Request):
        if not _authorized(request):
            return _json({'error': '未登录'}, 401)
        if request.method != 'GET' and request.headers.get('x-larkx') != '1':
            return _json({'error': '缺少 X-Larkx 请求头'}, 403)
        try:
            return fn(request)
        except ValueError as e:
            return _json({'error': str(e)}, 400)
        except PermissionError as e:
            return _json({'error': str(e)}, 403)
    return endpoint


# ---- 页面与登录 ----

def index(request: Request):
    return Response((STATIC / 'index.html').read_text(encoding='utf-8'), media_type='text/html',
                    headers={'Cache-Control': 'no-cache'})


def old_console(request: Request):
    """控制台以前在 /console, 旧书签跳回首页。# 后面的部分浏览器会带到新地址上。"""
    return RedirectResponse('/', status_code=302)


def login(request: Request):
    token = str(request.state.body.get('token') or '')
    if not token or not hmac.compare_digest(token, get_token()):
        return _json({'error': '令牌无效。在服务器上运行 larkx web url 获取登录链接。'}, 401)
    resp = _json({'ok': True})
    resp.set_cookie(COOKIE, token, max_age=180 * 86400, httponly=True, samesite='strict')
    return resp


# ---- 只读接口 ----

OWNER_CHAT_NAME = '我自己（私聊）'


def _owner_chat() -> str:
    from ..notify import _load
    return _load().get('owner_chat', '')


def _chat_names(st: Storage) -> dict:
    names = {c.chat_id: c.name for c in st.list_chats(limit=500)}
    owner = _owner_chat()
    if owner and not names.get(owner):
        names[owner] = OWNER_CHAT_NAME
    return names


def _login_state():
    if time.time() - _login_cache['at'] > 300:
        from ..protocol import LarkAuth
        auth = LarkAuth()
        ok, msg = auth.validate(fetch_profile=False)
        _login_cache.update(at=time.time(), value={'ok': ok, 'message': msg, 'user_id': auth.user_id})
    return _login_cache['value']


def _hourly(st: Storage, hours: int = 24) -> list:
    """最近 24 小时每小时: 交给数字人的消息数、它发出的消息数。"""
    start = datetime.now().replace(minute=0, second=0, microsecond=0) - timedelta(hours=hours - 1)
    buckets = {start + timedelta(hours=i): [0, 0] for i in range(hours)}
    s = st.Session()
    try:
        for (ts,) in s.query(InboxItem.created_at).filter(InboxItem.created_at >= start):
            buckets[ts.replace(minute=0, second=0, microsecond=0)][0] += 1
        for (ts,) in s.query(Action.created_at).filter(Action.created_at >= start, Action.actor == 'agent',
                                                       Action.action.in_(('send', 'forward')), Action.ok == 1):
            buckets[ts.replace(minute=0, second=0, microsecond=0)][1] += 1
    finally:
        s.close()
    return [{'hour': k.strftime('%H:00'), 'received': v[0], 'sent': v[1]} for k, v in buckets.items()]


def _lifecycle(st, cfg) -> dict:
    """总览上的例行任务和各关注度的会话数。"""
    from .. import groups, jobs
    store, now = jobs.JobStore(st), datetime.now()
    att = {}
    if cfg.get('chat_groups'):
        for r in groups.ChatStore(st).view():
            att[r['attention']] = att.get(r['attention'], 0) + 1
    return {'jobs': [{'name': j.name, 'title': j.title, 'once': j.once, 'enabled': j.when(cfg) is not None,
                      'next': jobs.next_time(j, cfg, now), 'last': jobs.describe(store.last(j.name))} for j in jobs.JOBS],
            'chat_groups': bool(cfg.get('chat_groups')), 'attention': att,
            'backfill': store.get_state('backfill'), 'bootstrap': store.get_state('bootstrap'),
            'proposal': _pending_proposal(st)}


def _pending_proposal(st):
    """最新的整理方案里还有没决定的条目时, 总览上提示一下; 没打开整理方案时不提示。"""
    from .. import organize
    if not organize.enabled(load_config()):
        return None
    store = organize.OrganizeStore(st)
    prop = store.get()
    pending = [i for i in store.items(prop.id) if i.status == 'pending'] if prop else []
    return {'id': prop.id, 'pending': len(pending)} if pending else None


@api
def overview(request: Request):
    cfg = load_config()
    st = Storage()
    today = datetime.combine(date.today(), datetime.min.time())
    s = st.Session()
    try:
        inbox_pending = s.query(InboxItem).filter(InboxItem.status == 'pending').count()
        handled_today = s.query(InboxItem).filter(InboxItem.created_at >= today).count()
        denied_today = s.query(Action).filter(Action.actor == 'agent', Action.ok == 0, Action.created_at >= today).count()
        messages_today = s.query(Message).filter(Message.create_time >= today.timestamp()).count()
    finally:
        s.close()
    key = 'main:codex' if cfg['agent_backend'] == 'codex' else 'main'
    sess = st.session_get(key)
    turn = state.read_turn()
    home = Path(cfg['agent_home'])
    memory_files = sorted(p.name for p in (home / 'memory').glob('*.md'))
    units = {k: service.status(k) for k in service.UNITS} if service.available() else {}
    return _json({
        'now': datetime.now(),
        'backend': cfg['agent_backend'],
        'signature': cfg.get('reply_signature'),
        'trigger_prefix': (cfg.get('triggers') or {}).get('prefix'),
        'services': units,
        'login': _login_state(),
        'session': {'id': sess.agent_session_id if sess else '', 'turns': sess.msg_count if sess else 0,
                    'last_active': sess.last_active if sess else None},
        # 不可信的轮次(升级窗口里旧 daemon 写的、被改过的)没有 started_at, 单独标出来
        # 签名和锁定的配置不给网页: 只回显展示要用的字段
        'turn': turn and dict({k: v for k, v in turn.items() if k not in ('sig', 'locked')},
                              elapsed=(time.time() - turn['started_at']) if turn.get('started_at') else None),
        'inbox_pending': inbox_pending,
        'today': {'handled': handled_today, 'sent': st.count_actions('agent', ('send', 'forward'), today), 'denied': denied_today,
                  'messages': messages_today},
        'tasks_pending': len([t for t in st.list_tasks() if t.status == 'pending']),
        'takeovers': [{'chat_id': t.chat_id, 'until': t.until, 'goal': t.goal} for t in st.takeover_list() if t.until > time.time()],
        'memory_files': memory_files,
        'lifecycle': _lifecycle(st, cfg),
        'hourly': _hourly(st),
    })


@api
def activity(request: Request):
    st = Storage()
    names = _chat_names(st)
    rows = st.list_actions(limit=int(request.query_params.get('limit', 100)))
    rows.reverse()
    sends = ('send', 'forward', 'deny_reply')
    recalled = st.recalled_ids(r.msg_id for r in rows if r.action in sends)
    return _json([{'id': r.id, 'time': r.created_at, 'actor': r.actor, 'action': r.action, 'chat_id': r.chat_id,
                   'chat_name': names.get(r.chat_id, ''), 'msg_id': r.msg_id, 'content': r.content, 'ok': bool(r.ok),
                   'error': r.error, 'turn_kind': r.turn_kind, 'turn_ref': r.turn_ref,
                   'recalled': r.action in sends and r.msg_id in recalled} for r in rows])


@api
def conversations(request: Request):
    """交给数字人的每条消息, 以及它在那一轮里发出的回复。"""
    st = Storage()
    names = _chat_names(st)
    items = st.list_inbox(limit=int(request.query_params.get('limit', 40)))
    out = []
    for it in items:
        try:
            p = json.loads(it.payload)
        except json.JSONDecodeError:
            continue
        m = p.get('msg') or {}
        out.append({'id': it.id, 'msg_id': it.msg_id, 'status': it.status, 'time': it.created_at,
                    'chat_id': it.chat_id, 'chat_name': p.get('chat_name') or names.get(it.chat_id, ''),
                    'sender_name': p.get('sender_name', ''), 'is_owner': p.get('is_owner', False),
                    'body': p.get('body', ''), 'takeover': bool(p.get('note') and '接管' in p.get('note', '')),
                    'content': m.get('content', ''), 'replies': []})
    by_ref = {c['msg_id']: c for c in out}
    sends = [a for a in st.actions_for_turns(list(by_ref)) if a.action in ('send', 'forward')]
    recalled = st.recalled_ids(a.msg_id for a in sends)
    for a in sends:
        by_ref[a.turn_ref]['replies'].append({'time': a.created_at, 'chat_id': a.chat_id, 'chat_name': names.get(a.chat_id, ''),
                                              'content': a.content, 'ok': bool(a.ok), 'error': a.error, 'msg_id': a.msg_id,
                                              'recalled': a.msg_id in recalled})
    return _json(out)


@api
def chats(request: Request):
    owner = _owner_chat()
    return _json([{'chat_id': c.chat_id, 'name': c.name or (OWNER_CHAT_NAME if c.chat_id == owner else ''),
                   'chat_type': c.chat_type, 'updated_at': c.updated_at} for c in Storage().list_chats(limit=100)])


@api
def chat_profiles(request: Request):
    """「会话」页: 按最近活跃排序的会话, 带分类、关注度和画像信号。"""
    from .. import groups
    st, owner = Storage(), _owner_chat()
    order = [c.chat_id for c in st.list_chats(limit=int(request.query_params.get('limit', 500)))]
    rows = groups.ChatStore(st).view(order)
    for r in rows:
        r['name'] = r['name'] or (OWNER_CHAT_NAME if r['chat_id'] == owner else '')
    return _json(rows)


def _group_rows(st) -> dict:
    from .. import groups
    cs = groups.ChatStore(st)
    counts = {}
    for r in cs.view():
        counts[r['group_id']] = counts.get(r['group_id'], 0) + 1
    return {'groups': [{'id': g.id, 'name': g.name, 'attention': g.attention, 'private': bool(g.private),
                        'keywords': json.loads(g.keywords or '[]'), 'chats': counts.get(g.id, 0)} for g in cs.groups()],
            'unassigned': counts.get(0, 0), 'attention_names': groups.ATTENTION,
            'enabled': bool(load_config().get('chat_groups'))}


@api
def groups_api(request: Request):
    """会话分类: GET 列表; POST 建分类 {name, attention, keywords, private}。改了立即生效。"""
    from .. import groups
    st = Storage()
    if request.method == 'POST':
        b = request.state.body
        g = groups.ChatStore(st).create_group(str(b.get('name') or ''), groups.parse_attention(b.get('attention') or 'light'),
                                              b.get('keywords') or [], bool(b.get('private')))
        st.log_action(actor='human', action='group', content=f'网页建分类 {g.name}')
    return _json(_group_rows(st))


@api
def group_item(request: Request):
    """PUT 改分类(name/attention/keywords/private); DELETE 删分类(分类里的会话变成未分类)。"""
    from .. import groups
    st, gid = Storage(), request.path_params['group_id']
    cs = groups.ChatStore(st)
    if request.method == 'DELETE':
        cs.delete_group(gid)
        st.log_action(actor='human', action='group', content=f'网页删分类 #{gid}')
    else:
        b = request.state.body
        fields = {k: b[k] for k in ('name', 'keywords') if k in b}
        if 'attention' in b:
            fields['attention'] = groups.parse_attention(b['attention'])
        if 'private' in b:
            fields['private'] = 1 if b['private'] else 0
        cs.update_group(gid, **fields)
        st.log_action(actor='human', action='group', content=f'网页改分类 #{gid}: {fields}')
    return _json(_group_rows(st))


@api
def chat_group(request: Request):
    """PUT {group_id: 分类 id 或 0, attention: 关注度 或 '' 跟分类}: 本人指定会话的分类和关注度, 立即生效。"""
    from .. import groups
    st, cid, b = Storage(), request.path_params['chat_id'], request.state.body
    cs = groups.ChatStore(st)
    gid = None
    if 'group_id' in b:
        gid = int(b['group_id'] or 0)
        if gid and not cs.group(gid):
            raise ValueError('没有这个分类')
    att = None
    if 'attention' in b:
        att = '' if not b['attention'] else groups.parse_attention(b['attention'])
    cs.assign([cid], group_id=gid, attention=att, by='web')
    st.log_action(actor='human', action='group', chat_id=cid, content=f'网页归类 group={gid} attention={att}')
    return _json(cs.view([cid])[0])


@api
def jobs_api(request: Request):
    """例行任务: 每个任务的开关、上次结果、下次时间, 以及最近的运行记录。"""
    from .. import jobs
    st, cfg, now = Storage(), load_config(), datetime.now()
    store = jobs.JobStore(st)
    rows = []
    for job in jobs.JOBS:
        nxt = jobs.next_time(job, cfg, now)
        rows.append({'name': job.name, 'title': job.title, 'kind': job.kind, 'once': job.once,
                     'enabled': job.when(cfg) is not None, 'next': nxt, 'last': jobs.describe(store.last(job.name))})
    return _json({'jobs': rows, 'runs': [jobs.describe(r) for r in store.log(limit=int(request.query_params.get('limit', 30)))],
                  'backfill': store.get_state('backfill'), 'bootstrap': store.get_state('bootstrap')})


@api
def proposals(request: Request):
    """最近的整理方案和每条的状态; 没打开整理方案时 enabled=false, 列表为空。"""
    from .. import organize
    cfg = load_config()
    if not organize.enabled(cfg):
        return _json({'enabled': False, 'items': [], 'proposals': []})
    store = organize.OrganizeStore(Storage())
    return _json({'enabled': True, 'items': sorted(organize.ITEM_NAMES[k] for k in organize.enabled_kinds(cfg)
                                                   if k in organize.ITEM_NAMES), 'proposals': [{'id': p.id, 'trigger': p.trigger, 'status': p.status, 'summary': p.summary, 'created_at': p.created_at,
                   'items': [{'seq': i.seq, 'kind': i.kind, 'title': i.title, 'reason': i.reason, 'status': i.status,
                              'error': i.error} for i in store.items(p.id)]}
                  for p in store.list(int(request.query_params.get('limit', 10)))]})


@api
def proposal_item(request: Request):
    """POST {action: apply|reject|undo, items: [编号]}: 本人在网页上同意、放弃或撤销。"""
    from .. import organize
    cfg = load_config()
    if not organize.enabled(cfg):
        raise ValueError('整理方案没有打开(LARKX_CHAT_GROUPS 和 LARKX_ORGANIZE)')
    st, pid, b = Storage(), request.path_params['proposal_id'], request.state.body
    store = organize.OrganizeStore(st, organize.enabled_kinds(cfg))
    if not store.get(pid):
        raise ValueError('没有这个方案')
    action, seqs = b.get('action'), [int(x) for x in b.get('items') or []]
    if action in ('apply', 'reject') and not seqs:
        raise ValueError('要选上至少一条')
    if action == 'reject':
        res = [(s, '已放弃') for s in seqs] if store.reject(pid, seqs) else []
    elif action in ('apply', 'undo'):
        ops = organize.FeishuOps(_lark())
        res = store.apply(pid, seqs, ops) if action == 'apply' else store.undo(pid, ops)
    else:
        raise ValueError('不支持的操作')
    st.log_action(actor='human', action='organize', content=f'网页{action} 整理方案 #{pid}: {res}')
    return _json({'ok': True, 'results': [{'seq': s, 'result': r} for s, r in res]})


@api
def job_run(request: Request):
    from .. import jobs
    name = request.path_params['name']
    if not jobs.get(name):
        raise ValueError('没有这个例行任务')
    return control_wait(f'job:{name}')


@api
def mentions(request: Request):
    from ..protocol import LarkAuth
    since = time.time() - state.parse_duration(request.query_params.get('since', '24h'))
    st = Storage()
    uid = LarkAuth().user_id
    return _json(st.mentions(uid, st.owner_name(uid), int(since)))


def _task_dict(t) -> dict:
    return {'id': t.id, 'kind': t.kind, 'status': t.status, 'due_at': t.due_at, 'every_sec': t.every_sec, 'chat_id': t.chat_id,
            'by_owner': bool(t.by_owner), 'creator': t.creator, 'run_count': t.run_count, 'last_run_at': t.last_run_at,
            'prompt': t.prompt, 'result': t.result, 'created_at': t.created_at}


@api
def tasks(request: Request):
    st = Storage()
    if request.method == 'POST':
        b = request.state.body
        prompt = (b.get('prompt') or '').strip()
        if not prompt:
            raise ValueError('任务内容为空')
        due = state.parse_at(b['at']) if b.get('at') else time.time() + state.parse_duration(b.get('in') or '')
        every = state.parse_duration(b['every']) if b.get('every') else 0
        task_id = st.add_task(kind='task', due_at=int(due), every_sec=every, prompt=prompt, chat_id=b.get('chat_id') or '',
                              by_owner=1, creator='human')
        return _json({'ok': True, 'id': task_id})
    show_all = request.query_params.get('all') == '1'
    rows = st.list_tasks(include_finished=show_all, limit=200)
    return _json([_task_dict(t) for t in rows if show_all or t.kind == 'task'])


@api
def task_item(request: Request):
    st = Storage()
    t = st.get_task(int(request.path_params['task_id']))
    if not t:
        return _json({'error': '没有这个任务'}, 404)
    if request.method == 'DELETE':
        st.update_task(t.id, status='cancelled')
        return _json({'ok': True})
    return _json(_task_dict(t))


@api
def say(request: Request):
    text = (request.state.body.get('text') or '').strip()
    if not text:
        raise ValueError('内容为空')
    task_id = Storage().add_task(kind='say', due_at=0, prompt=text, by_owner=1, creator='human')
    return _json({'ok': True, 'id': task_id})


@api
def takeovers(request: Request):
    st = Storage()
    names = _chat_names(st)
    if request.method == 'POST':
        b = request.state.body
        if not b.get('chat_id'):
            raise ValueError('需要 chat_id')
        seconds = state.parse_duration(b['for']) if b.get('for') else load_config()['takeover_default']
        st.takeover_start(b['chat_id'], time.time() + seconds, b.get('goal') or '', 'human')
        return _json({'ok': True})
    return _json([{'chat_id': t.chat_id, 'chat_name': names.get(t.chat_id, ''), 'until': t.until, 'goal': t.goal,
                   'creator': t.creator, 'created_at': t.created_at} for t in st.takeover_list() if t.until > time.time()])


@api
def takeover_item(request: Request):
    ok = Storage().takeover_expire(request.path_params['chat_id'], time.time())
    return _json({'ok': ok})


# ---- 唤起白名单 ----
# 和 larkx whitelist 共用一张表; daemon 每条消息现查, 改了立即生效。数字人没有网页令牌, 改不了这里

def _auth():
    from ..protocol import LarkAuth
    return LarkAuth()


def _lark():
    from ..protocol import LarkClient
    return LarkClient(_auth())


def _whitelist_state(st: Storage) -> dict:
    cfg = load_config()
    triggers = cfg['triggers']
    rows = st.whitelist_list()
    auth = _auth()
    owner = st.owner_name(auth.user_id) if '{owner}' in (cfg.get('deny_reply') or '') else ''
    return {'rule': wl.rule_sentence(len(rows), triggers), 'deny': wl.deny_sentence(cfg, len(rows), owner),
            'owner_only': bool(triggers.get('owner_only')), 'group_at_only': bool(triggers.get('group_at_only')),
            'entries': [wl.entry_dict(r, auth.region) for r in rows]}


@api
def whitelist(request: Request):
    st = Storage()
    if request.method == 'POST':
        b = request.state.body
        uid = str(b.get('user_id') or '').strip()
        if not wl.is_user_id(uid):
            raise ValueError('user_id 不合法')
        if uid == str(_auth().user_id):
            raise ValueError('你本人一直能唤起数字人, 不用加进白名单')
        name, dept, avatar = (str(b.get(k) or '').strip() for k in ('name', 'department', 'avatar_key'))
        if not name:
            person = wl.lookup_user(_lark(), uid)
            if not person:
                raise ValueError(f'找不到 user_id 为 {uid} 的人')
            name, dept, avatar = person['name'], person['department'], person['avatar_key']
        created = st.whitelist_add(uid, name, dept, avatar, added_by='web')
        if created:
            st.log_action(actor='human', action='whitelist', content=f'网页加入白名单 {wl.label({"user_id": uid, "name": name, "department": dept})}')
        return _json(dict(_whitelist_state(st), ok=True, created=created))
    return _json(_whitelist_state(st))


@api
def whitelist_item(request: Request):
    st = Storage()
    uid = request.path_params['user_id']
    if not wl.is_user_id(uid):
        raise ValueError('user_id 不合法')
    row = st.whitelist_get(uid)
    if not row:
        return _json({'error': '白名单里没有这个人'}, 404)
    st.whitelist_remove(uid)
    st.log_action(actor='human', action='whitelist', content=f'网页移出白名单 {wl.label(row)}')
    return _json(dict(_whitelist_state(st), ok=True))


@api
def people(request: Request):
    """按名字搜人, 给白名单页添加用: 带部门和头像区分同名的人, 标出已在白名单里的和本人。"""
    q = (request.query_params.get('q') or '').strip()
    if not q:
        raise ValueError('输入名字再搜')
    try:
        client = _lark()
        found = wl.search_people(client, q)
    except Exception as e:
        return _json({'error': f'搜索失败: {e}'[:300]}, 502)
    listed = Storage().whitelist_ids()
    me = str(client.auth.user_id)
    region = client.auth.region
    return _json({'query': q, 'people': [dict(p, avatar=region.avatar_url(p['avatar_key']), whitelisted=p['user_id'] in listed,
                                              is_owner=p['user_id'] == me) for p in found]})


# ---- 设置 ----
# 存在库里(settings 表), daemon 每条消息现查, 改了下一条就生效, 不用重启。数字人没有网页令牌, 存储层也拦着它

ACK_SCOPE_TEXT = {'owner': '只对本人自己发的 /ai 消息', 'all': '所有触发数字人的消息'}


def _settings_state(st: Storage) -> dict:
    # inbox_max_age: 超过这个秒数的旧消息不交给数字人, 也不加表情(页面上的说明用)
    return {'ack_reaction': dict(st.ack_reaction(), reaction=ACK_REACTION), 'inbox_max_age': load_config()['inbox_max_age']}


@api
def settings(request: Request):
    st = Storage()
    if request.method == 'PUT':
        b = request.state.body
        unknown = sorted(set(b) - {'ack_reaction'})
        if unknown or not b:
            raise ValueError(f'不认识的设置: {", ".join(unknown)}' if unknown else '没有要改的设置')
        ack = b['ack_reaction']
        if not isinstance(ack, dict) or not ack or set(ack) - {'enabled', 'scope'}:
            raise ValueError('ack_reaction 只能改 enabled、scope')
        before = st.ack_reaction()
        after = st.set_ack_reaction(ack.get('enabled'), ack.get('scope'))
        if after != before:
            st.log_action(actor='human', action='setting',
                          content=f'网页设置: 触发时先回 {ACK_REACTION} 表情{"打开" if after["enabled"] else "关闭"}, '
                                  f'范围: {ACK_SCOPE_TEXT[after["scope"]]}')
        return _json(dict(_settings_state(st), ok=True))
    return _json(_settings_state(st))


def _agent_home() -> Path:
    return Path(load_config()['agent_home'])


def _git(home: Path, *args) -> str:
    return subprocess.run(['git', '-C', str(home), *args], capture_output=True, text=True).stdout


@api
def memory(request: Request):
    """记忆文件(含 archive/ 里的归档)和开头的字段。打开结构化记忆时 MEMORY.md 由代码生成, 只读。"""
    from .. import memory as mem
    home, structured = _agent_home(), bool(load_config().get('memory_structured'))
    base = home / 'memory'
    paths = sorted(base.glob('*.md'), key=lambda p: (p.name != 'MEMORY.md', p.name)) + sorted((base / 'archive').glob('*.md'))
    files = []
    for p in paths:
        text, name = p.read_text(encoding='utf-8'), p.relative_to(base).as_posix()
        files.append({'name': name, 'content': text, 'mtime': datetime.fromtimestamp(p.stat().st_mtime),
                      'meta': mem.parse(text)[0], 'archived': name.startswith('archive/'),
                      'generated': structured and name == mem.INDEX, 'structured': structured})
    return _json(files)


@api
def memory_issues(request: Request):
    """结构化记忆的检查结果; 没打开时为空。"""
    from .. import memory as mem
    home = _agent_home()
    if not load_config().get('memory_structured') or not (home / 'memory').is_dir():
        return _json([])
    return _json([{'level': l, 'file': f, 'message': m} for l, f, m in mem.lint(home)])


@api
def memory_item(request: Request):
    from ..memory import INDEX
    name = request.path_params['name']
    if not MEMORY_NAME.match(name) or '..' in name:
        raise ValueError('文件名不合法')
    if name == INDEX and load_config().get('memory_structured'):
        raise ValueError('MEMORY.md 由程序按各文件的字段生成, 改记忆请改对应文件')
    home = _agent_home()
    path = home / 'memory' / name
    if not path.resolve().is_relative_to((home / 'memory').resolve()):
        raise ValueError('文件名不合法')
    path.parent.mkdir(parents=True, exist_ok=True)
    if request.method == 'DELETE':
        path.unlink(missing_ok=True)
        commit_agent_home(home, f'网页删除 memory/{name}')
        return _json({'ok': True})
    path.write_text(request.state.body.get('content', ''), encoding='utf-8')
    commit_agent_home(home, f'网页编辑 memory/{name}')
    return _json({'ok': True})


@api
def persona(request: Request):
    home = _agent_home()
    path = home / 'CLAUDE.md'
    if request.method == 'PUT':
        path.write_text(request.state.body.get('content', ''), encoding='utf-8')
        commit_agent_home(home, '网页编辑 CLAUDE.md')
        return _json({'ok': True, 'note': '新会话才一定会读到新的人设: 需要时在"指挥"里重置会话'})
    return _json({'content': path.read_text(encoding='utf-8')})


@api
def history(request: Request):
    """数字人目录的提交记录: 记忆和人设每次是怎么变的。"""
    home = _agent_home()
    out = []
    log = _git(home, 'log', '-n', '60', '--date=format:%Y-%m-%d %H:%M:%S', '--format=%h%x1f%ad%x1f%s', '--shortstat')
    for block in log.strip().split('\n\n'):
        lines = [l for l in block.strip().splitlines() if l.strip()]
        for line in lines:
            if '\x1f' in line:
                sha, when, subject = line.split('\x1f', 2)
                out.append({'sha': sha, 'time': when, 'subject': subject, 'stat': ''})
            elif out:
                out[-1]['stat'] = line.strip()
    return _json(out)


@api
def history_item(request: Request):
    sha = request.path_params['sha']
    if not re.fullmatch(r'[0-9a-f]{4,40}', sha):
        raise ValueError('提交号不合法')
    diff = _git(_agent_home(), 'show', '--format=%s%n%ad', '--date=format:%Y-%m-%d %H:%M:%S', sha, '--', 'memory', 'CLAUDE.md')
    return _json({'sha': sha, 'diff': diff[:60000]})


@api
def logs(request: Request):
    from ..daemon import LOG_PATH
    n = min(int(request.query_params.get('lines', 300)), 2000)
    if not LOG_PATH.exists():
        return _json({'lines': []})
    with open(LOG_PATH, 'rb') as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - 400 * n))
        lines = f.read().decode('utf-8', errors='replace').splitlines()[-n:]
    return _json({'lines': lines})


@api
def control(request: Request):
    action = request.state.body.get('action')
    if action not in ('interrupt', 'clear'):
        raise ValueError('不支持的操作')
    return control_wait(action)


def control_wait(action: str):
    """交给 daemon 的控制请求, 最多等 15 秒拿结果。"""
    st = Storage()
    cid = st.control_add(action)
    deadline = time.time() + 15
    while time.time() < deadline:
        c = st.control_get(cid)
        if c.status != 'pending':
            return _json({'ok': True, 'result': c.result})
        time.sleep(0.5)
    st.control_done(cid, '超时', status='expired')
    return _json({'ok': False, 'result': 'daemon 15 秒内没有响应, 它在运行吗?'})


@api
def restart(request: Request):
    r = service.systemctl('restart', service.UNITS['daemon'])
    return _json({'ok': r.returncode == 0, 'result': (r.stderr or '已重启').strip()})


@api
def recall(request: Request):
    from ..protocol import LarkAuth, LarkClient
    msg_id = str(request.state.body.get('msg_id') or '')
    if not msg_id.isdigit():
        raise ValueError('msg_id 不合法')
    st = Storage()
    sent = (st.find_action('agent', ('send', 'forward'), msg_id) or st.find_action('human', ('send', 'forward'), msg_id)
            or st.find_action('daemon', 'deny_reply', msg_id))
    try:
        LarkClient(LarkAuth()).api('messages.DeleteMessageRequest', {'id': msg_id})
    except Exception as e:
        st.log_action(actor='human', action='recall', chat_id=sent.chat_id if sent else '', msg_id=msg_id,
                      content=f'网页撤回 {msg_id}', ok=0, error=str(e)[:300])
        return _json({'ok': False, 'error': '撤回失败: 只能撤回自己账号发的消息, 且要在飞书允许的撤回时限内'}, 400)
    st.log_action(actor='human', action='recall', chat_id=sent.chat_id if sent else '', msg_id=msg_id, content=f'网页撤回 {msg_id}')
    return _json({'ok': True})


class _Static(StaticFiles):
    """静态文件每次都向服务器确认(有 ETag, 没改动时返回 304), 升级后浏览器不会用旧的 js/css。"""

    def file_response(self, *args, **kwargs) -> Response:
        resp = super().file_response(*args, **kwargs)
        resp.headers['Cache-Control'] = 'no-cache'
        return resp


class _JSONBody:
    """把 JSON 请求体解析成 dict 放进 request.state.body, 供同步接口使用。其他类型的请求体不读, body 为空。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope['method'] in ('GET', 'HEAD'):
            return await self.app(scope, receive, send)
        ctype = dict(scope.get('headers') or []).get(b'content-type', b'').split(b';')[0].strip().lower()
        if ctype not in (b'', b'application/json', b'text/plain'):
            scope.setdefault('state', {})['body'] = {}
            return await self.app(scope, receive, send)
        chunks, more = [], True
        while more:
            msg = await receive()
            chunks.append(msg.get('body', b''))
            more = msg.get('more_body', False)
        raw = b''.join(chunks)
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:  # 不是 JSON, 或者不是 UTF-8
            body = {}
        scope.setdefault('state', {})['body'] = body if isinstance(body, dict) else {}

        async def replay():
            return {'type': 'http.request', 'body': raw, 'more_body': False}
        return await self.app(scope, replay, send)


def create_app() -> Starlette:
    routes = [
        Route('/', index), Route('/console', old_console), Route('/login', login, methods=['POST']),
        Route('/api/overview', overview), Route('/api/activity', activity), Route('/api/conversations', conversations),
        Route('/api/chats', chats), Route('/api/chat-profiles', chat_profiles), Route('/api/mentions', mentions),
        Route('/api/chats/{chat_id}/group', chat_group, methods=['PUT']),
        Route('/api/groups', groups_api, methods=['GET', 'POST']),
        Route('/api/groups/{group_id:int}', group_item, methods=['PUT', 'DELETE']),
        Route('/api/jobs', jobs_api), Route('/api/jobs/{name}/run', job_run, methods=['POST']),
        Route('/api/tasks', tasks, methods=['GET', 'POST']), Route('/api/tasks/{task_id:int}', task_item, methods=['GET', 'DELETE']),
        Route('/api/say', say, methods=['POST']),
        Route('/api/takeovers', takeovers, methods=['GET', 'POST']),
        Route('/api/takeovers/{chat_id}', takeover_item, methods=['DELETE']),
        Route('/api/whitelist', whitelist, methods=['GET', 'POST']),
        Route('/api/whitelist/{user_id}', whitelist_item, methods=['DELETE']),
        Route('/api/people', people),
        Route('/api/settings', settings, methods=['GET', 'PUT']),
        Route('/api/proposals', proposals), Route('/api/proposals/{proposal_id:int}', proposal_item, methods=['POST']),
        Route('/api/memory', memory), Route('/api/memory-issues', memory_issues),
        Route('/api/memory/{name:path}', memory_item, methods=['PUT', 'DELETE']),
        Route('/api/persona', persona, methods=['GET', 'PUT']),
        Route('/api/history', history), Route('/api/history/{sha}', history_item),
        Route('/api/logs', logs), Route('/api/control', control, methods=['POST']),
        Route('/api/restart', restart, methods=['POST']), Route('/api/recall', recall, methods=['POST']),
        Mount('/static', _Static(directory=str(STATIC)), name='static'),
    ]
    app = Starlette(routes=routes)
    return _JSONBody(app)


def run_web():
    import uvicorn
    cfg = load_config()
    get_token()
    uvicorn.run(create_app(), host='127.0.0.1', port=cfg['web_port'], log_level='warning', proxy_headers=True)
