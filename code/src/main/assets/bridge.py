"""pi bridge for the Pi Agent app.

Runs inside the app's own Linux userland (Alpine under proot, started by the app's foreground
service) and lets the app talk to pi (pi.dev, `@earendil-works/pi-coding-agent`): each conversation
is a `pi --mode rpc` process, kept alive between turns and opened again from its saved session when
needed. pi's events are handed to the app as the stream-json messages its conversation view reads
(Chat.apply), so the app shows pi's replies, thinking and tool steps as it did Claude Code's.
Listens on a Unix socket in the app's own files (SOCKET_FILE): no other app can reach it, so
requests need no token.

Standard library only.
"""
import argparse
import json
import os
import re
import shlex
import shutil
import signal
import socketserver
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

VERSION = 1  # Bridge VERSION in the app: an app update replaces an older running bridge
HOME = os.path.expanduser('~')
PI_DIR = os.environ.get('PI_CODING_AGENT_DIR') or os.path.join(HOME, '.pi', 'agent')
STATE_DIR = os.path.join(HOME, '.piapp')
PID_FILE = os.path.join(STATE_DIR, 'bridge.pid')
LOCK_FILE = os.path.join(STATE_DIR, 'bridge.lock')
SOCKET_FILE = os.path.join(STATE_DIR, 'bridge.sock')  # Bridge.SOCKET in the app
DEFAULT_CWD = os.path.join(HOME, 'work')
LOG_FILE = os.path.join(STATE_DIR, 'bridge.log')
LAUNCH_FILE = os.path.join(STATE_DIR, 'launch.json')
SPARE_FILE = os.path.join(STATE_DIR, 'spare.json')  # how the last new conversation was started
TZ_FILE = os.path.join(STATE_DIR, 'timezone')  # the phone's time zone, kept by the app (BridgeService)
SUMMARY_FILE = os.path.join(STATE_DIR, 'sessions.json')  # the list's summaries, kept for the next start
MODELS_FILE = os.path.join(STATE_DIR, 'models.json')  # pi's models with a way to sign in, as it last listed them
INFO_FILE = os.path.join(STATE_DIR, 'pi-info.json')
STARTED = time.time()  # tells the app a new bridge took over (turns of the old one are gone)
THINKING_LEVELS = ['off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max']
DEFAULT_TOOLS = ['read', 'bash', 'edit', 'write']
SHORT_TITLE = 60  # characters of a first message shown as its conversation's name


def sessions_dir():
    """Where pi keeps its sessions: PI_CODING_AGENT_SESSION_DIR, its sessionDir setting, or its own."""
    d = os.environ.get('PI_CODING_AGENT_SESSION_DIR') or pi_settings().get('sessionDir')
    if isinstance(d, str) and d:
        return os.path.expanduser(d) if os.path.isabs(os.path.expanduser(d)) else os.path.join(HOME, d)
    return os.path.join(PI_DIR, 'sessions')


def read_json(path, default):
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, type(default)) else default
    except (OSError, ValueError):
        return default


def write_json(path, data, mode=None):
    """Written aside, then moved in: a write cut short leaves the last one whole."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    part = path + '.tmp'
    with open(part, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    if mode is not None:
        os.chmod(part, mode)
    os.replace(part, path)


def pi_settings():
    return read_json(os.path.join(PI_DIR, 'settings.json'), {})


class Hub:
    """Numbered events for the app; a reconnecting client asks for those after its last one."""

    def __init__(self, keep=4000):
        self.events = []
        self.seq = 0
        self.keep = keep
        self.cond = threading.Condition()

    def emit(self, chat, kind, data=None):
        with self.cond:
            self.seq += 1
            self.events.append({'seq': self.seq, 'chat': chat, 'kind': kind, 'data': data})
            if len(self.events) > self.keep:
                del self.events[:len(self.events) - self.keep]
            self.cond.notify_all()

    def oldest(self):
        """The first event still kept; seq + 1 when none is."""
        with self.cond:
            return self.events[0]['seq'] if self.events else self.seq + 1

    def after(self, seq, timeout):
        with self.cond:
            if self.seq <= seq:
                self.cond.wait(timeout)
            if not self.events:
                return []
            # Numbers follow one another: the first one after seq is found by its place.
            return self.events[max(0, seq + 1 - self.events[0]['seq']):]


hub = Hub()


# pi's options that say which session a pi runs, or how it talks: the bridge's own to give (pi_args).
# Among the extra arguments one would win over them (--continue opens the folder's last session),
# and a message would go to another conversation than the one it was written in.
OWN_OPTIONS = ('-c', '--continue', '-r', '--resume', '--session', '--session-id', '--fork', '--no-session',
               '--session-dir', '--mode', '-p', '--print', '--export', '--list-models', '-h', '--help', '-v', '--version')


def extra_args(text):
    """The extra arguments set in the app, as pi gets them. ValueError, in words for the user, when
    they are not arguments (bad quoting, or text beginning with no option: instructions for the model
    written here, whose words would be arguments) or say which session runs or how pi talks."""
    args = shlex.split(text)
    if args and not re.match(r'^--?[A-Za-z]', args[0]):
        raise ValueError('启动参数要以选项开头（如 --tools read,bash）；给模型的说明请写在「系统提示」里')
    for arg in args:
        if arg.split('=', 1)[0] in OWN_OPTIONS:
            raise ValueError('启动参数里不能有 %s：开哪个对话、怎么交流由 App 决定' % arg.split('=', 1)[0])
    return [os.path.expanduser(a) for a in args]


class Launch:
    """How pi is started, set in the app: text appended to the system prompt, extra arguments and
    tools left out. A change (posting to /launch, even nothing) is for new conversations: the spare
    is started again with it, and a conversation's pi running already keeps how it was started."""

    def __init__(self):
        self.gen = 0
        self.data = {'appendSystemPrompt': '', 'extraArgs': '', 'disallowedTools': ''}
        self.data.update({k: v for k, v in read_json(LAUNCH_FILE, {}).items() if k in self.data and isinstance(v, str)})

    def set(self, data):
        new = dict(self.data)
        for key in self.data:
            if key in data:
                new[key] = str(data[key] or '')
        # Only when changed: ones saved before they were checked (args leaves those out) must not
        # make every other setting refused.
        if new['extraArgs'] != self.data['extraArgs']:
            extra_args(new['extraArgs'])
        write_json(LAUNCH_FILE, new)
        self.data = new
        self.gen += 1

    def args(self):
        args = []
        if self.data['appendSystemPrompt'].strip():
            args += ['--append-system-prompt', self.data['appendSystemPrompt']]
        tools = self.data.get('disallowedTools', '').replace(',', ' ').split()
        if tools:
            args += ['--exclude-tools', ','.join(tools)]
        try:
            args += extra_args(self.data['extraArgs'])
        except ValueError:
            pass  # saved before they were checked: left out whole (view says so)
        return args

    def view(self):
        """The settings as the app shows them, with why the saved extra arguments are not used when they are not."""
        try:
            extra_args(self.data['extraArgs'])
        except ValueError as e:
            return dict(self.data, extraArgsUnused=str(e))
        return self.data


launch = Launch()


def memory_path(scope, cwd=None):
    """AGENTS.md the app edits: the user's own (every conversation) or a working folder's."""
    if scope == 'global':
        return os.path.join(PI_DIR, 'AGENTS.md')
    if scope == 'folder' and cwd:
        return os.path.join(os.path.expanduser(cwd), 'AGENTS.md')
    raise ValueError('unknown scope')


def pi_path():
    return shutil.which('pi') or '/usr/local/bin/pi'


def pi_env():
    """A pi's environment: the bridge's, and the phone's time zone in TZ (the userland has no
    /etc/localtime); no TZ when the app has told no zone the userland has."""
    env = dict(os.environ)
    env.setdefault('PI_SKIP_VERSION_CHECK', '1')
    try:
        with open(TZ_FILE, encoding='utf-8') as f:
            tz = f.read().strip()
    except OSError:
        return env
    if re.fullmatch(r'[\w+-]+(/[\w+-]+)*', tz, re.ASCII) and os.path.isfile('/usr/share/zoneinfo/' + tz):
        env['TZ'] = tz
    return env


def pi_args(session, model, effort):
    """session: ('new', id) for a session made with that id, ('open', path) for a saved one."""
    args = [pi_path(), '--mode', 'rpc']
    args += ['--session-id', session[1]] if session[0] == 'new' else ['--session', session[1]]
    if model:
        args += ['--model', model]
    if effort:
        args += ['--thinking', effort]
    return args + launch.args()


