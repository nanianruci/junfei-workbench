import app_paths
"""APIMart reference-image generation and persisted asynchronous task tracking."""
import base64
import hashlib
import json
import os
import re
import sqlite3
import threading
import uuid
import time
from contextlib import closing
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen, build_opener, HTTPRedirectHandler
import secrets_store
import aigocode_provider
import image_budget
import openai_image_provider as custom_images
import local_image_provider as local_images

ROOT = app_paths.DATA
KEY = secrets_store.load('apimart') or os.environ.get('APIMART_API_KEY', '')
LOCK = threading.Lock()
MODELS = ['gpt-image-2', 'gpt-image-2-official', 'gemini-3-pro-image-preview', 'gemini-3-pro-image-preview-official']
AVAILABLE = None


class ReferenceChanged(ValueError):
    """A repair cannot reproduce the original request's reference images."""


def active_provider():
    try:
        value = json.loads((ROOT / 'image-provider.json').read_text(encoding='utf-8'))['provider']
        if value not in ('apimart', 'aigocode', 'openai', 'comfyui'):
            raise ValueError('当前生图服务配置无效，不会自动切换接口')
        return value
    except FileNotFoundError:
        return 'aigocode' if aigocode_provider.public()['active'] else 'apimart'


def select_provider(name):
    target = ROOT / 'image-provider.json'
    temporary = target.with_suffix('.'+uuid.uuid4().hex+'.tmp')
    temporary.write_text(json.dumps({'provider':name}), encoding='utf-8')
    temporary.replace(target)


def public():
    relay = aigocode_provider.public()
    custom = custom_images.public()
    selected = active_provider()
    local = local_images.public()
    active = {'openai': custom, 'aigocode': {**relay, 'provider': 'AIGoCode'}, 'comfyui': local}.get(selected)
    return {'aigocode': relay, 'openai': custom, 'comfyui': local, 'activeProvider': selected,
            'mode': 'local' if selected == 'comfyui' else 'api',
            'provider': active['provider'] if active else 'APIMart', 'hasKey': bool(KEY),
            'models': active['models'] if active else MODELS if AVAILABLE is None else [m for m in AVAILABLE if m in MODELS]}


def configure(data):
    if data.get('provider') == 'comfyui':
        local_images.configure(data)
        if data.get('activate') is True:
            select_provider('comfyui')
        return public()
    if data.get('provider') == 'openai':
        custom_images.configure(data)
        if data.get('activate') is True:
            select_provider('openai')
        return public()
    if data.get('activeProvider') == 'apimart':
        select_provider('apimart')
        cfg = aigocode_provider._config(); cfg['active'] = False; aigocode_provider.CONFIG.parent.mkdir(parents=True, exist_ok=True); aigocode_provider.CONFIG.write_text(json.dumps(cfg), encoding='utf-8')
    if data.get("provider") == "aigocode":
        # Keep the same response shape as GET /api/image/settings. The settings
        # UI reads the configured relay from `response.aigocode`; returning the
        # relay object directly made saving succeed but threw before /test ran.
        aigocode_provider.configure(data)
        if data.get('activate') is True:
            select_provider('aigocode')
        return public()
    global KEY, AVAILABLE
    key = data.get('api_key', '')
    if not isinstance(key, str) or any(c in key for c in '\r\n') or len(key) > 2000:
        raise ValueError('图片密钥格式不正确')
    with LOCK:
        if data.get('clearKey'):
            secrets_store.save('apimart', '')
        elif key.strip():
            secrets_store.save('apimart', key.strip())
        if key.strip():
            if key.strip() != KEY:
                AVAILABLE = None
            KEY = key.strip()
        if data.get('clearKey'):
            KEY = ''
            AVAILABLE = None
    return public()


