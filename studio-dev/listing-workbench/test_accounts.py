import json
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from pathlib import Path
from unittest.mock import patch
import accounts
import server
import account_spaces

class AccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patch = patch.object(accounts, 'DB', Path(self.temp.name)/'accounts.sqlite3')
        self.patch.start()
        self.spaces=patch.object(account_spaces,'WORKSPACE_ROOT',Path(self.temp.name)/'spaces');self.spaces.start()
        account_spaces.shutdown();account_spaces.WORKERS.clear()
        self.http = server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        self.thread = threading.Thread(target=self.http.serve_forever,daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:'+str(self.http.server_port)

    def tearDown(self):
        self.http.shutdown(); self.http.server_close(); self.thread.join()
        account_spaces.shutdown();account_spaces.WORKERS.clear();self.spaces.stop()
        self.patch.stop(); self.temp.cleanup()

    def call(self,path,data=None,token='',extra=None):
        headers = {'Content-Type':'application/json','Cookie':'studio_session='+token}
        headers.update(extra or {})
        request = urllib.request.Request(self.base+path,data=json.dumps(data).encode() if data is not None else None,headers=headers)
        try:
            r=urllib.request.urlopen(request)
        except urllib.error.HTTPError as e:
            r=e
        with r:
            return r.status,json.load(r),r.headers

    def test_capture_assigns_sku_immediately(self):
        accounts.register({'username':'admin','name':'示例用户','password':'administrator-test-123'},setup=True)
        session,user=accounts.login({'username':'admin','password':'administrator-test-123'},'test')
        capture=accounts.capture_token(user)['token']
        import tempfile
        # Use an isolated workspace DB, never the development workspace.
        with patch.object(server,'DB',Path(self.temp.name)/'capture.sqlite3'):
            with server.database() as c:
                c.execute('CREATE TABLE workspace (id INTEGER PRIMARY KEY, revision INTEGER NOT NULL, body TEXT NOT NULL)')
            code,result,_=self.call('/api/import/browser',{'url':'https://detail.1688.com/offer/123456.html','title':'Supplier garment title','attributes':{'材质':'cotton'},'images':[],'market':'US'},extra={'Origin':'chrome-extension://test','X-Capture-Token':capture})
            self.assertEqual(code,200)
            self.assertRegex(result['sku'],r'^SLYH[0-9]{14}$')
            with server.database() as c: state=json.loads(c.execute('SELECT body FROM workspace').fetchone()[0])
            self.assertEqual(state['products'][0]['sku'],result['sku'])

    def test_targeted_supplier_capture_preserves_listing(self):
        import importer
        product={'id':'existing','name':'Original','status':'制作中','market':'US','sourceImport':{'asin':'B012345678','market':'US'},'generatedImages':['keep']}
        state={'products':[product], 'copy':{'existing-US':{'title':'keep'}},'reviews':{}}
        data={'url':'https://detail.1688.com/offer/123456.html','title':'Supplier garment title','attributes':{'材质':'cotton'},'images':['https://cbu01.alicdn.com/img/ibank/test.jpg'],'market':'US','targetProductId':'existing'}
        self.assertEqual(importer.merge_browser_capture(state,data),'existing')
        self.assertEqual(len(state['products']),1)
        self.assertEqual(product['sourceImport']['asin'],'B012345678')
        self.assertEqual(product['material'],'cotton')
        self.assertEqual(product['generatedImages'],['keep'])
        self.assertEqual(state['copy']['existing-US']['title'],'keep')
        data['targetProductId']='missing'
        with self.assertRaises(ValueError): importer.merge_browser_capture(state,data)
        self.assertEqual(len(state['products']),1)

    def test_permanent_delete_cleans_only_deleted_employee(self):
        accounts.register({'username':'admin','password':'administrator-test-123'},setup=True)
        accounts.register({'username':'staff','password':'employee-test-123'})
        accounts.register({'username':'other','password':'employee-test-123'})
        token,actor=accounts.login({'username':'admin','password':'administrator-test-123'},'test')
        root=account_spaces.WORKSPACE_ROOT
        for uid in (2,3):
            (root/str(uid)).mkdir(parents=True)
            (root/str(uid)/'private.txt').write_text('private data')
        for data in ({'id':1,'action':'purge','confirmUsername':'admin'}, {'id':2,'action':'purge','confirmUsername':'staff'}):
            self.assertEqual(self.call('/api/auth/users',data,token)[0],400)
        accounts.update({'id':2,'action':'delete'},actor)
        self.assertEqual(self.call('/api/auth/users',{'id':2,'action':'purge','confirmUsername':'wrong'},token)[0],400)
        self.assertTrue((root/'2').exists())
        self.assertEqual(self.call('/api/auth/users',{'id':2,'action':'purge','confirmUsername':'staff'},token)[0],200)
        self.assertFalse((root/'2').exists())
        self.assertTrue((root/'3'/'private.txt').exists())
        self.assertEqual([u['username'] for u in accounts.users()['users']],['admin','other'])
        with self.assertRaises(ValueError): accounts.update({'id':2,'action':'restore'},actor)

    def test_extension_account_login_and_revocation(self):
        admin={'username':'admin','password':'administrator-test-123'}
        staff={'username':'staff','password':'employee-test-123'}
        accounts.register(admin,setup=True);accounts.register(staff)
        _,actor=accounts.login(admin,'admin-test')
        origin={'Origin':'chrome-extension://test'}
        endpoint='/api/auth/extension-login'
        self.assertEqual(self.call(endpoint,staff,extra=origin)[0],400)
        accounts.update({'id':2,'status':'active'},actor)
        self.assertEqual(self.call(endpoint,staff,extra={'Origin':'https://evil.example'})[0],403)
        code,result,headers=self.call(endpoint,staff,extra=origin)
        self.assertEqual(code,200);self.assertNotIn('Set-Cookie',headers)
        token=result['token'];self.assertEqual(result['user']['username'],'staff')
        self.assertIsNone(accounts.identify(token))
        self.assertEqual(accounts.identify(token,'capture')['id'],2)
        capture={**origin,'X-Capture-Token':token}
        self.assertEqual(self.call('/api/auth/extension-status',{},extra=capture)[1]['user']['id'],2)
        self.assertEqual(self.call('/api/state',extra=capture)[0],401)
        # Capture identity must take priority over a different browser session.
        admin_session,_=accounts.login(admin,'admin-browser')
        with patch.object(account_spaces,'proxy',side_effect=lambda handler,user:handler.reply(200,{'ok':True})) as proxied:
            self.call('/api/import/browser',{},admin_session,capture)
            self.assertEqual(proxied.call_args.args[1]['id'],2)
        self.assertEqual(self.call('/api/auth/extension-logout',{},extra=capture)[0],200)
        self.assertIsNone(accounts.identify(token,'capture'))
        result=self.call(endpoint,staff,extra=origin)[1]
        accounts.update({'id':2,'status':'disabled'},actor)
        self.assertEqual(self.call('/api/auth/extension-status',{},extra={**origin,'X-Capture-Token':result['token']})[0],401)

    def test_extension_download_requires_login_and_contains_no_credentials(self):
        import io, zipfile
        self.assertEqual(self.call('/api/auth/extension-download')[0],401)
        admin={'username':'admin','password':'administrator-test-123'}
        accounts.register(admin,setup=True)
        accounts.register({'username':'staff','password':'employee-test-123'})
        _,actor=accounts.login(admin,'test')
        accounts.update({'id':2,'status':'active'},actor)
        token,_=accounts.login({'username':'staff','password':'employee-test-123'},'staff')
        on_disk = json.loads((server.ROOT / 'extension' / 'manifest.json').read_text(encoding='utf-8'))
        request=urllib.request.Request(self.base+'/api/auth/extension-download',headers={'Cookie':'studio_session='+token})
        with urllib.request.urlopen(request) as response:
            self.assertIn('attachment',response.headers['Content-Disposition'])
            self.assertIn(on_disk['version'],response.headers['Content-Disposition'])
            archive=zipfile.ZipFile(io.BytesIO(response.read()))
        manifest=json.loads(archive.read('extension/manifest.json'))
        self.assertEqual(manifest, on_disk)
        self.assertIn(b'data-renderkey',archive.read('extension/1688-search.js'))
        self.assertIn(b'1688-search.js',archive.read('extension/popup.html'))
        for icon in manifest['icons'].values(): self.assertIn('extension/'+icon,archive.namelist())
        self.assertEqual(len(archive.namelist()),11)
        self.assertEqual(archive.read('extension/collector.js'), (server.ROOT / 'extension' / 'collector.js').read_bytes())
        self.assertIn(b"defaultCollectorServer='http://127.0.0.1:18769'",archive.read('extension/popup.js'))
        self.assertIn(b'copyAmazonReferenceToClipboard',archive.read('extension/popup.js'))
        self.assertIn(b'id="imageSearch"',archive.read('extension/popup.html'))
        self.assertIn(b'linkedAsin',archive.read('extension/popup.js'))
        self.assertIn(b'id="clearAssociation"',archive.read('extension/popup.html'))
        self.assertIn(b'value="AU"',archive.read('extension/popup.html'))
        self.assertIn(b'amazon.com.au',archive.read('extension/popup.js'))
        self.assertIn(b'uploadReference',archive.read('extension/popup.js'))
        self.assertIn(b'navigator.clipboard.write',archive.read('extension/popup.js'))
        self.assertIn('clipboardWrite',manifest['permissions'])
        self.assertNotIn(b'input.files=transfer.files',archive.read('extension/popup.js'))
        for handler in (b"document.getElementById('capture').onclick", b"document.getElementById('captureAmazon').onclick", b"document.getElementById('openFrontSearch').onclick", b"document.getElementById('captureFrontSearch').onclick", b"$('captureSellerSprite').onclick"):
            self.assertIn(handler, archive.read('extension/popup.js'))
        self.assertIn(b'id="captureSellerStore"',archive.read('extension/popup.html'))
        self.assertIn(b'/api/selection/capture-seller-store',archive.read('extension/popup.js'))

    def test_delete_and_restore_staff(self):
        admin={'username':'admin','password':'administrator-test-123'}
        staff={'username':'staff','password':'employee-test-123'}
        accounts.register(admin, setup=True)
        token,actor=accounts.login(admin,'test-admin')
        accounts.register(staff)
        uid=accounts.users()['users'][1]['id']
        accounts.update({'id':uid,'status':'active'},actor)
        session,user=accounts.login(staff,'test-staff')
        capture=accounts.capture_token(user)['token']
        self.assertEqual(self.call('/api/auth/users',{'id':uid,'action':'delete'},session)[0],403)
        for target in (actor['id'],):
            with self.assertRaises(ValueError): accounts.update({'id':target,'action':'delete'},actor)
        accounts.update({'id':uid,'action':'delete'},actor)
        self.assertIsNone(accounts.identify(session))
        self.assertIsNone(accounts.identify(capture,'capture'))
        with self.assertRaises(ValueError): accounts.login(staff,'test-staff')
        with self.assertRaises(ValueError): accounts.update({'id':uid,'status':'active'},actor)
        accounts.update({'id':uid,'action':'restore'},actor)
        self.assertEqual(accounts.users()['users'][1]['status'],'disabled')
        accounts.update({'id':uid,'status':'active'},actor)
        self.assertEqual(accounts.login(staff,'test-staff')[1]['id'],uid)

    def test_full_lifecycle_and_permissions(self):
        admin={'username':'admin','password':'administrator-test-123'}
        staff={'username':'staff','password':'employee-test-123'}
        self.assertTrue(self.call('/api/auth/status')[1]['setup'])
        self.assertEqual(self.call('/api/auth/setup',admin)[0],201)
        self.assertEqual(self.call('/api/auth/setup',admin)[0],400)
        code,d,h=self.call('/api/auth/login',admin)
        self.assertEqual(code,200)
        self.assertIn('HttpOnly',h['Set-Cookie'])
        token=h['Set-Cookie'].split(';')[0].split('=',1)[1]
        self.assertEqual(self.call('/api/auth/register',staff)[0],201)
        self.assertEqual(self.call('/api/auth/login',staff)[0],400)
        uid=self.call('/api/auth/users',token=token)[1]['users'][1]['id']
        self.assertEqual(self.call('/api/auth/users',{'id':uid,'status':'active'},token)[0],200)
        _,_,h=self.call('/api/auth/login',staff)
        employee=h['Set-Cookie'].split(';')[0].split('=',1)[1]
        # Staff share the MCP without permission to change/reveal the credential.
        import sellersprite, secrets_store
        with patch.object(secrets_store, 'ROOT', Path(self.temp.name)/'shared-credentials'):
            self.assertEqual(self.call('/api/sellersprite/settings', {'api_key':'shared-test-only'}, token)[0],200)
            code,config,_=self.call('/api/sellersprite/settings',token=employee)
            self.assertEqual(code,200);self.assertTrue(config['hasKey']);self.assertTrue(config['shared'])
            self.assertFalse(config['canManage']);self.assertNotIn('shared-test-only',json.dumps(config))
            self.assertEqual(self.call('/api/sellersprite/settings', {'clearKey':True}, employee)[0],403)
            self.assertEqual(self.call('/api/sellersprite/test', {'api_key':'override'}, employee)[0],403)
            with patch.object(sellersprite,'test_key',return_value={'ok':True}) as tested:
                self.assertEqual(self.call('/api/sellersprite/test', {}, employee)[0],200)
                tested.assert_called_once_with({})
            self.assertEqual(self.call('/api/credentials/reveal',{'service':'sellersprite'},employee,{'Origin':self.base})[0],403)
        for path in ['/api/auth/users']:
            self.assertEqual(self.call(path,{},employee)[0],403)
        self.assertEqual(self.call('/api/auth/users',token=employee)[0],403)
        for path in ['/api/state','/api/image/jobs','/uploads/nonexistent.png']:
            self.assertEqual(self.call(path)[0],401)
        self.assertEqual(self.call('/api/import/browser',{},extra={'Origin':'chrome-extension://test'})[0],401)
        capture=self.call('/api/auth/capture-token',{},employee)[1]['token']
        self.assertIsNotNone(accounts.identify(capture,'capture'))
        self.assertIsNone(accounts.identify(capture))
        self.assertEqual(self.call('/api/state',extra={'X-Capture-Token':capture})[0],401)
        with accounts.connect() as c:
            stored=c.execute('SELECT password FROM users LIMIT 1').fetchone()[0]
            self.assertNotIn(admin['password'],stored)
            self.assertFalse(c.execute('SELECT hash FROM tokens WHERE hash=?',(token,)).fetchone())
        self.call('/api/auth/logout',{},employee)
        self.assertIsNone(accounts.identify(employee))
        self.call('/api/auth/users',{'id':uid,'status':'disabled'},token)
        self.assertIsNone(accounts.identify(capture,'capture'))
        self.assertEqual(self.call('/api/auth/login',admin,extra={'Origin':'https://evil.example'})[0],403)

    def test_account_workspaces_keys_uploads_and_private_runtime_are_isolated(self):
        admin={'username':'admin','password':'administrator-test-123'}
        self.call('/api/auth/setup',admin)
        admin_token=self.call('/api/auth/login',admin)[2]['Set-Cookie'].split(';')[0].split('=',1)[1]
        tokens=[]
        for index in (1,2):
            staff={'username':'staff'+str(index),'password':'employee-test-123'}
            self.call('/api/auth/register',staff)
            self.assertEqual(self.call('/api/auth/login',staff)[0],400)
            uid=self.call('/api/auth/users',token=admin_token)[1]['users'][-1]['id']
            self.call('/api/auth/users',{'id':uid,'status':'active'},admin_token)
            token=self.call('/api/auth/login',staff)[2]['Set-Cookie'].split(';')[0].split('=',1)[1];tokens.append(token)
            code,data,_=self.call('/api/state',token=token)
            self.assertEqual(code,200);self.assertEqual(data['state']['products'],[])
            state={'products':[{'id':'SAME-ID','name':'Account '+str(index),'cat':'服装','owner':'me','status':'制作中','color':'#000000','colors':1,'sizes':1}],'copy':{},'reviews':{}}
            self.assertEqual(self.call('/api/state',{'revision':0,'state':state},token)[0],200)
            self.assertEqual(self.call('/api/settings',{'model':'own-model-'+str(index),'api_key':'isolated-test-key-'+str(index)},token)[0],200)
            self.assertEqual(self.call('/api/image/settings',{'api_key':'isolated-image-'+str(index)},token)[0],200)
        for index,token in enumerate(tokens,1):
            self.assertEqual(self.call('/api/state',token=token)[1]['state']['products'][0]['name'],'Account '+str(index))
            self.assertEqual(self.call('/api/settings',token=token)[1]['model'],'own-model-'+str(index))
            code,data,_=self.call('/api/credentials/reveal',{'service':'deepseek'},token,{'Origin':self.base})
            self.assertEqual(code,200);self.assertEqual(data['key'],'isolated-test-key-'+str(index))
            self.assertEqual(self.call('/api/auth/users',token=token)[0],403)
        # Restarting an account runtime preserves its own model and encrypted credential.
        item=account_spaces.WORKERS.pop(2);item['process'].terminate();item['process'].wait(timeout=5)
        self.assertEqual(self.call('/api/settings',token=tokens[0])[1]['model'],'own-model-1')
        self.assertTrue(self.call('/api/settings',token=tokens[0])[1]['hasKey'])
        self.assertEqual(self.call('/api/state',token=tokens[0],extra={'X-Workspace-Account':'3'})[0],409)
        # Same-origin uploads from A cannot be fetched through B or unauthenticated worker ports.
        req=urllib.request.Request(self.base+'/api/upload',data=b'\x89PNG\r\n\x1a\nprivate-A',headers={'Cookie':'studio_session='+tokens[0]})
        with urllib.request.urlopen(req) as response:uploaded=json.load(response)['url']
        req=urllib.request.Request(self.base+uploaded,headers={'Cookie':'studio_session='+tokens[1]})
        with self.assertRaises(urllib.error.HTTPError) as error:urllib.request.urlopen(req)
        self.assertEqual(error.exception.code,404)
        item=next(iter(account_spaces.WORKERS.values()))
        with self.assertRaises(urllib.error.HTTPError) as error:urllib.request.urlopen('http://127.0.0.1:'+str(item['port'])+'/api/state')
        self.assertEqual(error.exception.code,403)
        # Disabling the account revokes access immediately.
        self.call('/api/auth/users',{'id':2,'status':'disabled'},admin_token)
        self.assertEqual(self.call('/api/state',token=tokens[0])[0],401)

    def test_login_throttles_and_expired_tokens_fail(self):
        accounts.register({'username':'admin','password':'administrator-test-123'},setup=True)
        for _ in range(8):
            with self.assertRaises(ValueError):
                accounts.login({'username':'admin','password':'wrong'},'rate-test')
        with self.assertRaisesRegex(ValueError,'15'):
            accounts.login({'username':'admin','password':'administrator-test-123'},'rate-test')
        token,_=accounts.login({'username':'admin','password':'administrator-test-123'},'other-ip')
        with accounts.connect() as c:
            c.execute('UPDATE tokens SET expires=0')
        self.assertIsNone(accounts.identify(token))

if __name__=='__main__':
    unittest.main()

