"""One durable 1688 queue, one Ego session, account-scoped observations.

No anonymous HTTP fallback, CAPTCHA retries, cookie copying or lease expiry retry.
The browser page stays open for manual verification and is read in place on resume.
"""
import copy
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from contextlib import ExitStack, closing
from urllib.parse import urlsplit

import app_paths
import runtime_environment

ROOT = app_paths.LIBRARY_ROOT
DB = ROOT / 'supplier-capture-queue.sqlite3'
def scope_for_data(path):
    return hashlib.sha256(str(Path(path).resolve()).encode()).hexdigest()


SCOPE = scope_for_data(app_paths.DATA)
# Local pacing preference, not a platform safety threshold. Packaged runtimes
# without the automation config use the same slower default.
try:
    _config = json.loads((app_paths.APP.parent / 'automation' / 'config.json').read_text(encoding='utf-8'))
    MIN_INTERVAL = max(30, int(_config['sourcing']['supplier_capture_min_interval_seconds']))
except (OSError, ValueError, TypeError, KeyError):
    MIN_INTERVAL = 90
_THREAD_LOCK = threading.Lock()
_THREAD = None
_WATCH_THREAD = None
_WATCH_STOP = threading.Event()


class Pending(ValueError):
    def __init__(self, record):
        self.record = record
        super().__init__(record.get('message') or '1688采集已排队；遇到登录或验证时暂停，完成后继续')


def canonical(url):
    parsed = urlsplit(str(url))
    if (parsed.scheme != 'https' or parsed.netloc != 'detail.1688.com'
            or not re.fullmatch(r'/offer/\d+\.html', parsed.path)):
        raise ValueError('仅支持 HTTPS 1688商品详情链接')
    return 'https://detail.1688.com' + parsed.path


def connect():
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute('CREATE TABLE IF NOT EXISTS gate(id INTEGER PRIMARY KEY, body TEXT NOT NULL)')
    c.execute('''CREATE TABLE IF NOT EXISTS captures(
        id TEXT PRIMARY KEY, scope TEXT NOT NULL, url TEXT NOT NULL, state TEXT NOT NULL,
        created REAL NOT NULL, body TEXT NOT NULL, UNIQUE(scope,url))''')
    c.execute('CREATE TABLE IF NOT EXISTS scope_controls(scope TEXT PRIMARY KEY,status TEXT NOT NULL,updated REAL NOT NULL)')
    c.execute('''CREATE TABLE IF NOT EXISTS capture_events(
        seq INTEGER PRIMARY KEY,capture_id TEXT,scope TEXT NOT NULL,kind TEXT NOT NULL,at REAL NOT NULL,body TEXT NOT NULL)''')
    c.execute('INSERT OR IGNORE INTO gate VALUES(1,?)', (json.dumps({'paused': False, 'active': None, 'nextAt': 0}),))
    c.commit()
    return c


def gate(c):
    return json.loads(c.execute('SELECT body FROM gate WHERE id=1').fetchone()[0])


def save_gate(c, value):
    c.execute('UPDATE gate SET body=? WHERE id=1', (json.dumps(value, ensure_ascii=False),))


def _event(c, row, kind, **body):
    c.execute('INSERT INTO capture_events(capture_id,scope,kind,at,body) VALUES(?,?,?,?,?)',
              (row['id'], row['scope'], kind, time.time(), json.dumps(body, ensure_ascii=False)))


def _scope_enabled(c, scope):
    row = c.execute('SELECT status FROM scope_controls WHERE scope=?', (scope,)).fetchone()
    return row is None or row['status'] == 'active'


def _account_active():
    # A request authenticated before account revocation must not start a new page afterwards.
    if not app_paths.WORKER_USER:
        return True
    import accounts
    with accounts.connect() as c:
        row = c.execute('SELECT status FROM users WHERE id=?', (app_paths.WORKER_USER,)).fetchone()
    return row is not None and row['status'] == 'active'


