"""Characterization test for bridge.py's HTTP interface.

Starts the bridge's Handler on a Unix socket in a temporary HOME, with a fake `pi` on PATH that speaks
pi's RPC mode (records on stdin and stdout, session files as pi writes them), calls every route and
compares status and body with bridge_http.golden.json. A refactoring of the bridge must leave it as it
is; a change of the interface is recorded with --record.

    python3 -I code/src/test/python/test_bridge_http.py [--record] [path/to/bridge.py]
"""
import http.client
import importlib.machinery
import importlib.util
import json
import os
import re
import socket
import stat
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
GOLDEN = os.path.join(HERE, 'bridge_http.golden.json')

# pi's RPC mode, as far as the bridge uses it: a session file written as pi writes one, a run per
# prompt ('tool' in it: a bash step first; 'slow': it waits for an abort or a steer; 'fail': the model
# errs), steering and follow-up queues, abort, and the commands the bridge sends.
FAKE_PI = r'''#!/usr/bin/env python3
import json, os, sys, threading, time, uuid
a = sys.argv[1:]
if a[:1] == ['--version']:
    print('9.9.9'); sys.exit(0)
def arg(name):
    return a[a.index(name) + 1] if name in a else None
home = os.environ['HOME']
cwd = os.getcwd()
path = arg('--session')
if path:
    sid = os.path.basename(path)[:-6].split('_', 1)[1]
    with open(path) as f:
        lines = [json.loads(l) for l in f if l.strip()]
    cwd = lines[0].get('cwd') or cwd
    leaf = next((e['id'] for e in reversed(lines) if e.get('id') and e.get('type') != 'session'), None)
else:
    sid = arg('--session-id') or str(uuid.uuid4())
    folder = os.path.join(home, '.pi', 'agent', 'sessions', '--' + cwd.strip('/').replace('/', '-') + '--')
    path = os.path.join(folder, '2026-01-01T00-00-00-000Z_%s.jsonl' % sid)
    leaf = None
model = arg('--model') or 'fake/fake-1'
lock = threading.Lock()
steering, follow, state = [], [], {'running': False, 'abort': False, 'n': 0}
def out(o):
    with lock:
        sys.stdout.write(json.dumps(o) + '\n'); sys.stdout.flush()
def entry(e):
    global leaf
    state['n'] += 1
    e = dict(e, id='e%04d' % state['n'], parentId=leaf, timestamp='2026-01-01T00:00:00.000Z')
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write(json.dumps({'type': 'session', 'version': 3, 'id': sid, 'timestamp': e['timestamp'], 'cwd': cwd}) + '\n')
    with open(path, 'a') as f:
        f.write(json.dumps(e) + '\n')
    leaf = e['id']
def message(m):
    out({'type': 'message_start', 'message': m})
    out({'type': 'message_end', 'message': m})
    entry({'type': 'message', 'message': m})
def assistant(content, stop='stop', error=None):
    m = {'role': 'assistant', 'content': [], 'provider': model.split('/')[0], 'model': model.split('/')[-1],
         'usage': {'input': 10, 'output': 2, 'cacheRead': 0, 'cacheWrite': 0}, 'stopReason': 'pending'}
    out({'type': 'message_start', 'message': m})
    for i, b in enumerate(content):
        kind = {'text': 'text', 'thinking': 'thinking', 'toolCall': 'toolcall'}[b['type']]
        start = {'type': kind + '_start', 'contentIndex': i}
        if kind == 'toolcall':
            start.update(id=b['id'], toolName=b['name'])
        out({'type': 'message_update', 'assistantMessageEvent': start})
        delta = b.get('text') or b.get('thinking') or json.dumps(b.get('arguments'))
        out({'type': 'message_update', 'assistantMessageEvent': {'type': kind + '_delta', 'contentIndex': i, 'delta': delta}})
        out({'type': 'message_update', 'assistantMessageEvent': {'type': kind + '_end', 'contentIndex': i}})
    m = dict(m, content=content, stopReason=stop)
    if error:
        m['errorMessage'] = error
    out({'type': 'message_end', 'message': m})
    entry({'type': 'message', 'message': m})
def run(text, images):
    state['running'], state['abort'] = True, False
    out({'type': 'agent_start'})
    message({'role': 'user', 'content': [{'type': 'text', 'text': text}] + images})
    pending = [text]
    while pending:
        text = pending.pop(0)
        if 'tool' in text:
            assistant([{'type': 'thinking', 'thinking': 'Look first.'},
                       {'type': 'toolCall', 'id': 'call_1', 'name': 'bash', 'arguments': {'command': 'ls'}}], 'toolUse')
            result = {'content': [{'type': 'text', 'text': 'a.txt'}]}
            out({'type': 'tool_execution_start', 'toolCallId': 'call_1', 'toolName': 'bash', 'args': {'command': 'ls'}})
            out({'type': 'tool_execution_end', 'toolCallId': 'call_1', 'toolName': 'bash', 'result': result, 'isError': False})
            message({'role': 'toolResult', 'toolCallId': 'call_1', 'toolName': 'bash', 'content': result['content'], 'isError': False})
        if 'slow' in text:  # 1.5 s, unless stopped: what is written meanwhile waits until then
            for _ in range(30):
                if state['abort']:
                    break
                time.sleep(0.05)
        if state['abort']:
            assistant([{'type': 'text', 'text': 'cut'}], 'aborted', 'Request was aborted')
            break
        if 'fail' in text:
            assistant([], 'error', '429 rate limited')
            break
        assistant([{'type': 'text', 'text': 'ok: ' + text}])
        with lock:
            given = steering[:] + follow[:]
            del steering[:], follow[:]
        if given:
            out({'type': 'queue_update', 'steering': [], 'followUp': []})
        for t in given:
            message({'role': 'user', 'content': [{'type': 'text', 'text': t}]})
            pending.append(t)
    out({'type': 'agent_end', 'messages': []})
    aborted = state['abort']
    state['running'] = False
    out({'type': 'agent_settled', 'aborted': aborted})
def reply(c, data=None, ok=True, error=None):
    r = {'id': c.get('id'), 'type': 'response', 'command': c['type'], 'success': ok}
    if data is not None:
        r['data'] = data
    if error:
        r['error'] = error
    out(r)
for line in sys.stdin:
    try:
        c = json.loads(line)
    except ValueError:
        out({'type': 'response', 'command': 'parse', 'success': False, 'error': 'bad json'}); continue
    t = c.get('type')
    if t == 'prompt':
        if state['running']:
            (steering if c.get('streamingBehavior') == 'steer' else follow).append(c['message'])
            out({'type': 'queue_update', 'steering': steering, 'followUp': follow})
            reply(c, {'disposition': 'queued'})
        elif c['message'] == 'refuse':
            reply(c, ok=False, error='No API key for provider fake')
        else:
            reply(c, {'disposition': 'started'})
            threading.Thread(target=run, args=(c['message'], c.get('images') or []), daemon=True).start()
    elif t == 'abort':
        state['abort'] = True
        while state['running']:
            time.sleep(0.02)
        reply(c)
    elif t == 'clear_queue':
        with lock:
            data = {'steering': steering[:], 'followUp': follow[:]}
            del steering[:], follow[:]
        reply(c, data)
    elif t == 'get_commands':
        reply(c, {'commands': [{'name': 'review', 'source': 'prompt'}]})
    elif t == 'get_session_stats':
        reply(c, {'sessionId': sid, 'tokens': {'input': 30, 'output': 6, 'cacheRead': 0, 'cacheWrite': 0, 'total': 36}, 'cost': 0.0012,
                  'userMessages': 1, 'assistantMessages': 1, 'toolCalls': 0, 'contextUsage': {'tokens': 12, 'contextWindow': 1000, 'percent': 1.2}})
    elif t == 'set_session_name':
        entry({'type': 'session_info', 'name': c['name']})
        out({'type': 'session_info_changed', 'name': c['name']})
        reply(c)
    elif t == 'compact':
        out({'type': 'compaction_start', 'reason': 'manual'})
        entry({'type': 'compaction', 'summary': 'all of it', 'firstKeptEntryId': leaf, 'tokensBefore': 100})
        out({'type': 'compaction_end', 'reason': 'manual', 'result': {'summary': 'all of it'}, 'aborted': False})
        reply(c, {'summary': 'all of it'})
    elif t == 'get_available_models':
        reply(c, {'models': [{'id': 'fake-1', 'name': 'Fake One', 'provider': 'fake', 'reasoning': True, 'input': ['text'], 'contextWindow': 200000}]})
    elif t in ('set_model', 'set_thinking_level'):
        if t == 'set_model' and c.get('modelId') == 'nope':
            reply(c, ok=False, error='Model not found: fake/nope')
        else:
            reply(c)
    else:
        reply(c, ok=False, error='unknown command')
'''


