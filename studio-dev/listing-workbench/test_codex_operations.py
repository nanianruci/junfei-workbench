import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import accounts
import codex_operations as operations
import server

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'automation'))
import codex_workbench


class CodexOperationsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        patches = (
            patch.object(accounts, 'DB', root / 'accounts.sqlite3'),
            patch.object(server, 'DB', root / 'studio.sqlite3'),
            patch.object(operations.app_paths, 'DATA', root),
            patch.object(operations.image_provider,'ROOT',root),
            patch.object(operations.image_engine, 'DB', root / 'image-engine.sqlite3'),
            patch.object(operations.listing_jobs,'DB',root/'studio.sqlite3'),
            patch.object(operations.agent_center, 'DB', root / 'agent.sqlite3'),
            patch.object(operations.visual_studio, 'DB', root / 'visual.sqlite3'),
            patch.object(operations.erp_collection, 'ROOT', root / 'erp'),
            patch.object(operations.publication_flow, 'ROOT', root / 'publication'),
        )
        for change in patches:
            change.start()
            self.addCleanup(change.stop)
        self.password = 'fixture-password-1234'
        accounts.register({'username': 'operator', 'password': self.password}, setup=True)
        self.session, _ = accounts.login({'username': 'operator', 'password': self.password}, 'test')
        body = {'products': [{'id': 'P', 'name': 'Fixture', 'material': 'cotton'},
                             {'id': 'Q', 'name': 'Other product'}], 'copy': {}, 'reviews': {}}
        with server.database() as db:
            db.execute('CREATE TABLE workspace(id INTEGER PRIMARY KEY, revision INT, body TEXT)')
            db.execute('INSERT INTO workspace VALUES(1, 7, ?)', (json.dumps(body),))
        self.http = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.http.server_close)
        self.addCleanup(self.http.shutdown)
        self.scope = {'productId': 'P', 'market': 'US'}
        code, result = self.call('/api/codex/pair', self.scope)
        self.assertEqual(code, 201)
        self.token = result['token']

    def call(self, path, body=None, bearer=False):
        headers = {'Content-Type': 'application/json'}
        if bearer:
            headers['Authorization'] = 'Bearer ' + self.token
        else:
            headers['Cookie'] = 'studio_session=' + self.session
        conn = http.client.HTTPConnection('127.0.0.1', self.http.server_port, timeout=5)
        try:
            conn.request('GET' if body is None else 'POST', path, None if body is None else json.dumps(body), headers)
            response = conn.getresponse()
            return response.status, json.load(response)
        finally:
            conn.close()

    def request(self):
        return {'type': 'generate_copy', 'revision': 7, 'target': {}, 'parameters': {}}

    def assign(self):
        payload = {**self.scope, 'action': self.request()}
        code, item = self.call('/api/codex/assign', payload)
        self.assertEqual(code, 200, item)
        return item

    def review(self, item, decision='ready'):
        return {'requestId': item['request']['requestId'], 'decision': decision,
                'note': '已核对本款已保存资料', 'evidence': [dict(check,decision=decision) for check in item['assignment']['checks']]}

    def test_backend_assigns_agent_reviews_operator_executes_once(self):
        with patch.dict(os.environ, {'JUNFEI_CODEX_TOKEN': self.token,
                                     'JUNFEI_CODEX_URL': f'http://127.0.0.1:{self.http.server_port}'}), \
                patch.object(operations.listing_jobs, 'start', return_value={'id': 'copy-job', 'status': 'queued'}) as start:
            self.assertIsNone(codex_workbench.call('next')['assignment'])
            self.assertEqual(self.call('/api/codex/assign', {**self.scope, 'action': self.request()}, bearer=True)[0], 401)
            self.assertEqual(self.call('/api/codex/proposals', self.request(), bearer=True)[0], 401)
            item = self.assign()
            self.assertEqual(self.assign(), item)
            self.assertEqual(codex_workbench.call('next')['assignment'], item)
            result = self.review(item)
            key = {**self.scope, 'requestId': result['requestId']}
            self.assertEqual(self.call('/api/codex/approve', key)[1]['status'], 'assigned')
            self.assertEqual(self.call('/api/codex/review', {**result, 'target': 'other'}, bearer=True)[0], 400)
            self.assertEqual(self.call('/api/codex/review', {**result, 'evidence': ['supplier']}, bearer=True)[0], 400)
            self.assertEqual(codex_workbench.call('review', result)['status'], 'pending')
            self.assertEqual(codex_workbench.call('review', result)['status'], 'pending')
            self.assertEqual(self.call('/api/codex/approve', key, bearer=True)[0], 401)
            start.assert_not_called()
            self.assertEqual(self.call('/api/codex/approve', key)[1]['jobId'], 'copy-job')
            self.assertEqual(self.call('/api/codex/approve', key)[1]['jobId'], 'copy-job')
            start.assert_called_once_with(1, {'productId':'P','market':'US','requestId':result['requestId'],'expectedRevision':7},
                                          handoff=(1,'P','US',result['requestId']))

    def test_changed_source_reported_gap_and_cancellation_stop_dispatch(self):
        with patch.object(operations, 'dispatch') as dispatch:
            item = self.assign()
            review = self.review(item, 'blocked')
            self.assertEqual(self.call('/api/codex/review', review, bearer=True)[1]['status'], 'blocked')
            key = {**self.scope, 'requestId': review['requestId']}
            self.assertEqual(self.call('/api/codex/approve', key)[1]['status'], 'blocked')
            cancelled = self.assign()
            key['requestId'] = cancelled['request']['requestId']
            self.assertEqual(self.call('/api/codex/decline', key)[1]['status'], 'declined')
            self.assertEqual(self.call('/api/codex/review', self.review(cancelled), bearer=True)[0], 400)
            stale = self.assign()
            with server.database() as db:
                db.execute("UPDATE workspace SET revision=8, body=json_set(body,'$.products[0].material','wool')")
            self.assertEqual(self.call('/api/codex/review', self.review(stale), bearer=True)[1]['status'], 'stale')
            self.assertIsNone(self.call('/api/codex/next', bearer=True)[1]['assignment'])
            dispatch.assert_not_called()

    def test_unrelated_product_edit_keeps_review_and_rechecks_latest_revision(self):
        item = self.assign()
        with server.database() as db:
            db.execute("UPDATE workspace SET revision=8, body=json_set(body,'$.products[1].name','Renamed')")
        self.assertEqual(self.call('/api/codex/review', self.review(item), bearer=True)[1]['status'], 'pending')
        with patch.object(operations.listing_jobs, 'start', return_value={'id':'copy','status':'queued'}) as start:
            result = self.call('/api/codex/approve', {**self.scope,'requestId':item['request']['requestId']})[1]
            self.assertEqual(result['jobId'], 'copy')
            self.assertEqual(start.call_args.args[1]['expectedRevision'], 8)

    def test_distinct_pair_codes_cannot_claim_same_review_and_can_be_revoked(self):
        item = self.assign()
        self.assertEqual(self.call('/api/codex/next', bearer=True)[1]['assignment'], item)
        status = self.call('/api/codex/status?productId=P&market=US')[1]
        self.assertTrue(status['proposals'][0]['reviewInProgress'])
        self.assertNotIn('reviewClaim', status['proposals'][0])
        heartbeat = {'requestId': item['request']['requestId']}
        self.assertEqual(self.call('/api/codex/heartbeat', heartbeat)[0], 401)
        code, lease = self.call('/api/codex/heartbeat', heartbeat, bearer=True)
        self.assertEqual(code, 200)
        self.assertEqual(lease['requestId'], heartbeat['requestId'])
        self.assertLessEqual(lease['reviewUntil'], item['expiresAt'])
        other = self.call('/api/codex/pair', self.scope)[1]['token']
        first, self.token = self.token, other
        self.assertEqual(self.call('/api/codex/heartbeat', heartbeat, bearer=True)[0], 400)
        self.assertIsNone(self.call('/api/codex/next', bearer=True)[1]['assignment'])
        self.assertEqual(self.call('/api/codex/review', self.review(item), bearer=True)[0], 400)
        self.token = first
        self.assertEqual(self.call('/api/codex/review', self.review(item), bearer=True)[1]['status'], 'pending')
        self.assertEqual(self.call('/api/codex/heartbeat', heartbeat, bearer=True)[0], 400)
        self.assertEqual(self.call('/api/codex/revoke', self.scope)[0], 200)
        for token in (first, other):
            self.token = token
            self.assertEqual(self.call('/api/codex/status', bearer=True)[0], 401)

    def test_linked_cli_product_keeps_original_executor_without_selection_metadata(self):
        with server.database() as db:
            operations.discovery_intake.ensure_schema(db)
            db.execute('INSERT INTO discovery_product_links VALUES (?,?,?,?,?,?,?,?,?,?)',
                       (1, 'US', 'B000000001', 'P', 'original-cli', '2026-11-01', 'cycle', 1, 'event', 'hash'))
        code, status = self.call('/api/codex/status', bearer=True)
        self.assertEqual(code, 200)
        self.assertEqual(status['selection']['cliTask']['id'], 'original-cli')
        self.assertEqual(status['checkpoints'], [
            {'kind':'selectionCli','id':'original-cli','status':'unavailable','market':'US'}])
        self.assertEqual(status['actions'], [])
        code,other=self.call('/api/codex/status?productId=P&market=UK')
        self.assertEqual(code,200)
        self.assertEqual(other['actions'],[])
        self.assertEqual(other['selection']['cliTask']['marketplace'],'US')
        self.assertEqual(self.call('/api/codex/assign', {**self.scope, 'action':self.request()})[0], 400)

    def test_checkpoints_keep_task_completion_gallery_approval_and_publication_distinct(self):
        image = {'id':'image-job','status':'completed','request':{'mode':'suite'}}
        gallery = {'accountId':1,'versionId':'gallery-version','approvedAt':100}
        publication = {'id':'publication-job','markets':['US'],'status':'submitted','usPublishId':'123','canObserveLatestReceipts':True,
                       'usPublicationObservation':{'status':'publishing','verification':'accepted'}}
        with patch.object(operations.image_engine,'coordination_tasks',return_value=[image]), \
                patch.object(operations.image_engine_delivery,'selection',return_value=gallery), \
                patch.object(operations.publication_flow,'list_jobs',return_value=[publication]):
            code, status = self.call('/api/codex/status', bearer=True)
            self.assertEqual(code, 200)
            self.assertEqual(status['checkpoints'], [
                {'kind':'image','id':'image-job','status':'completed'},
                {'kind':'gallery','id':'gallery-version','status':'approval_recorded','approvedAt':100},
                {'kind':'publication','id':'publication-job','status':'submitted','market':'US',
                 'externalId':'123','observation':'publishing','observationVerified':'accepted'},
            ])
            code, status = self.call('/api/codex/status?productId=P&market=UK')
            self.assertEqual(code, 200)
            self.assertNotIn('publication', {row['kind'] for row in status['checkpoints']})
            self.assertEqual(status['publication'], [])
            self.assertNotIn('observe_publication', {row['type'] for row in status['actions']})

    def test_prior_image_request_still_controls_dispatch_after_thirty_newer_repairs(self):
        engine = operations.image_engine
        old = {'id':'original', 'productId':'P', 'requestId':'original-request',
               'request':{'mode':'suite','count':7,'sourceVariant':{'colorKey':'blue'}},
               'status':'submission_unknown'}
        with engine.db() as db:
            tasks = [old] + [{'id':f'repair-{n}', 'productId':'P', 'requestId':f'r{n}',
                              'request':{'mode':'repair'}, 'status':'completed'} for n in range(31)]
            db.executemany('INSERT INTO tasks VALUES(?,?,?,?,?)', [
                (t['id'], 'P', t['requestId'], json.dumps(t), n) for n, t in enumerate(tasks)])
        db.close()
        with patch.object(operations, 'formal_suite_target', return_value={'colorKey':'blue'}):
            for state in ('submission_unknown', 'completed'):
                with engine.db() as db:
                    db.execute('UPDATE tasks SET body=? WHERE id=?', (json.dumps(dict(old, status=state)), old['id']))
                db.close()
                snapshot = operations.status(1, server.database, 'P', 'US')
                self.assertNotIn('generate_image_suite', {a['type'] for a in snapshot['actions']})
                self.assertIn('original', {r['id'] for r in snapshot['imageEngine']['tasks']})
                self.assertNotIn('repair-0', {r['id'] for r in snapshot['imageEngine']['tasks']})

    def test_native_image_engine_task_is_observable_without_legacy_image_dispatch(self):
        task = {'id':'a'*32,'productId':'P','status':'completed','updated':1,
                'request':{'mode':'suite'},'results':[{'index':0,'review':{'blocking':False,'identityPreserved':True}}]}
        with patch.object(operations.image_engine,'coordination_tasks',return_value=[task]), \
                patch.object(operations.visual_studio,'list_projects',return_value={
                    'projects':[{'id':'legacy','market':'US','deliverable':'gallery','plan':{'slots':[{}]}}],
                    'runs':[]}):
            snapshot = operations.status(1,server.database,'P','US')
            self.assertIn({'type':'observe_image','label':'核对原生作图任务',
                           'target':{'taskId':task['id']},
                           'authority':operations.authority('observe_image')},snapshot['actions'])
            self.assertEqual(snapshot['imageEngine']['tasks'][0]['reviews'][0]['blocking'],False)
            self.assertFalse(any(action['type']=='generate_image' for action in snapshot['actions']))
            request={'revision':snapshot['revision'],'type':'observe_image','target':{'taskId':task['id']},'parameters':{}}
            assigned=operations.assign(1,server.database,'P','US',request)
            review=self.review(assigned)
            operations.submit_review(1,server.database,'P','US',review)
            with patch.object(operations.image_engine,'get',return_value=task), \
                    patch.object(operations.image_engine,'poll',return_value=task):
                result=operations.approve(1,server.database,'P','US',assigned['request']['requestId'])
            self.assertEqual((result['jobId'],result['jobStatus']),('a'*32,'completed'))

    def test_approved_formal_suite_uses_native_image_engine_with_frozen_source_and_templates(self):
        target = {'colorKey':'blue','reference':'/uploads/source.png','sourceSha256':'f'*64,
                  'templates':[{'id':str(i)} for i in range(1,6)]}
        with patch.object(operations,'formal_suite_target',return_value=target), \
                patch.object(operations.image_engine,'coordination_tasks',return_value=[]), \
                patch.object(operations.image_engine,'start',return_value={'id':'image-task','status':'queued'}) as start, \
                patch('image_source_variants.prepare',return_value={'source':{'url':'/uploads/bound.png','sha256':'f'*64},
                      'association':{'receiptId':'a'*64}}):
            request = {'revision':7,'type':'generate_image_suite','target':target,'parameters':{'brief':'真实七图'}}
            item = operations.assign(1,server.database,'P','US',request)
            operations.submit_review(1,server.database,'P','US',self.review(item))
            start.assert_not_called()
            request_id = item['request']['requestId']
            response = operations.approve(1,server.database,'P','US',request_id)
            self.assertEqual(response['jobId'],'image-task')
            payload = start.call_args.args[0]
            self.assertEqual(payload['sourceVariant'], {'receiptId':'a'*64})
            self.assertEqual(payload['apparelSuite']['pointTemplateIds'], ['1','2'])
            self.assertEqual(payload['apparelSuite']['lifestyleTemplateIds'], ['3','4','5'])
            self.assertEqual(payload['apparelSuite']['shotOrder'], ['white','seller','seller','ins','ins','ins','size'])
            self.assertEqual(start.call_args.kwargs['handoff'],(1,'P','US',request_id))

    def test_new_request_cannot_replace_unresolved_handoff(self):
        first=self.assign()
        req=first['request']['requestId']
        self.call('/api/codex/review',self.review(first),bearer=True)
        with patch.object(operations,'dispatch',side_effect=RuntimeError('unknown')) as dispatch:
            result=self.call('/api/codex/approve',{**self.scope,'requestId':req})[1]
            self.assertEqual(result['status'],'needs_review')
            code,error=self.call('/api/codex/assign',{**self.scope,'action':self.request()})
            self.assertEqual(code,400)
            self.assertIn(req,error['error'])
            dispatch.assert_called_once()
        body = {**self.scope, 'requestId': req, 'reference': 'Service ticket fixture-42: no submission'}
        self.assertEqual(self.call('/api/codex/resolve-unsubmitted', body, bearer=True)[0], 401)
        with operations.dispatch_guard((1, 'P', 'US', req)):
            self.assertEqual(self.call('/api/codex/resolve-unsubmitted', body)[0], 400)
        code, result = self.call('/api/codex/resolve-unsubmitted', body)
        self.assertEqual((code, result['status']), (200, 'not_submitted'))
        self.assertEqual(result['resolution']['reference'], body['reference'])
        self.assertNotEqual(self.assign()['request']['requestId'], req)

    def test_exact_handoff_recovers_lost_response_and_is_scoped(self):
        item=self.assign()
        key=item['request']['requestId']
        self.call('/api/codex/review',self.review(item),bearer=True)
        def lost_response(*args,handoff):
            with operations.listing_jobs.database() as db:
                operations.codex_handoff.record(db,handoff,'original','queued')
            raise RuntimeError('lost local response')
        with patch.object(operations.listing_jobs,'start',side_effect=lost_response) as start:
            self.call('/api/codex/approve',{**self.scope,'requestId':key})
            self.assertEqual(operations.receipt(1,'P','US',key)['jobId'],'original')
            self.assertEqual(operations.list_proposals(1,'P','US')[0]['status'],'dispatched')
            self.assertEqual(operations.approve(1,server.database,'P','US',key)['handoffStage'],'queued')
            self.assertEqual(start.call_count,1)
        body = {**self.scope, 'requestId': key, 'reference': 'Service ticket fixture-42'}
        self.assertEqual(self.call('/api/codex/resolve-unsubmitted', body)[1]['status'], 'dispatched')
        with operations.listing_jobs.database() as db:
            self.assertIsNone(operations.codex_handoff.lookup(db,(1,'P','UK',key)))

    def test_online_receipt_reads_original_persisted_children_with_pairing(self):
        flow = operations.publication_flow
        job_id = 'test-publication'
        with server.database() as db:
            state = json.loads(db.execute('SELECT body FROM workspace WHERE id=1').fetchone()[0])
            state['products'][0]['selectionResearch'] = {'reference': {'asin': 'B000000001', 'market': 'US'}}
            db.execute('UPDATE workspace SET body=? WHERE id=1', (json.dumps(state),))
        scope = {'accountId': 1, 'productId': 'P', 'jobId': job_id}
        job = {**scope, 'id': job_id, 'status': 'submitted'}
        with flow.connect() as db, db:
            db.execute('INSERT INTO jobs(id,account_id,product_id,destination_id,status,body) VALUES(?,?,?,?,?,?)',
                       (job_id, 1, 'P', 'test-us', 'submitted', json.dumps(job)))
        folder = flow.ROOT / job_id
        folder.mkdir()
        market = {'marketId': '3', 'countryCode': 'US'}
        children = [{'sku': 'P-S', 'status': 'online', 'asin': 'B000000002'}]
        source = {**scope, 'sha256': 'fixture-sha', 'store': 'test-us', 'destination': market,
                  'goods': {'parentSku': 'P'}, 'goodsItems': [{'sku': 'P-S'}]}
        latest = {'publishId': '1001', 'recordId': '1002', 'verification': 'accepted', 'failed': False,
                  'status': 'online', 'allChildrenOnline': True, 'childCount': 1,
                  'observedAt': '2026-11-20T07:00:00Z'}
        publication = {'publishId': '1001', 'latestObservation': latest,
                       'onlineObservation': {'recordId': '1002', 'identity': {**scope, **market,
                           'publishId': '1002', 'store': 'test-us', 'parentSku': 'P', 'children': children}}}
        record = {**scope, 'snapshotSha256': 'fixture-sha', 'usPublish': publication}
        (folder / 'input.json').write_text(json.dumps({'input': source}), encoding='utf-8')
        receipt = folder / 'receipt.json'
        receipt.write_text(json.dumps(record), encoding='utf-8')
        path = '/api/codex/publication-receipt?jobId=test-publication&publishId=1001&sourceId=B000000001&pipelineJobId=fixture'
        self.assertEqual(self.call(path, bearer=True)[1]['children'], children)
        self.assertEqual(self.call(path.replace('1001', '1003'), bearer=True)[0], 400)
        self.assertEqual(self.call(path.replace('B000000001', 'B000000003'), bearer=True)[0], 400)
        children[0]['status'] = 'accepted'
        receipt.write_text(json.dumps(record), encoding='utf-8')
        self.assertEqual(self.call(path, bearer=True)[0], 400)

    def test_variant_fields_evidence_and_legacy_assignment(self):
        with patch.object(operations.erp_collection,'eligible_variant_rows',return_value=[
                ('SKU-S','红','Red','S','supplier','mapped','B000000001')]):
            item=self.assign()
            variant=item['assignment']['inputs']['product']['variants'][0]
            self.assertEqual((variant['size'],variant['sourceAsin']),('S','B000000001'))
            review=self.review(item)
            for invalid in (review['evidence'][:-1],review['evidence']*2,
                            [dict(review['evidence'][0],digest='wrong'),*review['evidence'][1:]]):
                self.assertEqual(self.call('/api/codex/review',{**review,'evidence':invalid},bearer=True)[0],400)
            item['assignment'].pop('version')
            with operations.connect() as db:
                db.execute('UPDATE proposals SET body=?',(json.dumps(item),))
            self.assertEqual(operations.list_proposals(1,'P','US')[0]['status'],'requires_assignment')
            self.assertIsNone(operations.next_assignment(1,'P','US')['assignment'])

    def test_older_assignment_can_be_found_and_expired(self):
        item = self.assign()
        key = item['request']['requestId']
        with operations.connect() as db:
            for i in range(30):
                body = {'request': {'requestId': f'later-{i:03d}'}, 'status': 'declined'}
                db.execute('INSERT INTO proposals VALUES (?,?,?,?,?)', (1, 'P', 'US', body['request']['requestId'], json.dumps(body)))
        self.assertEqual(len(operations.list_proposals(1, 'P', 'US')), 31)
        self.assertEqual(self.call('/api/codex/receipt?productId=Q&market=US&requestId='+key)[0], 400)
        with patch.object(operations.time, 'time', return_value=item['expiresAt'] + 1):
            self.assertEqual(operations.receipt(1, 'P', 'US', key)['status'], 'expired')
            self.assertEqual(len(operations.list_proposals(1, 'P', 'US')), 30)
            self.assertIsNone(operations.next_assignment(1, 'P', 'US')['assignment'])
            self.assertEqual(operations.approve(1, server.database, 'P', 'US', key)['status'], 'expired')


if __name__ == '__main__':
    unittest.main()
