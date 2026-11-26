import app_paths
"""Local development server. Run: py -3 server.py. No third-party packages."""
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
import provider
import image_provider
import importer
import supplier_capture
import competitors
import review_insights
import product_insights
import sellersprite
import market_research
import accounts
import visual_studio
import assistant_chat
import studio_bridge
import workbench_overview
import workbench_skills
import product_library
import creative_skills
import listing_skills
import product_artwork
import listing_preview
import listing_locks
import listing_jobs
import publication_flow
import codex_operations
import colour_sets
import colour_api
import pixel_recolour
import local_colour_tasks
import ssl
import os
import ipaddress
import hmac
import re
import account_spaces
import agent_center
import agent_intake
import agent_sourcing
import amazon_selection_capture
import discovery_intake
import product_progress

ROOT = Path(__file__).resolve().parent
DATA = app_paths.DATA
DATA.mkdir(exist_ok=True)
(DATA / 'uploads').mkdir(exist_ok=True)
DB = DATA / 'studio.sqlite3'
LOCK = threading.Lock()
STATE_BODY_LIMIT = 64 * 1024 * 1024
CAPTURE_PATHS = {'/api/import/browser', '/api/selection/capture', '/api/selection/capture-seller-store'}
EXTENSION_SESSION_PATHS = {'/api/auth/extension-status', '/api/auth/extension-logout'}
ACCOUNT_ACTIONS = {'/api/auth/setup', '/api/auth/register', '/api/auth/login',
                   '/api/auth/logout', '/api/auth/users', '/api/auth/extension-login'}


def local_mode():
    return not app_paths.WORKER_USER and os.environ.get('JUNFEI_LOCAL_MODE') == '1'


@contextmanager
def database():
    conn = sqlite3.connect(DB)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


with database() as conn:
    conn.execute('CREATE TABLE IF NOT EXISTS workspace (id INTEGER PRIMARY KEY, revision INTEGER NOT NULL, body TEXT NOT NULL)')
    discovery_intake.ensure_schema(conn)


