"""Account-owned rotation checkpoints advanced only by native publication receipts."""
import hashlib
import json
import re
import sqlite3
import time
import uuid
from contextlib import closing
from urllib.parse import urlsplit

import app_paths
import erp_collection
import store_rotation_setup

QUOTA = 3


def connect():
    db = sqlite3.connect(app_paths.DATA / 'store-rotation.sqlite3', timeout=20)
    db.execute('CREATE TABLE IF NOT EXISTS runs (account INTEGER PRIMARY KEY, body TEXT NOT NULL)')
    db.execute('''CREATE TABLE IF NOT EXISTS reservations (
        account INTEGER, product TEXT, publication TEXT, receipt TEXT,
        PRIMARY KEY(account, product), UNIQUE(account, publication))''')
    db.execute('''CREATE TABLE IF NOT EXISTS identities (
        account INTEGER, identity TEXT, product TEXT, PRIMARY KEY(account, identity))''')
    db.execute('''CREATE TABLE IF NOT EXISTS history (
        account INTEGER, run_id TEXT, body TEXT NOT NULL, archived_at REAL NOT NULL,
        PRIMARY KEY(account, run_id))''')
    return db


def _read(db, owner):
    row = db.execute('SELECT body FROM runs WHERE account=?', (owner,)).fetchone()
    return json.loads(row[0]) if row else None


def status(owner):
    store_rotation_setup.owner(owner)
    with closing(connect()) as db:
        return _read(db, owner)


def history(owner):
    store_rotation_setup.owner(owner)
    with closing(connect()) as db:
        return [json.loads(row[0]) for row in db.execute(
            'SELECT body FROM history WHERE account=? ORDER BY archived_at DESC LIMIT 20', (owner,))]


