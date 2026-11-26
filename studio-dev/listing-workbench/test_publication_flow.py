"""No real browser, uploads, model calls or ERP writes."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import publication_flow as flow


class PublicationFlowTests(unittest.TestCase):
    def setUp(self):
        policy = {'publishing': {'cross_marketplace_followup': {'enabled': True,
                    'source_marketplace': 'US', 'target_marketplace': 'UK', 'trigger': 'publishing'}}}
        config = patch('publication_policy._configuration', return_value=policy)
        config.start()
        self.addCleanup(config.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for name, value in [('ROOT', root / 'jobs'), ('SETTINGS', root / 'settings.json')]:
            p = patch.object(flow, name, value);p.start();self.addCleanup(p.stop)
        supplier_url = 'https://detail.1688.com/offer/821424146428.html'
        self.product = {'id': 'P', 'name': 'Product', 'sku': 'P', 'sourceUrl': supplier_url,
            'sourceImport': {'supplier': {'url': supplier_url, 'attributes': {'货号': '1156'}}},
            'specialSizeType': 'Standard', 'itemCondition': 'new_new',
            'itemConditionEvidence': {'source': 'user_confirmation', 'productId': 'P',
                'confirmedAt': '2026-11-18T10:00:00+00:00', 'instruction': 'Test fixture confirms new item'},
            'erpDelivery': {'UK': {'price': '24.19', 'quantity': 17, 'handlingDays': 3}}}
        self.workspace = {'products': [self.product], 'copy': {'P-US': {'title': 'Original'}}}
        self.destination = {'id': 'US', 'marketplace': 'US', 'store': 'Shop-US', 'accountId': 2,
            'storeName': 'merchant', 'browserBinding': 'binding', 'page': 'p1', 'storeId': '1',
            'seller': 'Seller', 'erpAccount': 'user'}
        self.snapshot = {'accountId': 2, 'productId': 'P', 'marketplace': 'US', 'store': 'Shop-US',
            'sha256': 'source-sha', 'destination': {'erpUserId': 19335, 'destinationId': 'US'},
            'goods': {'parentSku': 'P'}, 'goodsItems': [{'sku': 'P-White-S', 'color': 'White',
            'size': 'S', 'quantity': '18', 'imageIndices': [0]}], 'assets': [{'sha256': 'image'}]}
        self.receipt = {'draftId': '123', 'uploads': {'image': 'https://images.example/file.png'}}
        self.source_job = {'id': 'a' * 32}
        p = patch.object(flow.collection, 'destinations', return_value=[self.destination]);p.start();self.addCleanup(p.stop)
        flow.ACTIVE.clear();self.addCleanup(flow.ACTIVE.clear)

    def configure(self):
        return flow.save_settings(2, {'sourceDestinationId': 'US', 'storeName': 'Shop-UK',
                                   'shippingTemplate': 'Default UK'})

    def start(self):
        with patch.object(flow, 'source_receipt', return_value=(self.source_job, self.snapshot, self.receipt)), \
                patch.object(flow.collection, 'finished_snapshot', return_value=self.snapshot):
            return flow.start(2, self.workspace, {'productId': 'P', 'destinationId': 'US', 'confirmed': True})

    def test_single_us_requires_current_confirmation_and_freezes_no_uk_target(self):
        data = {'productId': 'P', 'destinationId': 'US', 'confirmed': True, 'markets': ['US']}
        with patch.object(flow, 'source_receipt', return_value=(self.source_job, self.snapshot, self.receipt)), \
                patch.object(flow.collection, 'finished_snapshot', return_value=self.snapshot), \
                patch.object(flow.publication_policy, 'default_followup', return_value=None), \
                patch.object(flow.threading, 'Thread') as thread:
            preview = flow.preview(2, self.workspace, 'start', data)
            self.assertEqual(preview['markets'], ['US'])
            self.assertIsNone(preview['targetStore'])
            with self.assertRaisesRegex(ValueError, '预检内容'):
                flow.start(2, self.workspace, data, confirmation='f' * 64)
            thread.assert_not_called()
            result = flow.start(2, self.workspace, data, confirmation=preview['digest'])
            packet = json.loads((flow.ROOT / result['id'] / 'input.json').read_text())['input']
            self.assertEqual(packet['publicationMarkets'], ['US'])
            self.assertNotIn('ukTarget', packet)
            thread.assert_called_once()

    def test_settings_owned_preserve_history_and_never_start(self):
        with patch.object(flow.threading, 'Thread') as thread:
            old = self.configure()
            self.assertEqual(old['id'], 'US-UK')
            self.assertEqual(flow.pairs(3), [])
            flow.save_settings(2, {'sourceDestinationId': 'US', 'storeName': 'Other-UK', 'shippingTemplate': 'UK'})
            thread.assert_not_called()
        self.assertEqual(json.loads(flow.SETTINGS.read_text())['history'][0], old)

    def test_no_implicit_publication_or_caller_draft_binding(self):
        self.configure()
        for payload in ({'productId':'P','destinationId':'US'},
                        {'productId':'P','destinationId':'US','confirmed':True,'draftId':'999'},
                        {'productId':'P','destinationId':'other','confirmed':True}):
            with self.assertRaises(ValueError):flow.start(2,self.workspace,payload)
        self.assertEqual(flow.list_jobs(2), [])

    def test_original_uk_sweatshirt_weave_keeps_evidence_tied_to_supplier_and_unsent_draft(self):
        folder = flow.ROOT / 'exact-job'
        folder.mkdir(parents=True)
        url = self.product['sourceUrl']
        job = {'id': 'exact-job', 'productId': 'P'}
        packet = {'input': {'sha256': 'source-sha', 'goods': {'sourceUrl': url}}}
        receipt = {'jobId': job['id'], 'productId': 'P', 'accountId': 2, 'snapshotSha256': 'source-sha',
                   'phase': 'uk_preparation', 'error': 'Missing required property: weave_type',
                   'usPublish': {'publishId': '123'}, 'ukCopy': {'phase': 'copy_verified'},
                   'ukPrepare': {'schemaObservation': {'country_code': 'GB', 'product_type_name': 'SWEATSHIRT'}}}
        evidence = {'value': 'Knit', 'source': 'user_confirmation', 'instruction': '针织（Knit）', 'productId': 'P', 'sourceUrl': url}
        flow.write_json(folder / 'receipt.json', receipt)
        with self.assertRaises(ValueError):
            flow._append_uk_sweatshirt_weave(2, job, folder, packet, {**evidence, 'sourceUrl': url + '?different'})
        self.assertFalse((folder / 'input.json').exists())
        flow._append_uk_sweatshirt_weave(2, job, folder, packet, evidence)
        amended = json.loads((folder / 'input.json').read_text(encoding='utf-8'))
        self.assertEqual(amended['input']['ukPreparation']['sweatshirtWeave'], evidence)
        self.assertEqual(amended['input']['sha256'], packet['input']['sha256'])
        receipt['ukPrepare']['saveIntendedAt'] = 'already recorded'
        flow.write_json(folder / 'receipt.json', receipt)
        with self.assertRaises(ValueError):
            flow._append_uk_sweatshirt_weave(2, job, folder, amended, evidence)

    def test_new_job_freezes_original_image_urls_and_skus_before_worker(self):
        self.configure()
        with patch.object(flow.threading, 'Thread') as thread:
            result = self.start()
            packet = json.loads((flow.ROOT / result['id'] / 'input.json').read_text())
            self.assertEqual(packet['input']['jobId'], result['id'])
            self.assertEqual(packet['input']['goodsItems'], self.snapshot['goodsItems'])
            self.assertEqual(packet['input']['urls'], self.receipt['uploads'])
            self.assertEqual(packet['input']['destination']['draftId'], '123')
            self.assertEqual(packet['input']['ukCommerce']['variantPrices'], {'P-White-S':'24.19'})
            self.assertEqual(thread.call_count, 1)
            self.assertEqual(flow.owned(2,result['id'])['status'], 'queued')

    def test_default_condition_does_not_hide_a_missing_us_draft(self):
        self.configure()
        self.product.pop('itemCondition')
        self.product.pop('itemConditionEvidence')
        original = copy.deepcopy(self.workspace)
        draft_gap = '请先完成本款美国站一键采集与草稿回读'
        with patch.object(flow, 'source_receipt', side_effect=ValueError(draft_gap)), \
                patch.object(flow.threading, 'Thread') as thread:
            target = flow.view(2, self.workspace, 'P')['targets'][0]
            self.assertFalse(target['sourceReady'])
            self.assertFalse(target['ready'])
            self.assertEqual(target['missing'], [draft_gap])
            thread.assert_not_called()
        self.assertEqual(self.workspace, original)
        self.assertEqual(flow.list_jobs(2), [])

    def test_verified_draft_without_item_condition_freezes_the_new_default(self):
        self.configure()
        self.product.pop('itemCondition')
        self.product.pop('itemConditionEvidence')
        with patch.object(flow, 'source_receipt', return_value=(self.source_job, self.snapshot, self.receipt)), \
                patch.object(flow.threading, 'Thread') as thread:
            target = flow.view(2, self.workspace, 'P')['targets'][0]
            self.assertTrue(target['sourceReady'])
            self.assertTrue(target['ready'])
            self.assertEqual(target['missing'], [])
            result = self.start()
            packet = json.loads((flow.ROOT / result['id'] / 'input.json').read_text())
            facts = packet['input']['publicationFacts']
            self.assertEqual(facts['itemCondition'], 'new_new')
            self.assertEqual(facts['itemConditionEvidence']['source'], 'user_configured_defaults')
            thread.assert_called_once()
        self.assertNotIn('itemCondition', self.product)

    def test_double_click_and_uncertain_start_reuse_same_job_no_replay(self):
        self.configure()
        with patch.object(flow.threading, 'Thread') as thread:
            first=self.start();second=self.start()
            self.assertEqual(first['id'],second['id']);self.assertTrue(second['reused'])
            job=flow.owned(2,first['id']);job.update(status='needs_reconciliation',error='unknown');flow.persist(job)
            third=self.start();self.assertEqual(third['id'],first['id']);self.assertEqual(thread.call_count,1)
        with self.assertRaises(ValueError):flow.owned(3,first['id'])

    def test_publication_freezes_global_policy_preserving_old_source_without_changing_collection_or_resume(self):
        self.configure()
        self.product.update(countryOfOrigin='US', countryOfOriginEvidence='Old recorded source evidence',
                            careInstructions='Dry Clean Only；旧资料来源备注')
        before_product = copy.deepcopy(self.product)
        original = copy.deepcopy(self.snapshot)
        with patch.object(flow.threading, 'Thread'):
            result = self.start()
            path = flow.ROOT / result['id'] / 'input.json'
            packet = json.loads(path.read_text())
            facts = packet['input']['publicationFacts']
            self.assertEqual(facts['countryOfOrigin'], 'CN')
            self.assertNotIn('countryOfOriginEvidence', facts)
            self.assertEqual(facts['sourceProductFacts']['countryOfOriginEvidence'], 'Old recorded source evidence')
            self.assertEqual(facts['countryOfOriginSource']['source'], 'user_configured_defaults')
            self.assertEqual(facts['careInstructions'], 'Machine Wash')
            self.assertEqual(facts['sourceProductFacts']['careInstructions'], before_product['careInstructions'])
            self.assertEqual({entry['field'] for entry in facts['policyContradictions']}, {'countryOfOrigin', 'careInstructions'})
            self.assertEqual(self.product, before_product)
            self.assertEqual(self.snapshot, original)
            self.assertEqual(packet['input']['sha256'], original['sha256'])
            before = path.read_bytes()
            self.product.update(countryOfOrigin='US', countryOfOriginEvidence='new unsaved preparation')
            job = flow.owned(2, result['id'])
            job['status'] = 'needs_reconciliation';flow.persist(job)
            flow.resume(2, {'id': result['id'], 'confirmed': True})
            self.assertEqual(path.read_bytes(), before)

    def test_new_publication_uses_user_defaults_without_rewriting_product_or_inferring_supplier_facts(self):
        self.configure()
        before = copy.deepcopy(self.product)
        with patch.object(flow.threading, 'Thread'):
            result = self.start()
        packet = json.loads((flow.ROOT / result['id'] / 'input.json').read_text())
        facts = packet['input']['publicationFacts']
        self.assertEqual(facts['countryOfOrigin'], 'CN')
        self.assertEqual(facts['careInstructions'], 'Machine Wash')
        for key in ('countryOfOriginSource', 'careInstructionsSource'):
            self.assertEqual(facts[key]['source'], 'user_configured_defaults')
        self.assertNotIn('countryOfOriginEvidence', facts)
        self.assertEqual(self.product, before)

    def test_legacy_origin_values_do_not_override_or_block_global_publication_policy(self):
        self.configure()
        self.product['countryOfOrigin'] = 'Legacy source spelling'
        with patch.object(flow.threading, 'Thread') as thread:
            result = self.start()
            facts = json.loads((flow.ROOT / result['id'] / 'input.json').read_text())['input']['publicationFacts']
            self.assertEqual(facts['countryOfOrigin'], 'CN')
            self.assertEqual(facts['sourceProductFacts']['countryOfOrigin'], 'Legacy source spelling')
            self.assertEqual(thread.call_count, 1)

    def test_care_and_bound_supplier_article_number_freeze_without_competitor_inference(self):
        self.configure()
        self.product.update(careInstructions=' Follow the confirmed garment care label ',
                            sourceUrl='https://detail.1688.com/offer/821424146428.html',
                            sourceImport={'supplier': {
                                'url': 'https://detail.1688.com/offer/821424146428.html',
                                'attributes': {'货号': '1156'}, 'retrievedAt': '2026-11-15T02:47:10.208Z'},
                                'amazon': {'attributes': {'model_name': 'competitor-model', 'careInstructions': 'Machine Wash'}}})
        original = copy.deepcopy(self.snapshot)
        with patch.object(flow.threading, 'Thread'):
            result = self.start()
            path = flow.ROOT / result['id'] / 'input.json'
            facts = json.loads(path.read_text())['input']['publicationFacts']
            self.assertEqual(facts['careInstructions'], 'Machine Wash')
            self.assertEqual(facts['sourceProductFacts']['careInstructions'], self.product['careInstructions'])
            self.assertEqual(facts['supplierModelNumber'], '1156')
            self.assertEqual(facts['modelName'], '1156')
            self.assertEqual(facts['modelNameEvidence']['path'], 'sourceImport.supplier.attributes.货号')
            self.assertEqual(facts['modelNameEvidence']['sourceUrl'], self.product['sourceUrl'])
            self.assertEqual(facts['countryOfOrigin'], 'CN')
            self.assertEqual(facts['countryOfOriginSource']['source'], 'user_configured_defaults')
            self.assertEqual(facts['careInstructionsSource']['source'], 'user_configured_defaults')
            self.assertEqual(self.snapshot, original)
            before = path.read_bytes()
            self.product['careInstructions'] = 'Changed later'
            self.product['sourceImport']['supplier']['attributes']['货号'] = 'changed-later'
            job = flow.owned(2, result['id']);job['status'] = 'needs_reconciliation';flow.persist(job)
            flow.resume(2, {'id': result['id'], 'confirmed': True})
            self.assertEqual(path.read_bytes(), before)

    def test_empty_or_unbound_facts_do_not_use_competitor_or_supplier_care_text(self):
        product = {'careInstructions': '', 'sourceUrl': 'https://detail.1688.com/offer/1.html',
                   'sourceImport': {'supplier': {'url': 'https://detail.1688.com/offer/2.html',
                       'attributes': {'货号': 'wrong-product', '护理说明': 'Machine Wash'}},
                       'amazon': {'attributes': {'货号': 'competitor', 'careInstructions': 'Machine Wash'}}}}
        facts = flow.publication_inputs.publication_facts(product)
        self.assertEqual(facts['countryOfOrigin'], 'CN')
        self.assertEqual(facts['careInstructions'], 'Machine Wash')
        self.assertEqual(facts['careInstructionsSource']['source'], 'user_configured_defaults')
        self.assertNotIn('modelName', facts)
        for care in (True, 'x' * 501):
            product['careInstructions'] = care
            facts = flow.publication_inputs.publication_facts(product)
            self.assertEqual(facts['careInstructions'], 'Machine Wash')
            self.assertEqual(facts['sourceProductFacts']['careInstructions'], care)

    def test_explicit_user_condition_freezes_before_worker_and_is_not_regenerated_on_resume(self):
        self.configure()
        evidence = {'source': 'user_confirmation', 'productId': 'P',
                    'confirmedAt': '2026-11-18T10:00:00Z',
                    'instruction': 'Test-only confirmation that product P is brand new.'}
        self.product.update(itemCondition='new_new', itemConditionEvidence=evidence)
        original_product, original_source = copy.deepcopy(self.product), copy.deepcopy(self.snapshot)
        with patch.object(flow.threading, 'Thread') as thread:
            result = self.start()
            path = flow.ROOT / result['id'] / 'input.json'
            before = path.read_bytes()
            facts = json.loads(before)['input']['publicationFacts']
            self.assertEqual(facts['itemCondition'], 'new_new')
            self.assertEqual(facts['itemConditionEvidence'], evidence)
            self.assertEqual(thread.call_count, 1)
            self.assertEqual(self.product, original_product)
            self.assertEqual(self.snapshot, original_source)
            self.product['itemConditionEvidence']['instruction'] = 'Changed later'
            self.product['itemCondition'] = 'used_like_new'
            job = flow.owned(2, result['id']);job['status'] = 'needs_reconciliation';flow.persist(job)
            flow.resume(2, {'id': result['id'], 'confirmed': True})
            self.assertEqual(path.read_bytes(), before)

    def test_incomplete_user_condition_stops_before_freezing_or_launching_a_job(self):
        self.configure()
        self.product.update(itemCondition='new_new', itemConditionEvidence={
            'source': 'user_confirmation', 'productId': 'another-product',
            'confirmedAt': '2026-11-18T10:00:00Z', 'instruction': 'Test confirmation for another product.'})
        with patch.object(flow.threading, 'Thread') as thread:
            with self.assertRaisesRegex(ValueError, '商品成色'):
                self.start()
            thread.assert_not_called()
        self.assertEqual(flow.list_jobs(2), [])
        self.assertEqual(list(flow.ROOT.glob('*/input.json')), [])

    def test_resume_keeps_frozen_configuration_and_receipt(self):
        self.configure()
        with patch.object(flow.threading,'Thread') as thread:
            result=self.start();job=flow.owned(2,result['id']);job['status']='needs_reconciliation';flow.persist(job)
            path=flow.ROOT/job['id']/'input.json';before=path.read_bytes()
            receipt=flow.ROOT/job['id']/'receipt.json';flow.write_json(receipt,{'copyIntent':'original'})
            continued=flow.resume(2,{'id':job['id'],'confirmed':True})
            self.assertEqual(continued['id'],job['id']);self.assertEqual(path.read_bytes(),before)
            self.assertEqual(json.loads(receipt.read_text()),{'copyIntent':'original'})
            self.assertEqual(thread.call_count,2)

    def test_uk_category_amendment_changes_only_unsaved_target_and_keeps_evidence(self):
        self.configure()
        with patch.object(flow.threading,'Thread'):
            result=self.start(); job=flow.owned(2,result['id'])
            job.update(status='needs_reconciliation',error='ERP类目推荐有歧义，请补充路径'); flow.persist(job)
            folder=flow.ROOT/job['id']; before=json.loads((folder/'input.json').read_text())
            receipt={'ukCopy':{'phase':'copy_verified','draftId':'200'},'ukPrepare':{},'ukPublish':{}}
            flow.write_json(folder/'receipt.json',receipt)
            flow.resume(2,{'id':job['id'],'confirmed':True,'ukCategoryPath':'Fashion Women Sweatshirts','categoryEvidence':'observed-category-options.json'})
            after=json.loads((folder/'input.json').read_text())
            expected=copy.deepcopy(before);expected['input']['ukTarget']['categoryHints']={'path':'Fashion Women Sweatshirts'}
            self.assertEqual(after,expected)
            amendment=json.loads(next((folder/'amendments').glob('*.json')).read_text())
            self.assertEqual(amendment['previousInput'],before)
            self.assertEqual(json.loads((folder/'receipt.json').read_text()),receipt)

    def test_uk_category_amendment_refuses_after_save_or_without_ambiguity(self):
        self.configure()
        with patch.object(flow.threading,'Thread'):
            result=self.start(); job=flow.owned(2,result['id']);folder=flow.ROOT/job['id']; before=(folder/'input.json').read_bytes()
            for error,prepare in [('unknown',{}),('ERP类目推荐有歧义',{'saveIntendedAt':'already'})]:
                job.update(status='needs_reconciliation',error=error);flow.persist(job)
                flow.write_json(folder/'receipt.json',{'ukCopy':{'phase':'copy_verified'},'ukPrepare':prepare})
                with self.assertRaises(ValueError):flow.resume(2,{'id':job['id'],'confirmed':True,'ukCategoryPath':'Fashion Women Sweatshirts','categoryEvidence':'observed.json'})
                self.assertEqual((folder/'input.json').read_bytes(),before)

    def test_unsaved_uk_shipping_override_does_not_change_frozen_copy_target(self):
        self.configure()
        with patch.object(flow.threading,'Thread'):
            result=self.start();job=flow.owned(2,result['id']);job.update(status='needs_reconciliation',error='template not found');flow.persist(job)
            folder=flow.ROOT/job['id'];before=json.loads((folder/'input.json').read_text())
            flow.write_json(folder/'receipt.json',{'ukCopy':{'phase':'copy_verified'},'ukPrepare':{}})
            flow.resume(2,{'id':job['id'],'confirmed':True,'ukShippingTemplate':'Actual UK Template','shippingEvidence':'actual-options.json'})
            after=json.loads((folder/'input.json').read_text());expected=copy.deepcopy(before)
            expected['input']['ukPreparation']={'shippingTemplate':{'name':'Actual UK Template'}}
            self.assertEqual(after,expected)

    def test_missing_price_or_changed_source_stops_before_any_job(self):
        self.configure()
        self.product['erpDelivery']['UK'].pop('price')
        with patch.object(flow.threading,'Thread') as thread:
            with self.assertRaises(ValueError):self.start()
            thread.assert_not_called()
        self.product['erpDelivery']['UK']['price']='24.19'
        with patch.object(flow,'source_receipt',return_value=(self.source_job,self.snapshot,self.receipt)), \
                patch.object(flow.collection,'finished_snapshot',return_value={**self.snapshot,'goods':{'parentSku':'changed'}}):
            with self.assertRaisesRegex(ValueError,'当前成品'):flow.start(2,self.workspace,{'productId':'P','destinationId':'US','confirmed':True})
        self.assertEqual(flow.list_jobs(2),[])

    def test_commerce_preserves_all_other_product_and_copy_history(self):
        before=copy.deepcopy(self.workspace)
        with patch.object(flow.collection,'eligible_variant_rows',return_value=[('P-White-S',)]):
            flow.update_commerce(self.workspace,{'productId':'P','revision':7,'details':{
                'price':'25.91','inventoryPolicy':'preserve_source','handlingDays':'3'}})
            with self.assertRaises(ValueError):flow.update_commerce(self.workspace,{'productId':'P','details':{
                'price':'25','quantity':'3','handlingDays':'3','variantPrices':{'other':'1'}}})
        self.assertEqual(self.workspace['copy'],before['copy'])
        self.assertEqual(self.product['erpDeliveryHistory'][0]['before'],before['products'][0]['erpDelivery']['UK'])

    def successful_record(self):
        r={'accountId':2,'productId':'P','snapshotSha256':'source-sha','ukCopy':{'draftId':'456'},
           'ukPrepare':{'phase':'uk_draft_verified'}}
        for key,country,store,destination in [('usPublish','US','Shop-US','US'),('ukPublish','GB','Shop-UK','US-UK')]:
            r[key]={'phase':'publishing_verified','publishId':'789','status':'accepted','identity':{
                'accountId':2,'productId':'P','destinationId':destination,'countryCode':country,'store':store,
                'parentSku':'P','children':[{'sku':'P-White-S','color':'White','size':'S'}]}}
        return r

    def test_final_status_needs_both_real_ids_correct_store_skus_and_colours(self):
        source={**self.snapshot,'ukTarget':{'id':'US-UK','storeName':'Shop-UK'}}
        record=self.successful_record();flow.validate_submitted(record,source)
        for identity in ({'countryCode':'US'},{'destinationId':'US'},{'parentSku':'P-UK'},
                         {'children':[{'sku':'P-White-S','color':'Black','size':'S'}]}):
            bad=copy.deepcopy(record);bad['ukPublish']['identity'].update(identity)
            with self.assertRaises(ValueError):flow.validate_submitted(bad,source)
        bad=copy.deepcopy(record);bad['usPublish']['publishId']=None
        with self.assertRaises(ValueError):flow.validate_submitted(bad,source)

    def test_worker_requires_us_acceptance_before_uk_login_and_preserves_original_ids(self):
        self.configure()
        with patch.object(flow.threading,'Thread'):
            result=self.start()
        job=flow.owned(2,result['id']);record=self.successful_record();events=[]
        def login(job,connection,market):events.append(market)
        def run(job,connection,phase):
            events.append(phase)
            return record
        from contextlib import nullcontext
        with patch.object(flow.collection,'browser_lease',return_value=nullcontext()), \
                patch.object(flow,'login',side_effect=login),patch.object(flow,'run_browser',side_effect=run):
            flow.execute(job)
        self.assertEqual(events,['US','us','UK','uk'])
        self.assertEqual(flow.owned(2,job['id'])['status'],'submitted')
        self.assertTrue(flow.public(job)['publishSubmitted']);self.assertFalse(flow.public(job)['onlineVerified'])

    def test_failed_us_result_stops_before_uk_session_or_copy(self):
        self.configure()
        with patch.object(flow.threading,'Thread'):result=self.start()
        job=flow.owned(2,result['id'])
        from contextlib import nullcontext
        with patch.object(flow.collection,'browser_lease',return_value=nullcontext()), \
                patch.object(flow,'login') as login,patch.object(flow,'run_browser',return_value={'usPublish':{'status':'unknown'}}) as run:
            flow.execute(job)
        self.assertEqual(login.call_count,1);self.assertEqual(run.call_count,1)
        self.assertEqual(flow.owned(2,job['id'])['status'],'needs_reconciliation')

    def test_previously_accepted_us_is_rechecked_before_uk_without_switching_to_us_login(self):
        self.configure()
        with patch.object(flow.threading, 'Thread'):
            result = self.start()
        job = flow.owned(2, result['id'])
        record = self.successful_record()
        # Preserve a past failure while allowing a new verified observation.
        record['usPublish']['failureObservation'] = {'status': 'failed', 'failed': True, 'verification': 'failed'}
        record['usPublish']['latestObservation'] = {'status': 'publishing', 'failed': False, 'verification': 'accepted'}
        flow.write_json(flow.ROOT / job['id'] / 'receipt.json', record)
        events = []
        def login(_job, _connection, market):
            events.append('login-' + market)
        def run(_job, _connection, phase):
            events.append('read-' + phase)
            return record
        from contextlib import nullcontext
        with patch.object(flow.collection, 'browser_lease', return_value=nullcontext()), \
                patch.object(flow, 'login', side_effect=login), patch.object(flow, 'run_browser', side_effect=run):
            flow.execute(job)
        self.assertEqual(events, ['read-us', 'login-UK', 'read-uk'])
        self.assertEqual(flow.owned(2, job['id'])['status'], 'submitted')

    def test_known_accepted_us_moving_online_allows_only_a_verified_complete_latest_observation(self):
        self.configure()
        with patch.object(flow.threading, 'Thread'):
            result = self.start()
        job = flow.owned(2, result['id'])
        base = self.successful_record()
        observation = {'status': 'online', 'verification': 'accepted', 'failed': False,
                       'allChildrenOnline': True}
        variants = [(dict(observation, status=status), True)
                    for status in ('online', 'buyable', 'discoverable', 'active')]
        variants.extend((dict(observation, **override), False) for override in (
            {'verification': 'pending'}, {'verification': None}, {'failed': True},
            {'failed': None}, {'allChildrenOnline': False}))
        for latest, allowed in variants:
            with self.subTest(latest=latest):
                record = copy.deepcopy(base)
                record['usPublish']['latestObservation'] = latest
                flow.write_json(flow.ROOT / job['id'] / 'receipt.json', record)
                events = []
                def login(_job, _connection, market):
                    events.append('login-' + market)
                def run(_job, _connection, phase):
                    events.append('read-' + phase)
                    return record
                from contextlib import nullcontext
                with patch.object(flow.collection, 'browser_lease', return_value=nullcontext()), \
                        patch.object(flow, 'login', side_effect=login), patch.object(flow, 'run_browser', side_effect=run):
                    flow.execute(job)
                self.assertEqual(events, ['read-us', 'login-UK', 'read-uk'] if allowed else ['read-us'])
                self.assertEqual(flow.owned(2, job['id'])['status'], 'submitted' if allowed else 'needs_reconciliation')
                self.assertEqual(record['usPublish']['status'], 'accepted')

    def test_fresh_us_failure_blocks_uk_even_if_historical_identity_still_says_accepted(self):
        self.configure()
        with patch.object(flow.threading, 'Thread'):
            result = self.start()
        job = flow.owned(2, result['id'])
        historical = self.successful_record()
        flow.write_json(flow.ROOT / job['id'] / 'receipt.json', historical)
        for phase, observation in (
                ('publication_failed', {'status': 'failed', 'failed': True, 'verification': 'failed'}),
                ('publication_failed', {'status': 'accepted', 'failed': True, 'verification': 'failed'}),
                ('publishing_verified', {'status': 'accepted', 'failed': True, 'verification': 'failed'})):
            current = copy.deepcopy(historical)
            current['usPublish'].update(phase=phase, latestObservation=observation,
                                        failureObservation=observation)
            events = []
            def run(_job, _connection, phase):
                events.append(phase)
                self.assertEqual(phase, 'us')
                return current
            from contextlib import nullcontext
            with patch.object(flow.collection, 'browser_lease', return_value=nullcontext()), \
                    patch.object(flow, 'login') as login, patch.object(flow, 'run_browser', side_effect=run):
                flow.execute(job)
            login.assert_not_called()
            self.assertEqual(events, ['us'])
            self.assertEqual(flow.owned(2, job['id'])['status'], 'needs_reconciliation')
            self.assertEqual(current['usPublish']['identity'], historical['usPublish']['identity'])

    def test_failed_us_recheck_exception_stops_before_any_uk_login_or_execution(self):
        self.configure()
        with patch.object(flow.threading, 'Thread'):
            result = self.start()
        job = flow.owned(2, result['id'])
        flow.write_json(flow.ROOT / job['id'] / 'receipt.json', self.successful_record())
        from contextlib import nullcontext
        with patch.object(flow.collection, 'browser_lease', return_value=nullcontext()), \
                patch.object(flow, 'login') as login, \
                patch.object(flow, 'run_browser', side_effect=ValueError('original US task now failed')) as run:
            flow.execute(job)
        login.assert_not_called()
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[2], 'us')
        self.assertEqual(flow.owned(2, job['id'])['status'], 'needs_reconciliation')

    def test_uncertain_us_intent_or_candidate_reconciles_without_us_login_or_new_submission(self):
        self.configure()
        with patch.object(flow.threading, 'Thread'):
            result = self.start()
        job = flow.owned(2, result['id'])
        for checkpoint in ({'intent': {'draftId': '123'}}, {'observedCandidate': {'taskId': '789'}}):
            flow.write_json(flow.ROOT / job['id'] / 'receipt.json', {'usPublish': checkpoint})
            events = []
            def login(_job, _connection, market):
                events.append('login-' + market)
            def run(_job, _connection, phase):
                events.append('read-' + phase)
                return self.successful_record()
            from contextlib import nullcontext
            with patch.object(flow.collection, 'browser_lease', return_value=nullcontext()), \
                    patch.object(flow, 'login', side_effect=login), patch.object(flow, 'run_browser', side_effect=run):
                flow.execute(job)
            self.assertEqual(events, ['read-us', 'login-UK', 'read-uk'])

    def test_incomplete_historical_us_receipt_is_not_treated_as_a_new_publication(self):
        self.configure()
        with patch.object(flow.threading, 'Thread'):
            result = self.start()
        job = flow.owned(2, result['id'])
        flow.write_json(flow.ROOT / job['id'] / 'receipt.json', {
            'usPublish': {'phase': 'publishing_verified', 'status': 'accepted', 'identity': {'publishId': '789'}}})
        from contextlib import nullcontext
        with patch.object(flow.collection, 'browser_lease', return_value=nullcontext()), \
                patch.object(flow, 'login') as login, patch.object(flow, 'run_browser') as run:
            flow.execute(job)
        login.assert_not_called();run.assert_not_called()
        self.assertEqual(flow.owned(2, job['id'])['status'], 'needs_reconciliation')

if __name__=='__main__':unittest.main()