class Handler(BaseHTTPRequestHandler):
    def request_identity(self, path, local):
        auth = self.headers.get('Authorization')
        if auth and (path.startswith('/api/codex/') or auth.startswith('Bearer ')):
            return self.codex_identity(path)
        if path in ('/api/codex/next', '/api/codex/review', '/api/codex/heartbeat', '/api/codex/publication-receipt'):
            return None, None
        user = accounts.local_workspace() if local else accounts.identify(accounts.session_token(self.headers))
        return user, None

    def codex_identity(self, path):
        auth = self.headers.get('Authorization', '')
        if path not in ('/api/codex/status', '/api/codex/next', '/api/codex/review', '/api/codex/receipt', '/api/codex/heartbeat',
                        '/api/codex/publication-receipt') or not auth.startswith('Bearer '):
            return None, None
        result = accounts.identify_codex(auth[7:])
        return result if result else (None, None)

    def codex_route(self, path, user, scope, data=None):
        from urllib.parse import parse_qs

        owner = user['id']
        payload = data if data is not None else {}
        if scope:
            product, market = scope
        elif data is None:
            query = parse_qs(urlsplit(self.path).query)
            product, market = (query.get(key, [''])[0] for key in ('productId', 'market'))
        else:
            product, market = payload.get('productId'), payload.get('market')
        if path in ('/api/codex/pair', '/api/codex/revoke') and data is not None and scope is None:
            _, workspace = codex_operations.read_workspace(database)
            codex_operations.validate_scope(workspace, product, market)
            if path.endswith('/revoke'):
                accounts.revoke_codex(owner, product, market)
                return self.reply(200, {'revoked': True})
            token = accounts.issue_codex(owner, product, market, codex_operations.TTL)
            return self.reply(201, {'token': token, 'expiresIn': codex_operations.TTL})
        if path == '/api/codex/status' and data is None:
            status = codex_operations.status(owner, database, product, market)
            status['proposals'] = codex_operations.list_proposals(owner, product, market)
            return self.reply(200, status)
        if path == '/api/codex/assign' and data is not None and scope is None:
            return self.reply(200, codex_operations.assign(owner, database, product, market, data.get('action')))
        if path == '/api/codex/next' and data is None and scope:
            reviewer = accounts.digest(self.headers['Authorization'][7:])
            return self.reply(200, codex_operations.next_assignment(owner, product, market, reviewer))
        if path == '/api/codex/heartbeat' and data is not None and scope:
            if set(data) != {'requestId'}:
                raise ValueError('续租仅接受原派单编号')
            reviewer = accounts.digest(self.headers['Authorization'][7:])
            return self.reply(200, codex_operations.renew_review(owner, product, market, data['requestId'], reviewer))
        if path == '/api/codex/publication-receipt' and data is None and scope:
            query = parse_qs(urlsplit(self.path).query)
            if set(query) != {'jobId', 'publishId', 'sourceId', 'pipelineJobId'} or any(len(v) != 1 for v in query.values()):
                raise ValueError('请提供原工作台、ERP 和 CLI 任务身份')
            job_id, publish_id, source_id, pipeline_id = (query[k][0] for k in ('jobId', 'publishId', 'sourceId', 'pipelineJobId'))
            _, workspace = codex_operations.read_workspace(database)
            item = codex_operations.validate_scope(workspace, product, market)
            reference = (item.get('selectionResearch') or {}).get('reference') or {}
            links = discovery_intake.links(app_paths.DATA / 'studio.sqlite3', owner, product)
            if (reference.get('asin') != source_id and not any(link['asin'] == source_id for link in links)
                    or links and not any(link['asin'] == source_id and link['job_id'] == pipeline_id
                                        and link['market'] == market for link in links)):
                raise ValueError('CLI 任务不属于该配对商品和站点')
            return self.reply(200, publication_flow.online_receipt(owner, product, market, job_id, publish_id))
        if path == '/api/codex/review' and data is not None and scope:
            reviewer = accounts.digest(self.headers['Authorization'][7:])
            return self.reply(200, codex_operations.submit_review(owner, database, product, market, data, reviewer=reviewer))
        if path == '/api/codex/receipt' and data is None:
            request_id = parse_qs(urlsplit(self.path).query).get('requestId', [''])[0]
            return self.reply(200, codex_operations.receipt(owner, product, market, request_id))
        if path == '/api/codex/resolve-unsubmitted' and data is not None and scope is None:
            return self.reply(200, codex_operations.resolve_submission(owner, product, market,
                              payload.get('requestId'), payload.get('reference')))
        if path == '/api/codex/decline' and data is not None and scope is None:
            return self.reply(200, codex_operations.decline(owner, product, market, payload.get('requestId')))
        if path == '/api/codex/approve' and data is not None and scope is None:
            return self.reply(200, codex_operations.approve(owner, database, product, market, payload.get('requestId')))
        return self.reply(404, {'error': '操作入口不存在'})

    def secure_request(self):
        return isinstance(self.connection, ssl.SSLSocket) or (
            os.environ.get('JUNFEI_TRUST_PROXY') == '1'
            and ipaddress.ip_address(self.client_address[0]).is_loopback
            and self.headers.get('X-Forwarded-Proto') == 'https')

    def origin(self):
        return ('https://' if self.secure_request() else 'http://') + self.headers.get('Host', '')

    def local_request_allowed(self, path):
        host = self.headers.get('Host', '').lower()
        port = self.server.server_port
        if (not ipaddress.ip_address(self.client_address[0]).is_loopback
                or os.environ.get('JUNFEI_TRUST_PROXY') == '1'
                or host not in {f'127.0.0.1:{port}', f'localhost:{port}'}
                or any(name in self.headers for name in ('Forwarded', 'X-Forwarded-For', 'X-Forwarded-Host',
                                                          'X-Forwarded-Proto'))):
            return False
        origin = self.headers.get('Origin', '')
        if self.command == 'POST' and path in CAPTURE_PATHS | EXTENSION_SESSION_PATHS:
            if re.fullmatch(r'chrome-extension://[a-p]{32}', origin):
                return True  # These routes still require the workbench capture token.
        if origin and origin != self.origin():
            return False
        site = self.headers.get('Sec-Fetch-Site')
        if site in (None, 'none', 'same-origin'):
            return True
        return (self.command == 'GET' and path in ('/', '/index.html')
                and self.headers.get('Sec-Fetch-Mode') == 'navigate'
                and self.headers.get('Sec-Fetch-Dest') == 'document')

    def reply(self, status, data, mime='application/json; charset=utf-8', cookie=None, download_name=None):
        payload = json.dumps(data, ensure_ascii=False).encode() if isinstance(data, dict) else data
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        if download_name:
            safe_name = ''.join(ch for ch in str(download_name) if ch.isalnum() or ch in '._-') or 'report.json'
            self.send_header('Content-Disposition', f'attachment; filename="{safe_name}"')
        if cookie:
            self.send_header('Set-Cookie', cookie)
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if app_paths.WORKER_USER and not hmac.compare_digest(self.headers.get('X-Junfei-Internal',''),app_paths.INTERNAL_TOKEN):return self.reply(403,{'error':'Private runtime'})
        path = urlsplit(self.path).path
        local = local_mode()
        if local and not self.local_request_allowed(path):
            return self.reply(403, {'error': '单机工作台只接受本机请求'})
        if local and path in ACCOUNT_ACTIONS:
            return self.reply(404, {'error': '单机工作台直接打开使用'})
        if path == '/api/desktop-health':
            return self.reply(200, {'app': 'amazon-listing-workbench'})
        # Static shell must load before account initialization; this also keeps the desktop bundle usable on first run.
        if path == '/discovery':
            self.send_response(302)
            self.send_header('Location', '/')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        if path in ('/', '/index.html', '/library', '/products', '/quality', '/settings', '/help', '/tasks', '/skills'):
            return self.reply(200, (ROOT / 'index.html').read_bytes(), 'text/html; charset=utf-8')
        user, codex_scope = self.request_identity(path, local)
        if path == '/api/auth/status':
            if local:
                return self.reply(200, {'setup': False, 'user': user, 'localMode': True})
            return self.reply(200, {'setup': accounts.setup_needed(), 'user': user, 'localSetupOnly': not ipaddress.ip_address(self.client_address[0]).is_loopback})
        if (path.startswith('/api/') or path.startswith('/uploads/')) and not user:
            return self.reply(401, {'error': '请先登录'})
        if app_paths.WORKER_USER and (not user or user['id']!=app_paths.WORKER_USER):return self.reply(403,{'error':'工作空间与账号不匹配'})
        if user and self.headers.get('X-Workspace-Account') not in (None,str(user['id'])):return self.reply(409,{'error':'登录账号已变化，请刷新页面','accountChanged':True})
        if account_spaces.needs_proxy(user,path):
            try:return account_spaces.proxy(self,user)
            except Exception:return self.reply(503,{'error':'独立工作空间暂时不可用，请重试'})
        if path.startswith('/api/codex/'):
            try:
                return self.codex_route(path, user, codex_scope)
            except (ValueError, KeyError, TypeError) as error:
                return self.reply(400, {'error': str(error)})
        if path.startswith('/api/image-templates/'):
            from urllib.parse import parse_qs
            import official_image_templates as library
            query=parse_qs(urlsplit(self.path).query)
            kind=query.get('kind',['layout'])[0]
            try:
                if path=='/api/image-templates/catalog':return self.reply(200,library.catalog(kind))
                if path=='/api/image-templates/item':return self.reply(200,library.get(query.get('id',[''])[0],kind))
                if path=='/api/image-templates/image':
                    raw,mime=library.image(query.get('id',[''])[0],kind,query.get('preview')==['1'])
                    return self.reply(200,raw,mime)
                if path=='/api/image-templates/colour':return self.reply(200,library.colour_resources())
            except (ValueError,OSError) as error:return self.reply(400,{'error':str(error)})
            return self.reply(404,{'error':'Unknown official template endpoint'})
        if path=='/api/image-tools/task':
            from urllib.parse import parse_qs
            import image_tools
            try:return self.reply(200,{'task':image_tools.poll(parse_qs(urlsplit(self.path).query).get('id',[''])[0])})
            except ValueError as error:return self.reply(400,{'error':str(error)})
        if path.startswith('/api/image-engine/'):
            from urllib.parse import parse_qs
            import image_engine
            query=parse_qs(urlsplit(self.path).query)
            try:
                if path=='/api/image-engine/variants':
                    import image_source_variants
                    with database() as conn:
                        row=conn.execute('SELECT body FROM workspace WHERE id=1').fetchone()
                    product=next((p for p in (json.loads(row[0]) if row else {}).get('products',[]) if p['id']==query.get('productId',[''])[0]),None)
                    if not product:raise ValueError('当前账号没有该商品')
                    return self.reply(200,image_source_variants.catalog(product))
                if path=='/api/image-engine/sources':
                    import image_workflow_sources
                    return self.reply(200,{'sources':image_workflow_sources.list_sources(query.get('productId',[''])[0])})
                if path == '/api/image-engine/delivery':
                    import erp_collection
                    import image_engine_delivery
                    revision, workspace = codex_operations.read_workspace(database)
                    product = erp_collection.product_for(workspace, query.get('productId', [''])[0])
                    return self.reply(200, image_engine_delivery.view(product, revision))
                if path=='/api/image-engine/tasks':
                    return self.reply(200,{'tasks':image_engine.list_tasks(query.get('productId',[''])[0])})
                if path == '/api/image-engine/request':
                    return self.reply(200, {'task': image_engine.by_request(
                        query.get('productId', [''])[0], query.get('requestId', [''])[0])})
                if path=='/api/image-engine/task':
                    return self.reply(200,{'task':image_engine.poll(query.get('id',[''])[0])})
            except ValueError as error:return self.reply(400,{'error':str(error)})
            return self.reply(404,{'error':'Unknown image engine endpoint'})
        if path.startswith('/api/erp-collection/'):
            import erp_collection
            from urllib.parse import parse_qs
            query=parse_qs(urlsplit(self.path).query)
            try:
                if path=='/api/erp-collection/status':
                    return self.reply(200,{'job':erp_collection.public(erp_collection.owned(user['id'],query.get('id',[''])[0]))})
                if path=='/api/erp-collection/view':
                    with database() as conn:
                        row=conn.execute('SELECT body FROM workspace WHERE id=1').fetchone()
                    return self.reply(200,erp_collection.view(user['id'],json.loads(row[0]) if row else {},query.get('productId',[''])[0]))
            except ValueError as error:return self.reply(400,{'error':str(error)})
            return self.reply(404,{'error':'Unknown collection endpoint'})
        if path.startswith('/api/publication/'):
            from urllib.parse import parse_qs
            query=parse_qs(urlsplit(self.path).query)
            try:
                if path=='/api/publication/status':
                    return self.reply(200,{'job':publication_flow.public(publication_flow.owned(user['id'],query.get('id',[''])[0]))})
                if path=='/api/publication/list':
                    return self.reply(200,{'jobs':publication_flow.list_jobs(user['id'])})
                if path=='/api/publication/view':
                    with database() as conn:
                        row=conn.execute('SELECT body FROM workspace WHERE id=1').fetchone()
                    return self.reply(200,publication_flow.view(user['id'],json.loads(row[0]) if row else {},query.get('productId',[''])[0]))
            except ValueError as error:return self.reply(400,{'error':str(error)})
            return self.reply(404,{'error':'Unknown publication endpoint'})
        if path.startswith('/api/listing-jobs/'):
            from urllib.parse import parse_qs
            query=parse_qs(urlsplit(self.path).query)
            try:
                if path=='/api/listing-jobs/status':
                    return self.reply(200,{'job':listing_jobs.status(user['id'],query.get('id',[''])[0])})
                if path=='/api/listing-jobs/list':
                    return self.reply(200,{'jobs':listing_jobs.list_jobs(user['id'],
                        productId=query.get('productId',[''])[0],market=query.get('market',[''])[0])})
            except (ValueError,KeyError) as error:return self.reply(400,{'error':str(error)})
            return self.reply(404,{'error':'Unknown listing task endpoint'})
        if path == '/api/workbench/overview':
            try:
                return self.reply(200, workbench_overview.overview(DATA, user['id']))
            except (sqlite3.Error, OSError, ValueError, TypeError, KeyError):
                return self.reply(503, {'error':'工作台数据暂时无法读取'})
        if path == '/api/products/progress':
            try:
                return self.reply(200, product_progress.summary(DATA, user['id']))
            except (sqlite3.Error, OSError, ValueError, TypeError, KeyError):
                return self.reply(503, {'error':'产品任务进度暂时无法读取'})
        if path == '/api/workbench/skills':
            try:
                return self.reply(200, workbench_skills.catalog())
            except (sqlite3.Error, OSError, ValueError):
                return self.reply(503, {'error':'技能目录暂时无法读取'})
        if path == '/api/product-library':
            try:
                with database() as conn:
                    row=conn.execute('SELECT body FROM workspace WHERE id=1').fetchone()
                return self.reply(200,product_library.catalog(json.loads(row[0]) if row else {},
                                                               user['role']=='admin'))
            except (OSError, sqlite3.Error, ValueError):
                return self.reply(503,{'error':'共享产品库暂时无法读取'})
        if path == '/api/supplier-capture/status':
            return self.reply(200, supplier_capture.status())
        if path == '/api/product-library/photo':
            from urllib.parse import parse_qs
            query=parse_qs(urlsplit(self.path).query)
            try:
                photo=product_library.photo(query.get('id',[''])[0],query.get('thumbnail')==['1'])
            except (OSError, sqlite3.Error, ValueError):
                photo=None
            if photo is None:
                return self.reply(404,{'error':'共享产品图片不可用'})
            return self.reply(200,*photo)
        if path.startswith('/api/agent/'):
            from urllib.parse import parse_qs
            query = parse_qs(urlsplit(self.path).query)
            try:
                if path == '/api/agent/rotation':
                    import store_rotation
                    return self.reply(200, {'run': store_rotation.status(user['id']),
                                            'history': store_rotation.history(user['id'])})
                if path == '/api/agent/rotation-setup':
                    import store_rotation_setup
                    return self.reply(200, store_rotation_setup.read(user['id'], query.get('setupId', [None])[0]))
                if path == '/api/agent/overview': return self.reply(200, agent_center.overview(user['id']))
                tid = query.get('id', [''])[0]
                if path == '/api/agent/task':
                    task = agent_center.detail(user['id'], tid)
                    result = agent_center.public(task)
                    product = task.get('snapshot', {}).get('product') or {}
                    result['selectionObservations'] = product.get('selectionObservations', [])[-10:]
                    return self.reply(200, result)
                if path == '/api/agent/report':
                    tid = urlsplit(self.path).query.split('id=', 1)[-1].split('&', 1)[0] if 'id=' in urlsplit(self.path).query else ''
                    if not tid: raise ValueError('缺少任务ID')
                    return self.reply(200, agent_center.report(user['id'], tid), download_name=f'agent-report-{tid}.json')
                if path == '/api/agent/image-preparation':
                    revision = query.get('revision', [''])[0]
                    if not revision.isdecimal(): raise ValueError('缺少有效的工作台版本')
                    return self.reply(200, {'preparation': agent_center.image_preparation(user['id'], tid, int(revision))})
                if path == '/api/agent/listing-draft': return self.reply(200, {'copy': agent_center.listing_draft(user['id'], tid)})
                if path == '/api/agent/research-report':
                    import agent_research
                    return self.reply(200, {'report': agent_research.report(user['id'], tid)})
                if path == '/api/agent/research-plan':
                    import agent_research
                    return self.reply(200, {'plan': agent_research.plan_report(user['id'], tid)})
                if path == '/api/agent/sourcing-report': return self.reply(200, {'report':agent_sourcing.report(user['id'],tid)})
                if path == '/api/agent/sourcing-plan': return self.reply(200, {'plan':agent_sourcing.plan_report(user['id'],tid)})
                if path == '/api/agent/download': return self.reply(200, agent_center.download(user['id'], tid), 'application/zip')
                if path == '/api/agent/junfei-plan': return self.reply(200, agent_center.download_junfei_plan(user['id'], tid), 'application/zip')
            except (ValueError, OSError) as error:
                return self.reply(400, {'error': str(error)})
            return self.reply(404, {'error': 'Unknown agent endpoint'})
        if path.startswith('/visual-app/'):
            name=path.removeprefix('/visual-app/')
            file=ROOT/'visual-ui'/'dist'/name
            if name and '..' not in name and '\\' not in name and file.is_file():
                import mimetypes
                return self.reply(200,file.read_bytes(),mimetypes.guess_type(name)[0] or 'application/octet-stream')
            return self.reply(404,{'error':'Visual asset not found'})
        if path in colour_api.GET_PATHS:
            return colour_api.get(self, path, database)
        if path == '/api/studio/view':
            from urllib.parse import parse_qs
            pid=parse_qs(urlsplit(self.path).query).get('productId',[''])[0]
            return self.reply(200,studio_bridge.get_view({'id':pid}))
        if path == '/api/studio/source':
            return self.reply(200,studio_bridge.source_archive(),'application/zip')
        if path == '/api/studio/image':
            from urllib.parse import parse_qs
            try:
                raw,mime=studio_bridge.result_image(parse_qs(urlsplit(self.path).query).get('runId',[''])[0])
                return self.reply(200,raw,mime)
            except Exception:
                return self.reply(400,{'error':'成图读取失败，请查看原始任务记录，链接可能已失效'})
        if path == '/api/studio/psd':
            from urllib.parse import parse_qs
            try:
                raw,name=studio_bridge.export_psd(parse_qs(urlsplit(self.path).query).get('runId',[''])[0])
                return self.reply(200,raw,'image/vnd.adobe.photoshop',download_name=name)
            except (ValueError,KeyError,OSError):
                return self.reply(400,{'error':'此图暂无可导出的 PSD，请检查成图状态'})
        if path == '/api/auth/extension-download':
            import io, zipfile
            buffer = io.BytesIO()
            extension = ROOT / 'extension'
            extension_manifest = json.loads((extension / 'manifest.json').read_text(encoding='utf-8'))
            names = ['manifest.json','popup.html','popup.js','1688-search.js','collector.js','logo.png','README.md',
                     'icons/icon16.png','icons/icon32.png','icons/icon48.png','icons/icon128.png']
            with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
                for name in names:
                    archive.writestr('extension/' + name, (extension / name).read_bytes())
            raw = buffer.getvalue()
            self.send_response(200)
            self.send_header('Content-Type','application/zip')
            self.send_header('Content-Disposition',f'attachment; filename="Junfei-Collector-{extension_manifest["version"]}.zip"')
            self.send_header('Cache-Control','no-store')
            self.send_header('Content-Length',str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if path == '/api/auth/users':
            return self.reply(200, accounts.users()) if user['role'] == 'admin' else self.reply(403, {'error': '仅管理员可管理账号'})
        if path in ('/assets/junfei-logo.png', '/assets/favicon.png'):
            return self.reply(200, (ROOT / path.lstrip('/')).read_bytes(), 'image/png')
        if path == '/api/sellersprite/settings':
            return self.reply(200, {**sellersprite.public(), 'canManage': user['role'] == 'admin'})
        if path.startswith('/api/discovery/'):
            import cyclic_discovery
            import discovery_browser
            from urllib.parse import parse_qs
            query = parse_qs(urlsplit(self.path).query)
            try:
                if path == '/api/discovery/captures': return self.reply(200, discovery_browser.history(user['id']))
                if path == '/api/discovery/capture': return self.reply(200, discovery_browser.detail(user['id'], query.get('id', [''])[0]))
                if path == '/api/discovery/live-run':
                    import discovery_live
                    return self.reply(200, discovery_live.status(user['id'], query.get('id', [''])[0]))
                if path == '/api/discovery/history': return self.reply(200, cyclic_discovery.history(user['id']))
                if path == '/api/discovery/report': return self.reply(200, cyclic_discovery.report(user['id'], query.get('id', [''])[0]))
                if path == '/api/discovery/template': return self.reply(200, cyclic_discovery.template(query.get('market', ['US'])[0]))
            except ValueError as error:
                return self.reply(400, {'error': str(error)})
            return self.reply(404, {'error': 'Unknown discovery endpoint'})
        if path == '/api/selection/history':
            return self.reply(200, market_research.history())
        if path == '/api/selection/report':
            from urllib.parse import parse_qs
            try:return self.reply(200, market_research.get(parse_qs(urlsplit(self.path).query).get('id',[''])[0]))
            except ValueError as e:return self.reply(400, {'error':str(e)})
        if path == '/api/selection/channels':
            from urllib.parse import parse_qs
            return self.reply(200, {'items':market_research.channels(parse_qs(urlsplit(self.path).query).get('market',['US'])[0])})
        if path == '/api/creative-skills':
            return self.reply(200, {**creative_skills.current(),'defaults':creative_skills.DEFAULTS,'editable':True})
        if path == '/api/listing-skills':
            return self.reply(200, {**listing_skills.current(),'defaults':listing_skills.DEFAULTS,'editable':True})
        if path == '/api/listing/export':
            from urllib.parse import parse_qs
            import delivery_export
            pid=parse_qs(urlsplit(self.path).query).get('productId',[''])[0]
            with database() as conn:
                row=conn.execute('SELECT body FROM workspace WHERE id=1').fetchone()
            workspace=json.loads(row[0]) if row else {}
            product=next((p for p in workspace.get('products',[]) if p['id']==pid),None)
            if not product:return self.reply(404,{'error':'产品不存在'})
            market=product.get('sourceImport',{}).get('market') or product.get('market') or 'US'
            try:
                raw=delivery_export.build(product,workspace.get('copy',{}).get(pid+'-'+market,{}))
                return self.reply(200,raw,'application/zip')
            except Exception as e:
                return self.reply(400,{'error':'导出失败：'+str(e)})
        if path == '/api/listing/preview':
            from urllib.parse import parse_qs
            pid=parse_qs(urlsplit(self.path).query).get('productId',[''])[0]
            with database() as conn:
                row=conn.execute('SELECT body FROM workspace WHERE id=1').fetchone()
            workspace=json.loads(row[0]) if row else {}
            p=next((p for p in workspace.get('products',[]) if p['id']==pid),None)
            if not p:return self.reply(404,{'error':'产品不存在'})
            market=p.get('market') or p.get('sourceImport',{}).get('market','UK')
            return self.reply(200,listing_preview.render(p,workspace.get('copy',{}).get(pid+'-'+market,{}),studio_bridge.get_view(p)),'text/html; charset=utf-8')
        if path == '/api/settings':
            return self.reply(200, provider.settings_public())
        if path == '/api/image/settings':
            return self.reply(200, image_provider.public())
        if path == '/api/image/jobs':
            return self.reply(200, image_provider.jobs())
        if path == '/api/image/budget':
            from urllib.parse import parse_qs
            return self.reply(200, image_provider.budget_status(parse_qs(urlsplit(self.path).query).get('productId',[None])[0]))
        if path == '/api/chat/history':
            from urllib.parse import parse_qs
            q=parse_qs(urlsplit(self.path).query)
            try:
                data=assistant_chat.history(q['id'][0],user['id']) if q.get('id') else assistant_chat.sessions(user['id'],q.get('productId',[''])[0])
                return self.reply(200,data)
            except ValueError as e:
                return self.reply(404,{'error':str(e)})
        if path == '/api/visual/projects':
            from urllib.parse import parse_qs
            query=parse_qs(urlsplit(self.path).query)
            result=visual_studio.list_projects(query.get('productId',[''])[0])
            if query.get('preflight')==['1']:
                for project in result['projects']:
                    preview=studio_bridge.native(project,[])
                    project['preflight']={key:preview.get(key) for key in
                                          ('sourceAnalysis','sceneDirectionFields','quality')}
                    project['preflight']['cards']=[{key:card.get(key) for key in
                                                   ('_isScene','sourceIssue','sourceObservation','product_source_url')}
                                                  for card in preview['images']]
            return self.reply(200,result)
        if path == '/api/state':
            with LOCK, database() as conn:
                conn.execute('BEGIN IMMEDIATE')
                row = conn.execute('SELECT revision, body FROM workspace WHERE id=1').fetchone()
                state = json.loads(row[1]) if row else ({'products': [], 'copy': {}, 'reviews': {}} if app_paths.WORKER_USER else None)
                revision = row[0] if row else 0
                if state:
                    before = json.dumps(state, ensure_ascii=False)
                    import product_sku
                    used={p.get('sku') for p in state['products'] if p.get('sku')}
                    for product in state['products']:
                        product_sku.ensure(product,user,used)
                        if product.get('sourceImport', {}).get('supplier', {}).get('attributes'):
                            importer.autofill_supplier(product)
                    after = json.dumps(state, ensure_ascii=False)
                    if before != after:
                        revision += 1
                        conn.execute('UPDATE workspace SET revision=?, body=? WHERE id=1', (revision, after))
            return self.reply(200, {'revision': revision, 'state': state})
        if path == '/ui-polish.css':
            return self.reply(200, (ROOT / 'ui-polish.css').read_bytes(), 'text/css; charset=utf-8')
        if path == '/workbench-design.css':
            return self.reply(200, (ROOT / 'workbench-design.css').read_bytes(), 'text/css; charset=utf-8')
        if path in ('/workbench-help.js', '/workbench-home.js', '/workbench-skills.js', '/product-library.js', '/supplier-capture.js', '/publication-confirmation.js', '/listing-jobs.js', '/publication-tasks.js',
                    '/colour-api.js', '/colour-view-rules.js', '/colour-page.js', '/colour-pixel-tools.js'):
            return self.reply(200, (ROOT / path[1:]).read_bytes(), 'text/javascript; charset=utf-8')
        if path in ('/', '/index.html', '/openai-images.js', '/generated-gallery.js', '/product-editor.js', '/agent-center.js', '/store-rotation-setup.js', '/discovery-ui.js', '/discovery-browser-ui.js', '/selection.js', '/auth.js', '/studio-migration.js', '/colour-sets.js', '/assistant-chat.js', '/visual-studio.js', '/enhancements.js', '/images.js', '/workflow.js', '/import.js', '/competitors.js', '/sellersprite.js'):
            name = path[1:] if path.endswith('.js') else 'index.html'
            return self.reply(200, (ROOT / name).read_bytes(), 'text/javascript; charset=utf-8' if name.endswith('.js') else 'text/html; charset=utf-8')
        if path.startswith('/uploads/'):
            name = path.removeprefix('/uploads/')
            if '/' not in name and '\\' not in name and name.endswith(('.jpg', '.png', '.webp')):
                file = DATA / 'uploads' / name
                if file.is_file():
                    mime = {'.jpg': 'image/jpeg', '.png': 'image/png', '.webp': 'image/webp'}[file.suffix]
                    return self.reply(200, file.read_bytes(), mime)
        self.reply(404, {'error': 'Not found'})

    def read_body(self, limit):
        length = int(self.headers.get('Content-Length', '0'))
        if length <= 0:
            raise ValueError('请求内容为空，请刷新页面后重试')
        if length > limit:
            raise ValueError(f'请求大小 {length // 1024 + 1} KB 超过接口上限 {limit // 1024} KB，请刷新页面后重试')
        body = self.rfile.read(length)
        if len(body) != length:
            raise ValueError('请求未完整接收')
        return body

    def do_POST(self):
        if app_paths.WORKER_USER and not hmac.compare_digest(self.headers.get('X-Junfei-Internal',''),app_paths.INTERNAL_TOKEN):return self.reply(403,{'error':'Private runtime'})
        path = urlsplit(self.path).path
        local = local_mode()
        if local and not self.local_request_allowed(path):
            return self.reply(403, {'error': '单机工作台只接受本机请求'})
        if local and path in ACCOUNT_ACTIONS:
            return self.reply(404, {'error': '单机工作台直接打开使用'})
        # Prevent cross-origin browser writes to the local development server.
        origin = self.headers.get('Origin')
        extension_capture = path in CAPTURE_PATHS | EXTENSION_SESSION_PATHS | {'/api/auth/extension-login'} and origin and origin.startswith('chrome-extension://')
        if origin and origin != self.origin() and not extension_capture:
            return self.reply(403, {'error': 'Origin rejected'})
        try:
            if path == '/api/auth/extension-login':
                data = json.loads(self.read_body(4096))
                token, user = accounts.login(data, self.client_address[0])
                try:
                    paired = accounts.capture_token(user)
                finally:
                    accounts.logout(token)
                return self.reply(200, {'user': user, 'token': paired['token']})
            if path in EXTENSION_SESSION_PATHS:
                token = self.headers.get('X-Capture-Token', '')
                user = accounts.identify(token, 'capture')
                if local and user and user['id'] != accounts.local_workspace()['id']:
                    user = None
                if not user: return self.reply(401, {'error': '插件登录已过期或账号已停用，请重新登录'})
                if path.endswith('/extension-logout'):
                    accounts.logout(token)
                    return self.reply(200, {'message': '插件已退出登录'})
                return self.reply(200, {'user': user})
            if path in ('/api/auth/setup', '/api/auth/register', '/api/auth/login'):
                data = json.loads(self.read_body(4096))
                if path.endswith('/setup') and (not ipaddress.ip_address(self.client_address[0]).is_loopback or os.environ.get('JUNFEI_TRUST_PROXY') == '1'):
                    return self.reply(403, {'error': '请在工作台主机的本机页面创建首位管理员'})
                if path.endswith('/login'):
                    token, user = accounts.login(data, self.client_address[0])
                    return self.reply(200, {'user': user}, cookie='studio_session='+token+'; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200'+('; Secure' if self.secure_request() else ''))
                return self.reply(201, accounts.register(data, setup=path.endswith('/setup')))
            user, codex_scope = self.request_identity(path, local)
            capture_token = self.headers.get('X-Capture-Token', '')
            if path in CAPTURE_PATHS and (capture_token or local):
                capture_user = accounts.identify(capture_token, 'capture')
                if local and capture_user and capture_user['id'] != user['id']:
                    capture_user = None
                user = capture_user
            if not user:
                return self.reply(401, {'error': '请先登录；采集插件请填写工作台生成的配对码'})
            if self.headers.get('X-Workspace-Account') not in (None,str(user['id'])):return self.reply(409,{'error':'登录账号已变化，请刷新页面','accountChanged':True})
            if app_paths.WORKER_USER and user['id']!=app_paths.WORKER_USER:return self.reply(403,{'error':'工作空间与账号不匹配'})
            if account_spaces.needs_proxy(user,path):return account_spaces.proxy(self,user)
            if path.startswith('/api/codex/'):
                limit = 4 * 1024 * 1024 if codex_scope and path == '/api/codex/review' else 16 * 1024
                data = json.loads(self.read_body(limit))
                if not isinstance(data, dict):
                    raise ValueError('操作参数格式无效')
                return self.codex_route(path, user, codex_scope, data)
            if path in ('/api/image-tools/start','/api/image-tools/retry'):
                import image_tools
                data=json.loads(self.read_body(32*1024))
                with database() as conn:row=conn.execute('SELECT body FROM workspace WHERE id=1').fetchone()
                product=next((p for p in (json.loads(row[0]) if row else {}).get('products',[])if p['id']==data.get('productId')),None)
                if not product:raise ValueError('当前账号没有该商品')
                return self.reply(200,{'task':image_tools.retry_known_failure(data.get('taskId',''),product) if path.endswith('/retry') else image_tools.start(data,product)})
            if path in ('/api/image-templates/prepare-mask','/api/image-templates/prepare-source','/api/image-templates/colour-recipe'):
                data=json.loads(self.read_body(4096))
                with database() as conn:
                    row=conn.execute('SELECT body FROM workspace WHERE id=1').fetchone()
                product=next((p for p in (json.loads(row[0]) if row else {}).get('products',[]) if p['id']==data.get('productId')),None)
                if not product:raise ValueError('当前账号没有该商品')
                if path.endswith('/prepare-source'):
                    import official_image_templates
                    return self.reply(200,official_image_templates.canonical_colour_source(data.get('source')))
                if path.endswith('/colour-recipe'):
                    import image_engine,sku_colour_recipe
                    source,_=image_engine.asset(data.get('source'));mask,_=image_engine.asset(data.get('mask'))
                    if source.size!=mask.size:raise ValueError('色样选区尺寸与原图不一致')
                    if data.get('renderedImage'):
                        preview,_=image_engine.asset(data['renderedImage'])
                        return self.reply(200,sku_colour_recipe.feedback(source,mask,data.get('target'),preview,data.get('settings')))
                    return self.reply(200,sku_colour_recipe.compile(source,mask,data.get('target')))
                import image_engine, garment_colour_mask, garment_auto
                kind=garment_auto.category(product)
                if kind=='auto':raise ValueError('非服装商品请明确涂选部件')
                source,sha=image_engine.asset(data.get('source'))
                protection=image_engine.asset(data['protected'])[0] if data.get('protected') else None
                mask,receipt=garment_colour_mask.prepare(source,kind,protected=protection)
                return self.reply(200,{'mask':image_engine.cache(mask),'sourceSha256':sha,'receipt':receipt})
            if path=='/api/image-engine/prepare-variant-source':
                import image_source_variants
                data=json.loads(self.read_body(8192))
                if not isinstance(data,dict):raise ValueError('颜色选图参数无效')
                with database() as conn:
                    row=conn.execute('SELECT body FROM workspace WHERE id=1').fetchone()
                product=next((p for p in (json.loads(row[0]) if row else {}).get('products',[]) if p['id']==data.get('productId')),None)
                if not product:raise ValueError('当前账号没有该商品')
                return self.reply(200,image_source_variants.prepare(product,data))
            if path == '/api/image-engine/delivery':
                import erp_collection
                import image_engine_delivery
                data = json.loads(self.read_body(16 * 1024))
                if not isinstance(data, dict):
                    raise ValueError('图库确认参数无效')
                with LOCK:
                    revision, workspace = codex_operations.read_workspace(database)
                    if type(data.get('expectedRevision')) is not int or revision != data['expectedRevision']:
                        return self.reply(409, {'error': '商品资料已变化，请重新核对图库'})
                    product = erp_collection.product_for(workspace, data.get('productId'))
                    receipt = image_engine_delivery.approve(user['id'], product, data)
                return self.reply(200, {'selection': receipt})
            if path=='/api/image-engine/start':
                import image_engine
                data=json.loads(self.read_body(32*1024))
                with database() as conn:
                    row=conn.execute('SELECT body FROM workspace WHERE id=1').fetchone()
                product=next((p for p in (json.loads(row[0]) if row else {}).get('products',[])
                              if p['id']==data.get('productId')),None)
                if not product:raise ValueError('请选择当前账号已有的商品')
                return self.reply(202,{'task':image_engine.start(data,product)})
            if path in ('/api/listing-jobs/start' ,'/api/listing-jobs/apply','/api/listing-jobs/resume-review',
                        '/api/listing-jobs/continue-returned-review','/api/listing-jobs/continue-exhausted-review',
                        '/api/listing-jobs/continue-format'):
                data=json.loads(self.read_body(16*1024))
                if not isinstance(data,dict) or path.endswith('/apply') and set(data)-{'id'}:
                    raise ValueError('文案任务参数无效')
                import listing_format_recovery
                result=(listing_format_recovery.start(user['id'],data) if path.endswith('/continue-format') else
                        listing_jobs.start(user['id'],data) if path.endswith('/start') else
                        listing_jobs.resume_review(user['id'],data) if path.endswith('/resume-review') else
                        listing_jobs.continue_returned_review(user['id'],data) if path.endswith('/continue-returned-review') else
                        listing_jobs.continue_exhausted_review(user['id'],data) if path.endswith('/continue-exhausted-review') else
                        listing_jobs.apply(user['id'],data.get('id','')))
                return self.reply(202 if not path.endswith('/apply') and not result.get('reused') else 200,{'job':result})
            if path=='/api/erp-collection/start':
                import erp_collection
                data=json.loads(self.read_body(16*1024))
                with database() as conn:
                    row=conn.execute('SELECT body FROM workspace WHERE id=1').fetchone()
                result=erp_collection.start(user['id'],json.loads(row[0]) if row else {},data)
                return self.reply(200 if result.get('reused') else 202,{'job':result})
            if path in ('/api/publication/start','/api/publication/continue','/api/publication/settings','/api/publication/commerce',
                        '/api/publication/preview','/api/publication/observe','/api/publication/setup-confirmation'):
                data=json.loads(self.read_body(32*1024))
                if not isinstance(data, dict):
                    raise ValueError('发布参数无效')
                if path.endswith('/setup-confirmation'):
                    if not local or not origin or set(data) != {'password'}:
                        return self.reply(403, {'error': '只能在本机工作台页面初始化发布确认密码'})
                    accounts.set_local_confirmation(user['id'], data['password'])
                    return self.reply(200, {'configured': True})
                if path.endswith('/preview'):
                    if set(data) != {'action', 'request'}:
                        raise ValueError('发布预检参数无效')
                    _, workspace = codex_operations.read_workspace(database)
                    preview = publication_flow.preview(user['id'], workspace, data['action'], data['request'])
                    return self.reply(200, {'preview': preview,
                                            'needsLocalConfirmationPassword': local and accounts.local_confirmation_unset(user['id'])})
                if path.endswith('/observe'):
                    if set(data) != {'id'}:
                        raise ValueError('只读核对仅接受原发布任务编号')
                    result = publication_flow.observe(user['id'], data['id'])
                    return self.reply(200 if result.get('reused') else 202, {'job': result})
                if path.endswith('/settings'):
                    return self.reply(200,{'target':publication_flow.save_settings(user['id'],data)})
                if path.endswith('/commerce'):
                    with LOCK,database() as conn:
                        conn.execute('BEGIN IMMEDIATE')
                        row=conn.execute('SELECT revision,body FROM workspace WHERE id=1').fetchone()
                        revision=row[0] if row else 0
                        if not isinstance(data,dict) or type(data.get('revision')) is not int or data.get('revision')!=revision:
                            return self.reply(409,{'error':'资料已更新，请刷新后保存英国经营资料'})
                        workspace=json.loads(row[1]) if row else {}
                        publication_flow.update_commerce(workspace,data)
                        conn.execute('UPDATE workspace SET revision=?,body=? WHERE id=1',
                                     (revision+1,json.dumps(workspace,ensure_ascii=False)))
                    return self.reply(200,{'revision':revision+1,'state':workspace})
                digest = data.pop('confirmationDigest', None)
                password = data.pop('operatorPassword', None)
                if not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest):
                    raise ValueError('请先预检并确认当前发布内容')
                if 'observeOnly' in data:
                    raise ValueError('只读核对请使用原发布回执入口')
                accounts.confirm_operator(user, password, self.client_address[0])
                if path.endswith('/continue'):
                    result = publication_flow.resume(user['id'], data, confirmation=digest)
                else:
                    with LOCK:
                        _, workspace = codex_operations.read_workspace(database)
                        result = publication_flow.start(user['id'], workspace, data, confirmation=digest)
                return self.reply(200 if result.get('reused') else 202,{'job':result})
            if path in ('/api/product-library/add', '/api/product-library/review-supplement'):
                if user['role']!='admin':
                    return self.reply(403,{'error':'只有管理员可以添加共享产品'})
                data=json.loads(self.read_body(16384))
                with database() as conn:
                    row=conn.execute('SELECT body FROM workspace WHERE id=1').fetchone()
                p=next((p for p in (json.loads(row[0]) if row else {}).get('products',[])
                        if p['id']==data.get('id')),None)
                if p is None:
                    raise ValueError('请选择当前账号已有的产品档案')
                if path.endswith('/review-supplement'):
                    import discovery_review
                    return self.reply(200, discovery_review.supplement(p, data.get('report') or {}))
                return self.reply(200,{'created':product_library.add_product(p,DATA/'uploads')})
            if path == '/api/product-library/claim':
                data=json.loads(self.read_body(4096))
                with LOCK, database() as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    row=conn.execute('SELECT revision,body FROM workspace WHERE id=1').fetchone()
                    revision=row[0] if row else 0
                    if data.get('revision')!=revision:
                        return self.reply(409,{'error':'个人档案已更新，请刷新后认领'})
                    workspace=json.loads(row[1]) if row else {'products':[],'copy':{},'reviews':{}}
                    pid,created=product_library.claim(workspace,user,data.get('id'),DATA/'uploads')
                    if created:
                        revision+=1
                        conn.execute('INSERT OR REPLACE INTO workspace VALUES(1,?,?)',
                                     (revision,json.dumps(workspace,ensure_ascii=False)))
                return self.reply(200,{'productId':pid,'created':created,'revision':revision,'state':workspace})
            if path == '/api/products/export-table':
                import product_table_export
                data = json.loads(self.read_body(64 * 1024))
                if not isinstance(data, dict): raise ValueError('请求格式无效')
                with LOCK, database() as conn:
                    row = conn.execute('SELECT body FROM workspace WHERE id=1').fetchone()
                state = json.loads(row[0]) if row else {'products':[], 'copy':{}}
                result = product_table_export.build(state, data.get('ids'), self.origin())
                return self.reply(200, result, product_table_export.MIME, download_name='products.xlsx')
            if path.startswith('/api/agent/'):
                data = json.loads(self.read_body(64 * 1024 if path == '/api/agent/rotation-setup' else 16 * 1024))
                if not isinstance(data, dict): raise ValueError('请求格式无效')
                if path == '/api/agent/rotation':
                    import store_rotation
                    with LOCK:
                        _, workspace = codex_operations.read_workspace(database)
                        run = store_rotation.update(user['id'], workspace, data)
                    return self.reply(200, {'run': run})
                if path == '/api/agent/rotation-setup':
                    import store_rotation_setup
                    try: return self.reply(200, store_rotation_setup.save(user['id'], data))
                    except store_rotation_setup.Conflict as error: return self.reply(409, {'error': str(error)})
                if path == '/api/agent/profile': return self.reply(200, {'profile': agent_center.save_profile(user['id'], data)})
                if path == '/api/agent/create': return self.reply(201, {'task': agent_center.create(user['id'], data)})
                if path == '/api/agent/prepare-copy-handoff': return self.reply(200, {'task': agent_center.prepare_copy_handoff(user['id'], data.get('id'), data)})
                if path == '/api/agent/recheck': return self.reply(200, {'task': agent_center.recheck(user['id'], data.get('id'))})
                if path == '/api/agent/refresh-preflight': return self.reply(200, {'task': agent_center.refresh_local_preflight(user['id'], data.get('id'))})
                if path == '/api/agent/observe-erp': return self.reply(200, {'observation': agent_center.observe_erp(user['id'], data.get('id'))})
                if path == '/api/agent/research-asins': return self.reply(200, {'task': agent_center.set_research_asins(user['id'], data.get('id'), data.get('researchAsins', ''))})
                if path == '/api/agent/adopt-sourcing-candidate': return self.reply(200, {'task': agent_center.adopt_sourcing_candidate(user['id'], data.get('id'), data.get('url', ''))})
                if path == '/api/agent/verify-sourcing-candidate':
                    return self.reply(200, {'task': agent_center.verify_sourcing_candidate(
                        user['id'], data.get('id'), data.get('url', ''), data.get('checks'), data.get('note', ''))})
                if path == '/api/agent/adopt-listing': return self.reply(200, agent_center.adopt_listing(user['id'], data.get('id')))
                if path == '/api/agent/start': return self.reply(202, {'task': agent_center.start(user['id'], data.get('id'))})
                if path == '/api/agent/save-library-draft':
                    if (not ipaddress.ip_address(self.client_address[0]).is_loopback
                            or os.environ.get('JUNFEI_TRUST_PROXY') == '1'):
                        return self.reply(403, {'error': '产品库草稿入口仅在本机工作台可用'})
                    return self.reply(202, {'task': agent_center.start(user['id'], data.get('id'), save_library=True)})
                if path == '/api/agent/start-queued': return self.reply(202, agent_center.start_queued(user['id']))
                if path == '/api/agent/pause-running': return self.reply(200, agent_center.pause_running(user['id']))
                if path == '/api/agent/resume-after-review':
                    return self.reply(200, {'task': agent_center.resume_after_review(user['id'], data.get('id'), data.get('reconciliation', ''))})
                if path == '/api/agent/pause': return self.reply(200, {'task': agent_center.pause(user['id'], data.get('id'))})
                return self.reply(404, {'error': 'Unknown agent endpoint'})
            if path == '/api/auth/logout':
                accounts.logout(accounts.session_token(self.headers))
                return self.reply(200, {'message': '已退出'}, cookie='studio_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0')
            if path == '/api/auth/capture-token':
                return self.reply(200, accounts.capture_token(user))
            if path == '/api/auth/users' and user['role'] != 'admin':return self.reply(403,{'error':'仅管理员可审核账号'})
            if path == '/api/auth/users':
                return self.reply(200, accounts.update(json.loads(self.read_body(4096)), user))
            if path == '/api/creative-skills':
                return self.reply(200, creative_skills.save(json.loads(self.read_body(128*1024))))
            if path in ('/api/discovery/capture-seller', '/api/discovery/capture-store', '/api/discovery/capture-search', '/api/discovery/capture-family'):
                import discovery_browser
                data = json.loads(self.read_body(4096))
                if not isinstance(data, dict): raise ValueError('Invalid capture request')
                capture = {'/api/discovery/capture-seller': discovery_browser.capture,
                           '/api/discovery/capture-store': discovery_browser.capture_store,
                           '/api/discovery/capture-search': discovery_browser.capture_search,
                           '/api/discovery/capture-family': discovery_browser.capture_family}[path]
                return self.reply(200, capture(user['id'], data))
            if path == '/api/discovery/start-live':
                import discovery_live
                data = json.loads(self.read_body(4096))
                if not isinstance(data, dict): raise ValueError('Invalid isolated run request')
                return self.reply(202, discovery_live.start(user['id'], data))
            if path == '/api/discovery/stop-live':
                import discovery_live
                data = json.loads(self.read_body(4096))
                if not isinstance(data, dict) or set(data) != {'runId', 'cancel'} or type(data['cancel']) is not bool:
                    raise ValueError('Expected runId and boolean cancel only')
                return self.reply(200, discovery_live.stop(user['id'], data['runId'], cancel=data['cancel']))
            if path == '/api/discovery/assess-families':
                import discovery_family_bridge
                data = json.loads(self.read_body(4096))
                if not isinstance(data, dict): raise ValueError('Invalid family request')
                return self.reply(200, discovery_family_bridge.assess(user['id'], data))
            if path == '/api/discovery/inspect-run':
                import discovery_run
                data = json.loads(self.read_body(4096))
                if not isinstance(data, dict): raise ValueError('Invalid run inspection request')
                return self.reply(200, discovery_run.inspect(user['id'], data))
            if path == '/api/discovery/prepare-plan':
                import discovery_plan
                data = json.loads(self.read_body(4096))
                if not isinstance(data, dict): raise ValueError('Invalid plan request')
                return self.reply(200, discovery_plan.prepare(user['id'], data))
            if path == '/api/discovery/prepare-keyword':
                import cyclic_discovery
                data = json.loads(self.read_body(4096))
                if not isinstance(data, dict): raise ValueError('Invalid keyword request')
                import discovery_live
                discovery_live.assert_owner_idle(user['id'])
                return self.reply(200, cyclic_discovery.prepare_keyword(user['id'], data))
            if path == '/api/discovery/ingest':
                import cyclic_discovery
                data = json.loads(self.read_body(1024 * 1024))
                if not isinstance(data, dict): raise ValueError('发现证据格式无效')
                import discovery_live
                with discovery_live.owner_write_guard(user['id']):
                    result = cyclic_discovery.ingest(user['id'], data)
                return self.reply(200, result)
            if path == '/api/selection/start':
                return self.reply(202, market_research.start(json.loads(self.read_body(16*1024))))
            if path == '/api/selection/capture':
                data = json.loads(self.read_body(16 * 1024))
                observation = amazon_selection_capture.validate(data)
                with LOCK, database() as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    row = conn.execute('SELECT revision, body FROM workspace WHERE id=1').fetchone()
                    state = json.loads(row[1]) if row else {'products': [], 'copy': {}, 'reviews': {}}
                    product_id, count = amazon_selection_capture.attach(state, observation)
                    conn.execute('INSERT OR REPLACE INTO workspace VALUES (1, ?, ?)', ((row[0] if row else 0) + 1, json.dumps(state, ensure_ascii=False)))
                return self.reply(200, {'message': '页面观察记录已保存；请在原选品任务点击“更新资料重检”加载新快照。该记录不自动满足准入条件。', 'productId': product_id, 'observationCount': count})
            if path == '/api/selection/capture-seller-store':
                data = json.loads(self.read_body(256 * 1024))
                observation = amazon_selection_capture.validate_seller_store(data)
                with LOCK, database() as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    row = conn.execute('SELECT revision, body FROM workspace WHERE id=1').fetchone()
                    state = json.loads(row[1]) if row else {'products': [], 'copy': {}, 'reviews': {}}
                    _, count = amazon_selection_capture.attach_seller_store(state, observation)
                    conn.execute('INSERT OR REPLACE INTO workspace VALUES (1, ?, ?)', ((row[0] if row else 0) + 1, json.dumps(state, ensure_ascii=False)))
                report = market_research.save_seller_store_capture(observation)
                return self.reply(200, {'message': '店铺可见商品已保存为本账号选品观察，可在选品调研历史中加入智能体核验；不代表近7天新品或合格同行店铺。', 'reportId': report['id'], 'productCount': len(observation['products']), 'observationCount': count})
            if path == '/api/selection/freight':
                return self.reply(200, market_research.freight(json.loads(self.read_body(16*1024))))
            if path == '/api/discovery/promote':
                data = json.loads(self.read_body(16 * 1024))
                if (not isinstance(data, dict) or set(data) != {'cycleId', 'expectedRevision', 'productKeys', 'revision'}
                        or type(data['revision']) is not int):
                    raise ValueError('请求格式无效')
                with agent_center.LOCK, LOCK, database() as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    row = conn.execute('SELECT revision,body FROM workspace WHERE id=1').fetchone()
                    revision = row[0] if row else 0
                    if revision != data['revision']:
                        return self.reply(409, {'error': '产品库已变化，请刷新后重新入库'})
                    workspace = json.loads(row[1]) if row else {'products': [], 'copy': {}, 'reviews': {}}
                    outcomes = discovery_intake.promote(conn, DATA, user, data, workspace,
                                                        agent_center.occupied_products(user['id']))
                    if any(item['status'] in ('created', 'linked') for item in outcomes):
                        revision += 1
                        conn.execute('INSERT OR REPLACE INTO workspace VALUES(1,?,?)',
                                     (revision, json.dumps(workspace, ensure_ascii=False)))
                return self.reply(200, {'outcomes': outcomes, 'revision': revision, 'state': workspace})
            if path == '/api/selection/import':
                data=json.loads(self.read_body(16*1024))
                if not isinstance(data,dict):raise ValueError('请求格式无效')
                auto_selection = None
                if data.get('autoSelect') is True:
                    auto_selection = market_research.auto_select_candidates(data.get('reportId'), data.get('limit', 10))
                    data['asins'] = [item['asin'] for item in auto_selection['candidates'] if item.get('asin')]
                    if not data['asins']:
                        raise ValueError('当前报告没有可加入核验队列的候选')
                intake = None
                if data.get('queueAgent') is True:
                    intake=agent_intake.validate(user['id'],data.get('reportId'),data.get('asins'),data.get('profileId'))
                with LOCK, database() as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    row=conn.execute('SELECT revision,body FROM workspace WHERE id=1').fetchone()
                    revision=row[0] if row else 0
                    if data.get('revision')!=revision:return self.reply(409, {'error':'产品库已变化，请刷新后重新入库'})
                    workspace=json.loads(row[1]) if row else {'products':[],'copy':{},'reviews':{}}
                    result=market_research.import_products(data.get('reportId'),data.get('asins'),workspace,user)
                    revision+=1
                    conn.execute('INSERT OR REPLACE INTO workspace VALUES(1,?,?)',(revision,json.dumps(workspace,ensure_ascii=False)))
                if intake:
                    result['agentIntake']=agent_intake.enqueue(user['id'],intake[0],data.get('reportId'),intake[1])
                if auto_selection:
                    result['autoSelection'] = auto_selection
                return self.reply(200,{**result,'revision':revision,'state':workspace})
            if path == '/api/listing-skills':
                return self.reply(200, listing_skills.save(json.loads(self.read_body(128*1024))))
            if path in colour_api.POST_PATHS:
                return colour_api.post(self, path, user, database, LOCK)
            if path.startswith('/api/studio/'):
                data=json.loads(self.read_body(256*1024))
                with database() as conn:
                    row=conn.execute('SELECT body FROM workspace WHERE id=1').fetchone()
                workspace=json.loads(row[0]) if row else {}
                p=next((p for p in workspace.get('products',[]) if p['id']==data.get('productId')),None)
                if not p:raise ValueError('请先保存产品资料')
                if path in ('/api/studio/plan','/api/studio/render'):
                    market=p.get('market') or p.get('sourceImport',{}).get('market','US')
                    action=studio_bridge.start_plan if path.endswith('/plan') else studio_bridge.render
                    return self.reply(200,action(data,p,workspace.get('copy',{}).get(p['id']+'-'+market,{})))
                if path == '/api/studio/review-image':
                    import studio_artwork_review
                    return self.reply(200, studio_artwork_review.start(data, p))
                action={'/api/studio/save':studio_bridge.persist,'/api/studio/review-set':studio_bridge.review_set,'/api/studio/source-layout':product_artwork.start}.get(path)
                if action:return self.reply(200,action(data,p))
                return self.reply(404,{'error':'Unknown studio action'})
            if path in ('/api/chat/send','/api/chat/execute'):
                data=json.loads(self.read_body(64*1024))
                with database() as conn:
                    row=conn.execute('SELECT body FROM workspace WHERE id=1').fetchone()
                workspace=json.loads(row[0]) if row else {}
                p=next((p for p in workspace.get('products',[]) if p['id']==data.get('productId')),None)
                if data.get('productId') and not p:
                    raise ValueError('关联产品已不存在，请重新选择产品')
                market=(p.get('market') or p.get('sourceImport',{}).get('market','US')) if p else 'US'
                listing=workspace.get('copy',{}).get(p['id']+'-'+market,{}) if p else {}
                action=assistant_chat.send if path.endswith('/send') else assistant_chat.execute
                return self.reply(200,action(data,user,p,listing))
            if path.startswith('/api/visual/'):
                data=json.loads(self.read_body(128*1024))
                if path == '/api/visual/plan':
                    with database() as conn:
                        row=conn.execute('SELECT body FROM workspace WHERE id=1').fetchone()
                    state=json.loads(row[0]) if row else {}
                    p=next((p for p in state.get('products',[]) if p['id']==data.get('productId')),None)
                    if not p:
                        raise ValueError('请先保存产品资料')
                    market=p.get('market') or p.get('sourceImport',{}).get('market','US')
                    return self.reply(200,visual_studio.plan(data,p,state.get('copy',{}).get(p['id']+'-'+market,{})))
                action={'/api/visual/save':visual_studio.update,'/api/visual/generate':visual_studio.generate,'/api/visual/approve':visual_studio.approve}.get(path)
                if action:
                    return self.reply(200,action(data))
                return self.reply(404,{'error':'Unknown visual action'})
            if path == '/api/credentials/reveal':
                if origin != self.origin():
                    return self.reply(403, {'error': '仅允许本软件设置页查看密钥'})
                service = json.loads(self.read_body(1024)).get('service')
                if service not in ('deepseek', 'apimart', 'sellersprite'):
                    raise ValueError('未知密钥服务')
                if service == 'sellersprite' and user['role'] != 'admin':
                    return self.reply(403, {'error': '共用 MCP 密钥仅管理员可查看'})
                key = provider.SETTINGS['api_key'] if service == 'deepseek' else image_provider.KEY if service == 'apimart' else sellersprite.current_key()
                return self.reply(200, {'key': key})
            if path in ('/api/sellersprite/settings', '/api/sellersprite/test'):
                data = json.loads(self.read_body(16384))
                if user['role'] != 'admin' and (path.endswith('/settings') or data.get('api_key') or data.get('clearKey')):
                    return self.reply(403, {'error': '全员共用卖家精灵 MCP，由管理员统一配置'})
                action = sellersprite.configure if path.endswith('/settings') else sellersprite.test_key
                return self.reply(200, action(data))
            if path == '/api/listing/rewrite':
                data = json.loads(self.read_body(128 * 1024))
                if data.get('productId'):
                    with database() as conn:
                        row = conn.execute('SELECT body FROM workspace WHERE id=1').fetchone()
                    workspace = json.loads(row[0]) if row else {}
                    pid = data['productId']
                    if not any(p['id'] == pid for p in workspace.get('products', [])):
                        raise ValueError('当前账号没有该产品')
                    key = f"{pid}-{data.get('market')}"
                    if data.get('field') in listing_locks.locked(workspace.get('copy', {}).get(key, {})):
                        raise ValueError('该字段已锁定，请先解锁再改写')
                return self.reply(200, provider.rewrite_field(data))
            if path == '/api/translate':
                return self.reply(200, provider.translate_text(json.loads(self.read_body(128 * 1024))))
            if path in ('/api/reviews/fetch', '/api/reviews/analyse'):
                action = review_insights.collect if path.endswith('/fetch') else review_insights.analyse
                return self.reply(200, action(json.loads(self.read_body(2 * 1024 * 1024))))
            if path in ('/api/asin-reference/capture', '/api/asin-reference/analyse'):
                import asin_reference
                import ivyea_asin_insights
                data = json.loads(self.read_body(8192))
                with database() as conn:
                    row = conn.execute('SELECT body FROM workspace WHERE id=1').fetchone()
                workspace = json.loads(row[0]) if row else {}
                product = next((p for p in workspace.get('products', []) if p['id']==data.get('productId')), None)
                if not product:
                    raise ValueError('请先保存当前账号的产品')
                if path.endswith('/analyse'):
                    return self.reply(200, ivyea_asin_insights.analyze(product))
                previous = product.get('editorAsinReference') or {}
                result = asin_reference.capture(data.get('asin'), data.get('market'), previous)
                if previous.get('insight', {}).get('source') == ivyea_asin_insights.source(result):
                    result['insight'] = previous['insight']
                return self.reply(200, result)
            if path == '/api/product-insights':
                data = json.loads(self.read_body(16384))
                with database() as conn:
                    row = conn.execute('SELECT body FROM workspace WHERE id=1').fetchone()
                workspace = json.loads(row[0]) if row else {}
                product = next((p for p in workspace.get('products',[]) if p['id']==data.get('productId')),None)
                if not product:
                    raise ValueError('请先保存当前账号的产品')
                action = data.get('action')
                if action == 'view':
                    result = product_insights.report(product)
                elif action == 'analyse':
                    result = product_insights.analyse(product,data.get('url'))
                elif action == 'confirm':
                    result = product_insights.confirm(product,data)
                else:
                    raise ValueError('未知素材洞察操作')
                return self.reply(200,result)
            if path == '/api/competitors/analyse':
                return self.reply(200, competitors.analyse(json.loads(self.read_body(128 * 1024))))
            if path == '/api/listing/ivyea-trial':
                import ivyea_listing_trial
                data = json.loads(self.read_body(8192))
                with database() as conn:
                    row = conn.execute('SELECT body FROM workspace WHERE id=1').fetchone()
                workspace = json.loads(row[0]) if row else {}
                product = next((p for p in workspace.get('products', []) if p['id'] == data.get('productId')), None)
                market = data.get('market')
                if not product or market not in provider.MARKET_LANGUAGES:
                    raise ValueError('请选择当前账号的产品及站点')
                return self.reply(200, ivyea_listing_trial.run(product, market))
            if path == '/api/import/browser':
                data = json.loads(self.read_body(1024 * 1024))
                with LOCK, database() as conn:
                    row = conn.execute('SELECT revision, body FROM workspace WHERE id=1').fetchone()
                    state = json.loads(row[1]) if row else {'products': [], 'copy': {}, 'reviews': {}}
                    product_id = importer.merge_browser_capture(state, data)
                    import product_sku
                    used = {p.get('sku') for p in state['products'] if p.get('sku')}
                    captured = next(p for p in state['products'] if p['id'] == product_id)
                    product_sku.ensure(captured, user, used)
                    conn.execute('INSERT OR REPLACE INTO workspace VALUES (1, ?, ?)', ((row[0] if row else 0) + 1, json.dumps(state, ensure_ascii=False)))
                try:
                    supplier_capture.remember(captured['sourceImport']['supplier'])
                except (ValueError, sqlite3.Error, OSError):
                    pass  # The product was saved; it is also a cache source on the next read.
                return self.reply(200, {'message': '商品资料已保存，SKU：' + captured['sku'], 'productId': product_id, 'sku': captured['sku']})
            if path == '/api/supplier-capture/resume':
                data = json.loads(self.read_body(4096))
                if data.get('confirmed') is not True:
                    raise ValueError('请先在原采集窗口完成验证，再确认继续')
                if type(data.get('reconnectMissingBrowser', False)) is not bool:
                    raise ValueError('重连丢失工作区选项必须为布尔值')
                return self.reply(200, supplier_capture.resume(
                    reconnect_missing_browser=data.get('reconnectMissingBrowser', False)))
            if path == '/api/supplier-capture/result':
                data = json.loads(self.read_body(4096))
                return self.reply(200, supplier_capture.item(data.get('url')))
            if path == '/api/supplier-capture/request':
                data = json.loads(self.read_body(4096))
                return self.reply(200, supplier_capture.request(data.get('url')))
            if path == '/api/import/preview':
                return self.reply(200, importer.preview(json.loads(self.read_body(8192))))
            if path in ('/api/image/settings', '/api/image/test', '/api/image/generate', '/api/image/poll', '/api/image/budget-settle', '/api/image/budget-quote'):
                action = {'/api/image/settings': image_provider.configure, '/api/image/test': image_provider.test_key, '/api/image/generate': image_provider.submit, '/api/image/poll': image_provider.poll, '/api/image/budget-settle': image_provider.settle_budget, '/api/image/budget-quote': image_provider.budget_quote}[path]
                data = json.loads(self.read_body(64 * 1024))
                if (path in ('/api/image/settings', '/api/image/test') and data.get('provider') == 'comfyui'
                        and (user['role'] != 'admin' or app_paths.WORKER_USER
                             or not ipaddress.ip_address(self.client_address[0]).is_loopback
                             or os.environ.get('JUNFEI_TRUST_PROXY') == '1')):
                    return self.reply(403, {'error': 'ComfyUI 配置只允许本机管理员操作'})
                return self.reply(200, action(data))
            if path == '/api/settings':
                return self.reply(200, provider.configure(json.loads(self.read_body(8192))))
            if path == '/api/settings/test':
                return self.reply(200, provider.test_key(json.loads(self.read_body(8192))))
            if path == '/api/generate/listing':
                data = json.loads(self.read_body(128 * 1024))
                if data.get('productId'):
                    with database() as conn:
                        row = conn.execute('SELECT body FROM workspace WHERE id=1').fetchone()
                    workspace = json.loads(row[0]) if row else {}
                    product = next((p for p in workspace.get('products',[]) if p['id']==data['productId']),None)
                    if not product:
                        raise ValueError('当前账号没有该产品')
                    return self.reply(200,provider.generate_listing(data.get('product'),data.get('market'),evidence_product=product))
                return self.reply(200, provider.generate_listing(data.get('product'), data.get('market')))
            if path == '/api/state':
                request = json.loads(self.read_body(STATE_BODY_LIMIT))
                state = request['state']
                if not isinstance(state, dict) or not isinstance(state.get('products'), list) or not isinstance(state.get('copy'), dict) or not isinstance(state.get('reviews'), dict):
                    raise ValueError('产品库格式不正确')
                ids = []
                for p in state['products']:
                    if not isinstance(p, dict) or not all(isinstance(p.get(k), str) and p[k] for k in ('id', 'name', 'cat', 'owner', 'status', 'color')):
                        raise ValueError('产品字段不完整')
                    if any(type(p.get(k)) is not int or not 1 <= p[k] <= 500 for k in ('colors', 'sizes')):
                        raise ValueError('颜色和尺码数量必须在 1 到 500 之间')
                    for field in ('brand', 'manufacturer'):
                        value = p.get(field, '')
                        if not isinstance(value, str):
                            raise ValueError('品牌和制造商必须为文字')
                        p[field] = value.strip() or 'Generic'
                    ids.append(p['id'])
                if len(set(ids)) != len(ids):
                    raise ValueError('父 SKU 不能重复')
                import product_sku
                used = {p.get('sku') for p in state['products'] if p.get('sku')}
                for product in state['products']:
                    product_sku.ensure(product, user, used)
                with LOCK, database() as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    row = conn.execute('SELECT revision, body FROM workspace WHERE id=1').fetchone()
                    revision = row[0] if row else 0
                    if request.get('revision') != revision:
                        return self.reply(409, {'error': '另一个页面已修改数据，请备份当前修改后刷新', 'revision': revision})
                    listing_locks.validate_state(json.loads(row[1]) if row else {}, state)
                    import variant_colours
                    variant_colours.validate_master_changes(json.loads(row[1]) if row else {}, state)
                    revision += 1
                    conn.execute('INSERT OR REPLACE INTO workspace VALUES (1, ?, ?)', (revision, json.dumps(state, ensure_ascii=False)))
                return self.reply(200, {'revision': revision})
            if path == '/api/upload':
                body = self.read_body(15 * 1024 * 1024)
                ext = '.png' if body.startswith(b'\x89PNG\r\n\x1a\n') else '.jpg' if body.startswith(b'\xff\xd8\xff') else '.webp' if body[:4] == b'RIFF' and body[8:12] == b'WEBP' else None
                if not ext:
                    raise ValueError('仅支持 JPG、PNG、WebP 图片')
                name = uuid.uuid4().hex + ext
                (DATA / 'uploads' / name).write_bytes(body)
                return self.reply(201, {'url': '/uploads/' + name})
            self.reply(404, {'error': 'Not found'})
        except (ValueError, KeyError, TypeError) as error:
            self.reply(400, {'error': str(error)})
        except Exception:
            self.reply(500, {'error': '服务端保存失败，请检查服务器日志'})
            raise