def _targets(owner, setup):
    import publication_flow
    import publication_policy

    uk_required = 'UK' in setup['markets']
    if uk_required and not publication_policy.default_followup('US'):
        raise ValueError('请先启用并核对美国接续英国的工作台策略')
    destinations = erp_collection.destinations(owner)
    pairs = {pair['sourceDestinationId']: pair for pair in publication_flow.pairs(owner)}
    targets = []
    for row in setup['targets']:
        matches = [d for d in destinations if d.get('marketplace') == 'US'
                   and d.get('store') == row['usStoreName'] and d.get('seller') == row['ziniaoStoreName']
                   and d.get('erpAccount') == row['erpAccount']]
        if len(matches) != 1:
            raise ValueError('无法唯一匹配本账号已启用的紫鸟及美国ERP目标：' + row['usStoreName'])
        dest = matches[0]
        uk = pairs.get(dest['id']) if uk_required else None
        if uk_required and (not uk or uk.get('storeName') != row['ukStoreName'] or not uk.get('id')):
            raise ValueError('英国ERP店铺与已保存的配对目标不同：' + row['ukStoreName'])
        digest = hashlib.sha256(json.dumps([dest, uk], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        targets.append({'destinationId': dest['id'], 'ukDestinationId': uk['id'] if uk else None,
                        'store': dest['store'], 'ukStore': uk['storeName'] if uk else None,
                        'bindingDigest': digest})
    if len({t['destinationId'] for t in targets}) != len(targets):
        raise ValueError('轮转配置重复使用同一美国ERP目标')
    return targets


def _confirm_targets(owner, run):
    setup = store_rotation_setup.read(owner, run['setupId'])['setup']
    if _targets(owner, setup) != run['targets']:
        raise ValueError('原轮转目标配置已变化，请核对原版本及原任务')


def _source_identities(product):
    reference = (product.get('selectionResearch') or {}).get('reference') or {}
    source = product.get('sourceImport') or {}
    asin = reference.get('asin') or source.get('asin')
    market = reference.get('market') or source.get('market') or product.get('market')
    identities = []
    if re.fullmatch(r'[A-Z0-9]{10}', str(asin or '')) and market:
        identities.append(str(market) + ':' + asin)
    url = urlsplit(str(product.get('sourceUrl') or (source.get('supplier') or {}).get('url') or ''))
    if url.hostname == 'detail.1688.com' and re.fullmatch(r'/offer/\d+\.html', url.path):
        identities.append('1688:' + url.path)
    if not identities:
        raise ValueError('没有可排重的商品身份，请先核对原始ASIN或1688货源')
    return identities


def _assign(db, owner, run, workspace, product_id):
    import publication_flow

    current = run['current']
    if current and current['productId'] == product_id:
        return
    if current:
        raise ValueError('当前商品未对账完成，继续沿原商品和店铺处理')
    product = erp_collection.product_for(workspace, product_id)
    identities = _source_identities(product)
    if publication_flow.list_jobs(owner, product_id):
        raise ValueError('商品已有原发布任务，请核对原记录并选择新商品')
    try:
        db.execute('INSERT INTO reservations(account,product) VALUES (?,?)', (owner, product_id))
        db.executemany('INSERT INTO identities VALUES (?,?,?)', ((owner, key, product_id) for key in identities))
    except sqlite3.IntegrityError as error:
        raise ValueError('原商品或同一货源已被分配，不能作为另一新款') from error
    run['current'] = {'productId': product_id, 'name': product.get('name'), 'assignedAt': time.time()}


def _scope(run):
    return {'runId': run['id'], 'round': run['round'], 'index': run['index'],
            'target': run['targets'][run['index']]}


def publication_scope(owner, product, destination, markets):
    """Bind new native publication to its current assignment, if reserved."""
    with closing(connect()) as db:
        assigned = db.execute('SELECT publication FROM reservations WHERE account=? AND product=?',
                              (owner, product)).fetchone()
        if not assigned:
            return None
        run = _read(db, owner)
    if (not run or assigned[0] is not None or (run['current'] or {}).get('productId') != product or run['paused']):
        raise ValueError('本款已登记轮转，请先对账或恢复原轮转')
    _confirm_targets(owner, run)
    if destination != run['targets'][run['index']]['destinationId'] or markets != run['markets']:
        raise ValueError('发布店铺或站点范围与本款的轮转分配不符')
    return _scope(run)


def _reconcile(db, owner, run, job_id):
    import publication_flow

    if not re.fullmatch(r'[a-f0-9]{32}', job_id):
        raise ValueError('需要原工作台发布任务编号')
    if db.execute('SELECT 1 FROM reservations WHERE account=? AND publication=?', (owner, job_id)).fetchone():
        return
    product = (run['current'] or {}).get('productId')
    if not product:
        raise ValueError('当前轮转位置尚未分配商品')
    receipt = publication_flow.rotation_receipt(owner, job_id, product, run['markets'], _scope(run))
    db.execute('UPDATE reservations SET publication=?,receipt=? WHERE account=? AND product=?',
               (job_id, json.dumps(receipt, ensure_ascii=False), owner, product))
    run['lastCompleted'] = {'product': run['current'], 'receipt': receipt}
    run['current'] = None
    run['count'] += 1
    if run['count'] == QUOTA:
        run['count'] = 0
        run['index'] = (run['index'] + 1) % len(run['targets'])
        if run['index'] == 0:
            run['round'] += 1


def _withdraw(db, owner, run, workspace, product_id, reason):
    import agent_center
    import codex_operations
    import image_engine
    import listing_jobs
    import publication_flow

    if any(row['product']['productId'] == product_id for row in run.get('withdrawn', [])):
        return
    current = run.get('current')
    if not current or current['productId'] != product_id:
        raise ValueError('当前商品已变化，请刷新轮转检查点')
    request_id = codex_operations.unresolved_submission(owner, product_id)
    if request_id:
        raise ValueError('原 Codex 派单尚未对账，不能淘汰，请核对请求 ' + request_id)
    if publication_flow.list_jobs(owner, product_id):
        raise ValueError('该商品已有原发布任务，请先核对任务状态；有未知提交时不能淘汰')
    product = erp_collection.product_for(workspace, product_id)
    with listing_jobs.database() as jobs:
        pending_copy = jobs.execute(
            """SELECT 1 FROM listing_generation_jobs WHERE owner=? AND product_id=?
               AND status IN ('queued','running','ready','needs_review') LIMIT 1""", (owner, product_id)).fetchone()
    if pending_copy or any(job.get('status') in ('queued', 'running', 'ready', 'uncertain', 'needs_review')
                           for job in (product.get('listingGeneration') or {}).values()):
        raise ValueError('原文案任务尚未完成或对账，不能淘汰')
    with closing(agent_center.connect()) as jobs:
        pending_agent = jobs.execute(
            """SELECT 1 FROM tasks WHERE user_id=? AND json_extract(body, '$.productId')=?
               AND json_extract(body, '$.status') IN ('running','needs_review') LIMIT 1""", (owner, product_id)).fetchone()
    if pending_agent:
        raise ValueError('原 Agent 任务尚未完成或对账，不能淘汰')
    with erp_collection.connect() as collection:
        pending_erp = collection.execute(
            """SELECT 1 FROM jobs WHERE account_id=? AND product_id=?
               AND status IN ('queued','running','needs_reconciliation') LIMIT 1""", (owner, product_id)).fetchone()
    if pending_erp:
        raise ValueError('原 ERP 草稿任务尚未对账，不能淘汰')
    if any(task['status'] not in ('completed', 'needs_revision', 'failed')
           for task in image_engine.list_tasks(product_id, limit=None)):
        raise ValueError('原图片任务尚未完成或对账，不能淘汰')
    reservation = db.execute('SELECT publication FROM reservations WHERE account=? AND product=?',
                             (owner, current['productId'])).fetchone()
    if not reservation or reservation[0] is not None:
        raise ValueError('无法确认原商品分配状态，请先对账')
    run.setdefault('withdrawn', []).append({'product': current, 'reason': reason, 'at': time.time(),
                                            'store': run['targets'][run['index']]})
    run['current'] = None


def _archive(db, owner, run):
    if run.get('archivedAt'):
        return
    if run.get('current'):
        raise ValueError('请先完成或安全淘汰当前商品，再归档轮转')
    run['paused'] = True
    run['archivedAt'] = time.time()
    db.execute('INSERT OR REPLACE INTO history VALUES (?,?,?,?)',
               (owner, run['id'], json.dumps(run, ensure_ascii=False), run['archivedAt']))


def update(owner, workspace, data):
    store_rotation_setup.owner(owner)
    if not isinstance(data, dict) or not isinstance(data.get('action'), str):
        raise ValueError('轮转操作格式无效')
    action = data['action']
    fields = {'start': {'setupId'}, 'pause': {'runId'}, 'resume': {'runId'},
              'assign': {'runId', 'productId'}, 'reconcile': {'runId', 'publicationId'},
              'withdraw': {'runId', 'productId', 'reason'}, 'archive': {'runId'}}
    optional = {'afterRunId'} if action == 'start' and 'afterRunId' in data else set()
    if action not in fields or set(data) != fields[action] | optional | {'action'} or any(
            not isinstance(data[key], str) or not data[key] for key in fields[action] | optional):
        raise ValueError('轮转操作参数无效')
    if action == 'withdraw' and not 1 <= len(data['reason'].strip()) <= 500:
        raise ValueError('请说明淘汰本款的原因')
    with closing(connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        run = _read(db, owner)
        if action == 'start':
            if run:
                if run['setupId'] == data['setupId'] and not run.get('archivedAt'):
                    return run
                if not run.get('archivedAt'):
                    raise ValueError('已有原轮转，须先完成并归档，不能重置游标')
                if data.get('afterRunId') != run['id']:
                    raise ValueError('请刷新并确认当前归档记录后再开始新轮转')
            setup = store_rotation_setup.read(owner, data['setupId'])['setup']
            if not setup:
                raise ValueError('请先保存目标站点和店铺配置')
            run = {'id': uuid.uuid4().hex, 'setupId': setup['id'], 'setupRevision': setup['revision'],
                   'markets': setup['markets'], 'targets': _targets(owner, setup), 'paused': False,
                   'round': 1, 'index': 0, 'count': 0, 'current': None}
        else:
            if not run or run['id'] != data['runId']:
                raise ValueError('轮转任务不属于当前账号或已变化')
            if run.get('archivedAt') and action != 'archive':
                raise ValueError('原轮转已归档，请读取归档记录或开始新轮转')
            if action == 'pause':
                run['paused'] = True
            elif action == 'resume':
                _confirm_targets(owner, run)
                run['paused'] = False
            elif action == 'assign':
                if run['paused']:
                    raise ValueError('当前轮转已暂停，请由运营员恢复')
                _confirm_targets(owner, run)
                _assign(db, owner, run, workspace, data['productId'])
            elif action == 'reconcile':
                _reconcile(db, owner, run, data['publicationId'])
            elif action == 'withdraw':
                _withdraw(db, owner, run, workspace, data['productId'], data['reason'].strip())
            elif action == 'archive':
                _archive(db, owner, run)
        db.execute('INSERT OR REPLACE INTO runs VALUES (?,?)', (owner, json.dumps(run, ensure_ascii=False)))
        return run