last_spawn = 0.0  # when a conversation's pi was last started


def spawn(args, cwd):
    global last_spawn
    last_spawn = time.time()
    os.makedirs(cwd, exist_ok=True)
    # Binary pipes: records are split on LF only (a text reader would also split on U+2028 inside JSON).
    return subprocess.Popen(args, cwd=cwd, env=pi_env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, start_new_session=True)


class Spare:
    """A pi started ahead for the next new conversation, with its session id: starting one is most
    of the wait for a first reply (seconds on a phone, under proot). Started only while no turn
    runs; its output waits in the pipe until a conversation takes it and reads it."""

    def __init__(self):
        self.lock = threading.Lock()
        self.proc = None
        self.key = None
        self.session_id = None
        # Folder, model and thinking level of the last new conversation (kept for the next bridge).
        saved = read_json(SPARE_FILE, [])
        self.want = (DEFAULT_CWD, None, None)
        if len(saved) == 3 and all(v is None or isinstance(v, str) for v in saved):
            self.want = (saved[0] or DEFAULT_CWD, saved[1], saved[2])
        self.retry_at = 0.0
        self.failures = 0

    def wish(self, cwd, model, effort):
        """The next spare is started for a new conversation like this one."""
        want = (cwd or DEFAULT_CWD, model, effort)
        if want == self.want:
            return
        self.want = want
        try:
            write_json(SPARE_FILE, list(want))
        except OSError:
            pass

    def take(self, cwd, model, effort):
        """The spare's process, the model and level it was started with and its session id, when it
        fits a new conversation in cwd (a different model or level is changed in it; pi's own default
        cannot be changed back to); None otherwise."""
        with self.lock:
            proc, key, sid, self.proc = self.proc, self.key, self.session_id, None
        if proc is None:
            return None
        if key[0] == cwd and key[3] == launch.gen and proc.poll() is None \
                and not (model is None and key[1] is not None) and not (effort is None and key[2] is not None):
            self.failures = 0
            return proc, key[1], key[2], sid
        threading.Thread(target=end, args=(proc,), daemon=True).start()
        return None

    def fill(self):
        """Starts the spare unless one fits, however long ago it was started."""
        cwd, model, effort = self.want
        key = (cwd, model, effort, launch.gen)
        with self.lock:
            if self.proc is not None:
                if self.proc.poll() is None and self.key == key:
                    return
                old, self.proc = self.proc, None
                if old.poll() is not None:  # it ended by itself: wait longer each time
                    self.failures += 1
                    self.retry_at = time.time() + min(600, 30 * 2 ** self.failures)
                    log('spare pi exited with %s' % old.returncode)
                else:
                    threading.Thread(target=end, args=(old,), daemon=True).start()
            if time.time() < self.retry_at or not os.path.exists(pi_path()):
                return
            sid = str(uuid.uuid4())
            try:
                self.proc = spawn(pi_args(('new', sid), model, effort), cwd)
            except OSError as e:
                self.failures += 1
                self.retry_at = time.time() + min(600, 30 * 2 ** self.failures)
                log('spare pi not started: %s' % e)
                return
            self.key, self.session_id = key, sid
            pid = self.proc.pid
        log('spare pi pid %d started for a new conversation in %s' % (pid, cwd))


spare = Spare()


def split_model(model):
    """'provider/id' -> (provider, id); a model id alone has no provider."""
    if '/' in model:
        provider, mid = model.split('/', 1)
        return provider, mid
    return None, model


def claude_content(blocks):
    """pi's content blocks (text, image) as the app reads them (Claude's: an image has a source)."""
    out = []
    for b in blocks if isinstance(blocks, list) else [{'type': 'text', 'text': str(blocks or '')}]:
        if not isinstance(b, dict):
            continue
        if b.get('type') == 'text':
            out.append({'type': 'text', 'text': b.get('text', '')})
        elif b.get('type') == 'image':
            out.append({'type': 'image', 'source': {'type': 'base64', 'media_type': b.get('mimeType') or 'image/png',
                                                    'data': b.get('data', '')}})
    return out


