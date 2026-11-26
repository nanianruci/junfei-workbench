import copy
import csv
import json
import sqlite3
import tempfile
import unittest
import hashlib
from datetime import date, timedelta
from pathlib import Path
from pipeline import Pipeline, CHECKS, STAGES, ROOT, doctor, read_json, readiness, reserve_selected_candidate


class PipelineTests(unittest.TestCase):
    def test_new_evidence_can_block_ready_without_claiming_or_reserving_images(self):
        job=self.p.add('US','B000000091','us')
        before=dict(self.p.get(job))
        self.p.block_ready(job,'Supplier photograph contradicts actual variant texture')
        after=dict(self.p.get(job))
        self.assertEqual(after['status'],'blocked')
        for key in ('stage','token','attempts','config'):
            self.assertEqual(after[key],before[key])
        self.assertEqual(self.p.db.execute('SELECT COUNT(*) FROM image_costs WHERE job_id=?',(job,)).fetchone()[0],0)
        with self.assertRaises(ValueError):self.p.block_ready(job,'repeat')

    def test_ready_block_cannot_change_running_or_unknown_tasks(self):
        job=self.p.add('US','B000000091','us')
        with self.assertRaises(ValueError):self.p.block_ready(job,' ')
        token=self.p.claim(job)['token']
        before=dict(self.p.get(job))
        with self.assertRaises(ValueError):self.p.block_ready(job,'Do not bypass current claim')
        self.assertEqual(dict(self.p.get(job)),before)
        self.p.hold(job,token,'uncertain','External request result unknown')
        before=dict(self.p.get(job))
        with self.assertRaises(ValueError):self.p.block_ready(job,'Do not replace unknown result')
        self.assertEqual(dict(self.p.get(job)),before)

    def test_thirty_day_window_is_snapshotted_and_rejects_day_31(self):
        self.cfg['selection']['window_days'] = 30
        job = self.p.add('US', 'B000000092', 'us')
        token = self.p.claim(job)['token']
        e = self.evidence(job, 'selection')
        e['selection_evidence']['seller_listing_date'] = (date.today()-timedelta(days=31)).isoformat()
        with self.assertRaises(ValueError): self.p.finish(job, token, e)
        e['selection_evidence']['seller_listing_date'] = (date.today()-timedelta(days=30)).isoformat()
        self.cfg['selection']['window_days'] = 7
        self.p.finish(job, token, e)
        self.assertEqual(self.p.get(job)['stage'], 1)

    def test_generic_only_and_explicitly_unsearched_patents_are_task_scoped(self):
        self.cfg['selection']['brand_policy']={'mode':'generic_only'}
        instruction='用户要求不查专利'
        self.cfg['selection']['patent_search_policy']={'mode':'skip_by_user_instruction','instruction':instruction}
        job=self.p.add('US','B000000095','us');token=self.p.claim(job)['token'];e=self.evidence(job,'selection')
        checks=e['selection_evidence']['ip_checks']
        checks['brand'].update(observedBrand='Generic',url='https://www.amazon.com/dp/B000000095',raw='Brand: Generic')
        for key in ('designPatent','utilityPatent'):
            checks[key]={'status':'skipped_by_user','searchPerformed':False,'instruction':instruction,'judgment':'未检索，不作无专利结论'}
        for key,value in [('raw','Brand: Other'),('url','https://www.amazon.com/dp/B000000094'),('observedBrand','Other')]:
            bad=copy.deepcopy(e);bad['selection_evidence']['ip_checks']['brand'][key]=value
            with self.assertRaises(ValueError): self.p.finish(job,token,bad)
        bad=copy.deepcopy(e);bad['selection_evidence']['ip_checks']['designPatent']['status']='clear'
        with self.assertRaises(ValueError): self.p.finish(job,token,bad)
        self.p.finish(job,token,e)
        self.assertEqual(self.p.get(job)['stage'],1)

    def test_design_screening_policy_requires_actual_search_and_no_obvious_match(self):
        job=self.p.add('US','B000000096','us');token=self.p.claim(job)['token'];e=self.evidence(job,'selection')
        item=e['selection_evidence']['ip_checks']['designPatent']
        item.update(status='no_obvious_match',searchPerformed=True,obviousOverallMatchFound=False,drawingsAvailable=False)
        for field, value in [('searchPerformed',False),('obviousOverallMatchFound',True),('raw','')]:
            bad=copy.deepcopy(e);bad['selection_evidence']['ip_checks']['designPatent'][field]=value
            with self.assertRaises(ValueError):self.p.finish(job,token,bad)
        self.p.finish(job,token,e)
        self.assertEqual(self.p.get(job)['stage'],1)

    def test_user_default_supplier_images_are_not_merchant_license_and_require_source_review(self):
        self.cfg['images'].update(supplier_image_policy='user_default_supplier_images',
                                  supplier_image_authorization={'instruction':'historical policy fixture'})
        job=self.p.add('US','B000000097','us');token=self.p.claim(job)['token'];e=self.evidence(job,'selection')
        item=e['selection_evidence']['ip_checks']['imageRights']
        item.update(status='user_default',basis='user_default_supplier_use_not_supplier_license',
                    url='https://detail.1688.com/offer/952542058876.html',visuallyReviewed=True,noUnauthorizedMarks=True)
        bad=copy.deepcopy(e);bad['selection_evidence']['ip_checks']['imageRights']['noUnauthorizedMarks']=False
        with self.assertRaises(ValueError):self.p.finish(job,token,bad)
        self.p.finish(job,token,e)
        self.assertEqual(self.p.get(job)['stage'],1)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cfg = read_json(ROOT / 'automation/config.example.json')
        self.cfg['images']['enabled'] = False
        self.cfg['selection']['freshness_basis'] = 'seller_listing'
        self.cfg['selection'].pop('brand_policy', None)
        self.cfg['selection'].pop('patent_search_policy', None)
        self.cfg['selection'].pop('same_product_rank_policy', None)
        self.cfg['selection']['design_patent_policy'] = {'mode':'obvious_overall_match_only','instruction':'historical policy fixture'}
        self.cfg['profiles'] = {
            'us': {'store': 'test-us', 'marketplace': 'US', 'shipping_template': 'test', 'quantity': 88},
            'uk': {'store': 'test-uk', 'marketplace': 'UK', 'shipping_template': 'uk', 'quantity': 12}}
        self.db = Path(self.temp.name) / 'test.sqlite3'
        self.selected = Path(self.temp.name) / 'selected.csv'
        self.selected.write_text('marketplace,asin,selected_at,source,status\nUS,B099999999,2026-01-01,test,selected\n', encoding='utf-8')
        self.cfg['selection']['selected_ledger'] = str(self.selected.resolve())
        self.p = Pipeline(self.db, self.cfg)

    def tearDown(self):
        self.p.db.close()
        self.temp.cleanup()

    def evidence(self, job, stage):
        result = {'job_id': job, 'stage': stage, 'simulated': False, 'summary': 'test fixture only',
                  'checks': dict.fromkeys(CHECKS[stage], True), 'sources': [{'url':'https://example.com/source','checked_at':date.today().isoformat(),'raw':'test fixture only'}]}
        if stage == 'selection':
            job_row = self.p.get(job)
            asin = job_row['source_id']
            def source(url, raw): return {'url':url,'checked_at':date.today().isoformat(),'raw':raw}
            item = {'asin':asin,'marketplace':'US','seller_id':'A1234567890123',
                    'collection_date':date.today().isoformat(),'seller_listing_date':date.today().isoformat(),
                    'freshness_scope':'seller_listing','seller_listing_source':source('https://www.sellersprite.com/newest','seller listing date: '+date.today().isoformat()),'seller_country':'CN',
                    'seller_country_source':source('https://www.sellersprite.com/seller','Country: China'),
                    'fulfillment_type':'FBM','fulfillment_source':source('https://www.amazon.com/dp/'+asin,'Fulfilled by FBM seller'),
                    'precise_keyword':'fixture product','front_search':{**source('https://www.amazon.com/s?k=fixture+product','Amazon search results'), 'status':'opened'},
                    'match':{'asin':'B012345678','level':'strong','source':source('https://www.amazon.com/dp/B012345678','same structure and specifications')},
                    'matched_rank':{'source':'SellerSprite same-product search','asin':'B012345678','value':1234,**source('https://www.sellersprite.com/v2/product-research?asin=B012345678','1,234')},
                    'ip_checks':{name:{'status':'clear','query':'fixture '+name,'judgment':'fixture review clear',**source('https://www.uspto.gov/search','fixture evidence')} for name in ('brand','trademark','designPatent','utilityPatent','imageRights')},
                    'dedupe':{'ledger_path':str(self.selected.resolve()),'ledger_sha256':hashlib.sha256(self.selected.read_bytes()).hexdigest()},
                    'daily_quota':{'marketplace':'US','seller_id':'A1234567890123','collection_date':date.today().isoformat(),'count_after_selection':1,'source':source('https://www.sellersprite.com/seller','one candidate this collection day')}}
            result['selection_evidence'] = item
        return result

    def test_product_date_policy_is_snapshotted_and_rejects_false_sources(self):
        self.cfg['selection']['freshness_basis'] = 'sellersprite_product_listing'
        job = self.p.add('US', 'B000000099', 'us')
        token = self.p.claim(job)['token']
        e = self.evidence(job, 'selection')
        item = e['selection_evidence']
        item.update(freshness_scope='sellersprite_product_listing', product_listing_date=date.today().isoformat(),
                    product_listing_source={'url':'https://www.sellersprite.com/product',
                    'checked_at':date.today().isoformat(), 'raw':'上架时间：'+date.today().isoformat()})
        for override in ({'raw':'Date First Available: '+date.today().isoformat()},
                         {'url':'https://www.amazon.com/'}, {'url':'https://sellersprite.com.evil.test/'}):
            bad = copy.deepcopy(e); bad['selection_evidence']['product_listing_source'].update(override)
            with self.assertRaises(ValueError): self.p.finish(job, token, bad)
        self.p.finish(job, token, e)
        self.assertEqual(self.p.get(job)['stage'], 1)

    def test_mcp_rank_matches_actual_other_asin_payload(self):
        job = self.p.add('US', 'B000000098', 'us'); token = self.p.claim(job)['token']
        e = self.evidence(job, 'selection')
        rank = e['selection_evidence']['matched_rank']
        rank.update(capture_type='sellersprite_mcp_asin_detail', url='https://mcp.sellersprite.com/mcp',
                    raw=json.dumps({'asin':'B012345678','bsrRank':1234}))
        for payload in ({'asin':'B000000098','bsrRank':1234}, {'asin':'B012345678','bsrRank':'1234'}):
            bad=copy.deepcopy(e);bad['selection_evidence']['matched_rank']['raw']=json.dumps(payload)
            with self.assertRaises(ValueError):self.p.finish(job,token,bad)
        self.p.finish(job,token,e)

    def test_original_sellersprite_extension_export_date_passes_selection(self):
        import openpyxl
        from sellersprite_date import export_source
        self.cfg['selection']['freshness_basis']='sellersprite_product_listing'
        job=self.p.add('US','B000000093','us');token=self.p.claim(job)['token'];e=self.evidence(job,'selection')
        path=Path(self.temp.name)/'export.xlsx';book=openpyxl.Workbook();sheet=book.active;sheet.title='US'
        sheet.append(['ASIN','上架时间','商品标题','商品详情页链接'])
        sheet.append(['B000000093',date.today().isoformat(),'Fixture product','https://www.amazon.com/dp/B000000093'])
        book.create_sheet('Note').append(['卖家精灵官网：https://www.sellersprite.com']);book.save(path);book.close()
        e['selection_evidence'].update(freshness_scope='sellersprite_product_listing',product_listing_date=date.today().isoformat(),
            product_listing_source=export_source(path.resolve(),'US','B000000093',date.today().isoformat()))
        bad=copy.deepcopy(e);bad['selection_evidence']['product_listing_source']['row']=3
        with self.assertRaises(ValueError):self.p.finish(job,token,bad)
        self.p.finish(job,token,e);self.assertEqual(self.p.get(job)['stage'],1)

    def test_sellersprite_extension_date_is_bound_to_tool_element_and_candidate(self):
        self.cfg['selection']['freshness_basis']='sellersprite_product_listing'
        job=self.p.add('US','B000000088','us');token=self.p.claim(job)['token'];e=self.evidence(job,'selection')
        e['selection_evidence'].update(freshness_scope='sellersprite_product_listing',product_listing_date=date.today().isoformat(),
            product_listing_source={'url':'https://www.amazon.com/dp/B000000088','checked_at':date.today().isoformat(),
                'capture_type':'sellersprite_browser_extension','tool':'SellerSprite','asin':'B000000088',
                'element_id':'seller-sprite-extension-quick-view-listing',
                'raw':'ASIN: B000000088\n上架时间: '+date.today().isoformat()})
        for mutation in ({'tool':'other'}, {'asin':'B000000087'}, {'url':'https://www.amazon.com/dp/B000000087'},
                         {'element_id':'amazon-details'}, {'raw':'ASIN: B000000087\n上架时间: '+date.today().isoformat()},
                         {'raw':'ASIN: B000000088\nDate First Available: '+date.today().isoformat()}):
            bad=copy.deepcopy(e);bad['selection_evidence']['product_listing_source'].update(mutation)
            with self.assertRaises(ValueError):self.p.finish(job,token,bad)
        self.p.finish(job,token,e)

    def test_any_seller_rank_policy_accepts_same_asin_only_for_new_task_snapshot(self):
        old=self.p.add('US','B000000090','us');old_token=self.p.claim(old)['token']
        self.cfg['selection']['same_product_rank_policy']={'seller_scope':'any_seller'}
        job=self.p.add('US','B000000091','us');token=self.p.claim(job)['token']
        e=self.evidence(job,'selection');item=e['selection_evidence']
        item['match'].update(asin=item['asin'])
        item['matched_rank'].update(asin=item['asin'])
        self.p.finish(job,token,e)
        e=self.evidence(old,'selection');item=e['selection_evidence']
        item['match'].update(asin=item['asin']);item['matched_rank'].update(asin=item['asin'])
        with self.assertRaises(ValueError):self.p.finish(old,old_token,e)

    def test_search_card_rank_requires_same_product_and_explicit_category_rank(self):
        self.cfg['selection']['same_product_rank_policy']={'seller_scope':'any_seller'}
        job=self.p.add('US','B000000089','us');token=self.p.claim(job)['token'];e=self.evidence(job,'selection')
        rank=e['selection_evidence']['matched_rank']
        rank.update(source='Amazon search page extension',capture_type='browser_search_card',
                    url=e['selection_evidence']['front_search']['url'],category="Women's Cardigans",raw="#1,234\ninWomen's Cardigans")
        for mutation in ({'raw':'自然位：第1页第1234位'}, {'asin':'B099999998'},
                         {'raw':"#1234\ninHome & Kitchen\n#5678\ninWomen's Cardigans"},
                         {'category':'Home & Kitchen'}, {'url':'https://www.amazon.com/s?k=other'}, {'value':0}):
            bad=copy.deepcopy(e);bad['selection_evidence']['matched_rank'].update(mutation)
            with self.assertRaises(ValueError):self.p.finish(job,token,bad)
        self.p.finish(job,token,e)

    def test_profile_required_and_market_must_match(self):
        with self.assertRaises(ValueError): self.p.add('US', 'A')
        with self.assertRaises(ValueError): self.p.add('UK', 'A', 'us')
        self.assertEqual(self.p.status(), [])

    def test_duplicate_and_claim_rejected_after_restart(self):
        job = self.p.add('US', 'B000000001', 'us')
        with self.assertRaises(sqlite3.IntegrityError): self.p.add('US', 'B000000001', 'us')
        first = self.p.claim(job)
        self.assertEqual(first['required_checks'], CHECKS['selection'])
        self.assertNotIn('checks', first)
        self.p.db.close()
        self.p = Pipeline(self.db, self.cfg)
        with self.assertRaises(ValueError): self.p.claim(job)
        self.p.hold(job, first['token'], 'uncertain', 'request timeout')
        with self.assertRaises(ValueError): self.p.claim(job)
        self.p.finish(job, first['token'], self.evidence(job, 'selection'))
        self.assertEqual(self.p.get(job)['stage'], 1)
        with self.assertRaises(ValueError): self.p.finish(job, first['token'], self.evidence(job, 'selection'))

    def test_reject_simulation_wrong_stage_and_failed_check(self):
        job = self.p.add('US', 'B000000002', 'us'); token = self.p.claim(job)['token']
        e = self.evidence(job, 'selection')
        for bad in (dict(e, simulated=True), dict(e, stage='listing'), dict(e, checks={})):
            with self.assertRaises(ValueError): self.p.finish(job, token, bad)
        self.assertEqual(self.p.get(job)['stage'], 0)

    def test_local_handoff_stops_at_export_even_after_resume(self):
        self.cfg['execution']['local_handoff_only'] = True
        job = self.p.add('US', 'B000000088', 'us')
        self.p.db.execute('UPDATE jobs SET stage=? WHERE id=?', (STAGES.index('export'), job))
        claim = self.p.claim(job)
        artifact = Path(self.temp.name) / 'delivery.zip'
        artifact.write_bytes(b'local handoff fixture')
        evidence = dict(self.evidence(job, 'export'), artifacts=[str(artifact)])
        self.p.finish(job, claim['token'], evidence)
        row = self.p.get(job)
        self.assertEqual((row['stage'], row['status']), (STAGES.index('publish'), 'blocked'))
        with self.assertRaisesRegex(ValueError, '不能恢复到ERP阶段'):
            self.p.resume(job, 'Confirmed local handoff file')
        with self.assertRaises(ValueError):
            self.p.claim(job)
        self.assertEqual(self.p.get(job)['status'], 'blocked')

    def test_selection_gate_requires_all_evidence_not_only_checkboxes(self):
        job = self.p.add('US', 'B000000004', 'us'); token = self.p.claim(job)['token']
        valid = self.evidence(job, 'selection')
        for mutate in (
            lambda e: e.pop('selection_evidence'),
            lambda e: e['selection_evidence'].update(freshness_scope='date_first_available'),
            lambda e: e['selection_evidence']['matched_rank'].update(raw='--'),
            lambda e: e['selection_evidence']['ip_checks']['imageRights'].update(status='review'),
            lambda e: e['selection_evidence']['daily_quota'].update(count_after_selection=3),
        ):
            bad = copy.deepcopy(valid); mutate(bad)
            with self.assertRaises(ValueError): self.p.finish(job, token, bad)
        self.p.finish(job, token, valid)
        self.assertEqual(self.p.get(job)['stage'], 1)
        with self.selected.open(encoding='utf-8', newline='') as stream:
            rows = list(csv.DictReader(stream))
        reserved = next(row for row in rows if row['asin'] == 'B000000004')
        self.assertEqual((reserved['status'], reserved['source']), ('validation_candidate', 'pipeline:'+job))
        current_hash = hashlib.sha256(self.selected.read_bytes()).hexdigest()
        self.assertEqual(reserve_selected_candidate(self.selected, 'US', 'B000000004', job, current_hash)['status'], 'already_reserved')
        with self.selected.open(encoding='utf-8', newline='') as stream:
            self.assertEqual(sum(row['asin'] == 'B000000004' for row in csv.DictReader(stream)), 1)

    def test_selection_dedupe_uses_current_ledger_hash_and_contents(self):
        job = self.p.add('US', 'B000000005', 'us'); token = self.p.claim(job)['token']
        e = self.evidence(job, 'selection')
        self.selected.write_text('marketplace,asin,selected_at,source,status\nUS,B000000005,2026-01-01,test,selected\n', encoding='utf-8')
        e['selection_evidence']['dedupe']['ledger_sha256'] = hashlib.sha256(self.selected.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError, '已存在'):
            self.p.finish(job, token, e)

    def test_selection_dedupe_rejects_changed_or_alternate_ledger(self):
        job = self.p.add('US', 'B000000008', 'us'); token = self.p.claim(job)['token']
        e = self.evidence(job, 'selection')
        other = Path(self.temp.name) / 'alternate.csv'; other.write_bytes(self.selected.read_bytes())
        e['selection_evidence']['dedupe'].update(ledger_path=str(other.resolve()), ledger_sha256=hashlib.sha256(other.read_bytes()).hexdigest())
        with self.assertRaises(ValueError): self.p.finish(job, token, e)
        e = self.evidence(job, 'selection')
        self.selected.write_text(self.selected.read_text(encoding='utf-8')+'US,B099999998,2026-01-01,test,selected\n', encoding='utf-8')
        with self.assertRaises(ValueError): self.p.finish(job, token, e)

    def test_snapshots_isolate_stores_and_future_changes(self):
        us = self.p.add('US', 'A', 'us'); uk = self.p.add('UK', 'A', 'uk')
        self.cfg['profiles']['us']['quantity'] = 999
        a = json.loads(self.p.get(us)['config'])['publishing']
        b = json.loads(self.p.get(uk)['config'])['publishing']
        self.assertEqual((a['store'], a['quantity']), ('test-us', 88))
        self.assertEqual((b['store'], b['quantity']), ('test-uk', 12))

    def test_retry_limit_does_not_reset_on_resume(self):
        job = self.p.add('US', 'B000000006', 'us')
        token = self.p.claim(job)['token']; self.p.hold(job, token, 'blocked', 'missing')
        self.p.resume(job, 'verified no submission')
        token = self.p.claim(job)['token']; self.p.hold(job, token, 'blocked', 'missing again')
        with self.assertRaises(ValueError): self.p.resume(job, 'no submission')

    def test_missing_artifacts_cannot_advance(self):
        job = self.p.add('US', 'B000000003', 'us')
        for stage in STAGES[:3]:
            token = self.p.claim(job)['token']; self.p.finish(job, token, self.evidence(job, stage))
        token = self.p.claim(job)['token']; e = self.evidence(job, 'listing')
        with self.assertRaises(ValueError): self.p.finish(job, token, e)
        artifact = Path(self.temp.name) / 'listing.txt'; artifact.write_text('test listing')
        e['artifacts'] = [str(artifact)]
        self.p.finish(job, token, e)
        image = self.p.claim(job)
        self.assertEqual(image['stage'], 'images')  # image generation is disabled; use supplied assets

    def test_enabling_paid_images_requires_both_budgets(self):
        self.cfg['images']['enabled'] = True
        self.cfg['images']['per_product_budget_rmb'] = None
        self.cfg['images']['daily_budget_rmb'] = None
        self.assertTrue(any('per_product_budget_rmb' in issue for issue in doctor(self.cfg)))
        self.assertTrue(any('daily_budget_rmb' in issue for issue in doctor(self.cfg)))
        job = self.p.add('US', 'B000000007', 'us')
        for stage in STAGES[:4]:
            token = self.p.claim(job)['token']
            e = self.evidence(job, stage)
            if stage == 'listing':
                artifact = Path(self.temp.name) / 'listing.txt'; artifact.write_text('test listing')
                e['artifacts'] = [str(artifact)]
            self.p.finish(job, token, e)
        with self.assertRaises(ValueError): self.p.claim(job)

    def test_image_budget_reservation_enforces_product_and_daily_caps(self):
        self.cfg['images'].update(enabled=True, per_product_budget_rmb=5, daily_budget_rmb=5)
        self.p.config = self.cfg
        first = self.p.add('US', 'B000000011', 'us')
        second = self.p.add('US', 'B000000012', 'us')
        self.p.db.execute("UPDATE jobs SET stage=4 WHERE id IN (?,?)", (first, second))
        with self.assertRaisesRegex(ValueError, '先登记费用上限'):
            self.p.claim(first)
        reservation = self.p.reserve_image_cost(first, '4.00')
        self.assertEqual(reservation['reserved_rmb'], '4.00')
        token = self.p.claim(first)['token']
        with self.assertRaises(ValueError):
            self.p.reserve_image_cost(first, '2.00')
        settled = self.p.settle_image_cost(reservation['reservation_id'], '3.50', 'Provider invoice confirmed', 'image-task-1')
        self.assertEqual(settled['state'], 'settled')
        artifact = Path(self.temp.name) / 'image.jpg'; artifact.write_bytes(b'fixture image')
        evidence = self.evidence(first, 'images'); evidence['artifacts'] = [str(artifact)]
        self.p.finish(first, token, evidence)
        self.p.db.execute("UPDATE jobs SET stage=4,status='ready',token=NULL WHERE id=?", (first,))
        with self.assertRaisesRegex(ValueError, '本产品生图预算不足'):
            self.p.reserve_image_cost(first, '1.51')
        self.p.db.execute("UPDATE jobs SET stage=4 WHERE id=?", (second,))
        self.assertEqual(self.p.reserve_image_cost(second, '1.50')['reserved_rmb'], '1.50')
        third = self.p.add('US', 'B000000013', 'us')
        self.p.db.execute("UPDATE jobs SET stage=4 WHERE id=?", (third,))
        with self.assertRaisesRegex(ValueError, '每日生图预算不足'):
            self.p.reserve_image_cost(third, '0.01')
        status = self.p.image_budget_status()
        self.assertEqual(status['daily_used_and_reserved_rmb'], '5.00')

    def test_image_budget_unsettled_cost_blocks_finish_and_excess_is_recorded(self):
        self.cfg['images'].update(enabled=True, per_product_budget_rmb=5, daily_budget_rmb=10)
        self.p.config = self.cfg
        job = self.p.add('US', 'B000000014', 'us')
        self.p.db.execute("UPDATE jobs SET stage=4 WHERE id=?", (job,))
        reservation = self.p.reserve_image_cost(job, '5.00')
        token = self.p.claim(job)['token']
        artifact = Path(self.temp.name) / 'image-overrun.jpg'; artifact.write_bytes(b'fixture image')
        evidence = self.evidence(job, 'images'); evidence['artifacts'] = [str(artifact)]
        with self.assertRaisesRegex(ValueError, '先结算'):
            self.p.finish(job, token, evidence)
        result = self.p.settle_image_cost(reservation['reservation_id'], '5.25', 'Provider invoice confirmed over estimate')
        self.assertEqual(result['state'], 'over_budget')
        with self.assertRaisesRegex(ValueError, '已超预算'):
            self.p.finish(job, token, evidence)
        self.assertEqual(self.p.image_budget_status()['daily_used_and_reserved_rmb'], '5.25')

    def test_publish_and_online_are_distinct(self):
        job = self.p.add('US', 'A', 'us')
        self.p.db.execute("UPDATE jobs SET stage=6,status='running',token='t' WHERE id=?", (job,))
        e = self.evidence(job, 'publish')
        with self.assertRaises(ValueError): self.p.finish(job, 't', e)
        e['external_ids'] = ['12345']
        with self.assertRaisesRegex(ValueError, '发布回执'):
            self.p.finish(job, 't', e)
        publishing = json.loads(self.p.get(job)['config'])['publishing']
        e.update(publication={'marketplace':'US', 'store':publishing['store'], 'publish_id':'12345',
                              'child_skus':['sku-1']}, workbench_publication_id='original-task')
        self.p.finish(job, 't', e)
        self.assertEqual(self.p.status()[0]['stage'], 'verify')
        self.assertEqual(self.p.get(job)['status'], 'ready')
        self.p.db.execute("UPDATE jobs SET status='running',token='verify-token' WHERE id=?", (job,))
        verify = self.evidence(job, 'verify')
        verify.update(external_ids=['12345'], workbench_publication_id='original-task')
        from unittest.mock import patch
        from datetime import datetime, timezone
        receipt = {'workbench_publication_id':'original-task', 'publish_id':'12345', 'marketplace':'US',
                   'store':publishing['store'], 'observed_at':datetime.now(timezone.utc).isoformat(),
                   'children':[{'sku':'sku-1','asin':'B000000001','status':'online'}]}
        with patch('pipeline.workbench_call', return_value={**receipt, 'children':[]}):
            with self.assertRaisesRegex(ValueError, '全部原子 SKU'):
                self.p.finish(job, 'verify-token', verify)
        with patch('pipeline.workbench_call', return_value=receipt) as call:
            self.p.finish(job, 'verify-token', verify)
        self.assertIn('publishId=12345', call.call_args.args[0])
        self.assertEqual(self.p.get(job)['status'], 'done')

    def test_doctor_catches_uncalibrated_publisher(self):
        self.assertTrue(any('未完成' in item for item in doctor(self.cfg)))

    def test_readiness_never_claims_unattended_execution(self):
        report = readiness(self.cfg)
        self.assertFalse(report['unattended_execution'])
        self.assertTrue(any('钧飞 recipe 未在通用脚本中校准' in gap for gap in report['gaps']))


if __name__ == '__main__':
    unittest.main()
