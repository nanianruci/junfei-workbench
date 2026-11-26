import copy
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
import zipfile
import build_portable as build


class PortableBuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'source'
        for path in ('packaging', 'automation', 'junfei-publisher/node_modules/pkg', 'listing-workbench'):
            (self.root / path).mkdir(parents=True)
        self.config = json.loads((build.ROOT / 'packaging/portable-config.json').read_text(encoding='utf-8'))
        (self.root / 'packaging/portable-config.json').write_text(json.dumps(self.config), encoding='utf-8')
        (self.root / 'automation/ego_browser.py').write_text('# fixture', encoding='utf-8')
        (self.root / 'automation/browser_space_lock.py').write_text('# runtime lock fixture', encoding='utf-8')
        (self.root / 'junfei-publisher/ego_link.py').write_text('# fixture', encoding='utf-8')
        for name in build.MODULES:
            (self.root / 'junfei-publisher' / name).write_text('// fixture', encoding='utf-8')
        (self.root / 'junfei-publisher/node_modules/pkg/native.node').write_bytes(b'fixture')
        (self.root / 'listing-workbench/server.py').write_text('# fixture', encoding='utf-8')
        self.stage = Path(self.temp.name) / 'stage'

    def test_family_and_offline_control_sources_are_distributed_without_evidence(self):
        app=self.root/'listing-workbench'
        (app/'desktop_launcher.py').write_text('import server',encoding='utf-8')
        (app/'server.py').write_text('import discovery_family_bridge\nimport discovery_run',encoding='utf-8')
        (app/'discovery_family_bridge.py').write_text('import discovery_family',encoding='utf-8')
        for name in ('discovery_family.py','discovery_run.py','discovery-family-collector.js','test_discovery_family.py','test_discovery_family_capture.py','test_discovery_family_api.py','test_discovery_family_plan.py','test_discovery_run.py','test_discovery_family_collector.cjs','DISCOVERY-FAMILY.md','DISCOVERY-RUN.md'):
            (app/name).write_text('# fixture' if name.endswith('.py') else 'fixture',encoding='utf-8')
        private=self.root/'outputs/variant-evidence'
        private.mkdir(parents=True);(private/'receipt.json').write_text('PRIVATE-EVIDENCE',encoding='utf-8')
        build.prepare_stage(self.stage,self.root)
        with zipfile.ZipFile(self.stage/'corresponding-source.zip') as archive:
            for name in ('discovery_family.py','discovery_family_bridge.py','discovery_run.py','discovery-family-collector.js','test_discovery_family_api.py','test_discovery_run.py','DISCOVERY-FAMILY.md','DISCOVERY-RUN.md'):
                self.assertIn('listing-workbench/'+name,archive.namelist())
            self.assertFalse(any(name.startswith('outputs/') for name in archive.namelist()))

    def test_shared_coordinator_is_source_only_without_enrollment_or_activation(self):
        names=('shared_browser_space.py','test_shared_browser_space.py',
               'SHARED-BROWSER-SPACE.md','SHARED-BROWSER-ADOPTION.md')
        for name in names:
            (self.root/'automation'/name).write_text('# source-only fixture',encoding='utf-8')
        private=self.root/'automation/data/shared-coordinator'
        private.mkdir(parents=True)
        (private/'enrollment.json').write_text('PRIVATE-ENROLLMENT',encoding='utf-8')
        (private/'coordinator.sqlite3').write_text('PRIVATE-COORDINATOR',encoding='utf-8')
        build.prepare_stage(self.stage,self.root)
        with zipfile.ZipFile(self.stage/'corresponding-source.zip') as archive:
            for name in names:
                self.assertIn('automation/'+name,archive.namelist())
            self.assertFalse(any('enrollment' in name or name.endswith('.sqlite3') for name in archive.namelist()))
        runtime=self.stage/'portable-runtime'
        self.assertFalse((runtime/'automation/shared_browser_space.py').exists())
        self.assertTrue((runtime/'automation/browser_space_lock.py').is_file())
        blob=b''.join(p.read_bytes() for p in self.stage.rglob('*') if p.is_file())
        self.assertNotIn(b'PRIVATE-ENROLLMENT',blob)
        self.assertNotIn(b'PRIVATE-COORDINATOR',blob)

    def test_coordinated_discovery_fixture_is_source_only(self):
        names=('discovery_live_coordinated.py','test_discovery_live_coordinated.py',
               'test_discovery_live_coordinated_adversarial.py','DISCOVERY-LIVE-COORDINATED.md')
        for name in names:
            (self.root/'listing-workbench'/name).write_text('# offline fixture only',encoding='utf-8')
        build.prepare_stage(self.stage,self.root)
        with zipfile.ZipFile(self.stage/'corresponding-source.zip') as archive:
            for name in names:
                self.assertIn('listing-workbench/'+name,archive.namelist())
        runtime=self.stage/'portable-runtime'
        self.assertFalse(any(path.name in names for path in runtime.rglob('*')))
        config=json.loads((runtime/'automation/config.json').read_text(encoding='utf-8'))
        self.assertEqual(config,self.config)

    def test_allowlist_does_not_copy_live_data_credentials_or_config(self):
        (self.root / 'automation/config.json').write_text('LIVE-CONFIG-SECRET', encoding='utf-8')
        (self.root / 'listing-workbench/data').mkdir()
        (self.root / 'listing-workbench/data/studio.sqlite3').write_text('PRIVATE-DATA', encoding='utf-8')
        (self.root / 'junfei-publisher/node_modules/.env').write_text('SECRET', encoding='utf-8')
        manifest = build.prepare_stage(self.stage, self.root)
        blob = b''.join(p.read_bytes() for p in self.stage.rglob('*') if p.is_file())
        self.assertNotIn(b'LIVE-CONFIG-SECRET', blob)
        self.assertNotIn(b'PRIVATE-DATA', blob)
        self.assertNotIn(b'SECRET', blob)
        self.assertNotIn(str(self.root).encode(), blob)
        self.assertFalse(manifest['productionDataIncluded'])
        self.assertFalse((self.stage / 'project-root.txt').exists())

    def test_unsafe_defaults_are_rejected(self):
        variants = []
        for category,key,value in [('images','enabled',True),('publishing','mode','publish'),
                                   ('publishing','store','old-store'),('browser','task_space_id',0),
                                   ('browser','cli','C:/Old/ego.exe'),('browser','discovery_account_bindings',{'1':{'enabled':True}}),('execution','scheduled',True)]:
            config=copy.deepcopy(self.config);config[category][key]=value;variants.append(config)
        config=copy.deepcopy(self.config);config['images']['codex_tools']['enabled']=True;variants.append(config)
        for config in variants:
            with self.subTest(config=config),self.assertRaises(ValueError):
                build.validate_defaults(config)

    def test_discovery_defaults_are_dedicated_and_unconfigured(self):
        self.assertEqual(self.config['discovery_browser'],
                         {'engine': 'ego-lite', 'cli': '', 'account_bindings': {}})
        self.assertNotIn('discovery_account_bindings', self.config['browser'])
        build.validate_defaults(self.config)

    def test_dedicated_discovery_machine_or_account_defaults_are_rejected(self):
        variants = [
            {'engine': 'ego-lite', 'cli': 'C:/Old/ego.exe', 'account_bindings': {}},
            {'engine': 'ego-lite', 'cli': '', 'account_bindings': {'1': {'enabled': True}}},
            {'engine': 'ego-lite', 'cli': '', 'account_bindings': {'1': {'enabled': False}}},
            {'engine': 'ego-lite', 'cli': '', 'account_bindings': {'1': {}}},
            {'engine': 'ego-lite', 'cli': '', 'account_bindings': []},
            {'engine': 'edge', 'cli': '', 'account_bindings': {}},
            {'engine': 'ego-lite', 'cli': '', 'account_bindings': {}, 'task_space_id': 5},
            {'engine': 'ego-lite', 'account_bindings': {}},
            None,
        ]
        for discovery in variants:
            config = copy.deepcopy(self.config)
            config['discovery_browser'] = discovery
            with self.subTest(discovery=discovery), self.assertRaisesRegex(ValueError, 'discovery defaults'):
                build.validate_defaults(config)
        missing = copy.deepcopy(self.config)
        del missing['discovery_browser']
        with self.assertRaisesRegex(ValueError, 'discovery defaults'):
            build.validate_defaults(missing)

    def test_source_only_verifier_and_regression_keep_config_clean(self):
        verifier = b'# opt-in fixture; never a startup entrypoint\n'
        (self.root / 'packaging/verify_live_discovery.py').write_bytes(verifier)
        (self.root / 'packaging/verify_live_store.py').write_bytes(verifier)
        (self.root / 'packaging/verify_live_search.py').write_bytes(verifier)
        regression = b'// offline supplier capture regression fixture\n'
        (self.root / 'listing-workbench/test_supplier_detail_capture.cjs').write_bytes(regression)
        (self.root / 'listing-workbench/test_discovery_store_collector.cjs').write_bytes(regression)
        (self.root / 'listing-workbench/test_discovery_search_collector.cjs').write_bytes(regression)
        (self.root / 'listing-workbench/test_discovery_plan.py').write_bytes(b'# offline planner test fixture')
        build.prepare_stage(self.stage, self.root)
        with zipfile.ZipFile(self.stage / 'corresponding-source.zip') as archive:
            self.assertEqual(archive.read('packaging/verify_live_discovery.py'), verifier)
            self.assertEqual(archive.read('packaging/verify_live_store.py'), verifier)
            self.assertEqual(archive.read('packaging/verify_live_search.py'), verifier)
            self.assertEqual(archive.read('listing-workbench/test_discovery_search_collector.cjs'), regression)
            self.assertEqual(archive.read('listing-workbench/test_discovery_plan.py'), b'# offline planner test fixture')
            self.assertEqual(archive.read('listing-workbench/test_discovery_store_collector.cjs'), regression)
            self.assertEqual(archive.read('listing-workbench/test_supplier_detail_capture.cjs'), regression)
            config = json.loads(archive.read('automation/config.json'))
            self.assertEqual(config['discovery_browser'], self.config['discovery_browser'])
        self.assertFalse(any(path.name in ('verify_live_discovery.py', 'verify_live_store.py', 'verify_live_search.py')
                             for path in (self.stage / 'portable-runtime').rglob('*')))
        runtime = json.loads((self.stage / 'portable-runtime/automation/config.json').read_text(encoding='utf-8'))
        self.assertEqual(runtime['discovery_browser'], self.config['discovery_browser'])

    def test_existing_stage_is_not_overwritten(self):
        build.prepare_stage(self.stage, self.root)
        with self.assertRaises(FileExistsError):
            build.prepare_stage(self.stage, self.root)

    def test_corresponding_source_covers_packaged_binary_assets(self):
        for name in ('assets/favicon.png', 'extension/logo.png'):
            asset = self.root / 'listing-workbench' / name
            asset.parent.mkdir()
            asset.write_bytes(b'asset fixture')
        build.prepare_stage(self.stage, self.root)
        app = Path(self.temp.name) / 'app'
        internal = app / '_internal'
        shutil.copytree(self.stage, internal)
        for name in build.SOURCE_ASSET_DIRS:
            shutil.copytree(self.root / 'listing-workbench' / name, internal / name)
        (app / 'AmazonAgentPortable.exe').write_bytes(b'MZ fixture')
        (internal / 'node.exe').write_bytes(b'MZ fixture')
        build.verify_artifact(app)
        source = internal / 'corresponding-source.zip'
        omitted = 'listing-workbench/extension/logo.png'
        with zipfile.ZipFile(source) as archive:
            self.assertEqual(archive.read('listing-workbench/assets/favicon.png'), b'asset fixture')
            self.assertEqual(archive.read(omitted), b'asset fixture')
            rest = {name: archive.read(name) for name in archive.namelist() if name != omitted}
        with zipfile.ZipFile(source, 'w') as archive:
            for name, raw in rest.items():
                archive.writestr(name, raw)
        manifest = internal / 'portable-release.json'
        data = json.loads(manifest.read_text(encoding='utf-8'))
        data['sourceArchiveSha256'] = hashlib.sha256(source.read_bytes()).hexdigest()
        manifest.write_text(json.dumps(data), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Corresponding source asset missing or changed'):
            build.verify_artifact(app)

    def test_artifact_verification_checks_hashes_and_forbidden_files(self):
        build.prepare_stage(self.stage, self.root)
        app=Path(self.temp.name)/'app';shutil.copytree(self.stage,app/'_internal')
        (app/'AmazonAgentPortable.exe').write_bytes(b'MZ fixture')
        (app/'_internal/node.exe').write_bytes(b'MZ fixture')
        self.assertGreater(build.verify_artifact(app)['runtimeFileCount'],1)
        script=app/'_internal/portable-runtime/automation/ego_browser.py'
        script.write_text('modified',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'hash mismatch'):
            build.verify_artifact(app)
        shutil.copy2(self.stage/'portable-runtime/automation/ego_browser.py',script)
        (app/'_internal/leaked.dpapi').write_text('forbidden',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'Forbidden'):
            build.verify_artifact(app)


if __name__ == '__main__':
    unittest.main()