class Chat:
    def __init__(self, key):
        self.key = key
        self.session_id = None
        self.cwd = DEFAULT_CWD
        self.model = None
        self.effort = None
        self.proc = None
        self.running = False
        self.used = time.time()
        self.stderr = []
        self.lock = threading.Lock()
        self.last_out = time.time()
        self.quiet_logged = False
        self.turn = 0  # counts messages sent: a forced stop is for the turn it was asked in
        self.began = 0  # counts runs begun (agent_start)
        self.write_lock = threading.Lock()
        self.waiting = {}  # request id -> {'event': ..., 'response': ...}: commands whose answer is waited for
        self.prompts = {}  # request id -> uuid of a message written while a run went on, None for a turn's own
        self.steered = []  # [uuid, text] of messages given to pi while a run went on, until the model has them
        self.taken = False  # its pi was the spare, started ahead
        self.commands = []  # pi's / commands (extensions, skills, prompt templates)
        self.tools = list(DEFAULT_TOOLS)
        # The run being translated: its users' messages so far, the assistant message streaming, how the last ended.
        self.run_users = 0
        self.msg_id = None
        self.msg_n = 0
        self.last_stop = None
        self.last_error = ''
        self.sent_at = None

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        self.stderr = []
        taken = None
        if not self.session_id:
            taken = spare.take(self.cwd, self.model, self.effort)
        if taken:
            proc, model, effort, self.session_id = taken
        else:
            if self.session_id:
                path = find_session(self.session_id)
                session = ('open', path) if path else ('new', self.session_id)
            else:
                self.session_id = str(uuid.uuid4())
                session = ('new', self.session_id)
            proc = spawn(pi_args(session, self.model, self.effort), self.cwd)
            model, effort = self.model, self.effort
        self.proc = proc
        self.taken = bool(taken)
        self.last_out = time.time()
        self.quiet_logged = False
        threading.Thread(target=self.read_out, args=(proc,), daemon=True).start()
        threading.Thread(target=self.read_err, args=(proc,), daemon=True).start()
        if taken:
            self.switch(model, effort)
        self.command({'type': 'get_commands'}, wait=False)
        log('chat %s: pi pid %d %s, session %s, model %s, thinking %s, folder %s' % (
            self.key[:8], proc.pid, 'taken (started ahead)' if taken else 'started', self.session_id,
            self.model or 'default', self.effort or 'default', self.cwd))

    # ---- what pi says ----

    def read_out(self, proc):
        for raw in proc.stdout:
            line = raw.decode('utf-8', 'replace').strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            self.last_out = time.time()
            if self.quiet_logged:
                log('chat %s: output again' % self.key[:8])
                self.quiet_logged = False
            if proc is not self.proc:
                continue  # one let go: what it still says is not this conversation's any more
            try:
                self.handle(msg)
            except Exception:  # one odd record must not end the reading of the rest
                import traceback
                log('chat %s: record not understood:\n%s' % (self.key[:8], traceback.format_exc()))
        code = proc.wait()
        log('chat %s: pi pid %d exited with %s%s' % (
            self.key[:8], proc.pid, code, ', stderr:\n  ' + '\n  '.join(self.stderr[-12:]) if self.stderr else ''))
        for slot in list(self.waiting.values()):
            slot['event'].set()
        if proc is self.proc:
            was_running = self.running
            self.running = False
            self.proc = None
            self.steered = []
            hub.emit(self.key, 'exit', {'code': code, 'running': was_running, 'stderr': '\n'.join(self.stderr[-12:])})

    def read_err(self, proc):
        for raw in proc.stderr:
            self.stderr.append(raw.decode('utf-8', 'replace').rstrip())
            del self.stderr[:-50]

    def cc(self, msg):
        msg.setdefault('session_id', self.session_id)
        hub.emit(self.key, 'cc', msg)

    def handle(self, msg):
        kind = msg.get('type')
        if kind == 'response':
            self.response(msg)
        elif kind == 'agent_start':
            self.began += 1
            self.running = True
            self.run_users = 0
            self.last_stop, self.last_error = None, ''
            self.cc({'type': 'system', 'subtype': 'init', 'model': self.model or '', 'cwd': self.cwd,
                     'tools': self.tools, 'slash_commands': ['compact', 'name'] + self.commands})
        elif kind == 'message_start':
            self.message_start(msg.get('message') or {})
        elif kind == 'message_update':
            self.message_update(msg.get('assistantMessageEvent') or {})
        elif kind == 'message_end':
            self.message_end(msg.get('message') or {})
        elif kind == 'tool_execution_end':
            result = msg.get('result') or {}
            self.cc({'type': 'user', 'message': {'role': 'user', 'content': [{
                'type': 'tool_result', 'tool_use_id': msg.get('toolCallId'), 'is_error': bool(msg.get('isError')),
                'content': claude_content(result.get('content') if isinstance(result, dict) else str(result))}]}})
        elif kind == 'auto_retry_start':
            log('chat %s: retry %s/%s in %sms: %s' % (self.key[:8], msg.get('attempt'), msg.get('maxAttempts'),
                                                     msg.get('delayMs'), msg.get('errorMessage')))
            self.cc({'type': 'system', 'subtype': 'api_retry', 'attempt': msg.get('attempt'),
                     'max_retries': msg.get('maxAttempts'), 'retry_delay_ms': msg.get('delayMs'),
                     'error_status': None, 'error': msg.get('errorMessage') or ''})
        elif kind == 'compaction_end':
            if msg.get('result') and not msg.get('aborted'):
                self.cc({'type': 'system', 'subtype': 'compact_boundary'})
            elif msg.get('errorMessage'):
                log('chat %s: compaction failed: %s' % (self.key[:8], msg.get('errorMessage')))
        elif kind == 'agent_settled':
            self.settled(bool(msg.get('aborted')))
        elif kind == 'session_info_changed' and msg.get('name') and self.session_id:
            hub.emit(self.key, 'title', {'session_id': self.session_id, 'title': msg['name']})
        elif kind == 'extension_ui_request':
            # A dialog of an extension: none can be shown here, so it is cancelled (pi would wait for it).
            if msg.get('method') in ('select', 'confirm', 'input', 'editor'):
                self.write(self.proc, {'type': 'extension_ui_response', 'id': msg.get('id'), 'cancelled': True})
                log('chat %s: extension asked %s (%s): cancelled' % (self.key[:8], msg.get('method'), msg.get('title')))
        elif kind == 'extension_error':
            log('chat %s: extension error: %s' % (self.key[:8], str(msg.get('error'))[:300]))

    def response(self, msg):
        rid = msg.get('id')
        slot = self.waiting.get(rid)
        if slot:
            slot['response'] = msg
            slot['event'].set()
        if msg.get('command') == 'get_commands' and msg.get('success'):
            self.commands = [c.get('name') for c in (msg.get('data') or {}).get('commands') or [] if c.get('name')]
        if rid in self.prompts:
            message_uuid = self.prompts.pop(rid)
            data = msg.get('data') or {}
            if not msg.get('success'):
                error = msg.get('error') or 'pi 没有接受这条消息'
                log('chat %s: message refused: %s' % (self.key[:8], error))
                if message_uuid:  # it waits for the run to end instead (the app sends it then)
                    self.steered = [s for s in self.steered if s[0] != message_uuid]
                    self.cc({'type': 'command_lifecycle', 'command_uuid': message_uuid, 'state': 'failed'})
                else:
                    self.end_turn({'type': 'result', 'subtype': 'error', 'is_error': True, 'result': error})
            elif data.get('disposition') == 'handled' and not message_uuid:
                self.end_turn({'type': 'result', 'subtype': 'success', 'is_error': False})  # no run comes for it
        elif not msg.get('success') and msg.get('command') in ('set_model', 'set_thinking_level'):
            log('chat %s: change refused (it stays as it was): %s' % (self.key[:8], msg.get('error')))

    def end_turn(self, result):
        self.running = False
        self.used = time.time()
        self.cc(result)

    def message_start(self, m):
        role = m.get('role')
        if role == 'user':
            self.run_users += 1
            if self.run_users > 1 or not self.sent_at:
                # Not the run's own message: one given while it went on, which the model has now.
                text = ' '.join(b.get('text', '') for b in m.get('content') or [] if isinstance(b, dict)) \
                    if isinstance(m.get('content'), list) else str(m.get('content') or '')
                found = next((s for s in self.steered if s[1].strip() == text.strip()), None)
                if found is None and self.steered:
                    found = self.steered[0]
                if found:
                    self.steered.remove(found)
                    self.cc({'type': 'command_lifecycle', 'command_uuid': found[0], 'state': 'started'})
            self.sent_at = None
        elif role == 'assistant':
            self.msg_n += 1
            self.msg_id = 'pi-%s-%d' % (self.key[-6:], self.msg_n)
            self.cc({'type': 'stream_event', 'event': {'type': 'message_start', 'message': {'id': self.msg_id}}})

    def message_update(self, e):
        kind, index = e.get('type') or '', e.get('contentIndex', 0)
        event = None
        if kind == 'text_start':
            event = {'type': 'content_block_start', 'index': index, 'content_block': {'type': 'text', 'text': ''}}
        elif kind == 'thinking_start':
            event = {'type': 'content_block_start', 'index': index, 'content_block': {'type': 'thinking', 'thinking': ''}}
        elif kind == 'toolcall_start':
            event = {'type': 'content_block_start', 'index': index,
                     'content_block': {'type': 'tool_use', 'id': e.get('id'), 'name': e.get('toolName')}}
        elif kind == 'text_delta':
            event = {'type': 'content_block_delta', 'index': index, 'delta': {'type': 'text_delta', 'text': e.get('delta', '')}}
        elif kind == 'thinking_delta':
            event = {'type': 'content_block_delta', 'index': index,
                     'delta': {'type': 'thinking_delta', 'thinking': e.get('delta', '')}}
        elif kind == 'toolcall_delta':
            event = {'type': 'content_block_delta', 'index': index,
                     'delta': {'type': 'input_json_delta', 'partial_json': e.get('delta', '')}}
        elif kind in ('text_end', 'thinking_end', 'toolcall_end'):
            event = {'type': 'content_block_stop', 'index': index}
        if event:
            self.cc({'type': 'stream_event', 'event': event})

    def message_end(self, m):
        if m.get('role') == 'system':
            added = [t.get('name') for t in m.get('toolsAdded') or [] if isinstance(t, dict) and t.get('name')]
            removed = {t.get('name') for t in m.get('toolsRemoved') or [] if isinstance(t, dict)}
            if added or removed:
                self.tools = [t for t in dict.fromkeys(self.tools + added) if t not in removed]
            return
        if m.get('role') != 'assistant':
            return
        if m.get('provider') and m.get('model'):
            self.model_seen = '%s/%s' % (m['provider'], m['model'])
        content = []
        for b in m.get('content') or []:
            if not isinstance(b, dict):
                continue
            if b.get('type') == 'text':
                content.append({'type': 'text', 'text': b.get('text', '')})
            elif b.get('type') == 'thinking':
                content.append({'type': 'thinking', 'thinking': b.get('thinking', '')})
            elif b.get('type') == 'toolCall':
                content.append({'type': 'tool_use', 'id': b.get('id'), 'name': b.get('name'), 'input': b.get('arguments') or {}})
        self.cc({'type': 'assistant', 'message': {'id': self.msg_id or 'pi-%s-x' % self.key[-6:], 'content': content}})
        self.last_stop, self.last_error = m.get('stopReason'), m.get('errorMessage') or ''
        if self.last_stop == 'error':
            log('chat %s: model error: %s' % (self.key[:8], self.last_error[:500]))

    def settled(self, aborted):
        """The run is over and pi goes on with nothing by itself: the turn ends as the app knows turns to."""
        if aborted or self.last_stop == 'aborted':
            result = {'type': 'result', 'subtype': 'error_during_execution', 'is_error': False}
        elif self.last_stop == 'error':
            result = {'type': 'result', 'subtype': 'error', 'is_error': True, 'result': self.last_error or '请求失败'}
        else:
            result = {'type': 'result', 'subtype': 'success', 'is_error': False}
        log('chat %s: turn ended (%s)' % (self.key[:8], result['subtype']))
        # pi gives the model what it holds before it settles, unless the run was stopped: what is left
        # is taken back from it and waits in the app again (sent there when the reply has ended).
        left, self.steered = self.steered, []
        if left:
            self.command({'type': 'clear_queue'}, wait=False)
        for message_uuid, text in left:
            self.cc({'type': 'command_lifecycle', 'command_uuid': message_uuid, 'state': 'discarded'})
        self.end_turn(result)

    # ---- what is said to pi ----

    def send(self, content, cwd=None, model=None, session_id=None, effort=None, message_uuid=None):
        with self.lock:
            before = self.proc if self.alive() else None
            # Written while a run goes on: pi gives it to the model after the tools running (steer).
            inserting = self.running and before is not None
            if session_id and not self.session_id:
                self.session_id = session_id
            if cwd and not self.alive():
                self.cwd = cwd
            old_model, old_effort = self.model, self.effort
            self.model, self.effort = model, effort
            if self.alive():  # changed in the running pi, never started again for it
                self.switch(old_model, old_effort)
            if not self.session_id and not self.alive():  # the next spare is started like this one
                spare.wish(self.cwd, model, effort)
            if not self.alive():
                self.start()
            text, images = '', []
            for b in content if isinstance(content, list) else [{'type': 'text', 'text': str(content)}]:
                if not isinstance(b, dict):
                    continue
                if b.get('type') == 'text':
                    text += ('\n\n' if text else '') + b.get('text', '')
                elif b.get('type') == 'image':
                    src = b.get('source') or {}
                    images.append({'type': 'image', 'data': src.get('data', ''), 'mimeType': src.get('media_type') or 'image/png'})
            self.turn += 1
            self.used = self.last_out = time.time()
            rid = 'prompt-%s' % uuid.uuid4().hex[:12]
            command = self.slash(text) if not inserting and not images else None
            if command is None:
                command = {'type': 'prompt', 'id': rid, 'message': text}
                if images:
                    command['images'] = images
                # While a run goes on (also one the app does not know of yet), the message waits for it.
                command['streamingBehavior'] = 'steer' if inserting else 'followUp'
                self.prompts[rid] = message_uuid if inserting else None
                if inserting:
                    self.steered.append([message_uuid or rid, text])
            if not inserting:
                self.running = True
                self.sent_at = time.time()
            log('chat %s: message %s (%d image)' % (self.key[:8], 'inserted' if inserting else 'sent', len(images)))
            self.write(self.proc, command)
            hub.emit(self.key, 'state', {'running': True})

    def slash(self, text):
        """A / command of pi's own that its RPC mode does as a command, not as a prompt (which would
        go to the model as it is): /compact [instructions] and /name <name>. None for any other."""
        m = re.match(r'^/(compact|name)(?:\s+([\s\S]*))?$', text.strip())
        if not m:
            return None
        rid = 'cmd-%s' % uuid.uuid4().hex[:12]
        arg = (m.group(2) or '').strip()
        if m.group(1) == 'name' and not arg:
            return None
        command = {'type': 'compact', 'id': rid} if m.group(1) == 'compact' else {'type': 'set_session_name', 'id': rid, 'name': arg}
        if m.group(1) == 'compact' and arg:
            command['customInstructions'] = arg

        def after():
            response = self.wait_for(rid, 600)
            if response is None:
                return
            if response.get('success'):  # a new name comes as pi's session_info_changed
                self.end_turn({'type': 'result', 'subtype': 'success', 'is_error': False})
            else:
                self.end_turn({'type': 'result', 'subtype': 'error', 'is_error': True, 'result': response.get('error') or '没有完成'})
        self.waiting[rid] = {'event': threading.Event()}
        threading.Thread(target=after, daemon=True).start()
        return command

    def wait_for(self, rid, timeout):
        slot = self.waiting.get(rid)
        if slot is None:
            return None
        try:
            slot['event'].wait(timeout)
            return slot.get('response')
        finally:
            self.waiting.pop(rid, None)

    def switch(self, model, effort):
        """Changes the running pi, started with model and thinking level, to this chat's."""
        if self.model and model != self.model:
            provider, mid = split_model(self.model)
            if provider:
                self.command({'type': 'set_model', 'provider': provider, 'modelId': mid}, wait=False)
            else:
                log('chat %s: model %s has no provider: not changed in the running pi' % (self.key[:8], self.model))
        if self.effort and effort != self.effort:
            self.command({'type': 'set_thinking_level', 'level': self.effort}, wait=False)

    def write(self, proc, message):
        """One record to pi: the reading thread writes too (extension dialogs)."""
        with self.write_lock:
            proc.stdin.write(json.dumps(message, ensure_ascii=False).encode('utf-8') + b'\n')
            proc.stdin.flush()

    def command(self, command, wait=True, timeout=15):
        """Sends a command; with wait, its response (None when none came or no pi runs)."""
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return None
        rid = command.get('id') or '%s-%s' % (command['type'], uuid.uuid4().hex[:12])
        command = dict(command, id=rid)
        if wait:
            self.waiting[rid] = {'event': threading.Event()}
        try:
            self.write(proc, command)
        except OSError:
            self.waiting.pop(rid, None)
            return None
        if command['type'] in ('set_model', 'set_thinking_level'):
            log('chat %s: %s %s' % (self.key[:8], command['type'], command.get('modelId') or command.get('level')))
        return self.wait_for(rid, timeout) if wait else None

    def context_usage(self):
        """What fills the context window and what the conversation used, from pi's session stats, as
        the app reads it. None when no pi runs this conversation, or it did not answer."""
        response = self.command({'type': 'get_session_stats'})
        if not response or not response.get('success'):
            return None
        d = response.get('data') or {}
        cu = d.get('contextUsage') or {}
        tokens = cu.get('tokens') or 0
        return {'categories': [{'name': 'Messages', 'tokens': tokens, 'deferred': False}],
                'totalTokens': tokens, 'maxTokens': cu.get('contextWindow') or 0, 'percentage': cu.get('percent') or 0,
                'model': getattr(self, 'model_seen', '') or self.model or '',
                'stats': {'tokens': d.get('tokens') or {}, 'cost': d.get('cost') or 0,
                          'userMessages': d.get('userMessages') or 0, 'assistantMessages': d.get('assistantMessages') or 0,
                          'toolCalls': d.get('toolCalls') or 0}}

    def cancel(self, message_uuid):
        """Takes back a message written while a run went on, while pi still holds it (the model has
        not got it): True when taken back, or when the pi that held it is gone; False when the model
        has it already (or it never came), None when there was no answer. pi takes back only all it
        holds at once (clear_queue): the others are given to it again, in their order."""
        if not self.alive():
            return True
        found = next((s for s in self.steered if s[0] == message_uuid), None)
        if found is None:
            return False
        response = self.command({'type': 'clear_queue'}, timeout=10)
        if response is None:
            return None if self.alive() else True
        data = response.get('data') or {}
        held, taken = list(data.get('steering') or []) + list(data.get('followUp') or []), False
        again = []
        for text in held:
            if not taken and text.strip() == found[1].strip():
                taken = True
                continue
            again.append(text)
        for text in again:
            self.command({'type': 'prompt', 'message': text, 'streamingBehavior': 'steer'}, wait=False)
        if taken:
            self.steered.remove(found)
        log('chat %s: queued message %s' % (self.key[:8], 'taken back' if taken else 'not taken back (the model has it)'))
        return taken

    def stop(self):
        """Stops the run; False when none runs (nothing to stop: no result will say it was)."""
        with self.lock:
            if not self.alive() or not self.running:
                return False
            log('chat %s: stop requested' % self.key[:8])
            proc, turn, began = self.proc, self.turn, self.began
        self.command({'type': 'abort'}, wait=False)

        def force():
            time.sleep(8)
            with self.lock:
                if self.running and self.proc is proc and self.turn == turn and self.began == began:
                    log('chat %s: not stopped 8 s after abort: pi ended' % self.key[:8])
                    end(self.proc)
        threading.Thread(target=force, daemon=True).start()
        return True