def test_key(data):
    if data.get('provider') == 'comfyui':
        return local_images.probe(data.get('baseUrl') or local_images.public()['baseUrl'])
    if data.get('provider') == 'openai':
        return custom_images.test_key(data)
    if data.get("provider") == "aigocode":
        return aigocode_provider.test_key(data)
    global AVAILABLE
    key = data.get('api_key', '')
    if not isinstance(key, str) or any(c in key for c in '\r\n') or len(key) > 2000:
        raise ValueError('图片密钥格式不正确')
    tested_key = key.strip() or KEY
    if not tested_key:
        raise ValueError('请先输入或保存 APIMart 图片密钥')
    request = Request('https://api.apimart.ai/v1/models?expand=category&category=image', headers={'Authorization': 'Bearer ' + tested_key})
    try:
        with urlopen(request, timeout=25) as response:
            result = json.loads(response.read(4 * 1024 * 1024))
        if result.get('success') is False or 'error' in result:
            raise ValueError('APIMart 未接受请求，请核对密钥权限')
        models = sorted({m['id'] for m in result['data'] if isinstance(m, dict) and isinstance(m.get('id'), str) and (m.get('category') == 'image' or m['id'] in MODELS)})
    except HTTPError as error:
        raise ValueError('APIMart 密钥认证失败，请核对密钥' if error.code == 401 else f'APIMart 返回 HTTP {error.code}，请检查权限或稍后重试') from None
    except (URLError, TimeoutError):
        raise ValueError('APIMart 网络连接失败或超时，请稍后重试') from None
    except (KeyError, TypeError, json.JSONDecodeError):
        raise ValueError('APIMart 模型列表格式无法解析') from None
    if tested_key == KEY:
        AVAILABLE = models
    return {'ok': True, 'models': models, 'supportedModels': [m for m in models if m in MODELS], 'message': '密钥认证成功。查询未生成图片；不代表余额充足或图片生成一定成功。'}


def api(path, body=None):
    if not KEY:
        raise ValueError('请在模型设置中配置 APIMart 图片密钥')
    request = Request('https://api.apimart.ai/v1/' + path, data=json.dumps(body).encode() if body is not None else None, headers={'Authorization': 'Bearer ' + KEY, 'Content-Type': 'application/json'})
    try:
        with urlopen(request, timeout=90) as response:
            result = json.loads(response.read(4 * 1024 * 1024))
        if result.get('code', 200) != 200 or 'error' in result:
            raise ValueError('APIMart 返回失败状态，请核对模型、参数或账户余额')
        return result
    except HTTPError as e:
        try:
            detail = json.loads(e.read(16384)).get('error', {})
            code = detail.get('code', '') if isinstance(detail, dict) else ''
        except (ValueError, TypeError, AttributeError):
            code = ''
        stage = '读图分析/图片策划' if path == 'chat/completions' else '图片服务'
        if code == 'model_not_found':
            message = f'APIMart {stage}模型通道不可用（HTTP {e.code}，model_not_found），请联系 APIMart 检查模型通道；这不代表密钥或余额有误'
        elif e.code >= 500:
            message = f'APIMart {stage}暂时不可用（HTTP {e.code}），请稍后重试或联系服务商；这不代表密钥或余额有误'
        elif e.code == 401:
            message = 'APIMart 密钥认证失败（HTTP 401），请检查该账号的图片密钥'
        elif e.code == 429:
            message = 'APIMart 请求受限（HTTP 429），请稍后重试或检查服务商额度限制'
        else:
            message = f'APIMart {stage}请求失败（HTTP {e.code}），请检查模型权限和请求配置'
        raise ValueError(message + '；未自动重复提交') from None
    except (URLError, TimeoutError):
        raise ValueError('图片接口连接失败或超时；请先在 APIMart 后台核对任务，避免重复提交计费') from None


def storage():
    ROOT.mkdir(exist_ok=True)
    conn = sqlite3.connect(ROOT / 'image_jobs.sqlite3')
    conn.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, body TEXT NOT NULL)')
    conn.execute('''CREATE TABLE IF NOT EXISTS submission_attempts
                    (id TEXT PRIMARY KEY, product_id TEXT NOT NULL, slot TEXT NOT NULL,
                     state TEXT NOT NULL, body TEXT NOT NULL)''')
    return conn


