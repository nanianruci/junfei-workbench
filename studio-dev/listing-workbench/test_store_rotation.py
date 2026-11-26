import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import agent_center
import app_paths
import codex_operations
import image_engine
import listing_jobs
import publication_flow
import store_rotation as rotation
import store_rotation_setup


class RotationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        workbench = patch.object(app_paths, 'DATA', root)
        workbench.start()
        self.addCleanup(workbench.stop)
        jobs = patch.object(publication_flow, 'ROOT', root / 'jobs')
        jobs.start()
        self.addCleanup(jobs.stop)
        for module in (agent_center, listing_jobs):
            database = patch.object(module, 'DB', root / module.__name__)
            database.start()
            self.addCleanup(database.stop)
        self.targets = [
            {'id': str(i), 'store': 'shop-' + str(i), 'seller': 'seller-' + str(i),
             'erpAccount': 'erp', 'marketplace': 'US'} for i in (1, 2)]
        config = patch.object(rotation.erp_collection, 'destinations', return_value=self.targets)
        config.start()
        self.addCleanup(config.stop)
        images = patch.object(image_engine, 'list_tasks', return_value=[])
        images.start()
        self.addCleanup(images.stop)
        self.workspace = {'products': [{'id': str(i), 'name': 'item-' + str(i),
            'sourceUrl': 'https://detail.1688.com/offer/' + str(i) + '.html'} for i in range(1, 6)]}
        setup = store_rotation_setup.save(2, {'requestId': 'abc' * 8, 'revision': 0, 'markets': ['US'],
            'targets': [{'ziniaoStoreName': d['seller'], 'erpAccount': 'erp',
                         'usStoreName': d['store'], 'ukStoreName': ''} for d in self.targets]})['setup']
        self.run = rotation.update(2, self.workspace, {'action': 'start', 'setupId': setup['id']})

    def advance(self, action, **data):
        return rotation.update(2, self.workspace, {'action': action, 'runId': self.run['id'], **data})

    def record(self, number):
        product = str(number)
        job_id = format(number, '032x')
        scope = rotation.publication_scope(2, product, '1', ['US'])
        source = {'rotation': scope, 'publicationMarkets': ['US'], 'accountId': 2, 'productId': product,
                  'jobId': job_id, 'sha256': 'f' * 64, 'store': 'shop-1', 'goods': {'parentSku': 'parent'},
                  'destination': {'destinationId': '1'}, 'goodsItems': [{'sku': 'child', 'color': 'blue', 'size': 'S'}]}
        job = {'id': job_id, 'accountId': 2, 'productId': product, 'destinationId': '1',
               'markets': ['US'], 'status': 'submitted'}
        record = {'jobId': job_id, 'accountId': 2, 'productId': product, 'snapshotSha256': source['sha256'],
                  'usPublish': {'publishId': str(1000 + number), 'phase': 'publishing_verified', 'status': 'accepted',
                                'identity': {'countryCode': 'US', 'store': 'shop-1', 'accountId': 2, 'productId': product,
                                  'destinationId': '1', 'parentSku': 'parent', 'children': source['goodsItems']}}}
        folder = publication_flow.ROOT / job_id
        publication_flow.write_json(folder / 'input.json', {'input': source})
        publication_flow.write_json(folder / 'receipt.json', record)
        with publication_flow.connect() as db, db:
            import json
            db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?)', (job_id, 2, product, '1', 'submitted', json.dumps(job)))
        return job_id

    def test_counts_each_original_native_receipt_once_and_switches_after_three(self):
        for number in (1, 2, 3):
            self.advance('assign', productId=str(number))
            with self.assertRaisesRegex(ValueError, '站点范围'):
                rotation.publication_scope(2, str(number), '1', ['US', 'UK'])
            with self.assertRaisesRegex(ValueError, '未对账完成'):
                self.advance('assign', productId='5')
            job_id = self.record(number)
            current = self.advance('reconcile', publicationId=job_id)
            self.assertEqual(current, self.advance('reconcile', publicationId=job_id))
            self.assertEqual(current, rotation.status(2))
        self.assertEqual((current['round'], current['index'], current['count']), (1, 1, 0))
        with self.assertRaisesRegex(ValueError, '已有原发布任务'):
            self.advance('assign', productId='1')
        self.assertIsNone(rotation.status(3))

    def test_pause_and_unknown_retain_current_product_and_store(self):
        self.advance('assign', productId='1')
        paused = self.advance('pause')
        with self.assertRaisesRegex(ValueError, '恢复'):
            rotation.publication_scope(2, '1', '1', ['US'])
        with self.assertRaises(ValueError):
            self.advance('reconcile', publicationId='f' * 32)
        self.assertEqual(paused, rotation.status(2))
        self.assertEqual(self.advance('resume')['current']['productId'], '1')

    def test_withdraw_preserves_slot_and_blocks_products_with_existing_jobs(self):
        self.advance('assign', productId='1')
        withdrawn = self.advance('withdraw', productId='1', reason='供应商确认断货')
        self.assertIsNone(withdrawn['current'])
        self.assertEqual((withdrawn['index'], withdrawn['count']), (0, 0))
        self.assertEqual(withdrawn['withdrawn'][-1]['reason'], '供应商确认断货')
        self.advance('assign', productId='2')
        replay = self.advance('withdraw', productId='1', reason='供应商确认断货')
        self.assertEqual(replay['current']['productId'], '2')
        self.record(2)
        with self.assertRaisesRegex(ValueError, '不能淘汰'):
            self.advance('withdraw', productId='2', reason='发布状态待核实')

    def test_archive_keeps_history_and_allows_new_run(self):
        self.advance('assign', productId='1')
        self.advance('withdraw', productId='1', reason='货源不稳定')
        archived = self.advance('archive')
        self.assertTrue(archived['archivedAt'])
        restarted = rotation.update(2, self.workspace, {'action': 'start', 'setupId': self.run['setupId'],
                                                       'afterRunId': self.run['id']})
        self.assertNotEqual(restarted['id'], self.run['id'])
        self.assertEqual((restarted['round'], restarted['index'], restarted['count']), (1, 0, 0))
        with closing(rotation.connect()) as db:
            history = db.execute('SELECT body FROM history WHERE account=? AND run_id=?',
                                 (2, self.run['id'])).fetchone()
        self.assertIsNotNone(history)

    def test_withdraw_rejects_an_unresolved_original_copy_submission(self):
        assigned = self.advance('assign', productId='1')
        with listing_jobs.database() as db:
            db.execute('INSERT INTO listing_generation_jobs VALUES (?,?,?,?,?,?,?,?,?)',
                       ('original-copy', 2, '1', 'US', 'needs_review', 1, 1, 'request', '{}'))
        with self.assertRaisesRegex(ValueError, '原文案任务.*不能淘汰'):
            self.advance('withdraw', productId='1', reason='货源不稳定')
        self.assertEqual(assigned, rotation.status(2))

    def test_withdraw_retains_product_after_dispatch_before_native_receipt(self):
        assigned = self.advance('assign', productId='1')
        key = (2, '1', 'UK', 'original-request')
        item = {'status':'needs_review', 'request':{'type':'generate_copy','requestId':key[-1]}}
        with codex_operations.connect() as db:
            db.execute('INSERT INTO proposals VALUES(?,?,?,?,?)', (*key, json.dumps(item)))
        with self.assertRaisesRegex(ValueError, '不能淘汰.*original-request'):
            self.advance('withdraw', productId='1', reason='货源不稳定')
        self.assertEqual(assigned, rotation.status(2))