def end(proc):
    """Ends a pi process: end of input (pi then shuts down in order), then SIGTERM, then SIGKILL;
    returns once it is gone, so a new one is never started (or written to) while the old one still runs."""
    if proc is None:
        return
    try:
        proc.stdin.close()
    except OSError:
        pass
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            proc.wait(4)
            return
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, sig)
            except OSError:
                proc.send_signal(sig)
    proc.wait()


def let_go(chat):
    """Ends the chat's pi and forgets it, so the next message starts it again. The caller holds chat.lock."""
    proc, chat.proc = chat.proc, None
    chat.running = False
    end(proc)


chats = {}
chats_lock = threading.Lock()
seen_messages = []  # ids of messages already sent: the app may repeat a request whose answer it lost


def first_sight(mid):
    """Whether a message (its id from the app, None for one without) is new; a repeat is not sent again."""
    with chats_lock:
        if mid is None:
            return True
        if mid in seen_messages:
            return False
        seen_messages.append(mid)
        del seen_messages[:-200]
        return True


def forget_message(mid):
    """A message that could not be sent is new again: the app's retry sends it."""
    with chats_lock:
        if mid in seen_messages:
            seen_messages.remove(mid)


def chat_for(key, session_id=None, create=True):
    """The conversation for the app's key. A session already running under another key (the app
    was restarted meanwhile) is taken over, so one session never has two processes. None when
    there is none and create is False."""
    with chats_lock:
        if key in chats:
            return chats[key]
        if session_id:
            for old_key, chat in list(chats.items()):
                if chat.session_id == session_id:
                    del chats[old_key]
                    chat.key = key
                    chats[key] = chat
                    return chat
        if not create:
            return None
        chats[key] = Chat(key)
        return chats[key]


