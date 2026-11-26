import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import market_research as m


class ResearchTests(unittest.TestCase):
    def test_validation(self):
        self.assertEqual(m.validate({'mode':'asin','query':'b012345678'})['query'],'B012345678')
        for data in ({'mode':'asin','query':'bad'}, {'month':'209912'}, {'minPrice':80,'maxPrice':10}, {'limit':0}):
            with self.assertRaises(ValueError): m.validate(data)

    def test_unknown_metrics_and_package_units(self):
        c=m.candidate({'asin':'B012345678','pkgWeight':'1 pounds','pkgDimensions':'10 x 5 x 2 inches'},'US')
        self.assertIsNone(c['monthlySales'])
        f=m.estimate_package(c)
        self.assertAlmostEqual(f['weightKg'],.4536)
        self.assertEqual(f['lengthCm'],25.4)
        self.assertEqual(m.estimate_package({'pkgWeight':'1','pkgDimensions':'1 x 2 x 3'})['status'],'missing')

    def test_freight_missing_costs_and_invalid_channel(self):
        with tempfile.TemporaryDirectory() as root, patch.object(m, 'RATE_ROOT', Path(root)):
            rates = {'records': [{'country_code': 'US', 'channel_code': 'fixture', 'start_weight': 0,
                                  'end_weight': 1000, 'first_freight': 10, 'handling_fee': 1}]}
            (Path(root) / 'fixture.json').write_text(json.dumps(rates), encoding='utf-8')
            options = m.channels('US')
            self.assertTrue(options)
            data = dict(market='US', channel=options[0]['id'], weightKg=.05, lengthCm=8, widthCm=8, heightCm=8)
            result = m.freight(data)
            self.assertEqual(result['chargeableKg'], .064)
            self.assertIsNone(result['profit'])
            for extra in ({'channel':'../secret|x'}, {'weightKg': -1}):
                with self.assertRaises(ValueError):
                    m.freight({**data, **extra})

    def test_import_reference_boundary_duplicate_and_deleted(self):
        with tempfile.TemporaryDirectory() as root, patch.object(m,'DB',Path(root)/'reports.db'):
            c=m.candidate({'asin':'B012345678','brand':'Competitor','imageUrl':'https://example.com/image.jpg'},'US')
            r={'id':'test','created':1,'status':'completed','request':{'month':'202609'},'candidates':[c]}
            m.save(r)
            state={}
            result=m.import_products('test',[c['asin']],state,{'username':'tester'})
            self.assertEqual(len(result['added']),1)
            p=state['products'][0]
            self.assertEqual(p['brand'],'Generic')
            self.assertEqual(p['assets'],[])
            self.assertFalse(p['productionReview']['publishReady'])
            self.assertEqual(m.import_products('test',[c['asin']],state,{})['added'],[])
            deleted={'deletedProducts':[{'product':{'id':p['id']}}]}
            self.assertEqual(m.import_products('test',[c['asin']],deleted,{})['added'],[])
            with self.assertRaises(ValueError):m.import_products('test',['B099999999'],state,{})

    def test_auto_select_prioritizes_observed_demand_efficiency_and_preserves_unknown_order(self):
        with tempfile.TemporaryDirectory() as root, patch.object(m,'DB',Path(root)/'reports.db'):
            report = {'id':'ranked','created':1,'status':'completed',
                      'request':{'mode':'discover','market':'US','limit':3,'minPrice':15,'maxPrice':60},
                      'candidates':[
                          {'asin':'B000000001','title':'Source first','monthlySales':None,'ratings':None,'price':None},
                          {'asin':'B000000002','title':'Higher demand but heavy competition','monthlySales':1000,'ratings':900,'price':25},
                          {'asin':'B000000003','title':'Balanced demand signal','monthlySales':300,'ratings':50,'price':30},
                      ]}
            m.save(report)
            result = m.auto_select_candidates('ranked', 10)
            self.assertEqual([row['asin'] for row in result['candidates']],
                             ['B000000002','B000000003','B000000001'])
            self.assertEqual(result['candidates'][0]['status'], 'review_queue_only')
            self.assertIsNone(result['candidates'][2]['priorityScore'])
            self.assertIn('不代表', result['note'])

    def test_auto_select_caps_to_report_count_and_keeps_store_source_order_when_metrics_unknown(self):
        with tempfile.TemporaryDirectory() as root, patch.object(m,'DB',Path(root)/'reports.db'):
            report = {'id':'store','created':1,'status':'completed',
                      'request':{'mode':'seller-store','market':'US','collectionDate':'2026-11-01'},
                      'candidates':[{'asin':'B000000001','title':'First'}, {'asin':'B000000002','title':'Second'}]}
            m.save(report)
            result = m.auto_select_candidates('store', 10)
            self.assertEqual([row['asin'] for row in result['candidates']],
                             ['B000000001','B000000002'])
            self.assertEqual(result['limit'], 10)
            self.assertTrue(all(row['priorityScore'] is None for row in result['candidates']))

    def test_seller_store_page_becomes_unverified_candidate_intake_without_invented_metrics(self):
        observation = {'sellerId': 'SELLER12345', 'marketplace': 'US',
                       'url': 'https://www.amazon.com/s?me=SELLER12345',
                       'capturedAt': '2026-11-01T12:00:00+00:00', 'sortKey': 'date-desc-rank',
                       'pageTitle': 'Storefront', 'pageSummary': '2 results',
                       'limitations': '排序不证明近7天上架或FBM',
                       'products': [{'asin': 'B012345678', 'title': 'Observed title',
                                     'brandText': 'Brand text', 'priceText': '$9.99',
                                     'imageUrl': '', 'productUrl': 'https://www.amazon.com/dp/B012345678',
                                     'raw': 'Observed card'}], 'trust': 'unverified_observation'}
        with tempfile.TemporaryDirectory() as root, patch.object(m, 'DB', Path(root) / 'reports.db'):
            report = m.save_seller_store_capture(observation)
            self.assertEqual(m.get(report['id'])['request']['mode'], 'seller-store')
            candidate = m.get(report['id'])['candidates'][0]
            self.assertIsNone(candidate['monthlySales'])
            self.assertEqual(candidate['fulfillment'], '')
            workspace = {}
            result = m.import_products(report['id'], ['B012345678'], workspace, {'username': 'tester'})
            product = workspace['products'][0]
            self.assertEqual(result['added'], ['IMP-US-B012345678'])
            self.assertIn('2026-11-01', product['selectionResearch']['sourceMonth'])
            self.assertFalse(product['productionReview']['publishReady'])


if __name__=='__main__':unittest.main()
