"""Local, explicitly bound Ego draft bridge. Never creates or publishes products."""
import hashlib
import importlib.util
import json
import subprocess
import uuid
from contextlib import contextmanager
from pathlib import Path
import app_paths
import runtime_environment


def project_root():
    return runtime_environment.project_root(app_paths.APP)


def binding(user, task):
    config = json.loads((project_root() / 'automation/config.json').read_text(encoding='utf-8'))['browser']
    if config.get('engine') != 'ego-lite':
        raise ValueError('当前账号未接通ego草稿工具')
    matches = [b for b in config.get('erp_account_bindings', {}).get(str(user), [])
               if b.get('product_id') == task.get('productId')
               and b.get('store') == task['profile']['store']
               and b.get('marketplace') == task['profile']['marketplace']]
    if len(matches) != 1 or matches[0].get('enabled') is not True:
        raise ValueError('此账号、产品及店铺没有已授权的ERP草稿绑定')
    b = matches[0]
    if (not isinstance(b.get('draft_id'), str) or not b['draft_id'].isdigit()
            or not isinstance(b.get('erp_sku'), str) or not b['erp_sku'].strip()):
        raise ValueError('ERP草稿绑定不完整')
    if task['profile']['mode'] != 'draft':
        raise ValueError('此ego工具仅支持已有草稿，不支持发布')
    return config, b


def request_for(user, task):
    config, b = binding(user, task)
    p = task['snapshot']['product'] or {}
    if p.get('id') != task.get('productId'):
        raise ValueError('产品快照与任务不一致')
    if p.get('brandUseAuthorized') is True and p.get('brand') not in (None, '', 'Generic'):
        raise ValueError('此适配仅支持Generic，不修改已授权品牌产品')
    c = task['snapshot']['listing']
    bullets = c.get('bullets')
    if isinstance(bullets, str):
        bullets = [s.strip() for s in bullets.splitlines() if s.strip()]
    patch = {k: c.get(k, '') for k in ('title', 'highlights')}
    patch.update(bullets=bullets, description=c.get('description', ''), terms=c.get('terms', ''),
                 brand='Generic', manufacturer='Generic')
    if not isinstance(bullets, list) or len(bullets) != 5:
        raise ValueError('草稿核对需要完整五点文案')
    for key, value in patch.items():
        values = value if key == 'bullets' else [value]
        limit = {'title': 75, 'highlights': 125, 'description': 2000, 'terms': 249,
                 'bullets': 500, 'brand': 50, 'manufacturer': 50}[key]
        if any(not isinstance(v, str) or not v.strip() or
               (len(v.encode('utf-8')) if key == 'terms' else len(v)) > limit for v in values):
            raise ValueError('草稿字段缺失或超长：' + key)
    request = {'draftId': b['draft_id'], 'store': b['store'], 'erpSku': b['erp_sku'], 'patch': patch,
               'evidence': {k: f'Account {user}, product {p["id"]}, task {task["id"]}: saved snapshot; Generic fallback only, no publication.' for k in patch}}
    return config, request


def receipt_path(request):
    identity = {k: request[k] for k in ('draftId', 'store', 'erpSku', 'patch')}
    digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()
    return project_root() / 'automation/data/ego-draft-executions' / (digest + '.json')


def folder_for(task):
    tid = task.get('id')
    if not isinstance(tid, str) or not tid or any(c not in '0123456789abcdef' for c in tid):
        raise ValueError('任务编号无效')
    folder = app_paths.DATA / 'agent-results' / tid / 'erp'
    folder.mkdir(parents=True, exist_ok=True)
    return folder


@contextmanager
def browser_lock(user, task):
    path = project_root() / 'automation/data/ego-workbench.lock'
    try:
        lock = path.open('x', encoding='utf-8')
    except FileExistsError:
        raise ValueError('ego工作台工具已有执行记录待结束或核对，未重复启动') from None
    state = {'keep': False}
    try:
        lock.write(json.dumps({'user': user, 'taskId': task['id'], 'state': 'started'})); lock.flush()
        yield state
    finally:
        lock.close()
        if not state['keep']:
            path.unlink()


def observe(user, task):
    request_for(user, task)
    with browser_lock(user, task) as state:
        return _observe(user, task, state)