def chat_of_session(session_id):
    with chats_lock:
        return next((c for c in chats.values() if c.session_id == session_id), None)


def reaper():
    """Logs a running turn with no output for a minute and the bridge itself having been paused
    (frozen by the system: it did not run for a while); starts the spare while nothing runs."""
    tick = time.monotonic()
    while True:
        time.sleep(5)
        now = time.monotonic()
        if now - tick > 20:
            log('bridge was paused for %ds (frozen by the system?)' % (now - tick - 5))
        tick = now
        with chats_lock:
            live = list(chats.values())
        for chat in live:
            if chat.running and not chat.quiet_logged and time.time() - chat.last_out > 60:
                chat.quiet_logged = True
                log('chat %s: no output from pi for 60s (alive: %s)' % (chat.key[:8], chat.alive()))
        # Not while a turn runs or a pi was just started: it would slow them down.
        if not any(chat.running for chat in live) and time.time() - last_spawn > 10:
            spare.fill()


# ---- Saved sessions (pi's own session files) ----

def session_files():
    root = sessions_dir()
    out = []
    try:
        for d in os.listdir(root):
            folder = os.path.join(root, d)
            if not os.path.isdir(folder):
                continue
            for name in os.listdir(folder):
                if name.endswith('.jsonl'):
                    out.append(os.path.join(folder, name))
    except OSError:
        pass
    return out


def file_session_id(path):
    """<timestamp>_<id>.jsonl -> id."""
    name = os.path.basename(path)[:-6]
    return name.split('_', 1)[1] if '_' in name else name


def find_session(session_id):
    if not session_id or not re.match(r'^[\w.-]+$', session_id):
        return None
    for path in session_files():
        if file_session_id(path) == session_id:
            return path
    return None


def read_entries(path):
    """The session file's records: (byte offset, record), the header included; lines not JSON left out."""
    out = []
    with open(path, 'rb') as f:
        offset = 0
        for raw in f:
            at, offset = offset, offset + len(raw)
            try:
                e = json.loads(raw)
            except ValueError:
                continue
            if isinstance(e, dict):
                out.append((at, e))
    return out


def active_path(entries):
    """The entries from the root to the current leaf (the last entry pi wrote), in order."""
    by_id = {e['id']: (at, e) for at, e in entries if e.get('id') and e.get('type') != 'session'}
    leaf = next((e for at, e in reversed(entries) if e.get('id') and e.get('type') != 'session'), None)
    path, seen = [], set()
    while leaf is not None and leaf['id'] not in seen:
        seen.add(leaf['id'])
        path.append(by_id[leaf['id']])
        parent = leaf.get('parentId')
        leaf = by_id[parent][1] if parent in by_id else None
    path.reverse()
    return path


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return '\n'.join(b.get('text', '') for b in content if isinstance(b, dict) and b.get('type') == 'text')
    return ''


def one_line(text, limit=SHORT_TITLE):
    t = ' '.join(text.split())
    return t if len(t) <= limit else t[:limit - 1] + '…'


summary_lock = threading.Lock()
summaries = None  # path -> [stamp, summary]


def session_summary(path):
    """id, title (its name, else its first message), folder, when it last changed and its size; None
    for a session with nothing said in it (a spare's)."""
    global summaries
    st = os.stat(path)
    stamp = '%d-%d' % (st.st_size, st.st_mtime_ns)
    with summary_lock:
        if summaries is None:
            summaries = read_json(SUMMARY_FILE, {}).get('sessions', {})
        hit = summaries.get(path)
        if hit and hit[0] == stamp:
            return hit[1]
    cwd, name, first = '', None, None
    with open(path, 'rb') as f:
        for i, raw in enumerate(f):
            if i == 0 or b'"session_info"' in raw or (first is None and b'"user"' in raw):
                try:
                    e = json.loads(raw)
                except ValueError:
                    continue
                if e.get('type') == 'session':
                    cwd = e.get('cwd') or ''
                elif e.get('type') == 'session_info':
                    name = e.get('name') or name
                elif e.get('type') == 'message' and (e.get('message') or {}).get('role') == 'user' and first is None:
                    first = one_line(text_of(e['message'].get('content'))) or '[图片]'
    summary = None if first is None else {'id': file_session_id(path), 'title': name or first, 'cwd': cwd,
                                          'mtime': st.st_mtime, 'size': st.st_size}
    with summary_lock:
        summaries[path] = [stamp, summary]
    return summary


def save_summaries(paths):
    with summary_lock:
        kept = {p: v for p, v in (summaries or {}).items() if p in paths}
    try:
        write_json(SUMMARY_FILE, {'version': VERSION, 'sessions': kept})
    except OSError:
        pass


def list_sessions():
    paths = session_files()
    out = []
    for path in paths:
        try:
            s = session_summary(path)
        except OSError:
            continue
        if s:
            chat = chat_of_session(s['id'])
            out.append(dict(s, running=bool(chat and chat.running)))
    out.sort(key=lambda s: (-s['mtime'], s['id']))
    threading.Thread(target=save_summaries, args=(set(paths),), daemon=True).start()
    return out


TEXT_CACHE_CHARS = 16_000_000  # kept for searching again; more is read again when searched
text_cache = {}  # path -> (stamp, text)


def session_text(path):
    """What was said in a session (users' messages and replies' text), lower case, for searching."""
    st = os.stat(path)
    stamp = '%d-%d' % (st.st_size, st.st_mtime_ns)
    hit = text_cache.get(path)
    if hit and hit[0] == stamp:
        return hit[1]
    parts = []
    for at, e in read_entries(path):
        m = e.get('message') if e.get('type') == 'message' else None
        if m and m.get('role') in ('user', 'assistant'):
            parts.append(text_of(m.get('content')))
    text = '\n'.join(parts)
    if sum(len(v[1]) for v in text_cache.values()) + len(text) > TEXT_CACHE_CHARS:
        text_cache.clear()
    text_cache[path] = (stamp, text)
    return text


def search_sessions(query):
    q = query.strip().lower()
    if not q:
        return []
    out = []
    for path in session_files():
        try:
            text = session_text(path)
        except OSError:
            continue
        at = text.lower().find(q)
        if at >= 0:
            snippet = ' '.join(text[max(0, at - 30):at + len(q) + 60].split())
            out.append({'id': file_session_id(path), 'snippet': snippet})
    return out


def image_refs(session_id, at, content):
    """References to a message's pictures (ccimg:<session>:<offset>:<n>), read when shown (session_image)."""
    n, refs = 0, []
    for b in content if isinstance(content, list) else []:
        if isinstance(b, dict) and b.get('type') == 'image':
            refs.append('ccimg:%s:%d:%d' % (session_id, at, n))
            n += 1
    return refs


def session_image(session_id, at, n):
    path = find_session(session_id)
    if not path:
        return None
    try:
        with open(path, 'rb') as f:
            f.seek(at)
            e = json.loads(f.readline())
    except (OSError, ValueError):
        return None
    content = ((e.get('message') or {}) if isinstance(e, dict) else {}).get('content')
    images = [b for b in content if isinstance(b, dict) and b.get('type') == 'image'] if isinstance(content, list) else []
    if n >= len(images):
        return None
    return images[n].get('mimeType') or 'image/png', images[n].get('data', '')