def begin_submission(data, provider_name):
    product_id, slot = str(data.get('productId') or '').strip(), str(data.get('slot') or '').strip()
    if not product_id or not slot:
        raise ValueError('生图必须指定产品ID和图位，才能记录提交与防止重复计费')
    attempt = {'id': 'submission-' + uuid.uuid4().hex, 'productId': product_id, 'slot': slot,
               'provider': provider_name, 'model': data['model'], 'status': 'submitting',
               'createdAt': time.time(), 'images': [], 'progress': 0,
               'imageEngineTaskId': data.get('imageEngineTaskId'),
               'storyboardProjectId': data.get('storyboardProjectId'), 'storyboardSlot': data.get('storyboardSlot')}
    if provider_name == local_images.PROVIDER:
        attempt['requestedPromptId'] = str(uuid.UUID(attempt['id'].removeprefix('submission-')))
    with closing(storage()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        previous = conn.execute('''SELECT id FROM submission_attempts
                                  WHERE product_id=? AND slot=? AND state IN ('submitting','submission_unknown')
                                  LIMIT 1''', (product_id, slot)).fetchone()
        if previous:
            raise ValueError('此产品图位已有未核对的提交记录：' + previous[0] + '；请先核对服务商结果，未重复提交')
        # Also protect legacy jobs created before the submission journal existed.
        for (body,) in conn.execute('SELECT body FROM jobs'):
            job = json.loads(body)
            if (job.get('productId') == product_id and job.get('slot') == slot
                    and job.get('status') not in ('completed', 'failed', 'cancelled')):
                raise ValueError('此产品图位已有进行中任务：' + str(job['id']) + '；请查询原任务，未重复提交')
        if provider_name != local_images.PROVIDER:
            try:
                attempt['budget'] = image_budget.reserve(conn, ROOT, attempt, data)
            except ValueError as error:
                raise image_budget.BudgetBlocked(str(error)) from None
        conn.execute('INSERT INTO submission_attempts VALUES (?,?,?,?,?)',
                     (attempt['id'], product_id, slot, attempt['status'], json.dumps(attempt, ensure_ascii=False)))
    return attempt


def update_submission(attempt, **values):
    attempt = {**attempt, **values, 'updatedAt': time.time()}
    with closing(storage()) as conn, conn:
        conn.execute('UPDATE submission_attempts SET state=?, body=? WHERE id=?',
                     (attempt['status'], json.dumps(attempt, ensure_ascii=False), attempt['id']))
        if attempt.get('budget',{}).get('accountingBasis')=='chatgpt_subscription_quota':
            image_budget.codex_tools_quota.update(conn,attempt)
        if values.get('externalId'):
            image_budget.schema(conn)
            conn.execute('UPDATE image_costs SET external_id=? WHERE id=?',
                         (values['externalId'], attempt['id']))
    return attempt


def save(job):
    with closing(storage()) as conn, conn:
        conn.execute('INSERT OR REPLACE INTO jobs VALUES (?, ?)', (job['id'], json.dumps(job, ensure_ascii=False)))
    return job


def local_image(url):
    """Read only this account's generated/uploaded raster assets."""
    if not isinstance(url, str) or not re.fullmatch(r'/uploads/[a-f0-9]{32}\.(png|jpg|webp)', url):
        raise ValueError('本地图片地址无效')
    directory = (ROOT / 'uploads').resolve()
    path = ROOT / url.lstrip('/')
    if path.is_symlink() or path.resolve().parent != directory or not path.is_file():
        raise ValueError('本账号的本地图片不存在')
    if path.stat().st_size > 25 * 1024 * 1024:
        raise ValueError('本地图片超过25MB')
    return path.read_bytes()


def jobs():
    with closing(storage()) as conn:
        pending = [json.loads(row[0]) for row in conn.execute(
            "SELECT body FROM submission_attempts WHERE state IN ('submitting','submission_unknown') ORDER BY rowid DESC")]
        for job in pending:
            rejection=provider_rejection(job)
            if rejection:job['providerRejection']=rejection
        return {'jobs': pending + [json.loads(row[0]) for row in conn.execute('SELECT body FROM jobs ORDER BY rowid DESC')]}


def provider_rejection(job):
    """Display explicit refusal evidence separately; preserve the no-replay journal.

    HTTP 400 alone does not prove a safety refusal or a billing settlement.
    This derived annotation never releases a reservation or reopens submission.
    """
    diagnostic=job.get('proxyDiagnostic') or {}
    if diagnostic.get('httpStatus')==400 and 'rejected by the safety system' in str(diagnostic.get('message','')).lower():
        return {'kind':'safety_refusal','label':'服务商明确拒绝，未重发','diagnosticId':diagnostic.get('id')}
    return None


def product_cost_receipts(product_id):
    path=ROOT/'image_jobs.sqlite3'
    if not path.is_file():
        return []
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='image_costs'").fetchone():
            return []
        rows=conn.execute('SELECT id,reserved_fen,actual_fen,state,body FROM image_costs WHERE product_id=? ORDER BY rowid',
                          (product_id,))
        results=[]
        for rid,reserved,actual,state,body in rows:
            quote=json.loads(body).get('quote',{})
            results.append({'reservationId':rid,'quoteCurrency':quote.get('currency'),
                            'quoteMaximum':quote.get('maximumRmb') if quote.get('currency')=='CNY' else quote.get('maximumUsd'),
                            'reservedFen':reserved,'settledFen':actual,'state':state})
        return results


def budget_status(product_id=None):
    with closing(storage()) as conn, conn:
        return image_budget.status(conn, product_id)


def budget_quote(data):
    """Read-only price preview; no model invocation or reservation."""
    relay = aigocode_provider.public()
    selected = active_provider()
    if selected == 'comfyui':
        return {'provider': local_images.PROVIDER, 'quote': {'reservationBasis': 'local_compute',
                'note': '本机 ComfyUI 使用本地算力，无云端 API 费用预留。'},
                'limits': image_budget.product_limits(data.get('productId')),
                'submitted': False, 'reserved': False, 'settled': False}
    spec = custom_images.specification(data) if selected == 'openai' else None
    provider = custom_images.PROVIDER if spec else 'AIGoCode' if selected == 'aigocode' else 'APIMart'
    endpoint = spec['providerEndpoint'] if spec else relay.get('baseUrl') if selected == 'aigocode' else 'https://api.apimart.ai/v1'
    quote_data = {**data, 'providerEndpoint': endpoint}
    if spec:
        quote_data.update(protocol=spec['protocol'], quality=spec['quality'])
    quote = image_budget.verified_quote(ROOT, provider, quote_data, preview=True)
    quote = {k:v for k,v in quote.items() if k != 'sourceEvidence'}
    if isinstance(quote.get('fx'), dict):
        quote['fx'] = {k:v for k,v in quote['fx'].items() if k != 'sourceEvidence'}
    return {'provider': provider, 'quote': quote, 'limits': image_budget.product_limits(data.get('productId')),
            'submitted': False, 'reserved': False, 'settled': False}


def settle_budget(data):
    with closing(storage()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        return image_budget.settle(conn, data)


class NoImageRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def remote_reference(ref):
    u = urlsplit(ref)
    if (u.scheme != 'https' or u.hostname not in
            ('m.media-amazon.com', 'images-na.ssl-images-amazon.com', 'cbu01.alicdn.com')
            or u.username or u.password or u.port not in (None, 443)):
        raise ValueError('仅支持采集的亚马逊或1688产品图片')
    try:
        request = Request(ref, headers={'User-Agent': 'Mozilla/5.0'})
        with build_opener(NoImageRedirect()).open(request, timeout=30) as response:
            content = response.read(15 * 1024 * 1024 + 1)
        if len(content) > 15 * 1024 * 1024:
            raise ValueError('单张采集图超过15 MB，请选择其他图片')
    except (HTTPError, URLError, TimeoutError):
        raise ValueError('采集图片下载失败或链接已失效，请重新采集或选择其他图片；尚未提交生成') from None
    if content.startswith(b'\x89PNG\r\n\x1a\n'):
        mime = 'image/png'
    elif content.startswith(b'\xff\xd8\xff'):
        mime = 'image/jpeg'
    elif content.startswith(b'RIFF') and content[8:12] == b'WEBP':
        mime = 'image/webp'
    else:
        raise ValueError('采集链接未返回PNG、JPG或WebP图片，请选择其他图片')
    return content, mime


class StoryboardRequired(ValueError):
    pass


def storyboard_submission(data, project_id, slot):
    # Only server-side renderers pass this linkage. Request JSON cannot supply it.
    import visual_studio as visual
    project = visual.get_project(project_id) if project_id else None
    if type(slot) is not int:
        raise StoryboardRequired('请先在图片工作台用 AI 生成分镜，并从分镜卡片发起生图')
    visual.require_ai_storyboard(project, data.get('productId'), slot, confirmed=True)
    return {'storyboardProjectId': project_id, 'storyboardSlot': slot}


def submit(data, *, storyboard_project_id=None, storyboard_slot=None, expected_provider=None, image_engine_task_id=None):
    if image_engine_task_id is not None:
        import image_engine
        linkage = image_engine.authorize(image_engine_task_id, data)
    else:
        linkage = storyboard_submission(data, storyboard_project_id, storyboard_slot)
    data = {**data, **linkage}
    model = data.get('model')
    prompt = data.get('prompt', '')
    refs = data.get('references', [])
    relay = aigocode_provider.public()
    selected = active_provider()
    if expected_provider is not None and expected_provider != selected:
        raise local_images.Unavailable('当前生图服务已切换，本次未提交，请重新选择模型和图位')
    relay = {**relay, 'active':selected == 'aigocode'}
    model_list = (local_images.public()['models'] if selected == 'comfyui' else custom_images.public()['models']
                  if selected == 'openai' else relay['models'] if relay['active'] else MODELS)
    if model not in model_list or not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 12000:
        raise ValueError('请选择支持的图片模型，并填写 1–12000 字的图片要求')
    if not isinstance(refs, list) or len(refs)>4:
        raise ValueError('请选择 1–4 张产品参考图')
    if not refs and image_engine_task_id is not None:
        raise ValueError('独立图片任务必须绑定商品参考图')
    if not refs:
        import visual_studio as visual
        project=visual.get_project(storyboard_project_id)
        if visual.slot_references(project,storyboard_slot):
            raise ValueError('需要展示商品的图位必须附商品真值图')
    size, resolution = data.get('size', '1:1'), data.get('resolution', '1K')
    resolutions = ('1K', '2K', '4K', '1600') if selected == 'openai' else ('1K', '2K', '4K')
    if size not in ('1:1', '4:5', '3:4', '16:9') or resolution not in resolutions:
        raise ValueError('图片比例或分辨率不支持')
    local_spec = local_images.specification(data) if selected == 'comfyui' else None
    if not refs and local_spec:
        raise local_images.Unavailable('此服务需要输入图，请切换到支持无参考生成的 API')
    if local_spec and len(refs)>1 and '[REFERENCE ROLES]' in prompt:
        raise ValueError('此生图服务只接受一张图，请移除版式参考或切换到支持双参考的 API')
    images = []
    reference_hashes = []
    total = 0
    for ref in refs:
        if isinstance(ref, str) and ref.startswith('https://'):
            content, mime = remote_reference(ref)
        else:
            if not isinstance(ref, str) or not re.fullmatch(r'/uploads/[a-f0-9]{32}\.(png|jpg|webp)', ref):
                raise ValueError('请选择素材库或采集的产品图')
            path = ROOT / 'uploads' / ref.split('/')[-1]
            if not path.is_file():
                raise ValueError('参考素材不存在，请重新上传')
            content = path.read_bytes()
            mime = {'.png': 'image/png', '.jpg': 'image/jpeg', '.webp': 'image/webp'}[path.suffix]
        total += len(content)
        if total > 30 * 1024 * 1024:
            raise ValueError('参考图合计请控制在 30 MB 内')
        images.append('data:' + mime + ';base64,' + base64.b64encode(content).decode())
        reference_hashes.append(hashlib.sha256(content).hexdigest())
    expected_hashes = data.get('expectedReferenceSha256')
    source_evidence_hash = data.get('expectedProductReferenceSha256')
    if source_evidence_hash and reference_hashes[:1] != [source_evidence_hash]:
        raise ReferenceChanged('商品实拍内容已变化，请重新分析素材并策划分镜')
    if expected_hashes is not None and reference_hashes != expected_hashes:
        raise ReferenceChanged('参考图内容已变化，无法沿用首发附图')
    spec = custom_images.specification(data) if selected == 'openai' else None
    if not refs and spec and spec['protocol']=='edits':
        raise local_images.Unavailable('当前 Images Edits 接口需要输入图，请切换到支持无参考生成的 API')
    custom_key = custom_images.submission_credentials(spec) if spec else None
    provider_name, endpoint = 'APIMart', 'https://api.apimart.ai/v1'
    if local_spec:
        provider_name, endpoint = local_images.PROVIDER, local_spec['baseUrl']
    elif spec:
        provider_name, endpoint = custom_images.PROVIDER, spec['providerEndpoint']
        data.update(protocol=spec['protocol'], quality=spec['quality'])
    elif relay['active']:
        provider_name, endpoint = 'AIGoCode', relay.get('baseUrl')
    attempt = begin_submission({**data, 'providerEndpoint': endpoint}, provider_name)
    def record_response(receipt):
        attempt['responseReceipt'] = receipt
        update_submission(attempt)
    try:
        if local_spec:
            job = local_images.submit(data, images[0], local_spec, attempt['requestedPromptId'])
        elif spec:
            job = custom_images.submit(data, images, spec, on_response=record_response, api_key=custom_key)
        else:
            job = dispatch_submission(data, relay, model, prompt, refs, size, resolution, images)
        job.update(linkage)
        job['referenceSha256'] = reference_hashes
        # Persist the provider handle before the job row; a later disk failure remains reconcilable.
        attempt = update_submission(attempt, externalId=job['id'], providerJob=job)
        save(job)
        update_submission(attempt, status='recorded')
        return job
    except Exception as error:
        update_submission(attempt, status='submission_unknown',
                          error='生图提交或结果落盘未确认；请核对服务商，禁止重新提交',
                          **({'proxyDiagnostic':error.diagnostic} if isinstance(error,custom_images.image_proxy_diagnostics.ProxyHTTPError) else {}))
        raise


def dispatch_submission(data, relay, model, prompt, refs, size, resolution, images):
    if relay['active']:
        canvas={'1:1':'1024x1024','4:5':'1024x1536','3:4':'1024x1536','16:9':'1536x1024'}[size]
        url=aigocode_provider.generate(model,prompt,canvas,resolution.lower(),images)
        return {'id':uuid.uuid4().hex,'productId':str(data.get('productId','')),'slot':str(data.get('slot','')),
                     'model':model,'provider':'AIGoCode','prompt':prompt,'references':refs,'status':'completed',
                     'progress':100,'images':[url]}
    if model == 'gpt-image-2':
        # APIMart current GPT-Image-2 contract: ratio and explicit resolution tier.
        # Pricing is tier-based, so never silently submit the default 1k tier.
        body={'model':model,'prompt':prompt,'image_urls':images,'size':size,
              'resolution':resolution.lower(),'n':1}
    elif model == 'gpt-image-2-official':
        # IvyeaOps original image renderer contract: pixel canvas and high input fidelity.
        canvas={'1:1':'1024x1024','4:5':'1024x1536','3:4':'1024x1536','16:9':'1464x600'}[size]
        body={'model':model,'prompt':prompt,'image_urls':images[:2],'size':canvas,'input_fidelity':'high','n':1}
    else:
        body={'model':model,'prompt':prompt,'image_urls':images,'size':size,'resolution':resolution,'n':1}
    if not images:
        body.pop('image_urls',None)
        body.pop('input_fidelity',None)
    result=api('images/generations',body)
    try:
        task = result['data'][0]['task_id']
        if not isinstance(task, str) or not task:
            raise ValueError()
    except (KeyError, IndexError, TypeError, ValueError):
        raise ValueError('接口未返回有效任务 ID，请在 APIMart 后台核对，勿重复提交') from None
    return {'id': task, 'productId': str(data.get('productId', '')), 'slot': str(data.get('slot', '')), 'model': model, 'prompt': prompt, 'references': refs, 'status': 'submitted', 'progress': 0, 'images': []}


def preserve_completed_images(job):
    """Keep provider completion distinct from a retryable local-download failure."""
    if job.get('status') != 'completed' or not job.get('images'):
        return job
    if all(isinstance(url, str) and url.startswith('/uploads/') for url in job['images']):
        return job
    original = list(job['images'])
    try:
        assets = [custom_images.remote_image(url) if not url.startswith('/uploads/')
                  else {'url':url} for url in original]
    except Exception:
        # Preserve the actual provider result; another poll only retries caching.
        job['resultCache'] = {'status':'pending', 'error':'成图已返回，本地保存未完成；查询原任务可重试保存，不会重新生图'}
        return save(job)
    job.setdefault('providerImages', original)
    job['images'] = [asset['url'] for asset in assets]
    job['resultCache'] = {'status':'saved', 'assets':assets, 'savedAt':time.time()}
    return save(job)


def poll(data):
    task_id = data.get('id')
    with LOCK:
        with closing(storage()) as conn:
            row = conn.execute('SELECT body FROM jobs WHERE id=?', (task_id,)).fetchone()
            attempt = conn.execute('SELECT body FROM submission_attempts WHERE id=?', (task_id,)).fetchone() if not row else None
        if not row:
            if attempt:
                pending = json.loads(attempt[0])
                if pending.get('provider') == custom_images.PROVIDER and pending.get('responseReceipt'):
                    recovered_job = custom_images.recover(pending['responseReceipt'], pending['productId'], pending['slot'])
                    for key in ('storyboardProjectId', 'storyboardSlot', 'imageEngineTaskId'):
                        if pending.get(key) is not None:
                            recovered_job[key] = pending[key]
                    save(recovered_job)
                    update_submission(pending, status='recorded', externalId=recovered_job['id'], providerJob=recovered_job)
                    return recovered_job
                recovered = pending.get('providerJob')
                if (isinstance(recovered, dict) and recovered.get('id') == pending.get('externalId')
                        and recovered.get('productId') == pending.get('productId')
                        and recovered.get('slot') == pending.get('slot')):
                    # Restore only a provider response already persisted before a local failure.
                    # This never sends another generation request or guesses a provider handle.
                    with closing(storage()) as conn, conn:
                        conn.execute('INSERT OR IGNORE INTO jobs VALUES (?, ?)',
                                     (recovered['id'], json.dumps(recovered, ensure_ascii=False)))
                        pending.update(status='recorded', updatedAt=time.time())
                        conn.execute('UPDATE submission_attempts SET state=?, body=? WHERE id=?',
                                     ('recorded', json.dumps(pending, ensure_ascii=False), pending['id']))
                        stored = conn.execute('SELECT body FROM jobs WHERE id=?', (recovered['id'],)).fetchone()
                    return json.loads(stored[0])
                return pending
            raise ValueError('任务不存在')
        job = json.loads(row[0])
        if job['status'] in ('completed', 'failed', 'cancelled'):
            return preserve_completed_images(job)
        if job.get('lastPolled',0)+8 > time.time():
            return job
        if job.get('provider') == local_images.PROVIDER:
            return save(local_images.poll(job))
        if job.get('provider') == custom_images.PROVIDER:
            updated = custom_images.poll(job)
            save(updated)
            return updated
        response = api('tasks/' + quote(task_id, safe='') + '?language=zh')['data']
        status = response.get('status')
        if status not in ('submitted', 'pending', 'processing', 'completed', 'failed', 'cancelled'):
            raise ValueError('图片接口返回未知任务状态')
        job.update(status=status, progress=response.get('progress', 0), cost=response.get('cost'), error=str(response.get('error', {}).get('message', '')))
        job['lastPolled']=time.time()
        job['images'] = []
        if status == 'completed':
            for item in response.get('result', {}).get('images', []):
                urls = item.get('url', [])
                if isinstance(urls, str):
                    urls = [urls]
                for url in urls:
                    if isinstance(url, str) and urlsplit(url).scheme == 'https':
                        job['images'].append(url)
            if not job['images']:
                job['status'] = 'failed'
                job['error'] = '接口任务完成但未返回有效图片链接，请在服务商后台查看结果'
        save(job)
        return preserve_completed_images(job)
