"""agent 每次调用工具前后的检查。

这里是 daemon 一侧的拦截, CLI 自己还有一层检查(见 larkx/cli.py 的 _agent_forbidden)。
按字符串匹配命令能被刻意绕过, 只用来挡住误操作, 不是安全边界。
"""
import json
import re

from claude_agent_sdk import HookMatcher
from loguru import logger

from .. import guest, state

# 原始网关接口、登录凭证、再起一个监听进程、管理自己的服务、以本人身份下指令、改 skill: 都不该由数字人自己做
FORBIDDEN_CMD = re.compile(r'\b(larkx|lark)\s+(api|auth|listen|daemon|service|doctor|say|skill|web|prune|backfill)\b')
# 唤起白名单只有本人能改(可以看): 免得有人让数字人把自己加进去。CLI 和存储层也会拒绝
WHITELIST_WRITE = re.compile(r'\b(larkx|lark)\s+whitelist\s+(add|remove)\b')
# 例行任务只能由本人手动触发(看状态和记录可以)
JOBS_RUN = re.compile(r'\b(larkx|lark)\s+jobs\s+run\b')
# 把命令放到 daemon 服务之外去跑(跳出 cgroup, 进程祖先里也没有数字人的标记), 之后就认不出是数字人了。
# 只看命令位置上的词(跳过引号里的内容和 heredoc 正文), 免得 larkx send ... "用 crontab 定时" 这种也被拦。
# 这一层只防误用和提示词注入, 不是安全边界: 换个写法(python 拼路径之类)还是能绕过
ESCAPE_PROGRAMS = {'systemd-run', 'crontab', 'at', 'batch'}
# 包一层再执行后面命令的程序, 以及它们带值的选项(值要一起跳过)
WRAPPERS = {'sudo', 'env', 'nohup', 'command', 'exec', 'time', 'nice', 'ionice', 'setsid', 'timeout', 'xargs', 'stdbuf',
            'chrt', 'taskset', 'flock', 'unbuffer'}
WRAPPER_VALUE_OPTS = {'-u', '--unset', '-n', '--adjustment', '-s', '--signal', '-k', '--kill-after', '-c', '-p', '-g',
                      '-C', '-E', '-I', '-L', '-d', '-a', '-P', '-o', '-e', '-i'}
SHELLS = {'bash', 'sh', 'zsh', 'dash', 'ksh', 'fish'}
HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][\w]*)\1")


def _strip_heredocs(command: str) -> str:
    out, end = [], None
    for line in command.splitlines():
        if end is not None:
            if line.strip() == end:
                end = None
            continue
        out.append(line)
        m = HEREDOC.search(line)
        if m:
            end = m.group(2)
    return '\n'.join(out)


def command_words(command: str, depth: int = 0):
    """命令里每一段(;、&&、||、|、&、(、换行、`、$( 分开)要执行的程序名; 引号里的内容不算。
    timeout 5 at、nice -n 5 at 这种跳过包装命令和它的参数; bash -c "..."、eval "..." 往里再看一层(最多三层)。
    解析不了时返回 None。"""
    import shlex
    text = _strip_heredocs(command or '').replace('$(', ' ; ').replace('`', ' ; ')
    words = []
    for line in text.splitlines():
        lex = shlex.shlex(line, posix=True, punctuation_chars=';&|()')
        lex.whitespace_split = True
        try:
            tokens = list(lex)
        except ValueError:
            return None
        i, start, wrapped = 0, True, False
        while i < len(tokens):
            tok = tokens[i]
            i += 1
            if tok and set(tok) <= set(';&|()'):
                start, wrapped = True, False
                continue
            if not start:
                continue
            if '=' in tok and not tok.startswith('-') and not wrapped:
                continue  # FOO=bar cmd
            if wrapped:
                # 包装命令的参数: 选项(带值的连值一起跳过)、timeout 的时长、nice/chrt 的数字、env 的 FOO=bar
                if tok.startswith('-'):
                    if tok in WRAPPER_VALUE_OPTS and i < len(tokens):
                        i += 1
                    continue
                if re.fullmatch(r'[\d.]+[smhd]?', tok) or ('=' in tok and words and words[-1] == 'env'):
                    continue
            name = tok.rsplit('/', 1)[-1]
            words.append(name)
            if name in WRAPPERS:
                wrapped = True
                continue
            if depth < 3 and (name in SHELLS or name == 'eval'):
                rest = tokens[i:]
                inner = None
                if name == 'eval':
                    inner = ' '.join(t for t in rest if not (t and set(t) <= set(';&|()')))
                elif '-c' in rest or any(t.startswith('-') and 'c' in t[1:] and not t.startswith('--') for t in rest[:3]):
                    j = next(k for k, t in enumerate(rest) if t == '-c' or (t.startswith('-') and 'c' in t[1:]))
                    inner = rest[j + 1] if j + 1 < len(rest) else ''
                if inner:
                    sub = command_words(inner, depth + 1)
                    if sub is None:
                        return None
                    words += sub
            start, wrapped = False, False
    return words