def load_history(session_id):
    """The conversation as the app shows it, from the session file's current branch: users'
    messages, replies (text, thinking, tool steps with their results), compactions."""
    path = find_session(session_id)
    if not path:
        return None
    entries = read_entries(path)
    header = next((e for at, e in entries if e.get('type') == 'session'), {})
    messages, tools, name, model, context = [], {}, None, None, 0
    reply = None
    for at, e in active_path(entries):
        kind = e.get('type')
        if kind == 'session_info':
            name = e.get('name') or name
        elif kind == 'compaction':
            messages.append({'role': 'divider', 'text': '上下文已压缩'})
            reply = None
        elif kind == 'branch_summary':
            messages.append({'role': 'divider', 'text': '换到了另一个分支'})
            reply = None
        elif kind == 'message':
            m = e.get('message') or {}
            role = m.get('role')
            if role == 'user':
                text = text_of(m.get('content'))
                refs = image_refs(session_id, at, m.get('content'))
                if text or refs:
                    messages.append({'role': 'user', 'text': text, 'images': refs, 'uuid': e.get('id')})
                reply = None
            elif role == 'assistant':
                if reply is None:
                    reply = {'role': 'assistant', 'blocks': [], 'uuid': e.get('id')}
                    messages.append(reply)
                for b in m.get('content') or []:
                    if not isinstance(b, dict):
                        continue
                    if b.get('type') == 'text' and b.get('text'):
                        reply['blocks'].append({'type': 'text', 'text': b['text']})
                    elif b.get('type') == 'thinking' and b.get('thinking'):
                        reply['blocks'].append({'type': 'thinking', 'text': b['thinking']})
                    elif b.get('type') == 'toolCall':
                        tool = {'type': 'tool', 'id': b.get('id'), 'name': b.get('name'), 'input': b.get('arguments') or {}}
                        tools[b.get('id')] = tool
                        reply['blocks'].append(tool)
                if m.get('stopReason') == 'error' and m.get('errorMessage'):
                    reply['blocks'].append({'type': 'text', 'text': '请求失败：' + m['errorMessage']})
                if m.get('provider') and m.get('model'):
                    model = '%s/%s' % (m['provider'], m['model'])
                u = m.get('usage') or {}
                context = (u.get('input') or 0) + (u.get('cacheRead') or 0) + (u.get('cacheWrite') or 0) or context
            elif role == 'toolResult':
                tool = tools.get(m.get('toolCallId'))
                if tool is not None:
                    tool['result'] = text_of(m.get('content'))
                    tool['images'] = image_refs(session_id, at, m.get('content'))
                    tool['isError'] = bool(m.get('isError'))
    return {'id': session_id, 'messages': messages, 'cwd': header.get('cwd') or '', 'title': name,
            'model': model, 'context': context}


def write_entries(path, kept):
    """Writes the session file again with these records only (raw lines as they were, or changed ones)."""
    part = path + '.tmp'
    with open(part, 'wb') as f:
        for e in kept:
            f.write(e if isinstance(e, bytes) else json.dumps(e, ensure_ascii=False).encode('utf-8') + b'\n')
    os.replace(part, path)


def messages_left(entries):
    return sum(1 for at, e in entries if e.get('type') == 'message'
               and (e.get('message') or {}).get('role') in ('user', 'assistant'))


def rewind_session(session_id, message_uuid):
    """Back to before one of the user's messages (/tree): it and all written after it are cut from the
    file. The number of messages left, None when it is not there; the file goes when none is left."""
    path = find_session(session_id)
    if not path:
        return None
    entries = read_entries(path)
    at = next((a for a, e in entries if e.get('id') == message_uuid and e.get('type') == 'message'
               and (e.get('message') or {}).get('role') == 'user'), None)
    if at is None:
        return None
    left = [(a, e) for a, e in entries if a < at]
    n = messages_left(left)
    if n == 0:
        os.remove(path)
        return 0
    with open(path, 'rb') as f:
        head = f.read(at)
    part = path + '.tmp'
    with open(part, 'wb') as f:
        f.write(head)
    os.replace(part, path)
    return n


def delete_message(session_id, message_uuid, role):
    """Takes one message out: a user's, or a reply (with its tool results, up to the next user's
    message); what came after is linked to what came before. The number of messages left, None when
    it is not there."""
    path = find_session(session_id)
    if not path:
        return None
    entries = read_entries(path)
    on_path = active_path(entries)
    i = next((k for k, (a, e) in enumerate(on_path) if e.get('id') == message_uuid), None)
    if i is None:
        return None
    gone = {message_uuid}
    if role == 'assistant':
        for a, e in on_path[i + 1:]:
            if e.get('type') == 'message' and (e.get('message') or {}).get('role') == 'user':
                break
            gone.add(e.get('id'))
    parents = {e.get('id'): e.get('parentId') for a, e in entries if e.get('id')}

    def kept_parent(pid):
        while pid in gone:
            pid = parents.get(pid)
        return pid
    out, left = [], []
    with open(path, 'rb') as f:
        raw_lines = f.readlines()
    offsets, offset = {}, 0
    for raw in raw_lines:
        offsets[offset] = raw
        offset += len(raw)
    for a, e in entries:
        if e.get('id') in gone:
            continue
        if e.get('parentId') in gone:
            e = dict(e, parentId=kept_parent(e['parentId']))
            out.append(e)
        else:
            out.append(offsets[a])
        left.append((a, e))
    write_entries(path, out)
    return messages_left(left)


def delete_session(session_id):
    """Deletes a conversation: its pi goes first (it would write the file again), with no exit event
    (the app let it go), then the file. True when there was one."""
    target = find_session(session_id) if session_id else None
    with chats_lock:
        gone = [chats.pop(k) for k in [k for k, c in chats.items() if session_id and c.session_id == session_id]]
    for chat in gone:
        with chat.lock:
            let_go(chat)
    if target:
        os.remove(target)
    return bool(target)


def keep_title(key, session_id, title):
    hub.emit(key, 'title', {'session_id': session_id, 'title': title})


