import json
from pathlib import Path
import runpy
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import app_paths
import supplier_capture as q

URL = 'https://detail.1688.com/offer/123.html'
OTHER = 'https://detail.1688.com/offer/456.html'
ROUTE = {'spaceId': 987, 'name': 'collector fixture', 'label': 'p1'}


def supplier(url=URL):
    return {'url': url, 'title': 'Fixture product only', 'attributes': {'材质': '棉'},
            'images': [], 'retrievedAt': '2026-11-11T00:00:00Z'}


class SupplierQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patches = [patch.object(q, 'DB', self.root / 'queue.sqlite3'), patch.object(q, 'SCOPE', 'account-a'),
                        patch.object(app_paths, 'DATA', self.root / 'account-a'), patch.object(q, 'kick'),
                        patch.object(q, 'MIN_INTERVAL', 90)]
        for p in self.patches:
            p.start()
        app_paths.DATA.mkdir()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def test_concurrent_requests_deduplicate_and_global_claim_is_exclusive(self):
        q.request(URL)
        with ThreadPoolExecutor(max_workers=8) as pool:
            records = list(pool.map(lambda _: q.request(URL+'?from=search#ignored'), range(12)))
            claims = list(pool.map(lambda _: q._claim(), range(12)))
        self.assertEqual(len({x['id'] for x in records}), 1)
        self.assertEqual(sum(x is not None for x in claims), 1)
        with closing(q.connect()) as c, c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM captures').fetchone()[0], 1)

    def test_bound_capture_does_not_dispatch_while_task_space_is_locked(self):
        module = Path(__file__).resolve().parent.parent / 'automation/browser_space_lock.py'
        active_space = runpy.run_path(str(module))['active_space']
        with active_space(ROUTE['spaceId'], root=self.root), \
                patch.object(q.runtime_environment, 'ego_space', side_effect=lambda space: active_space(space, root=self.root)), \
                patch.object(q.shutil, 'which', return_value='fixture-ego'), \
                patch.object(q.subprocess, 'run') as dispatch:
            self.assertEqual(q.browser_capture(URL, ROUTE), {'status': 'paused', 'reason': 'ego_space_unavailable'})
            dispatch.assert_not_called()

    def test_existing_product_is_reused_without_starting_browser(self):
        with closing(sqlite3.connect(app_paths.DATA/'studio.sqlite3')) as c, c:
            c.execute('CREATE TABLE workspace(id INTEGER PRIMARY KEY,body TEXT)')
            c.execute('INSERT INTO workspace VALUES(1,?)', (json.dumps({'products': [
                {'sourceImport': {'supplier': supplier()}}]}),))
        with patch.object(q, 'browser_capture') as browser:
            self.assertEqual(q.read(URL+'?query=1')['title'], 'Fixture product only')
            self.assertTrue(q.read(URL)['reused'])
        browser.assert_not_called()
        q.kick.assert_not_called()

    def test_paused_queue_blocks_other_account_but_allows_cache_hits(self):
        q.request(URL)
        row, g = q._claim()
        q._finish(row, g, {'status': 'paused', 'reason': 'login_or_verification', 'browser': ROUTE})
        with patch.object(q, 'SCOPE', 'account-b'):
            q.request(OTHER)
            self.assertIsNone(q._claim())
            status = q.status()
            self.assertTrue(status['paused'])
            self.assertFalse(status['canResume'])
            self.assertEqual(status['url'], '')
            self.assertEqual(status['browser'], {})
            q.remember(supplier(OTHER))
            self.assertEqual(q.read(OTHER)['title'], 'Fixture product only')
            with self.assertRaises(ValueError):
                q.resume()
        self.assertTrue(q.status()['canResume'])

    def test_explicit_resume_reads_original_page_and_preserves_queued_jobs(self):
        q.request(URL)
        q.request(OTHER)
        row, g = q._claim()
        q._finish(row, g, {'status': 'paused', 'reason': 'login_or_verification', 'browser': ROUTE})
        with patch.object(q, 'browser_capture', return_value={'status': 'collected', 'supplier': supplier(), 'browser': ROUTE}) as browser:
            result = q.resume()
        self.assertFalse(result['paused'])
        args = browser.call_args.args
        self.assertEqual(args[:3], (URL, ROUTE, True))
        self.assertEqual(q.item(OTHER)['status'], 'queued')
        self.assertIsNone(q._claim())  # A new URL still observes the navigation spacing.
        self.assertEqual(q.read(URL)['url'], URL)

    def pause_missing_browser(self, *, reason=None, receipt_browser=None):
        first = q.request(URL)
        q.request(OTHER)
        row, g = q._claim()
        value = {'status': 'paused', 'reason': reason or 'task space not found: 987',
                 'browser': ROUTE if receipt_browser is None else receipt_browser}
        folder = app_paths.DATA / 'supplier-capture-runs' / g['runId']
        folder.mkdir(parents=True)
        raw = json.dumps(value).encode()
        (folder / 'result.json').write_bytes(raw)
        q._finish(row, g, {**value, 'browser': ROUTE})
        return first, g, folder / 'result.json', raw

    def test_explicit_missing_browser_reconnect_preserves_capture_fifo_and_old_receipt(self):
        first, old, receipt, raw = self.pause_missing_browser()
        replacement = {'spaceId': 991, 'name': 'new fixture', 'label': 'p1'}
        def capture(url, browser, resume, run_id):
            self.assertEqual(url, URL)
            self.assertTrue(resume)
            self.assertTrue(browser['reconnectMissing'])
            self.assertEqual(browser['reconnectFromRunId'], old['runId'])
            self.assertNotEqual(run_id, old['runId'])
            return {'status': 'collected', 'supplier': supplier(), 'browser': replacement}
        with patch.object(q, 'browser_capture', side_effect=capture) as browser:
            q.resume(reconnect_missing_browser=True)
        browser.assert_called_once()
        self.assertEqual(q.item(URL)['id'], first['id'])
        self.assertEqual(receipt.read_bytes(), raw)
        self.assertEqual(q.item(OTHER)['status'], 'queued')
        self.assertIsNone(q._claim())  # Original 90 second spacing survives reconnection.
        with closing(q.connect()) as c:
            current = q.gate(c)
            events = c.execute('SELECT kind,body FROM capture_events WHERE capture_id=? ORDER BY seq', (first['id'],)).fetchall()
        self.assertEqual(current['browser'], replacement)
        self.assertEqual(events[0]['kind'], 'missing_browser_reconnect_authorized')
        self.assertEqual(json.loads(events[0]['body'])['previousRunId'], old['runId'])

    def test_missing_browser_reconnect_rejects_verification_unknown_mismatch_and_wrong_owner(self):
        for reason in ('login_or_verification', 'execution_unknown', 'task space not found: 988'):
            with self.subTest(reason=reason):
                first, old, receipt, raw = self.pause_missing_browser(reason=reason)
                with patch.object(q, 'browser_capture') as browser, self.assertRaises(ValueError):
                    q.resume(reconnect_missing_browser=True)
                browser.assert_not_called()
                with closing(q.connect()) as c, c:
                    c.execute('DELETE FROM captures'); c.execute('DELETE FROM capture_events')
                    q.save_gate(c, {'paused': False, 'active': None, 'nextAt': 0})
        self.pause_missing_browser(receipt_browser={**ROUTE, 'label': 'p2'})
        with patch.object(q, 'browser_capture') as browser:
            with self.assertRaisesRegex(ValueError, '回执与绑定不一致'):
                q.resume(reconnect_missing_browser=True)
            with patch.object(q, 'SCOPE', 'another-account'), self.assertRaises(ValueError):
                q.resume(reconnect_missing_browser=True)
        browser.assert_not_called()

    def test_missing_browser_reconnect_consumes_flag_on_unknown_outcome_without_replay(self):
        self.pause_missing_browser()
        with patch.object(q, 'browser_capture', side_effect=TimeoutError()) as browser:
            q.resume(reconnect_missing_browser=True)
            with self.assertRaises(ValueError):
                q.resume(reconnect_missing_browser=True)
        browser.assert_called_once()
        with closing(q.connect()) as c:
            current = q.gate(c)
        self.assertNotIn('reconnectMissing', current['browser'])
        self.assertNotIn('reconnectFromRunId', current['browser'])
        self.assertEqual(current['reason'], 'execution_unknown')

    def pause_created_native_blank(self):
        first, old, original_receipt, original_raw = self.pause_missing_browser()
        row, creation = q._claim(resume=True, reconnect_missing_browser=True)
        value = {'status': 'paused', 'reason': 'reconnect_blank_page_required', 'browser': ROUTE,
                 'reconnect': {'previousBrowser': ROUTE, 'previousRunId': old['runId'],
                               'runId': creation['runId'], 'oldSpaceAbsent': True, 'stage': 'creating'}}
        folder = app_paths.DATA / 'supplier-capture-runs' / creation['runId']
        folder.mkdir()
        raw = json.dumps(value).encode()
        (folder / 'result.json').write_bytes(raw)
        q._finish(row, creation, value)
        return first, creation, folder / 'result.json', raw

    def test_explicit_existing_creation_reconciliation_keeps_original_capture_and_both_old_receipts(self):
        first, creation, receipt, raw = self.pause_created_native_blank()
        replacement = {'spaceId': 991, 'name': 'Junfei 1688 serial collection reconnect '+creation['runId'], 'label': 'p1'}
        def capture(url, browser, resume, run_id):
            self.assertEqual(browser['reconnectExistingCreationRunId'], creation['runId'])
            self.assertEqual(browser['reconnectFromRunId'], creation['runId'])
            return {'status': 'collected', 'supplier': supplier(), 'browser': replacement,
                    'reconnect': {'existingCreationRunId': creation['runId'], 'creationCalls': 0}}
        with patch.object(q, 'browser_capture', side_effect=capture) as browser:
            q.resume(reconnect_missing_browser=True)
        browser.assert_called_once()
        self.assertEqual(receipt.read_bytes(), raw)
        self.assertEqual(q.item(URL)['id'], first['id'])
        self.assertEqual(q.item(OTHER)['status'], 'queued')
        self.assertIsNone(q._claim())
        with closing(q.connect()) as c:
            event = c.execute("SELECT body FROM capture_events WHERE kind='missing_browser_reconnect_authorized' ORDER BY seq DESC LIMIT 1").fetchone()
            self.assertEqual(q.gate(c)['browser'], replacement)
        self.assertEqual(json.loads(event['body'])['existingCreationRunId'], creation['runId'])

    def test_existing_creation_requires_exact_stage_identity_and_original_missing_receipt(self):
        first, creation, receipt, raw = self.pause_created_native_blank()
        good = json.loads(raw)
        for key, value in [('stage', 'bound'), ('runId', 'f'*32), ('previousBrowser', {**ROUTE, 'label': 'p2'}),
                           ('oldSpaceAbsent', False), ('previousRunId', 'e'*32)]:
            with self.subTest(key=key):
                bad = json.loads(raw); bad['reconnect'][key] = value
                receipt.write_text(json.dumps(bad), encoding='utf-8')
                with patch.object(q, 'browser_capture') as browser, self.assertRaises(ValueError):
                    q.resume(reconnect_missing_browser=True)
                browser.assert_not_called()
        receipt.write_bytes(raw)

    def test_bound_native_blank_reconciliation_traces_all_old_runs_and_keeps_current_bound_identity(self):
        first, creation, creation_receipt, creation_raw = self.pause_created_native_blank()
        row, bound = q._claim(resume=True, reconnect_missing_browser=True)
        replacement = {'spaceId': 991, 'name': 'Junfei 1688 serial collection reconnect '+creation['runId'], 'label': 'p1'}
        value = {'status': 'paused', 'reason': 'reconnect_blank_page_required', 'browser': replacement,
                 'reconnect': {'previousBrowser': ROUTE, 'previousRunId': creation['runId'],
                    'runId': bound['runId'], 'oldSpaceAbsent': True, 'stage': 'bound',
                    'existingCreationRunId': creation['runId'], 'creationCalls': 0,
                    'createdSpace': {k: replacement[k] for k in ('spaceId', 'name')}, 'browser': replacement}}
        folder = app_paths.DATA / 'supplier-capture-runs' / bound['runId']; folder.mkdir()
        raw = json.dumps(value).encode(); (folder / 'result.json').write_bytes(raw)
        q._finish(row, bound, value)
        def capture(url, browser, resume, run_id):
            self.assertEqual(browser['spaceId'], 991)
            self.assertEqual(browser['reconnectFromRunId'], bound['runId'])
            self.assertEqual(browser['reconnectExistingCreationRunId'], creation['runId'])
            self.assertEqual(browser['reconnectOriginalMissingBrowser'], ROUTE)
            self.assertEqual(browser['reconnectBoundCreationBrowser'], replacement)
            return {'status': 'collected', 'supplier': supplier(), 'browser': replacement}
        with patch.object(q, 'browser_capture', side_effect=capture) as browser:
            q.resume(reconnect_missing_browser=True)
        browser.assert_called_once()
        self.assertEqual(creation_receipt.read_bytes(), creation_raw)
        self.assertEqual((folder / 'result.json').read_bytes(), raw)
        self.assertEqual(q.item(URL)['id'], first['id'])
        with closing(q.connect()) as c:
            event = json.loads(c.execute("SELECT body FROM capture_events WHERE kind='missing_browser_reconnect_authorized' ORDER BY seq DESC LIMIT 1").fetchone()['body'])
            self.assertEqual(q.gate(c)['browser'], replacement)
        self.assertEqual([x['stage'] for x in event['receiptChain']], ['bound', 'creating', 'missing'])
        self.assertEqual(event['previousReconnectStage'], 'bound')

    def test_unknown_execution_freezes_without_replay_after_restart(self):
        q.request(URL)
        claim = q._claim()
        with patch.object(q, 'browser_capture', side_effect=TimeoutError()) as browser:
            q._perform(claim)
            q.request(URL)
            self.assertTrue(q.status()['paused'])
            self.assertIsNone(q._claim())
        browser.assert_called_once()
        self.assertEqual(q.status()['reason'], 'execution_unknown')

    def test_next_product_waits_ninety_seconds_while_cache_remains_available(self):
        q.request(URL)
        q.request(OTHER)
        row, g = q._claim()
        with patch.object(q.time, 'time', return_value=1000):
            q._finish(row, g, {'status': 'collected', 'supplier': supplier()})
        with patch.object(q.time, 'time', return_value=1089.99):
            self.assertIsNone(q._claim())
            self.assertTrue(q.read(URL)['reused'])
            self.assertEqual(q.item(OTHER)['status'], 'queued')
        with patch.object(q.time, 'time', return_value=1090):
            self.assertEqual(q._claim()[0]['url'], OTHER)

    def test_interrupted_success_is_reconciled_from_receipt_without_page_visit(self):
        q.request(URL)
        row, g = q._claim()
        folder = app_paths.DATA/'supplier-capture-runs'/g['runId']
        folder.mkdir(parents=True)
        (folder/'result.json').write_text(json.dumps({'status': 'collected', 'supplier': supplier(), 'browser': ROUTE}), encoding='utf-8')
        with closing(q.connect()) as c, c:
            g['startedAt'] = time.time()-181
            q.save_gate(c, g)
        with patch.object(q, 'browser_capture') as browser:
            q.status()
            self.assertEqual(q.read(URL)['url'], URL)
        browser.assert_not_called()

    def test_fifo_and_cache_are_account_scoped(self):
        q.request(URL)
        with patch.object(q, 'SCOPE', 'account-b'):
            q.request(OTHER)
            self.assertIsNone(q._claim())
        row, g = q._claim()
        q._finish(row, g, {'status': 'collected', 'supplier': supplier()})
        with patch.object(q, 'SCOPE', 'account-b'), patch.object(q, 'cached_workspace', return_value=None):
            record = q.request(URL)
            self.assertEqual(record['status'], 'queued')
            with closing(q.connect()) as c, c:
                gate = q.gate(c);gate['nextAt'] = 0;q.save_gate(c, gate)
            self.assertEqual(q._claim()[0]['url'], OTHER)

    def test_untrusted_or_non_detail_urls_never_enqueue(self):
        for value in ('https://detail.1688.com.evil/offer/1.html', 'http://detail.1688.com/offer/1.html',
                      'https://user:pass@detail.1688.com/offer/1.html', 'https://s.1688.com/search'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                q.request(value)


if __name__ == '__main__':
    unittest.main()