def start_supplier_workers():
    supplier_capture.start_worker()
    account_spaces.start_supplier_dispatcher()


if __name__ == '__main__':
    studio_bridge.restart_tasks()
    visual_studio.start_worker()
    colour_sets.start_worker()
    start_supplier_workers()
    deploy=DATA/'deployment.json'
    if deploy.is_file() and not local_mode():
        config=json.loads(deploy.read_text(encoding='utf-8'))
        if config.get('enabled'):
            address=config['address']
            if not ipaddress.ip_address(address).is_private or ipaddress.ip_address(address).is_loopback:
                raise ValueError('共享服务只允许绑定办公室私有网段地址')
            shared=ThreadingHTTPServer((address,int(config.get('port',18767))),Handler)
            tls=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls.minimum_version=ssl.TLSVersion.TLSv1_2
            tls.load_cert_chain(str(DATA/'tls'/'server.pem'),str(DATA/'tls'/'server-key.pem'))
            shared.socket=tls.wrap_socket(shared.socket,server_side=True)
            threading.Thread(target=shared.serve_forever,daemon=True).start()
            print(f'Shared studio: https://{address}:{shared.server_port}',flush=True)
    from runtime_environment import workbench_port
    port = workbench_port()
    print(f'Listing Studio: http://127.0.0.1:{port}', flush=True)
    ThreadingHTTPServer(('127.0.0.1', port), Handler).serve_forever()