def escape_denied(command: str) -> bool:
    words = command_words(command)
    if words is None:  # 引号不配对之类解析不了: 退回整段匹配, 宁可多拦
        return bool(re.search(r'\b(systemd-run|crontab)\b|(^|[;&|(`]\s*)(at|batch)\s', command or ''))
    return any(w in ESCAPE_PROGRAMS for w in words)


def _deny(reason: str) -> dict:
    return {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                                   'permissionDecisionReason': reason}}


def check_tool_use(tool_name: str, tool_input: dict):
    """返回拒绝原因; 放行时返回 None。"""
    blob = json.dumps(tool_input or {}, ensure_ascii=False)
    if 'credentials.json' in blob or 'web_token' in blob or 'turn.key' in blob:
        return '数字人不能读取或修改飞书登录凭证、网页控制台令牌和轮次签名密钥'
    if tool_name == 'Bash' and FORBIDDEN_CMD.search((tool_input or {}).get('command', '')):
        return '数字人不能调用 larkx api / auth / listen / daemon / service / doctor / say / skill / web / prune / backfill; 需要的能力请用 larkx 的其他子命令'
    if tool_name == 'Bash' and WHITELIST_WRITE.search((tool_input or {}).get('command', '')):
        return '数字人不能修改唤起白名单, 只有本人能改; 想加进白名单的人请直接找本人'
    if tool_name == 'Bash' and JOBS_RUN.search((tool_input or {}).get('command', '')):
        return '数字人不能手动触发例行任务; larkx jobs list / log 可以看'
    if tool_name == 'Bash' and escape_denied((tool_input or {}).get('command', '')):
        return '数字人不能用 systemd-run、crontab、at 把命令放到服务之外去跑; 定时的事用 larkx task add'
    return None


def make_pre_tool_use(agent_home: str, on_alert=None):
    async def _pre_tool_use(inp, tool_use_id, ctx):
        tool, ti = inp.get('tool_name'), inp.get('tool_input')
        try:
            # 先确定这一轮是谁发起的: 读不到或不可信时按访客处理(state.agent_turn); 检查过程中出错一律拒绝
            turn = state.agent_turn()
            guest_mode = guest.is_guest(turn)
            reason = check_tool_use(tool, ti)
            if not reason and guest_mode:
                reason = guest.tool_denied(tool, ti, agent_home)
        except Exception as e:
            logger.exception('检查工具调用时出错, 按拒绝处理')
            return _deny(f'检查这次工具调用时出错({e!r}), 已拒绝')
        if reason:
            detail = json.dumps(ti, ensure_ascii=False)[:200]
            logger.warning(f'[agent] 已拒绝 {tool}: {reason} | {detail}')
            if guest_mode and on_alert:
                on_alert(f'访客发起的请求({turn.get("ref")})想让数字人调用 {tool}, 已拦截: {reason}。{detail[:120]}')
            return _deny(reason)
        return {}
    return _pre_tool_use


async def _post_tool_use(inp, tool_use_id, ctx):
    logger.info(f'[agent] $ {((inp.get("tool_input") or {}).get("command") or "")[:300]}')
    return {}


async def _pre_compact(inp, tool_use_id, ctx):
    logger.info(f'[agent] 开始压缩上下文 trigger={inp.get("trigger")}')
    return {}


def build_hooks(agent_home: str, on_alert=None) -> dict:
    return {
        'PreToolUse': [HookMatcher(hooks=[make_pre_tool_use(agent_home, on_alert)])],
        'PostToolUse': [HookMatcher(matcher='Bash', hooks=[_post_tool_use])],
        'PreCompact': [HookMatcher(hooks=[_pre_compact])],
    }