def session_file(home, cwd, sid, entries):
    folder = os.path.join(home, '.pi', 'agent', 'sessions', '--' + cwd.strip('/').replace('/', '-') + '--')
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, '2025-01-01T00-00-00-000Z_%s.jsonl' % sid)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(json.dumps({'type': 'session', 'version': 3, 'id': sid, 'timestamp': '2025-01-01T00:00:00.000Z', 'cwd': cwd}) + '\n')
        parent = None
        for e in entries:
            e = dict(e, parentId=e.get('parentId', parent), timestamp='2025-01-01T00:00:00.000Z')
            f.write(json.dumps(e) + '\n')
            parent = e['id']
    os.utime(path, (1_700_000_000, 1_700_000_000))
    return path


def user(mid, text, images=()):
    return {'type': 'message', 'id': mid, 'message': {'role': 'user', 'content': [{'type': 'text', 'text': text}] + list(images)}}


def reply(mid, text, tool=None):
    content = [{'type': 'text', 'text': text}]
    if tool:
        content.append({'type': 'toolCall', 'id': tool, 'name': 'bash', 'arguments': {'command': 'ls'}})
    return {'type': 'message', 'id': mid, 'message': {'role': 'assistant', 'content': content, 'provider': 'fake',
                                                       'model': 'fake-1', 'usage': {'input': 100, 'cacheRead': 20}, 'stopReason': 'stop'}}