def _set_scope_status(c, scope, status):
    c.execute('INSERT OR REPLACE INTO scope_controls VALUES(?,?,?)', (scope, status, time.time()))
    g = gate(c)
    if status != 'active':
        for row in c.execute("SELECT * FROM captures WHERE scope=? AND state='queued'", (scope,)).fetchall():
            if row['id'] == g.get('active'):
                continue  # An inconsistent active record is still an unknown external result.
            c.execute("UPDATE captures SET state='cancelled' WHERE id=?", (row['id'],))
            _event(c, row, 'cancelled_before_execution', accountStatus=status)
    active = c.execute('SELECT scope FROM captures WHERE id=?', (g.get('active'),)).fetchone()
    if active is not None and active['scope'] == scope:
        if status == 'active':
            g.pop('ownerStatus', None)
        else:
            g['ownerStatus'] = status
        save_gate(c, g)  # Preserve active, paused, runId, browser and the original pause reason.


def set_scope_status(scope, status, *, purge=False):
    """Cancel only work that never started; external results must be reconciled first."""
    if status not in ('active', 'disabled', 'deleted', 'purged', 'missing', 'pending'):
        raise ValueError('无效的采集账号状态')
    if not DB.is_file():
        return
    with closing(connect()) as c, c:
        c.execute('BEGIN IMMEDIATE')
        g = gate(c)
        unsettled = c.execute("SELECT id FROM captures WHERE scope=? AND (state='reading' OR id=?) LIMIT 1",
                              (scope, g.get('active'))).fetchone()
        if purge and unsettled is not None:
            raise ValueError('该账号仍有进行中、验证暂停或结果待核对的1688采集；已保留原任务和回执，请恢复账号核对后再彻底删除')
        _set_scope_status(c, scope, status)
        if purge:
            # Do not let a future workspace at the same path reuse the deleted account's cache.
            for row in c.execute("SELECT * FROM captures WHERE scope=? AND state='collected'", (scope,)).fetchall():
                c.execute("UPDATE captures SET state='archived' WHERE id=?", (row['id'],))
                _event(c, row, 'cache_archived_on_purge')


def dispatch_scope(owners):
    """Main service routes the FIFO head; it never reads supplier payloads or runs a browser."""
    if not DB.is_file():
        return None
    with closing(connect()) as c, c:
        c.execute('BEGIN IMMEDIATE')
        pending = c.execute("SELECT DISTINCT scope FROM captures WHERE state='queued'").fetchall()
        for row in pending:
            status = owners.get(row['scope'], {}).get('status', 'missing')
            if status != 'active':
                _set_scope_status(c, row['scope'], status)
        g = gate(c)
        if g.get('active'):
            row = c.execute('SELECT scope FROM captures WHERE id=?', (g['active'],)).fetchone()
            if row is None:
                return None  # A damaged active identity is not permission to navigate again.
            scope = row['scope']
            status = owners.get(scope, {}).get('status', 'missing')
            if status != 'active':
                _set_scope_status(c, scope, status)
                return None
            # Starting this runtime only reads its durable receipt. No active task is replayed.
            return scope if _scope_enabled(c, scope) else None
        if g.get('paused') or time.time() < g.get('nextAt', 0):
            return None
        row = c.execute("SELECT scope FROM captures WHERE state='queued' ORDER BY created,id LIMIT 1").fetchone()
        return row['scope'] if row is not None and _scope_enabled(c, row['scope']) else None


def valid_supplier(value, url):
    return (isinstance(value, dict) and canonical(value.get('url', '')) == url
            and 5 < len(str(value.get('title', ''))) < 500
            and not re.search('登录|验证|访问受限|安全检查', str(value.get('title', '')))
            and bool(value.get('attributes') or value.get('images')))


def cached_workspace(url):
    path = app_paths.DATA / 'studio.sqlite3'
    if not path.is_file():
        return None
    with closing(sqlite3.connect(path, timeout=10)) as c:
        row = c.execute('SELECT body FROM workspace WHERE id=1').fetchone()
    products = json.loads(row[0]).get('products', []) if row else []
    for product in products:
        supplier = (product.get('sourceImport') or {}).get('supplier') or {}
        try:
            if valid_supplier(supplier, url):
                return copy.deepcopy(supplier)
        except ValueError:
            pass
    return None