def _observe(user, task, state):
    """Fresh read of persisted values; never advances production stages."""
    config, request = request_for(user, task)
    folder = folder_for(task)
    run_id = uuid.uuid4().hex
    output = folder / (run_id + '-observation.json')
    script = Path(config['script_directory']) / ('workbench-erp-observe-' + run_id + '.mjs')
    module = project_root() / 'junfei-publisher/ego-existing-draft.mjs'
    script.write_text(f'''const fs=await import('node:fs/promises');
const {{inspectDraft}}=await import({json.dumps(module.as_uri())});
const request={json.dumps(request,ensure_ascii=False)};
const page=globalThis.amazonTask.page({json.dumps(config['pages']['erp'])});
await page.goto(`https://www.junfeikuajing.com/#/amazon/product/${{request.draftId}}?type=draft`);
await page.reload();
await page.waitForFunction(()=>document.querySelector('input[placeholder="店铺"]')?.value&&document.querySelector('input[placeholder="亚马逊运输模板"]')?.value&&document.querySelector('textarea[placeholder="产品描述"]'),undefined,{{timeout:15000}});
const actual=await inspectDraft(page,request);
const differences=Object.keys(request.patch).filter(k=>JSON.stringify(actual.copy[k])!==JSON.stringify(request.patch[k]));
await fs.writeFile({json.dumps(str(output))},JSON.stringify({{draftId:request.draftId,store:actual.store,erpSku:actual.erpSku,actual,differences,observedAt:new Date().toISOString(),saved:false,published:false}},null,2),{{encoding:'utf8',flag:'wx'}});
console.log(JSON.stringify({{observed:true,differences}}));
''', encoding='utf-8')
    try:
        result = subprocess.run(runtime_environment.ego_command(project_root()) + [
                                 'run', str(script), '--timeout', '55'], capture_output=True, timeout=65)
    except subprocess.TimeoutExpired:
        state['keep'] = True
        raise ValueError('ego观察结果未知，请核对原浏览器进程；未重启或保存草稿') from None
    if result.returncode or not output.is_file():
        (folder / (run_id + '-error.txt')).write_bytes(result.stdout + result.stderr)
        state['keep'] = True
        raise ValueError('ego草稿读取失败，请检查专属浏览器登录、页面与控制状态；未保存或发布')
    observation = json.loads(output.read_text(encoding='utf-8'))
    if any(observation.get(k) != request[k] for k in ('draftId', 'store', 'erpSku')):
        raise ValueError('ego返回的草稿身份不匹配')
    return {**observation, 'note': '重新读取已保存的已有草稿；差异仅对照任务文案快照，不证明图片、产品事实或在售状态。'}


def sync_copy(user, task):
    """Production-only partial tool. Image upload and publication remain blocked."""
    if task.get('taskMode') != 'production' or not all(task['results'].get(k, {}).get('ok') for k in
            ('facts', 'research', 'listing', 'images', 'export', 'junfeiPreflight')):
        raise ValueError('资料、研究、已审核文案图片及交付预检全部通过后才能同步草稿')
    config, request = request_for(user, task)
    with browser_lock(user, task) as state:
        return _sync_copy(user, task, request, state)


def _sync_copy(user, task, request, state):
    path = receipt_path(request)
    if not path.is_file():
        # Reuse the exact independent tool and global duplicate barrier.
        spec = importlib.util.spec_from_file_location('bound_ego_link', project_root() / 'junfei-publisher/ego_link.py')
        link = importlib.util.module_from_spec(spec); spec.loader.exec_module(link)
        request_path = folder_for(task) / ('request-' + uuid.uuid4().hex + '.json')
        request_path.write_text(json.dumps(request, ensure_ascii=False), encoding='utf-8')
        browser = link.browser_config()
        command = link.ego_draft_command(request_path, config=browser)
        try:
            with runtime_environment.ego_space(browser['task_space_id']):
                state['keep'] = True
                response = subprocess.run(command, capture_output=True, timeout=65)
        except subprocess.TimeoutExpired:
            raise ValueError('ERP保存结果未知，请先对账；不自动重发') from None
        if response.returncode or not path.is_file():
            raise ValueError('ERP工具未返回可核对回执，请核查原操作；未自动重发')
    record = json.loads(path.read_text(encoding='utf-8'))
    if record.get('status') != 'draft_saved_reopened_verified' or record.get('published') is not False:
        raise ValueError('ERP原操作结果未知或未完成保存，必须对账；不自动重发')
    if any(record.get(k) != request[k] for k in ('draftId', 'store', 'erpSku', 'patch')):
        raise ValueError('ERP回执与本账号任务快照不一致')
    state['keep'] = False
    current = _observe(user, task, state)
    if current['differences']:
        raise ValueError('ERP草稿与原回执目标内容不同，未覆盖或重发')
    return {'ok': False, 'copySynced': True, 'draftId': request['draftId'], 'erpSku': request['erpSku'],
            'published': False, 'receiptSha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'issues': ['已有草稿文案已保存并重读；交付图片上传及新品创建尚未接通，未完成整款刊登'],
            'reason': '已有草稿文案同步完成，停在图片上传及完整刊登校准',
            'note': '仅现有草稿文案及Generic字段；不将文字同步记为整款商品完成。'}