def result(mid, tool, text):
    return {'type': 'message', 'id': mid, 'message': {'role': 'toolResult', 'toolCallId': tool, 'toolName': 'bash',
                                                       'content': [{'type': 'text', 'text': text}], 'isError': False}}


class Client:
    def __init__(self, sock):
        self.sock = sock

    def call(self, method, path, body=None, raw=None):
        class Conn(http.client.HTTPConnection):
            def connect(inner):
                inner.sock = socket.socket(socket.AF_UNIX)
                inner.sock.settimeout(30)
                inner.sock.connect(self.sock)
        c = Conn('bridge')
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        c.request(method, path, body=data)
        r = c.getresponse()
        text = r.read().decode('utf-8', 'replace')
        c.close()
        try:
            return r.status, json.loads(text)
        except ValueError:
            return r.status, text


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    record = '--record' in sys.argv
    bridge_path = os.path.abspath(args[0] if args else os.path.join(HERE, '..', '..', 'main', 'assets', 'bridge.py'))
    tmp = tempfile.mkdtemp(prefix='bridgetest')
    home = os.path.join(tmp, 'home')
    os.makedirs(os.path.join(tmp, 'bin'))
    os.makedirs(home)
    pi = os.path.join(tmp, 'bin', 'pi')
    with open(pi, 'w') as f:
        f.write(FAKE_PI)
    os.chmod(pi, os.stat(pi).st_mode | stat.S_IXUSR)
    os.environ['HOME'] = home
    os.environ.pop('PI_CODING_AGENT_DIR', None)
    os.environ.pop('PI_CODING_AGENT_SESSION_DIR', None)
    os.environ['PATH'] = os.path.join(tmp, 'bin') + os.pathsep + os.environ['PATH']

    spec = importlib.util.spec_from_file_location('bridge', bridge_path,
                                                  loader=importlib.machinery.SourceFileLoader('bridge', bridge_path))
    bridge = importlib.util.module_from_spec(spec)
    sys.modules['bridge'] = bridge
    spec.loader.exec_module(bridge)
    sock = os.path.join(tmp, 's.sock')
    server = bridge.Server(sock, bridge.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    bridge.find_pi_info()
    bridge.find_models()
    client = Client(sock)
    records = []

    def norm(v):
        text = json.dumps(v).replace(tmp, '<T>')
        # sessions made in the test have new ids each time (uuid4); those written beforehand are aaaaaaaa-0000-…
        text = re.sub(r'[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}', '<uuid>', text)
        text = re.sub(r'"mtime": [0-9.]+', '"mtime": 0', text)
        return json.loads(text)

    def call(method, path, body=None, raw=None):
        status, data = client.call(method, path, body, raw)
        records.append({'req': norm('%s %s %s' % (method, path, '' if body is None else json.dumps(body, sort_keys=True))),
                        'status': status, 'body': norm(data)})
        return status, data

    def events(chat):
        return [e for e in bridge.hub.events if e['chat'] == chat]

    def results(chat):
        return sum(1 for e in events(chat) if e['kind'] == 'cc' and (e['data'] or {}).get('type') == 'result')

    def settle(chat, n, timeout=20):
        end = time.time() + timeout
        while results(chat) < n and time.time() < end:
            time.sleep(0.05)
        time.sleep(0.2)

    def keep_events(chat, note):
        out = []
        for e in events(chat):
            d = dict(e['data'] or {})
            if e['kind'] == 'cc' and d.get('type') == 'stream_event':
                ev = d['event']
                out.append('stream %s %s %s' % (ev['type'], ev.get('index', ''), json.dumps(ev.get('content_block') or ev.get('delta') or '', sort_keys=True)))
            else:
                out.append('%s %s' % (e['kind'], json.dumps(norm(d), sort_keys=True)))
        records.append({'req': 'events of %s' % note, 'status': 0, 'body': out})

    w = os.path.join(tmp, 'w')
    # ---- GET ----
    call('GET', '/health')  # started is volatile
    records[-1]['body'].pop('started', None)
    call('GET', '/config')
    call('GET', '/launch')
    call('GET', '/memory?scope=global')
    call('GET', '/memory?scope=nope')
    call('GET', '/memory?scope=folder&cwd=' + os.path.join(tmp, 'f'))
    call('GET', '/dirs?path=' + tmp)
    call('GET', '/dirs?path=/definitely/not/here')
    call('GET', '/image?id=none&at=0&n=0')
    call('GET', '/history?id=none')
    call('GET', '/nothing')
    call('GET', '/events?since=abc')  # int() fails: ValueError

    # ---- session files ----
    s1, s2, s3 = 'aaaaaaaa-0000-0000-0000-000000000001', 'aaaaaaaa-0000-0000-0000-000000000002', 'aaaaaaaa-0000-0000-0000-000000000003'
    picture = {'type': 'image', 'data': 'iVBORw0KGgo=', 'mimeType': 'image/png'}
    session_file(home, '/tmp/proj', s1, [
        {'type': 'message', 'id': 'sys1', 'message': {'role': 'system', 'content': '', 'sections': {'preamble': 'x'}}},
        user('u1', 'first question', [picture]), reply('a1', 'first answer', tool='t1'), result('r1', 't1', 'a.txt'),
        reply('a1b', 'after the tool'), user('u2', 'second question about pelicans'), reply('a2', 'second answer')])
    session_file(home, '/tmp/proj', s2, [
        user('v1', 'other conversation'), reply('b1', 'other answer'), user('v2', 'another'), reply('b2', 'done'),
        {'type': 'session_info', 'id': 'n1', 'name': 'Named one'}])
    session_file(home, '/tmp/proj', s3, [user('w1', 'only one'), reply('c1', 'yes'),
                                         {'type': 'compaction', 'id': 'k1', 'summary': 's', 'firstKeptEntryId': 'c1', 'tokensBefore': 9},
                                         user('w2', 'after compaction'), reply('c2', 'fine')])
    # one with nothing said (a spare's): not listed
    session_file(home, '/tmp/proj', 'aaaaaaaa-0000-0000-0000-000000000009', [])
    call('GET', '/sessions')
    call('GET', '/search?q=pelicans')
    call('GET', '/search?q=')
    call('GET', '/history?id=' + s1)
    call('GET', '/history?id=' + s3)
    st, h = client.call('GET', '/history?id=' + s1)
    ref = h['messages'][0]['images'][0].split(':')
    call('GET', '/image?id=%s&at=%s&n=%s' % (ref[1], ref[2], ref[3]))

    # ---- POST: simple ----
    call('POST', '/launch', {'appendSystemPrompt': 'be brief', 'disallowedTools': 'write edit'})
    call('POST', '/launch', {'extraArgs': "'unclosed"})
    # which session a pi runs is not the extra arguments' to say; text for the model is not arguments
    call('POST', '/launch', {'extraArgs': '--tools read,bash -c'})
    call('POST', '/launch', {'extraArgs': '--session=' + s1})
    call('POST', '/launch', {'extraArgs': "- You run inside the app; use `rish -c '<command>'`"})
    call('POST', '/launch', {'extraArgs': '--no-skills --skill ~/more'})
    records.append({'req': 'launch.args()', 'status': 0, 'body': norm(bridge.launch.args())})
    call('POST', '/launch', {'extraArgs': ''})
    bridge.launch.data['extraArgs'] = "\n- You run inside the app\n- use `rish -c '<command>'`"  # saved before it was checked
    call('GET', '/launch')
    call('POST', '/launch', {'appendSystemPrompt': ''})
    records.append({'req': 'launch.args(), extra arguments saved long ago', 'status': 0, 'body': norm(bridge.launch.args())})
    call('POST', '/launch', {'extraArgs': '', 'disallowedTools': ''})
    call('POST', '/memory', {'scope': 'global', 'text': 'remember me'})
    call('POST', '/memory', {'scope': 'folder', 'cwd': os.path.join(tmp, 'f'), 'text': 'folder text'})
    call('POST', '/memory', {'scope': 'bogus', 'text': 'x'})
    call('GET', '/memory?scope=global')
    call('POST', '/mkdir', {'path': os.path.join(tmp, 'new', 'dir')})
    call('POST', '/mkdir', {'path': os.path.join(tmp, 'bin', 'pi', 'x')})
    call('POST', '/mkdir', {})
    call('POST', '/auth', {'provider': 'deepseek', 'key': 'sk-1'})
    call('POST', '/auth', {'provider': 'bad provider', 'key': 'x'})
    with open(os.path.join(home, '.pi', 'agent', 'auth.json')) as f:
        records.append({'req': 'auth.json', 'status': oct(os.stat(f.name).st_mode & 0o777), 'body': json.load(f)})
    call('POST', '/auth', {'provider': 'deepseek', 'key': ''})
    call('POST', '/title', {'id': s2, 'title': '  A   new   name '})
    call('POST', '/title', {'id': 'nonexistent', 'title': 'x'})
    call('POST', '/title', {'id': s2, 'title': '   '})
    call('GET', '/history?id=' + s2)
    call('POST', '/nothing', {})
    call('POST', '/send', raw=b'{not json')
    call('POST', '/send', {})
    call('POST', '/stop', {'chat': 'unknown'})
    call('POST', '/cancel', {'chat': 'unknown', 'uuid': 'x'})
    call('POST', '/permission', {'chat': 'unknown', 'requestId': 'r1', 'allow': True})
    call('POST', '/context', {'chat': 'unknown'})
    call('POST', '/open', {'cwd': os.path.join(tmp, 'work2')})
    call('POST', '/open', {'sessionId': s1})

    # ---- upload ----
    for name in ('doc.txt', 'doc.txt', '../evil.txt'):
        status, data = client.call('POST', '/upload?cwd=%s&name=%s' % (os.path.join(tmp, 'up'), name), raw=b'hello')
        records.append({'req': 'POST /upload name=%s' % name, 'status': status, 'body': norm(data)})
    records.append({'req': 'upload files', 'status': 0, 'body': sorted(os.listdir(os.path.join(tmp, 'up')))})

    # ---- a conversation: a turn with a tool step, then one more, its stats, a model change ----
    call('POST', '/send', {'chat': 'c1', 'cwd': w, 'content': [{'type': 'text', 'text': 'use a tool'},
                                                               {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': 'iVBORw0KGgo='}}], 'mid': 'm1'})
    call('POST', '/send', {'chat': 'c1', 'content': 'use a tool', 'mid': 'm1'})  # repeated
    settle('c1', 1)
    sid = bridge.chats['c1'].session_id
    call('POST', '/send', {'chat': 'c1', 'sessionId': sid, 'cwd': w, 'content': [{'type': 'text', 'text': 'hello'}], 'mid': 'm2',
                           'model': 'fake/fake-2', 'effort': 'high'})
    settle('c1', 2)
    call('POST', '/context', {'chat': 'c1', 'sessionId': sid})
    call('POST', '/send', {'chat': 'c1', 'sessionId': sid, 'content': [{'type': 'text', 'text': '/compact keep the names'}], 'mid': 'm3'})
    settle('c1', 3)
    call('POST', '/send', {'chat': 'c1', 'sessionId': sid, 'content': [{'type': 'text', 'text': '/name Tools talk'}], 'mid': 'm4'})
    settle('c1', 4)
    keep_events('c1', 'a conversation')
    call('GET', '/history?id=' + sid)

    # ---- written while a run goes on: inserted (steer), one taken back; then a stop ----
    call('POST', '/send', {'chat': 'c2', 'cwd': w, 'content': [{'type': 'text', 'text': 'slow'}], 'mid': 'n1'})
    time.sleep(0.5)
    call('POST', '/send', {'chat': 'c2', 'cwd': w, 'content': [{'type': 'text', 'text': 'inserted one'}], 'mid': 'n2', 'uuid': 'U1'})
    call('POST', '/send', {'chat': 'c2', 'cwd': w, 'content': [{'type': 'text', 'text': 'inserted two'}], 'mid': 'n3', 'uuid': 'U2'})
    call('POST', '/cancel', {'chat': 'c2', 'uuid': 'U1'})
    call('POST', '/cancel', {'chat': 'c2', 'uuid': 'never'})
    settle('c2', 1)
    sid2 = bridge.chats['c2'].session_id
    call('POST', '/send', {'chat': 'c2', 'sessionId': sid2, 'content': [{'type': 'text', 'text': 'slow again'}], 'mid': 'n4'})
    time.sleep(0.5)
    call('POST', '/stop', {'chat': 'c2', 'sessionId': sid2})
    settle('c2', 2)
    call('POST', '/stop', {'chat': 'c2', 'sessionId': sid2})  # nothing runs
    keep_events('c2', 'inserting, taking back, stopping')

    # ---- what fails: a message pi refuses, a model that errs ----
    call('POST', '/send', {'chat': 'c3', 'cwd': w, 'content': [{'type': 'text', 'text': 'refuse'}], 'mid': 'f1'})
    settle('c3', 1)
    call('POST', '/send', {'chat': 'c3', 'cwd': w, 'content': [{'type': 'text', 'text': 'fail'}], 'mid': 'f2'})
    settle('c3', 2)
    keep_events('c3', 'failures')
    real_spawn = bridge.spawn
    for exc in (OSError('cannot start'), ValueError('bad value'), RuntimeError('odd')):
        def failing(*a, _e=exc, **k):
            raise _e
        bridge.spawn = failing
        call('POST', '/send', {'chat': 'bad-' + type(exc).__name__, 'cwd': w, 'content': 'x', 'mid': 'f-' + type(exc).__name__})
        bridge.spawn = real_spawn
    call('POST', '/send', {'chat': 'bad-OSError', 'cwd': w, 'content': 'x', 'mid': 'f-OSError'})  # the retry is not "repeated"
    settle('bad-OSError', 1)

    # ---- rewind, delete-message, delete (session files) ----
    call('POST', '/rewind', {'chat': 'c1', 'sessionId': sid, 'uuid': 'nope'})
    call('POST', '/rewind', {'chat': 'none', 'sessionId': s1, 'uuid': 'u2'})
    call('GET', '/history?id=' + s1)
    call('POST', '/delete-message', {'chat': 'none', 'sessionId': s2, 'uuid': 'b1', 'role': 'assistant'})
    call('POST', '/delete-message', {'chat': 'none', 'sessionId': s2, 'uuid': 'zz', 'role': 'user'})
    call('POST', '/delete-message', {'chat': 'none', 'sessionId': s2, 'uuid': 'v2'})
    call('GET', '/history?id=' + s2)
    call('POST', '/rewind', {'chat': 'none', 'sessionId': s3, 'uuid': 'w1'})  # nothing left: the file goes
    call('POST', '/delete', {'id': s1})
    call('POST', '/delete', {'id': 'nonexistent'})
    call('POST', '/delete', {})
    call('GET', '/sessions')

    # ---- end ----
    for chat in list(bridge.chats.values()):
        bridge.end(chat.proc)
    if bridge.spare.proc is not None:
        bridge.end(bridge.spare.proc)
    server.shutdown()

    result_ = {'records': records}
    if record:
        with open(GOLDEN, 'w', encoding='utf-8') as f:
            json.dump(result_, f, ensure_ascii=False, indent=1, sort_keys=True)
        print('recorded %d requests' % len(records))
        return 0
    with open(GOLDEN, encoding='utf-8') as f:
        golden = json.load(f)
    got = json.loads(json.dumps(result_, sort_keys=True))
    bad = 0
    for i, (g, r) in enumerate(zip(golden['records'], got['records'])):
        if g != r:
            bad += 1
            print('DIFF #%d %s\n  golden: %s\n  now:    %s' % (i, g['req'], json.dumps(g, ensure_ascii=False)[:600],
                                                             json.dumps(r, ensure_ascii=False)[:600]))
    if len(golden['records']) != len(got['records']):
        bad += 1
        print('DIFF: %d records, golden has %d' % (len(got['records']), len(golden['records'])))
    print('%d requests, %d different' % (len(got['records']), bad))
    return 1 if bad else 0


if __name__ == '__main__':
    code = main()
    sys.stdout.flush()
    os._exit(code)