def remember(supplier):
    """Also feed successful extension captures into the same account cache."""
    url = canonical(supplier.get('url'))
    if not valid_supplier(supplier, url):
        raise ValueError('资料不完整，未写入1688采集缓存')
    with closing(connect()) as c, c:
        c.execute('''INSERT INTO captures VALUES(?,?,?,?,?,?)
            ON CONFLICT(scope,url) DO UPDATE SET state='collected',body=excluded.body''',
            (uuid.uuid4().hex, SCOPE, url, 'collected', time.time(), json.dumps(supplier, ensure_ascii=False)))


def request(url):
    url = canonical(url)
    with closing(connect()) as c:
        saved = c.execute('SELECT * FROM captures WHERE scope=? AND url=?', (SCOPE, url)).fetchone()
    if saved is not None and saved['state'] == 'collected':
        return {'status': 'collected', 'supplier': {**json.loads(saved['body']), 'reused': True}, 'id': saved['id']}
    existing = cached_workspace(url)
    if existing:
        remember(existing)
    with closing(connect()) as c, c:
        if not _scope_enabled(c, SCOPE) or not _account_active():
            raise ValueError('当前账号已停用，未新增1688采集')
        c.execute('INSERT OR IGNORE INTO captures VALUES(?,?,?,?,?,?)',
                  (uuid.uuid4().hex, SCOPE, url, 'queued', time.time(), '{}'))
        row = c.execute('SELECT * FROM captures WHERE scope=? AND url=?', (SCOPE, url)).fetchone()
        if row['state'] in ('cancelled', 'archived'):
            _event(c, row, 'explicitly_requested_after_cancellation', previousState=row['state'])
            c.execute("UPDATE captures SET state='queued',created=?,body='{}' WHERE id=?", (time.time(), row['id']))
            row = c.execute('SELECT * FROM captures WHERE id=?', (row['id'],)).fetchone()
    if row['state'] == 'collected':
        return {'status': 'collected', 'supplier': {**json.loads(row['body']), 'reused': True}, 'id': row['id']}
    kick()
    return item(url)


def item(url):
    with closing(connect()) as c:
        row = c.execute('SELECT * FROM captures WHERE scope=? AND url=?', (SCOPE, canonical(url))).fetchone()
        g = gate(c)
    if row is None:
        raise ValueError('采集任务不存在')
    result = {'id': row['id'], 'url': row['url'], 'status': row['state']}
    if row['state'] == 'collected':
        result['supplier'] = json.loads(row['body'])
    elif row['state'] in ('cancelled', 'archived'):
        result['message'] = '采集已随账号状态变化取消，原记录已保留；重新启用账号后可明确重新发起'
    elif g.get('paused'):
        result.update(status='paused', message='1688队列已暂停；请处理固定采集窗口后点击“已完成验证，继续采集”')
    return result


def read(url, timeout=125):
    result = request(url)
    until = time.monotonic() + timeout
    while result['status'] not in ('collected', 'paused', 'cancelled', 'archived') and time.monotonic() < until:
        time.sleep(.25)
        result = item(url)
    if result['status'] != 'collected':
        raise Pending(result)
    return result['supplier']


