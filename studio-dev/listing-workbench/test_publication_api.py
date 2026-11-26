import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch
import accounts
import server


class PublicationAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();root=Path(cls.temp.name)
        cls.patches=[patch.object(accounts,'DB',root/'accounts.sqlite3'),patch.object(server,'DB',root/'studio.sqlite3'),
                     patch.object(server.account_spaces,'needs_proxy',return_value=False)]
        for p in cls.patches:p.start()
        accounts.register({'username':'publication-test','password':'isolated-password-12345'},setup=True)
        cls.token,_=accounts.login({'username':'publication-test','password':'isolated-password-12345'},'test')
        cls.http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        cls.base='http://127.0.0.1:'+str(cls.http.server_port)
        cls.thread=threading.Thread(target=cls.http.serve_forever,daemon=True);cls.thread.start()
        with server.database() as db:
            db.execute('CREATE TABLE workspace(id INTEGER PRIMARY KEY,revision INTEGER,body TEXT)')
            db.execute('INSERT INTO workspace VALUES(1,7,?)',(json.dumps({'products':[{'id':'P'}],'copy':{}}),))

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown();cls.http.server_close();cls.thread.join()
        for p in reversed(cls.patches):p.stop()
        cls.temp.cleanup()

    def call(self,path,data=None,auth=True,**headers):
        h={'Content-Type':'application/json',**headers}
        if auth:h['Cookie']='studio_session='+self.token
        request=urllib.request.Request(self.base+path,headers=h,data=None if data is None else json.dumps(data).encode())
        try:response=urllib.request.urlopen(request)
        except urllib.error.HTTPError as response:return response.code,json.load(response)
        with response:return response.status,json.load(response)

    def test_auth_origin_and_account_checks_precede_executor(self):
        payload={'productId':'P','destinationId':'US','confirmed':True}
        with patch.object(server.publication_flow,'start',return_value={'id':'job'}) as start:
            self.assertEqual(self.call('/api/publication/start',payload,auth=False)[0],401)
            self.assertEqual(self.call('/api/publication/start',payload,Origin='https://other.example')[0],403)
            self.assertEqual(self.call('/api/publication/start',payload,**{'X-Workspace-Account':'2'})[0],409)
            start.assert_not_called()
            self.assertEqual(self.call('/api/publication/start',payload)[0],400)
            start.assert_not_called()
            proof={'confirmationDigest':'a'*64,'operatorPassword':'isolated-password-12345'}
            self.assertEqual(self.call('/api/publication/start',{**payload,**proof})[0],202)
            self.assertEqual(start.call_args.args[0],1)
            self.assertEqual(start.call_args.kwargs,{'confirmation':'a'*64})

    def test_local_confirmation_is_initialized_once_and_still_requires_reauthentication(self):
        data = {'productId':'P', 'destinationId':'US', 'markets':['US'], 'confirmed':True}
        password = 'new-local-fixture-password'
        preview = {'digest':'a'*64, 'markets':['US']}
        with patch.object(accounts, 'DB', Path(self.temp.name) / 'fresh-local.sqlite3'), \
                patch.object(server, 'local_mode', return_value=True), \
                patch.object(server.publication_flow, 'preview', return_value=preview), \
                patch.object(server.publication_flow, 'start', return_value={'id':'queued-fixture'}) as start:
            self.assertTrue(self.call('/api/publication/preview', {'action':'start','request':data},
                                      auth=False)[1]['needsLocalConfirmationPassword'])
            setup = '/api/publication/setup-confirmation'
            self.assertEqual(self.call(setup, {'password':password}, auth=False)[0],403)
            self.assertEqual(self.call(setup, {'password':password}, auth=False, Origin=self.base)[0],200)
            self.assertEqual(self.call(setup, {'password':'cannot-change-existing'}, auth=False, Origin=self.base)[0],400)
            self.assertFalse(self.call('/api/publication/preview', {'action':'start','request':data},
                                       auth=False)[1]['needsLocalConfirmationPassword'])
            proof = {'confirmationDigest':preview['digest']}
            self.assertEqual(self.call('/api/publication/start', {**data, **proof,
                            'operatorPassword':'wrong-test-password'}, auth=False)[0],400)
            start.assert_not_called()
            self.assertEqual(self.call('/api/publication/start', {**data, **proof,
                            'operatorPassword':password}, auth=False)[0],202)

    def test_read_only_views_cannot_start_publication(self):
        with patch.object(server.publication_flow,'view',return_value={'targets':[],'jobs':[]}), \
                patch.object(server.publication_flow,'list_jobs',return_value=[]), \
                patch.object(server.publication_flow,'start') as start:
            self.assertEqual(self.call('/api/publication/view?productId=P')[0],200)
            self.assertEqual(self.call('/api/publication/list')[0],200)
            self.assertEqual(self.call('/api/publication/start')[0],404)
            start.assert_not_called()

    def test_commerce_conflict_never_updates_workspace(self):
        with patch.object(server.publication_flow,'update_commerce') as update:
            for revision in (6,True,None):
                self.assertEqual(self.call('/api/publication/commerce',{'productId':'P','revision':revision,'details':{}})[0],409)
            update.assert_not_called()
            with server.database() as db:self.assertEqual(db.execute('SELECT revision FROM workspace').fetchone()[0],7)

    def test_explicit_recovery_and_observation_actions_use_owned_continue_route_with_auth_guards(self):
        payloads=[{'id':'original-job','confirmed':True,action:True} for action in
                  ('retryMediaFailures','observeOnly','repairMainImage','retryMainImageSave')]
        payloads.append({'id':'original-job','confirmed':True,'ukWaterResistanceLevel':'not_water_resistant',
                         'waterResistanceEvidence':'Explicit user selection; isolated API fixture'})
        for payload in payloads:
            with patch.object(server.publication_flow,'resume',return_value={'id':'original-job','status':'queued'}) as resume, \
                    patch.object(server.publication_flow,'start') as start:
                self.assertEqual(self.call('/api/publication/continue',payload,auth=False)[0],401)
                self.assertEqual(self.call('/api/publication/continue',payload,Origin='https://other.example')[0],403)
                self.assertEqual(self.call('/api/publication/continue',payload,**{'X-Workspace-Account':'2'})[0],409)
                resume.assert_not_called()
                if payload.get('observeOnly'):
                    self.assertEqual(self.call('/api/publication/observe',{'id':'original-job'})[0],202)
                else:
                    self.assertEqual(self.call('/api/publication/continue',payload)[0],400)
                    resume.assert_not_called()
                    proof={'confirmationDigest':'a'*64,'operatorPassword':'isolated-password-12345'}
                    self.assertEqual(self.call('/api/publication/continue',{**payload,**proof})[0],202)
                self.assertEqual(resume.call_args.args,(1,payload));start.assert_not_called()

if __name__=='__main__':unittest.main()