def rename(session_id, title):
    """Names a conversation: by its pi when one runs it, else by a session_info record written after
    the last (as pi itself writes one)."""
    chat = chat_of_session(session_id)
    if chat and chat.alive():
        response = chat.command({'type': 'set_session_name', 'name': title})
        if response and response.get('success'):
            keep_title(chat.key, session_id, title)
            return True
    path = find_session(session_id)
    if not path:
        return False
    entries = read_entries(path)
    leaf = next((e for a, e in reversed(entries) if e.get('id') and e.get('type') != 'session'), None)
    record = {'type': 'session_info', 'id': uuid.uuid4().hex[:8], 'parentId': leaf['id'] if leaf else None,
              'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime()), 'name': title}
    with open(path, 'ab') as f:
        f.write(json.dumps(record, ensure_ascii=False).encode('utf-8') + b'\n')
    return True


# ---- pi itself: its version and models ----

pi_version = ''
models_lock = threading.Lock()


def file_stamp(*paths):
    out = []
    for p in paths:
        try:
            st = os.stat(p)
            out.append('%d-%d' % (st.st_size, st.st_mtime))
        except OSError:
            out.append('-')
    return '/'.join(out)


def find_pi_info():
    """pi's version, asked once per pi installed (kept in INFO_FILE)."""
    global pi_version
    path = os.path.realpath(pi_path())
    stamp = file_stamp(path, os.path.join(os.path.dirname(path), '..', 'package.json'))
    saved = read_json(INFO_FILE, {})
    if saved.get('stamp') == stamp:
        pi_version = saved.get('version', '')
        return
    try:
        out = subprocess.run([pi_path(), '--version'], capture_output=True, text=True, timeout=120, env=pi_env()).stdout
    except (OSError, subprocess.SubprocessError):
        return
    pi_version = out.strip().split()[-1] if out.strip() else ''
    if pi_version:
        try:
            write_json(INFO_FILE, {'stamp': stamp, 'version': pi_version})
        except OSError:
            pass


def models_stamp():
    return file_stamp(os.path.realpath(pi_path()), *[os.path.join(PI_DIR, n) for n in ('auth.json', 'models.json', 'settings.json')])


def find_models():
    """The models pi can use (those of providers with a way to sign in), from a short pi of its own,
    asked again when pi, its sign-ins, models.json or settings change (kept in MODELS_FILE)."""
    if not models_lock.acquire(blocking=False):
        return
    try:
        stamp = models_stamp()
        if read_json(MODELS_FILE, {}).get('stamp') == stamp or not os.path.exists(pi_path()):
            return
        env = dict(pi_env(), PI_OFFLINE='1')
        proc = subprocess.Popen([pi_path(), '--mode', 'rpc', '--no-session', '--no-extensions', '--no-skills'],
                                cwd=HOME, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        proc.stdin.write(b'{"id":"m","type":"get_available_models"}\n')
        proc.stdin.flush()
        found = None
        deadline = time.time() + 60
        timer = threading.Timer(60, proc.kill)
        timer.start()
        try:
            for raw in proc.stdout:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(msg, dict) and msg.get('id') == 'm':
                    found = msg
                    break
                if time.time() > deadline:
                    break
        finally:
            timer.cancel()
            end(proc)
        if not found or not found.get('success'):
            log('models: pi did not list them: %s' % ((found or {}).get('error') or 'no answer'))
            return
        models = [{'value': '%s/%s' % (m.get('provider'), m.get('id')), 'displayName': m.get('name') or m.get('id'),
                   'description': '%s · %s · %dK' % (m.get('name') or m.get('id'), m.get('provider'), (m.get('contextWindow') or 0) // 1000),
                   'provider': m.get('provider'), 'reasoning': bool(m.get('reasoning')),
                   'images': 'image' in (m.get('input') or [])}
                  for m in (found.get('data') or {}).get('models') or [] if isinstance(m, dict)]
        write_json(MODELS_FILE, {'stamp': stamp, 'models': models})
        log('models: %d with a way to sign in' % len(models))
        hub.emit('', 'config')  # the app reads /config again
    except OSError as e:
        log('models: %s' % e)
    finally:
        models_lock.release()


def read_config():
    threading.Thread(target=find_models, daemon=True).start()  # a no-op unless something changed
    settings = pi_settings()
    models = read_json(MODELS_FILE, {}).get('models', [])
    provider, model = settings.get('defaultProvider'), settings.get('defaultModel')
    default = '%s/%s' % (provider, model) if provider and model else (model or '')
    level = settings.get('defaultThinkingLevel')
    return {'models': [m['value'] for m in models], 'defaultModel': default, 'auth': bool(models),
            'piVersion': pi_version, 'claudeVersion': pi_version, 'efforts': THINKING_LEVELS,
            'effort': level if level in THINKING_LEVELS else '', 'permissionModes': [], 'claudeModels': models,
            'providers': sorted(read_json(os.path.join(PI_DIR, 'auth.json'), {}).keys())}


AUTH_FILE = os.path.join(PI_DIR, 'auth.json')


def set_api_key(provider, key):
    """Keeps (or, with no key, takes out) a provider's API key in pi's auth.json, readable by this app alone."""
    auth = read_json(AUTH_FILE, {})
    if key:
        auth[provider] = {'type': 'api_key', 'key': key}
    else:
        auth.pop(provider, None)
    write_json(AUTH_FILE, auth, mode=0o600)
    launch.gen += 1  # new conversations start with it


def list_dirs(path):
    path = os.path.expanduser(path or HOME)
    names = []
    try:
        for name in sorted(os.listdir(path), key=str.lower):
            if not name.startswith('.') and os.path.isdir(os.path.join(path, name)):
                names.append(name)
    except OSError as e:
        return {'path': path, 'parent': os.path.dirname(path), 'dirs': [], 'error': str(e)}
    return {'path': path, 'parent': os.path.dirname(path) if path != '/' else None, 'dirs': names, 'home': HOME}


# ---- HTTP ----

class Handler(BaseHTTPRequestHandler):
    server_version = 'pibridge/%d' % VERSION
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def reply(self, status, data):
        self.status = status
        body = json.dumps(data, ensure_ascii=False).encode('utf-8', 'replace')  # lone surrogates
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get('Content-Length') or 0)
        return json.loads(self.rfile.read(n) or b'{}') if n else {}

    def logged(self, method, run):
        """Runs a request and logs it (not health checks), with its status and time."""
        path = self.path.split('?')[0]
        start = time.time()
        self.status = None
        try:
            run()
        finally:
            if path not in ('/health', '/events'):
                log('%s %s -> %s, %dms' % (method, path, self.status or 'no answer', (time.time() - start) * 1000))

    def do_GET(self):
        self.logged('GET', self.do_get)

    def do_POST(self):
        self.logged('POST', self.do_post)

    def do_get(self):
        try:
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            route = self.GET.get(url.path)
            if route is None:
                return self.reply(404, {'error': 'not found'})
            route(self, q)
        except ValueError as e:
            self.reply(400, {'error': str(e)})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self.failed(e)

    def do_post(self):
        try:
            path = urlparse(self.path).path
            if path == '/upload':
                return self.upload()
            route = self.POST.get(path)
            if route is None:
                return self.reply(404, {'error': 'not found'})
            route(self, self.body())
        except (ValueError, KeyError, TypeError) as e:
            self.reply(400, {'error': 'bad request: %s' % e})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self.failed(e)

    def failed(self, e):
        """An error the bridge did not expect: logged, and answered so the app can show it."""
        import traceback
        log('%s failed:\n%s' % (self.path.split('?')[0], traceback.format_exc()))
        try:
            self.reply(500, {'error': '%s: %s' % (type(e).__name__, e)})
        except OSError:
            pass

    # ---- GET ----

    def get_health(self, q):
        self.reply(200, {'ok': True, 'version': VERSION, 'started': STARTED, 'home': HOME, 'defaultCwd': DEFAULT_CWD,
                         'pi': shutil.which('pi')})

    def get_events(self, q):
        self.events(int(q.get('since') or 0), q.get('bridge'))

    def get_image(self, q):
        found = session_image(q.get('id'), int(q.get('at') or 0), int(q.get('n') or 0))
        if not found:
            return self.reply(404, {'error': 'not found'})
        self.reply(200, {'type': found[0], 'data': found[1]})

    def get_sessions(self, q):
        self.reply(200, {'sessions': list_sessions()})

    def get_search(self, q):
        self.reply(200, {'results': search_sessions(q.get('q', ''))})

    def get_history(self, q):
        data = load_history(q.get('id', ''))
        self.reply(200 if data else 404, data or {'error': 'not found'})

    def get_config(self, q):
        self.reply(200, read_config())

    def get_launch(self, q):
        self.reply(200, launch.view())

    def get_memory(self, q):
        path = memory_path(q.get('scope'), q.get('cwd'))
        try:
            with open(path, encoding='utf-8') as f:
                text = f.read()
        except OSError:
            text = ''
        self.reply(200, {'path': path, 'text': text})

    def get_dirs(self, q):
        self.reply(200, list_dirs(q.get('path')))

    # ---- POST ----

    def post_send(self, data):
        mid = data.get('mid')
        if not first_sight(mid):
            return self.reply(200, {'ok': True, 'repeated': True})
        chat = chat_for(data['chat'], data.get('sessionId'))
        try:
            chat.send(data['content'], cwd=data.get('cwd'), model=data.get('model') or None,
                      session_id=data.get('sessionId'), effort=data.get('effort') or None,
                      message_uuid=data.get('uuid') or None)
        except Exception as e:
            chat.running = False
            forget_message(mid)
            if isinstance(e, (OSError, ValueError)):
                return self.reply(500, {'error': str(e)})
            raise
        self.reply(200, {'ok': True, 'sessionId': chat.session_id})

    def post_open(self, data):
        # A new conversation was opened: the spare is made ready for it (a saved one's pi starts
        # only when a message is sent).
        if not data.get('sessionId'):
            spare.wish(data.get('cwd'), data.get('model') or None, data.get('effort') or None)
        threading.Thread(target=spare.fill, daemon=True).start()
        self.reply(200, {'ok': True})

    def post_launch(self, data):
        launch.set(data)
        self.reply(200, launch.view())

    def post_memory(self, data):
        target = memory_path(data.get('scope'), data.get('cwd'))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, 'w', encoding='utf-8') as f:
            f.write(data.get('text') or '')
        launch.gen += 1  # pi reads AGENTS.md when it starts: new conversations have it
        self.reply(200, {'ok': True, 'path': target})

    def post_auth(self, data):
        provider, key = str(data.get('provider') or '').strip(), str(data.get('key') or '').strip()
        if not re.match(r'^[\w.-]{1,60}$', provider):
            return self.reply(400, {'error': 'bad provider'})
        set_api_key(provider, key)
        threading.Thread(target=find_models, daemon=True).start()
        self.reply(200, {'ok': True})

    def post_cancel(self, data):
        chat = chat_for(data['chat'], data.get('sessionId'), create=False)
        cancelled = True if chat is None else chat.cancel(str(data.get('uuid') or ''))
        if cancelled is None:
            return self.reply(504, {'error': 'pi did not answer'})
        self.reply(200, {'cancelled': cancelled})

    def post_context(self, data):
        chat = chat_for(data['chat'], data.get('sessionId'), create=False)
        usage = chat.context_usage() if chat is not None else None
        self.reply(200, {'context': usage})

    def post_stop(self, data):
        chat = chat_for(data['chat'], data.get('sessionId'), create=False)
        self.reply(200, {'ok': True, 'stopped': bool(chat and chat.stop())})

    def post_delete(self, data):
        self.reply(200, {'ok': delete_session(data.get('id', ''))})

    def cut_session(self, data, cut):
        """Back to before one of the user's messages (/rewind), or one message out (/delete-message): its
        pi goes first (it holds the conversation, and would write it again), then the file is changed;
        the next message opens it again. No exit event: the app asked for it. `cut` is given the session
        and the message's id and returns the messages left, or None."""
        session_id = str(data.get('sessionId') or '')
        chat = chat_for(data['chat'], session_id, create=False)
        if chat:
            with chat.lock:
                if chat.running:
                    return self.reply(409, {'error': '回复还在进行'})
                let_go(chat)
        left = cut(session_id, str(data.get('uuid') or ''))
        if left is None:
            return self.reply(404, {'error': '记录里没有这条消息'})
        if not left and chat:
            chat.session_id = None  # nothing left to open: the next message starts it anew
        self.reply(200, {'ok': True, 'left': left})

    def post_rewind(self, data):
        self.cut_session(data, rewind_session)

    def post_delete_message(self, data):
        role = str(data.get('role') or 'user')
        self.cut_session(data, lambda session_id, mid: delete_message(session_id, mid, role))

    def post_title(self, data):
        session_id = str(data.get('id') or '')
        title = ' '.join(str(data.get('title') or '').split())[:200]
        if not title:
            return self.reply(400, {'error': 'bad request'})
        ok = rename(session_id, title)
        self.reply(200 if ok else 404, {'ok': ok})

    def post_permission(self, data):
        self.reply(409, {'error': 'pi asks nothing before a tool runs'})

    def post_mkdir(self, data):
        try:
            path = os.path.expanduser(str(data['path']))
            os.makedirs(path, exist_ok=True)
        except OSError as e:
            return self.reply(400, {'error': str(e)})
        self.reply(200, {'ok': True, 'path': path})

    def upload(self):
        """A file the app attached, its bytes as the body (streamed to disk, never held whole), into
        the folder given; never over a file already there: name (1).ext, name (2).ext …"""
        q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
        folder = os.path.expanduser(q.get('cwd') or DEFAULT_CWD)
        name = os.path.basename(str(q.get('name') or 'file')).strip() or 'file'
        left = int(self.headers.get('Content-Length') or 0)
        os.makedirs(folder, exist_ok=True)
        stem, ext = os.path.splitext(name)
        target, n = os.path.join(folder, name), 1
        while os.path.exists(target):
            target, n = os.path.join(folder, '%s (%d)%s' % (stem, n, ext)), n + 1
        try:
            with open(target, 'wb') as f:
                while left > 0:
                    chunk = self.rfile.read(min(left, 1 << 16))
                    if not chunk:
                        raise OSError('the upload was cut short')
                    f.write(chunk)
                    left -= len(chunk)
        except OSError as e:
            try:
                os.remove(target)
            except OSError:
                pass
            return self.reply(400, {'error': str(e)})
        self.reply(200, {'ok': True, 'path': target})

    def events(self, since, bridge=None):
        """The events after `since`, then each as it happens. `bridge` is the start the app's count
        is from: another bridge's count starts over here. Events dropped meanwhile (only the last
        ones are kept) are told as one 'gap'."""
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.close_connection = True
        try:
            if since < 0:  # only what happens from now on
                since = hub.seq
            elif since > hub.seq or (bridge and bridge != str(STARTED)):  # another bridge's count: start over
                since = 0
            self.wfile.write((': hello %s\n\n' % STARTED).encode())

            def send(e):
                self.wfile.write(b'data: ' + json.dumps(e, ensure_ascii=False).encode('utf-8', 'replace') + b'\n\n')
            oldest = hub.oldest()
            if since < oldest - 1:
                since = oldest - 1
                send({'seq': since, 'chat': '', 'kind': 'gap', 'data': None})
            self.wfile.flush()
            log('events: app connected')
            while True:
                batch = hub.after(since, 5)  # a ping at least every 5 s: the app can tell a live bridge
                if not batch:
                    self.wfile.write(b': ping\n\n')
                for e in batch:
                    since = e['seq']
                    send(e)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            log('events: app gone')

    # Where each request goes: the path to the method answering it, given the query (GET) or the
    # body (POST). /upload is not here: its body is the file, read as it is streamed to disk.
    GET = {'/health': get_health, '/events': get_events, '/image': get_image, '/sessions': get_sessions,
           '/search': get_search, '/history': get_history, '/config': get_config, '/launch': get_launch,
           '/memory': get_memory, '/dirs': get_dirs}
    POST = {'/context': post_context, '/send': post_send, '/open': post_open, '/launch': post_launch,
            '/memory': post_memory, '/auth': post_auth, '/cancel': post_cancel, '/stop': post_stop,
            '/delete': post_delete, '/rewind': post_rewind, '/delete-message': post_delete_message,
            '/title': post_title, '/permission': post_permission, '/mkdir': post_mkdir}


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    request_queue_size = 64  # the app opens many connections at once when it starts

    def server_bind(self):
        """Over a socket file left by a bridge before (only one bridge runs: replace_running_bridge),
        readable by this app alone."""
        try:
            os.remove(self.server_address)
        except OSError:
            pass
        old = os.umask(0o177)
        try:
            super().server_bind()
        finally:
            os.umask(old)


def running_bridge():
    """The pid of the bridge running before this one, if it still runs (not a process that got its pid since)."""
    try:
        with open(PID_FILE) as f:
            pid = int(f.read().strip())
        with open('/proc/%d/cmdline' % pid, 'rb') as f:
            if b'bridge.py' not in f.read():
                return None
        return pid if pid != os.getpid() else None
    except (OSError, ValueError):
        return None


_lock = None


def replace_running_bridge():
    """Leaves exactly one bridge, the newest: it stops the one running and takes the lock that
    bridge held for its life, so two started at once end with one."""
    import fcntl
    global _lock
    os.makedirs(STATE_DIR, exist_ok=True)
    _lock = open(LOCK_FILE, 'a')
    deadline = time.time() + 12
    while True:
        old = running_bridge()
        if old:
            try:
                os.kill(old, signal.SIGTERM)
            except OSError:
                pass
        try:
            fcntl.flock(_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            if time.time() > deadline:
                log('exit pid %d: another bridge keeps the lock' % os.getpid())
                sys.exit(1)
            time.sleep(0.2)
    old = running_bridge()  # a bridge from before the lock
    if old:
        try:
            os.kill(old, signal.SIGTERM)
            for _ in range(30):
                time.sleep(0.1)
                os.kill(old, 0)
        except OSError:
            pass
    with open(PID_FILE, 'w') as f:
        f.write(str(os.getpid()))


def log(text):
    try:
        if os.path.getsize(LOG_FILE) > 200_000:
            os.replace(LOG_FILE, LOG_FILE + '.1')
    except OSError:
        pass
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(LOG_FILE, 'a') as f:
            f.write(time.strftime('%Y-%m-%d %H:%M:%S ') + text + '\n')
    except OSError:
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--socket', default=SOCKET_FILE)
    parser.add_argument('--reason', default='')
    args = parser.parse_args()
    replace_running_bridge()
    log('start pid %d version %d (%s)' % (os.getpid(), VERSION, args.reason or 'started by hand'))
    os.makedirs(DEFAULT_CWD, exist_ok=True)

    def shutdown(signum, _frame):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        log('stop pid %d: signal %d' % (os.getpid(), signum))
        procs = [chat.proc for chat in list(chats.values()) if chat.proc is not None]
        if spare.proc is not None:
            procs.append(spare.proc)
        for proc in procs:
            try:
                proc.stdin.close()
            except OSError:
                pass
        deadline = time.time() + 3
        for proc in procs:
            try:
                proc.wait(max(0.1, deadline - time.time()))
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except OSError:
                    pass
        os._exit(0)
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)

    def crashed(kind, value, tb):
        import traceback
        log('crash pid %d:\n%s' % (os.getpid(), ''.join(traceback.format_exception(kind, value, tb))))
    sys.excepthook = crashed
    threading.excepthook = lambda a: crashed(a.exc_type, a.exc_value, a.exc_traceback)

    try:
        server = Server(args.socket, Handler)
    except OSError as e:
        log('exit pid %d: cannot listen on %s: %s' % (os.getpid(), args.socket, e))
        sys.exit('cannot listen on %s: %s' % (args.socket, e))
    threading.Thread(target=reaper, daemon=True).start()
    threading.Thread(target=spare.fill, daemon=True).start()  # ready for the first new conversation
    threading.Thread(target=lambda: (find_pi_info(), find_models()), daemon=True).start()
    server.serve_forever()


if __name__ == '__main__':
    main()