def status():
    maintain()
    with closing(connect()) as c, c:
        c.execute('BEGIN IMMEDIATE')
        g = gate(c)
        if g.get('active') and (not g.get('paused') or g.get('resuming')) and time.time() - g.get('startedAt', time.time()) > 180:
            g.update(paused=True, resuming=False, reason='execution_unknown')
            save_gate(c, g)
        rows = c.execute('SELECT id,url,state FROM captures WHERE scope=? ORDER BY created', (SCOPE,)).fetchall()
        active = c.execute('SELECT scope,url FROM captures WHERE id=?', (g.get('active'),)).fetchone()
        enabled = _scope_enabled(c, SCOPE)
    own = active is not None and active['scope'] == SCOPE
    return {'paused': bool(g.get('paused')), 'reason': g.get('reason', '') if own else ('shared_session_paused' if g.get('paused') else ''),
            'canResume': bool(own and g.get('paused') and enabled and _account_active()), 'url': active['url'] if own else '',
            'browser': g.get('browser', {}) if own else {},
            'ownerUnavailable': bool(g.get('ownerStatus')),
            'items': [dict(row) for row in rows], 'intervalSeconds': MIN_INTERVAL}


def _creation_reconciliation_chain(run_id, receipt, raw, browser):
    """Trace only blank-rejected creation/binding receipts to the original missing space."""
    chain, seen = [], set()
    creation_id = None
    missing_browser = None
    bound_browser = None
    initial_meta = receipt.get('reconnect')
    first_stage = initial_meta.get('stage') if isinstance(initial_meta, dict) else None
    for _ in range(8):
        if run_id in seen:
            break
        seen.add(run_id)
        meta = receipt.get('reconnect')
        if (receipt.get('status') != 'paused' or receipt.get('reason') != 'reconnect_blank_page_required'
                or not isinstance(meta, dict) or meta.get('runId') != run_id
                or meta.get('oldSpaceAbsent') is not True or meta.get('stage') not in ('creating', 'bound')
                or not re.fullmatch(r'[a-f0-9]{32}', str(meta.get('previousRunId', '')))):
            break
        chain.append({'runId': run_id, 'stage': meta['stage'], 'resultSha256': hashlib.sha256(raw).hexdigest()})
        candidate = meta.get('previousBrowser')
        if (not isinstance(candidate, dict) or type(candidate.get('spaceId')) is not int
                or candidate['spaceId'] <= 0 or not candidate.get('name')
                or not re.fullmatch(r'p[1-9]\d*', str(candidate.get('label', '')))
                or (missing_browser is not None and candidate != missing_browser)):
            break
        missing_browser = candidate
        if meta['stage'] == 'bound':
            current_bound = receipt.get('browser')
            candidate_creation = meta.get('existingCreationRunId', run_id)
            if (not re.fullmatch(r'[a-f0-9]{32}', str(candidate_creation))
                    or (creation_id is not None and candidate_creation != creation_id)
                    or not isinstance(current_bound, dict) or current_bound.get('label') != 'p1'
                    or current_bound.get('name') != f'Junfei 1688 serial collection reconnect {candidate_creation}'
                    or type(current_bound.get('spaceId')) is not int or current_bound['spaceId'] <= 0
                    or current_bound['spaceId'] == missing_browser['spaceId']
                    or meta.get('browser') != current_bound
                    or meta.get('createdSpace') != {k: current_bound[k] for k in ('spaceId', 'name')}
                    or (bound_browser is not None and current_bound != bound_browser)):
                break
            creation_id, bound_browser = candidate_creation, current_bound
        elif (receipt.get('browser') != missing_browser or meta.get('previousBrowser') != missing_browser
              or (creation_id is not None and creation_id != run_id)):
            break
        else:
            creation_id = run_id
        previous = meta['previousRunId']
        try:
            previous_raw = (app_paths.DATA / 'supplier-capture-runs' / previous / 'result.json').read_bytes()
            previous_receipt = json.loads(previous_raw)
        except (OSError, ValueError):
            break
        if not isinstance(previous_receipt, dict):
            break
        if meta['stage'] == 'creating':
            if (previous_receipt.get('status') != 'paused' or previous_receipt.get('browser') != missing_browser
                    or previous_receipt.get('reason') != f"task space not found: {missing_browser['spaceId']}"):
                break
            chain.append({'runId': previous, 'stage': 'missing', 'resultSha256': hashlib.sha256(previous_raw).hexdigest()})
            return {'existingCreationRunId': creation_id,
                    'existingCreationName': f'Junfei 1688 serial collection reconnect {creation_id}',
                    'originalMissingRunId': previous, 'originalMissingBrowser': copy.deepcopy(missing_browser),
                    'previousReconnectStage': first_stage, 'receiptChain': chain,
                    **({'boundCreationBrowser': copy.deepcopy(bound_browser)} if bound_browser else {})}
        run_id, receipt, raw = previous, previous_receipt, previous_raw
    raise ValueError('原空白工作区创建/绑定回执链不完整，不能接回；未知或验证结果须另行核对')


