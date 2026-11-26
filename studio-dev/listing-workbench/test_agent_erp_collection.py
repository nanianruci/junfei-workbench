"""Formal ERP task integration uses only isolated ledgers and mocked transfers."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch
import agent_center as agent
import agent_erp
import agent_erp_collection as bridge
import erp_collection as erp


class FormalERPTests(unittest.TestCase):
    def setUp(self):
        policy = {'publishing': {'cross_marketplace_followup': {'enabled': True,
                    'source_marketplace': 'US', 'target_marketplace': 'UK', 'trigger': 'publishing'}}}
        config = patch('publication_policy._configuration', return_value=policy)
        config.start()
        self.addCleanup(config.stop)
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.product={'id':'P','sku':'PARENT','name':'Fixture product','market':'US'}
        self.listing={'title':'Saved title','highlights':'Feature','bullets':'1\n2\n3\n4\n5','description':'Saved','terms':'terms'}
        self.workspace={'products':[self.product],'copy':{'P-US':self.listing},'reviews':{}}
        self.target={'id':'formal','accountId':2,'enabled':True,'store':'Store','marketplace':'US','name':'US target'}
        self.current={'accountId':2,'productId':'P','marketplace':'US','store':'Store','sha256':'snapshot',
            'goods':{'title':'Saved title'},'goodsItems':[{'sku':'PARENT-S','price':'20'}],
            'assets':[{'sha256':'image','color':'White','slot':0}],'handlingDays':'3','masterColor':'White'}
        self.patches=[patch.object(agent,'DATA',self.root),patch.object(agent,'DB',self.root/'agent.sqlite3'),
            patch.object(agent,'load_workspace',side_effect=lambda:copy.deepcopy(self.workspace)),
            patch.object(agent.discovery_intake,'assert_web_available'),patch.object(erp,'ROOT',self.root/'erp'),
            patch.object(erp,'destinations',side_effect=lambda user:[self.target] if user==2 else []),
            patch.object(agent_erp,'project_root',return_value=self.root/'legacy'),
            patch.object(erp,'finished_snapshot',side_effect=lambda *args,**kw:copy.deepcopy(self.current)),
            patch.object(erp,'verified_result'),patch.object(erp,'start')]
        for p in self.patches:p.start();self.addCleanup(p.stop)
        self.profile=agent.save_profile(2,{'name':'Store US','store':'Store','marketplace':'US','shippingTemplate':'Template','mode':'draft'})

    def create(self,**extra):
        return agent.create(2,{'goal':'交付已保存成品','taskMode':'erp_draft','productId':'P',
            'profileId':self.profile['id'],'erpDestinationId':'formal',**extra})

    def run_task(self,task):
        with patch.object(agent.threading.Thread,'start'):agent.start(2,task['id'])
        agent.run(2,task['id'])
        return agent.detail(2,task['id'])

    def job(self,status='verified'):
        job={'id':'a'*32,'accountId':2,'productId':'P','destinationId':'formal','status':status,
             'published':False,'engine':'native-v2','nativeJoined':True,'draftId':'123','collectionId':'456'}
        with erp.connect() as c,c:c.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)',
            (job['id'],2,'P','formal',status,json.dumps(job)))
        folder=erp.ROOT/job['id'];folder.mkdir()
        (folder/'collection.json').write_text(json.dumps(self.current))
        (folder/'receipt.json').write_text(json.dumps({'fixture':'only'}))
        return job

    def test_explicit_destination_and_exact_store_market_draft_are_required(self):
        for changes in ({'erpDestinationId':''},{'erpDestinationId':'other'},{'autoListing':True}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):self.create(**changes)
        for changes in ({'store':'Other'},{'marketplace':'UK'},{'mode':'publish'}):
            with self.assertRaises(ValueError):bridge.destination(2,{**self.profile,**changes},'formal')
        with self.assertRaises(ValueError):bridge.destination(3,self.profile,'formal')
        erp.start.assert_not_called()

    def test_old_library_upload_cannot_be_created_for_new_products(self):
        with self.assertRaisesRegex(ValueError,'正式ERP草稿交付'):
            self.create(taskMode='library_draft')
        self.assertEqual(agent.list_tasks(2),[])
        erp.start.assert_not_called()

    def test_new_formal_task_is_scoped_and_destination_list_has_no_browser_configuration(self):
        task=self.create()
        self.assertEqual((task['erpWorkflowVersion'],task['erpDestinationId']),(2,'formal'))
        self.assertFalse(task['autoSourcing']);self.assertFalse(task['autoListing'])
        self.assertEqual(task['publicationFollowup']['trigger'],'publishing')
        self.assertEqual(task['publicationFollowup']['targetMarketplace'],'UK')
        self.assertTrue(task['publicationFollowup']['preserveParentSku'])
        self.assertTrue(task['publicationFollowup']['preserveChildSkus'])
        self.assertTrue(task['publicationFollowup']['executorAvailable'])
        self.assertEqual(task['profile']['mode'],'draft')
        self.target['erpAccount']='private';self.target['page']='p9'
        self.assertNotIn('private',json.dumps(bridge.destinations(2)))

    def test_old_task_records_do_not_gain_a_new_publication_scope(self):
        legacy={'id':'old','productId':'P','status':'done','cursor':3,'profile':self.profile,
                'snapshot':{'product':self.product},'results':{}}
        raw=json.dumps(legacy)
        with closing(agent.connect()) as db,db:
            db.execute('INSERT INTO tasks VALUES(?,?,?,?)',('old',2,raw,0))
        self.assertNotIn('publicationFollowup',agent.detail(2,'old'))
        with closing(agent.connect()) as db:
            self.assertEqual(db.execute('SELECT body FROM tasks WHERE id=?',('old',)).fetchone()[0],raw)

    def test_destination_alone_supplies_safe_draft_profile_without_duplicate_setup(self):
        self.target.update(erpAccount='private-login',page='p99',browserBinding='private-binding')
        task=self.create(profileId='')
        self.assertEqual(task['profile']['store'],'Store')
        self.assertEqual(task['profile']['mode'],'draft')
        self.assertNotIn('private',json.dumps(task['profile']))
        self.assertEqual(len(agent.profiles(2)),1)  # Existing profile remains untouched; no second is saved.

    def test_verified_original_receipt_finishes_without_starting_any_transfer(self):
        self.job();task=self.create()
        with patch.object(agent_erp,'sync_copy') as old,patch('agent_library.sync_library') as library:
            done=self.run_task(task)
        self.assertEqual(done['status'],'done');self.assertFalse(done['results']['publish']['published'])
        self.assertEqual(set(done['results']),{'plan','junfeiPreflight','publish'})
        erp.start.assert_not_called();old.assert_not_called();library.assert_not_called()
        self.assertEqual(self.workspace['products'][0],self.product)

    def test_changed_business_content_cannot_reuse_old_success(self):
        self.job();self.current['goods']['title']='Changed'
        result=self.run_task(self.create())
        self.assertEqual(result['status'],'blocked')
        self.assertIn('版本不同',result['results']['junfeiPreflight']['issues'][0])
        erp.start.assert_not_called()

    def test_missing_or_legacy_verified_receipt_is_not_accepted_from_status_alone(self):
        job=self.job();(erp.ROOT/job['id']/'receipt.json').unlink()
        result=self.run_task(self.create())
        self.assertEqual(result['status'],'blocked');erp.start.assert_not_called()

    def test_existing_unknown_or_running_job_is_not_replayed(self):
        self.job('needs_reconciliation');task=self.create()
        pending=self.run_task(task)
        self.assertEqual(pending['status'],'erp_pending');self.assertEqual(pending['cursor'],2)
        self.assertEqual(pending['results']['publish']['erpCollection']['id'],'a'*32)
        again=self.run_task(pending)
        self.assertEqual(again['status'],'erp_pending');erp.start.assert_not_called()

    def test_absent_job_uses_formal_start_once_then_reads_same_receipt(self):
        erp.start.return_value={'id':'b'*32,'status':'queued','published':False}
        pending=self.run_task(self.create())
        self.assertEqual(pending['status'],'erp_pending');erp.start.assert_called_once()
        self.assertEqual(erp.start.call_args.args[2]['destinationId'],'formal')
        self.assertNotIn('verify',pending['results'])

    def test_old_sku_unknown_or_two_attempt_barrier_is_preserved(self):
        path=self.root/'legacy/automation/data/ego-library-draft-executions/2-PARENT.json'
        path.parent.mkdir(parents=True);path.write_text(json.dumps({'status':'library_save_unknown','attempt':2}))
        before=path.read_bytes()
        result=self.run_task(self.create())
        self.assertEqual(result['status'],'blocked');self.assertEqual(path.read_bytes(),before)
        erp.finished_snapshot.assert_not_called();erp.start.assert_not_called()

    def test_old_task_pending_effect_prevents_new_mode_bypass(self):
        with closing(agent.connect()) as c,c:
            legacy={'id':'old','productId':'P','status':'needs_review','cursor':7,'libraryDraftAttempted':True,
                    'profile':{**self.profile,'id':'other-profile'},'snapshot':{'product':self.product},'results':{}}
            c.execute('INSERT INTO tasks VALUES(?,?,?,?)',('old',2,json.dumps(legacy),0))
        result=self.run_task(self.create())
        self.assertEqual(result['status'],'blocked');erp.start.assert_not_called()

    def test_readonly_preflight_failure_can_refresh_snapshot_with_history(self):
        erp.finished_snapshot.side_effect=ValueError('missing source')
        failed=self.run_task(self.create())
        self.assertEqual(failed['status'],'blocked')
        self.workspace['products'][0]['material']='confirmed cotton'
        updated=agent.recheck(2,failed['id'])
        stored=agent.detail(2,updated['id'])
        self.assertEqual(stored['status'],'queued');self.assertEqual(len(stored['revisions']),1)
        self.assertNotIn('material',stored['revisions'][0]['snapshot']['product'])
        self.assertEqual(stored['snapshot']['product']['material'],'confirmed cotton')

    def test_new_preparation_task_cannot_use_old_bound_upload(self):
        task=agent.create(2,{'goal':'准备','taskMode':'production','productId':'P','profileId':self.profile['id'],'autoSourcing':False})
        with patch.object(agent_erp,'binding') as binding:
            result=agent.sync_erp(2,agent.detail(2,task['id']))
        self.assertFalse(result['ok']);binding.assert_not_called()


if __name__=='__main__':unittest.main()
