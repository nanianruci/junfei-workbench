import tempfile,unittest,hashlib,uuid
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from PIL import Image
import app_paths,image_engine as engine,official_image_templates as library
import codex_handoff


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        root=Path(self.folder.name)
        self.scope=patch.object(app_paths,'DATA',root);self.scope.start();self.addCleanup(self.scope.stop)
        self.db=patch.object(engine,'DB',root/'engine.sqlite3');self.db.start();self.addCleanup(self.db.stop)
        self.source=engine.cache(Image.new('RGB',(80,80),'#8b4b26'))['url']
        self.prompt='Original apparel template'
        self.template={'id':'123','style':'C63','name':'real','url':self.source,'sha256':engine.asset(self.source)[1],
                       'prompt':self.prompt,'promptSha256':hashlib.sha256(self.prompt.encode()).hexdigest()}

    def test_apparel_freezes_actual_codex_canvas_and_file_boundaries(self):
        data={'requestId':uuid.uuid4().hex,'mode':'suite','references':[self.source],'brief':'保留原流苏和真实棕色','count':2,
              'apparelSuite':{'counts':{'ins':1,'seller':1}}}
        with patch.object(engine,'require_codex',return_value={'outputSize':'1600x1600'}),patch.object(engine,'launch'),patch.object(library,'assignments',return_value=[self.template]):
            task=engine.start(data,{'id':'p','name':'women pants'})
            frozen=task['request']['apparelSuite']
            self.assertEqual((frozen['targetWidth'],frozen['targetHeight'],frozen['aspectRatio']),(1600,1600,'1:1'))
            self.assertEqual([s['kind']for s in frozen['slots']],['ins','seller'])
            with self.assertRaises(ValueError):engine.start({**data,'requestId':uuid.uuid4().hex,'apparelSuite':{'targetWidth':1536,'targetHeight':2048,'aspectRatio':'3:4'}},{'id':'p','name':'pants'})

    def test_model_reference_hash_is_server_resolved_and_frozen(self):
        model=engine.cache(Image.new('RGB',(80,80),'white'))['url']
        data={'requestId':uuid.uuid4().hex,'mode':'suite','references':[self.source],'brief':'同一模特展示','count':2,
              'apparelSuite':{'counts':{'model':2},'consistency':{'modelReference':{'url':model,'sha256':'forged'}}}}
        with patch.object(engine,'require_codex',return_value={'outputSize':'1600x1600'}),patch.object(engine,'launch'),patch.object(library,'assignments',return_value=[self.template]):
            task=engine.start(data,{'id':'p','name':'pants'})
        self.assertEqual(task['request']['assetSha256'][model],engine.asset(model)[1])
        engine.assert_assets(task)
        Image.new('RGB',(80,80),'black').save(app_paths.DATA/model.lstrip('/'),'PNG')
        with self.assertRaises(ValueError):engine.assert_assets(task)

    def test_repair_ignores_client_capability_and_freezes_typed_user_input(self):
        data={'requestId':uuid.uuid4().hex,'mode':'repair','references':[self.source],'brief':'只修背景','needOptimize':True,
              'repairCapabilities':{'source':{'provider':'external','model':'fake'}},
              'repairObjects':[{'type':'text','text':'只修背景'},{'type':'tag','text':'','placeholder':'do not send example'}]}
        with patch.object(engine,'require_codex'),patch.object(engine,'launch'),patch('provider.text_settings',return_value={'model':'real-codex-model','base_url':'http://127.0.0.1:8787/v1'}):
            task=engine.start(data,{'id':'p'})
            request=task['request']
            self.assertEqual(request['repairCapabilities']['source']['model'],'real-codex-model')
            self.assertEqual(request['repairSize'],'');self.assertTrue(request['repairRequestedOptimize'])
            with self.assertRaises(ValueError):engine.start({**data,'requestId':uuid.uuid4().hex,'brief':'stale'}, {'id':'p'})

    def test_native_task_and_agent_handoff_register_before_launch_and_recover_by_request(self):
        request_id = uuid.uuid4().hex
        data = {'requestId': request_id, 'mode': 'repair', 'references': [self.source], 'brief': '只修背景'}
        scope = (1, 'p', 'US', request_id)
        with patch.object(engine, 'require_codex'), patch.object(engine, 'launch') as launch:
            def lose_response(task_id):
                with closing(engine.db()) as connection:
                    self.assertEqual(codex_handoff.lookup(connection, scope)['jobId'], task_id)
                raise RuntimeError('Lost response fixture')
            launch.side_effect = lose_response
            with self.assertRaises(RuntimeError):
                engine.start(data, {'id':'p'}, handoff=scope)
            found = engine.by_request('p', request_id)
            self.assertIsNone(engine.by_request('other', request_id))
            self.assertEqual(engine.start(data, {'id':'p'}, handoff=scope)['id'], found['id'])
            launch.assert_called_once()


if __name__=='__main__':unittest.main()