def _missing_browser_evidence(row, g):
    """Only a durable pre-page missing-space failure permits an explicit reconnect."""
    browser = g.get('browser')
    run_id = g.get('runId', '')
    if (row['state'] != 'reading' or not isinstance(browser, dict)
            or type(browser.get('spaceId')) is not int or browser['spaceId'] <= 0
            or not isinstance(browser.get('name'), str) or not browser['name'].strip()
            or not re.fullmatch(r'p[1-9]\d*', str(browser.get('label', '')))
            or browser.get('reconnectMissing') or not re.fullmatch(r'[a-f0-9]{32}', run_id)):
        raise ValueError('缺少可核对的原采集工作区和运行回执，不能重连')
    missing_reason = f"task space not found: {browser['spaceId']}"
    reason = g.get('reason')
    if reason not in (missing_reason, 'reconnect_blank_page_required'):
        raise ValueError('仅已确认工作区不存在的暂停采集可明确重连；验证或未知结果须保留原页核对')
    output = app_paths.DATA / 'supplier-capture-runs' / run_id / 'result.json'
    try:
        raw = output.read_bytes()
        receipt = json.loads(raw)
    except (OSError, ValueError):
        raise ValueError('原采集暂停回执缺失，不能重连') from None
    if (not isinstance(receipt, dict) or receipt.get('status') != 'paused' or receipt.get('reason') != reason
            or receipt.get('browser') != browser):
        raise ValueError('原采集暂停回执与绑定不一致，不能重连')
    evidence = {'previousRunId': run_id, 'previousBrowser': copy.deepcopy(browser),
                'previousReason': reason, 'previousResultSha256': hashlib.sha256(raw).hexdigest()}
    if reason == 'reconnect_blank_page_required':
        evidence.update(_creation_reconciliation_chain(run_id, receipt, raw, browser))
    return evidence


def _claim(resume=False, *, reconnect_missing_browser=False):
    if type(reconnect_missing_browser) is not bool or (reconnect_missing_browser and not resume):
        raise ValueError('重连必须通过已确认的原暂停采集恢复入口')
    with closing(connect()) as c, c:
        c.execute('BEGIN IMMEDIATE')
        if not _scope_enabled(c, SCOPE) or not _account_active():
            if resume:
                raise ValueError('当前账号已停用，不能继续1688采集')
            return None
        g = gate(c)
        reconnect_evidence = None
        if resume:
            row = c.execute('SELECT * FROM captures WHERE id=? AND scope=?', (g.get('active'), SCOPE)).fetchone()
            if not g.get('paused') or row is None or g.get('resuming'):
                raise ValueError('没有可由当前账号继续的暂停任务')
            if reconnect_missing_browser:
                reconnect_evidence = _missing_browser_evidence(row, g)
                g['browser'] = {**g['browser'], 'reconnectMissing': True,
                                'reconnectFromRunId': g['runId']}
                if reconnect_evidence.get('existingCreationRunId'):
                    g['browser']['reconnectExistingCreationRunId'] = reconnect_evidence['existingCreationRunId']
                    g['browser']['reconnectOriginalMissingBrowser'] = reconnect_evidence['originalMissingBrowser']
                    if reconnect_evidence.get('boundCreationBrowser'):
                        g['browser']['reconnectBoundCreationBrowser'] = reconnect_evidence['boundCreationBrowser']
            g['resuming'] = True
            g['startedAt'] = time.time()
        else:
            if g.get('paused') or g.get('active') or time.time() < g.get('nextAt', 0):
                return None
            # Global FIFO across all account runtimes. Payloads stay account scoped.
            row = c.execute("SELECT * FROM captures WHERE state='queued' ORDER BY created,id LIMIT 1").fetchone()
            if row is None or row['scope'] != SCOPE:
                return None
            g.update(active=row['id'], startedAt=time.time(), pid=os.getpid())
            c.execute("UPDATE captures SET state='reading' WHERE id=?", (row['id'],))
        g['runId'] = uuid.uuid4().hex
        if reconnect_evidence:
            _event(c, row, 'missing_browser_reconnect_authorized',
                   **reconnect_evidence, runId=g['runId'], url=row['url'])
        save_gate(c, g)
        return dict(row), g


def _finish(row, g, result):
    with closing(connect()) as c, c:
        c.execute('BEGIN IMMEDIATE')
        current = gate(c)
        if current.get('active') != row['id'] or current.get('runId') != g.get('runId'):
            raise ValueError('采集会话状态已变化，未覆盖结果')
        if result.get('browser'):
            current['browser'] = copy.deepcopy(result['browser'])
        # The authorization belongs to this run, including unknown/failed outcomes.
        for key in ('reconnectMissing', 'reconnectFromRunId', 'reconnectExistingCreationRunId',
                    'reconnectOriginalMissingBrowser', 'reconnectBoundCreationBrowser'):
            (current.get('browser') or {}).pop(key, None)
        if (g.get('browser') or {}).get('reconnectMissing'):
            _event(c, row, 'missing_browser_reconnect_finished', runId=g['runId'],
                   browser=current.get('browser'), status=result.get('status'),
                   reason=result.get('reason'), reconnect=result.get('reconnect'))
        current['resuming'] = False
        valid = False
        try:
            valid = result.get('status') == 'collected' and valid_supplier(result.get('supplier'), row['url'])
        except ValueError:
            pass
        if valid:
            c.execute("UPDATE captures SET state='collected',body=? WHERE id=?", (json.dumps(result['supplier'], ensure_ascii=False), row['id']))
            current.update(paused=False, active=None, reason='', nextAt=time.time() + MIN_INTERVAL)
        else:
            current.update(paused=True, reason=result.get('reason', 'execution_unknown'))
        save_gate(c, current)


def _perform(claim, resume=False):
    row, g = claim
    result = {'status': 'paused', 'reason': 'execution_unknown'}
    try:
        result = browser_capture(row['url'], g.get('browser'), resume, g['runId'])
    except Exception:
        result = {'status': 'paused', 'reason': 'execution_unknown'}
    finally:
        _finish(row, g, result)


def resume(*, reconnect_missing_browser=False):
    """Only called by an explicit user action after manual verification."""
    _perform(_claim(resume=True, reconnect_missing_browser=reconnect_missing_browser), resume=True)
    kick()
    return status()


def _drain():
    try:
        while True:
            with closing(connect()) as c:
                g = gate(c)
                pending = c.execute("SELECT 1 FROM captures WHERE scope=? AND state='queued' LIMIT 1", (SCOPE,)).fetchone()
            if g.get('paused') or not pending:
                return
            claim = _claim()
            if claim:
                _perform(claim)
            else:
                time.sleep(1)
    finally:
        global _THREAD
        with _THREAD_LOCK:
            _THREAD = None


def kick():
    global _THREAD
    with _THREAD_LOCK:
        if _THREAD is None:
            _THREAD = threading.Thread(target=_drain, daemon=True, name='1688-serial-collector')
            _THREAD.start()


def maintain():
    """Account-local recovery never reopens reading/paused/unknown page work."""
    if not DB.is_file():
        return
    _recover_finished_run()
    with closing(connect()) as c, c:
        c.execute('BEGIN IMMEDIATE')
        g = gate(c)
        active = c.execute('SELECT scope FROM captures WHERE id=?', (g.get('active'),)).fetchone()
        if (active is not None and active['scope'] == SCOPE and (not g.get('paused') or g.get('resuming'))
                and time.time() - g.get('startedAt', time.time()) > 180):
            g.update(paused=True, resuming=False, reason='execution_unknown')
            save_gate(c, g)
        pending = c.execute("SELECT 1 FROM captures WHERE scope=? AND state='queued' LIMIT 1", (SCOPE,)).fetchone()
        ready = pending is not None and not g.get('paused') and not g.get('active') and _scope_enabled(c, SCOPE)
    if ready and _account_active():
        kick()


def start_worker():
    """Explicit runtime startup hook, including queues restored after a service restart."""
    global _WATCH_THREAD
    with _THREAD_LOCK:
        if _WATCH_THREAD is not None and _WATCH_THREAD.is_alive():
            return
        _WATCH_STOP.clear()
        def watch():
            while not _WATCH_STOP.is_set():
                try:
                    maintain()
                except Exception:
                    logging.getLogger(__name__).exception('1688队列状态维护失败；未重新提交采集')
                _WATCH_STOP.wait(2)
        _WATCH_THREAD = threading.Thread(target=watch, daemon=True, name='1688-account-queue-watch')
        _WATCH_THREAD.start()


def stop_worker():
    _WATCH_STOP.set()  # Do not kill an active browser operation or clear its durable identity.


def _recover_finished_run():
    """Reconcile a durable browser receipt after a server/process interruption."""
    with closing(connect()) as c:
        g = gate(c)
        row = c.execute('SELECT * FROM captures WHERE id=? AND scope=?', (g.get('active'), SCOPE)).fetchone()
    if row is None or time.time() - g.get('startedAt', time.time()) <= 180:
        return
    run = g.get('runId', '')
    if not re.fullmatch(r'[a-f0-9]{32}', run):
        return
    output = app_paths.DATA / 'supplier-capture-runs' / run / 'result.json'
    if output.is_file():
        try:
            result = json.loads(output.read_text(encoding='utf-8'))
            _finish(dict(row), g, result)
        except (ValueError, OSError):
            pass


def browser_capture(url, browser=None, resume=False, run_id=None):
    cli = shutil.which('ego-browser')
    if not cli:
        return {'status': 'paused', 'reason': 'ego_browser_not_installed'}
    folder = app_paths.DATA / 'supplier-capture-runs' / (run_id or uuid.uuid4().hex)
    folder.mkdir(parents=True)
    output = folder / 'result.json'
    params = {'url': url, 'browser': browser, 'resume': resume, 'runId': run_id, 'output': str(output),
              'collector': str(app_paths.APP / 'extension' / 'collector.js')}
    code = (app_paths.APP / 'supplier_capture_runtime.mjs').read_text(encoding='utf-8')
    script = folder / 'capture.mjs'
    script.write_text('const params=' + json.dumps(params, ensure_ascii=False) + ';\n' + code, encoding='utf-8')
    route = browser or {}
    # Initial creation and an unresolved reconnect have no known target ID yet.
    bound = route.get('reconnectBoundCreationBrowser') if route.get('reconnectMissing') else route
    space_id = (bound or {}).get('spaceId')
    with ExitStack() as locks:
        if space_id is not None:
            try:
                locks.enter_context(runtime_environment.ego_space(space_id))
            except (ValueError, OSError):
                return {'status': 'paused', 'reason': 'ego_space_unavailable'}
        # Official Ego runtime; no alternate browser or anonymous network request.
        try:
            subprocess.run([cli, 'nodejs', '-e', f"await import('{script.as_uri()}')"],
                           timeout=115, capture_output=True, encoding='utf-8')
        except (OSError, subprocess.TimeoutExpired):
            pass
        if output.is_file():
            result = json.loads(output.read_text(encoding='utf-8'))
            if result.get('status') == 'collected':
                import supplier_facts
                value = result['supplier']
                value.update(supplier_facts.normalize(value))
                value.update(supplier_facts.image_capture(value, value.get('images', [])))
                output.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
            return result
    return {'status': 'paused', 'reason': 'execution_unknown'}
