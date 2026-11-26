import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import runtime_environment as runtime


class RuntimeEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / 'moved project'
        (self.project / 'automation').mkdir(parents=True)
        (self.project / 'automation/config.json').write_text(json.dumps({'images': {'daily_budget_rmb': 200}}), encoding='utf-8')
        self.app = self.project / 'listing-workbench'
        self.app.mkdir()

    def test_relocated_source_does_not_depend_on_original_username(self):
        self.assertEqual(runtime.project_root(self.app, {}), self.project)

    def test_explicit_root_overrides_old_pointer(self):
        (self.app / 'project-root.txt').write_text(str(self.root / 'old-machine'), encoding='utf-8')
        self.assertEqual(runtime.project_root(self.app, {runtime.PROJECT_ENV: str(self.project)}), self.project)

    def test_invalid_explicit_root_does_not_fall_back(self):
        with self.assertRaisesRegex(FileNotFoundError, runtime.PROJECT_ENV):
            runtime.project_root(self.app, {runtime.PROJECT_ENV: str(self.root / 'missing')})

    def test_relative_explicit_root_is_rejected(self):
        with self.assertRaises(ValueError):
            runtime.project_root(self.app, {runtime.PROJECT_ENV: 'relative'})

    def test_valid_legacy_pointer_is_preserved(self):
        bundle = self.root / 'bundle'
        bundle.mkdir()
        (bundle / 'project-root.txt').write_text(str(self.project), encoding='utf-8')
        self.assertEqual(runtime.project_root(bundle, {}), self.project)

    def test_stale_pointer_never_falls_back_to_bundled_budget(self):
        (self.app / 'project-root.txt').write_text(str(self.root / 'missing'), encoding='utf-8')
        (self.app / 'automation').mkdir()
        (self.app / 'automation/config.json').write_text('{}', encoding='utf-8')
        with self.assertRaisesRegex(FileNotFoundError, runtime.PROJECT_ENV):
            runtime.project_root(self.app, {})

    def test_empty_pointer_is_rejected(self):
        (self.app / 'project-root.txt').write_text('', encoding='utf-8')
        with self.assertRaises(ValueError):
            runtime.project_root(self.app, {})

    def test_complete_bundle_config_is_found_without_pointer(self):
        self.assertEqual(runtime.project_root(self.project, {}), self.project)

    def test_missing_root_is_actionable(self):
        missing = self.root / 'empty'
        missing.mkdir()
        report = runtime.diagnose(missing, {})
        self.assertFalse(report['runtimeFilesReady'])
        self.assertIn(runtime.PROJECT_ENV, report['checks'][0]['message'])

    def test_diagnostics_do_not_write_or_expose_config_secrets(self):
        config = self.project / 'automation/config.json'
        config.write_text(json.dumps({'api_key': 'DO-NOT-REPORT', 'browser': {}, 'images': {}}), encoding='utf-8')
        before = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        report = runtime.diagnose(self.app, {})
        after = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        self.assertNotIn('DO-NOT-REPORT', json.dumps(report))
        self.assertFalse(report['readyForUnattendedRun'])
        self.assertFalse(report['runtimeFilesReady'])

    def test_python_source_uses_running_interpreter(self):
        with patch.object(runtime.sys, 'frozen', False, create=True), patch.object(runtime.sys, 'executable', 'known-python'):
            self.assertEqual(runtime.python_command({}), ['known-python'])

    def test_python_override_requires_existing_absolute_file(self):
        with self.assertRaises(ValueError):
            runtime.python_command({runtime.PYTHON_ENV: 'python'})
        executable = self.root / 'python.exe'
        executable.write_bytes(b'test fixture, never executed')
        self.assertEqual(runtime.python_command({runtime.PYTHON_ENV: str(executable)}), [str(executable)])

    def test_frozen_without_python_has_actionable_error(self):
        with patch.object(runtime.sys, 'frozen', True, create=True), patch.object(runtime.shutil, 'which', return_value=None):
            with self.assertRaisesRegex(ValueError, runtime.PYTHON_ENV):
                runtime.python_command({})

    def test_invalid_config_does_not_produce_ready_report(self):
        (self.project / 'automation/config.json').write_text('[]', encoding='utf-8')
        self.assertFalse(runtime.diagnose(self.app, {})['runtimeFilesReady'])

    def test_offline_backup_preserves_database_and_assets_and_detects_edits(self):
        from contextlib import closing
        import sqlite3
        import workspace_backup

        data = self.root / 'fixture-data'
        data.mkdir()
        (data / 'receipt.json').write_text('{"task":"original"}', encoding='utf-8')
        with closing(sqlite3.connect(data / 'studio.sqlite3')) as db, db:
            db.execute('CREATE TABLE tasks(id INTEGER PRIMARY KEY)')
            db.execute('INSERT INTO tasks VALUES (42)')
        snapshot = self.root / 'snapshot'
        self.assertEqual(workspace_backup.backup(data, snapshot), 2)
        self.assertEqual(workspace_backup.verify(snapshot), 2)
        with closing(sqlite3.connect(snapshot / 'data/studio.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT id FROM tasks').fetchone(), (42,))
        (snapshot / 'data/receipt.json').write_text('{}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '清单'):
            workspace_backup.verify(snapshot)

    def test_override_can_keep_bundled_node_when_path_has_none(self):
        node = self.app / 'node.exe'
        node.write_bytes(b'test fixture, never executed')
        with patch.object(runtime.shutil, 'which', return_value=None):
            self.assertEqual(runtime.node_command(self.app, self.project), [str(node)])
            report = runtime.diagnose(self.app, {runtime.PROJECT_ENV: str(self.project)})
        self.assertTrue(next(x for x in report['checks'] if x['id'] == 'node')['ok'])

    def test_node_missing_fails_without_starting_process(self):
        with patch.object(runtime.shutil, 'which', return_value=None):
            with self.assertRaises(FileNotFoundError):
                runtime.node_command(self.app, self.project)

    def test_explicit_root_selects_preflight_resources_too(self):
        default = self.root / 'old-code'
        self.assertEqual(runtime.resource_root(self.app, default, {}), default)
        self.assertEqual(runtime.resource_root(self.app, default, {runtime.PROJECT_ENV: str(self.project)}), self.project)

    def test_standalone_bridge_uses_override_for_config_and_modules(self):
        import importlib.util
        source = Path(__file__).resolve().parents[1] / 'junfei-publisher/ego_link.py'
        spec = importlib.util.spec_from_file_location('portability_test_ego_link', source)
        bridge = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bridge)
        with patch.dict(os.environ, {runtime.PROJECT_ENV: str(self.project)}):
            self.assertEqual(bridge.project_root(), self.project)
            for name in ('ego-existing-draft.mjs', 'ego-draft-images.mjs'):
                self.assertEqual(bridge.module_path(name), self.project / 'junfei-publisher' / name)

    def test_selection_and_budget_share_explicit_root(self):
        import agent_selection
        import agent_erp
        import image_budget
        config = self.project / 'automation/config.json'
        config.write_text(json.dumps({'images': {'daily_budget_rmb': 321}}), encoding='utf-8')
        with patch.dict(os.environ, {runtime.PROJECT_ENV: str(self.project)}):
            self.assertEqual(agent_selection.project_root(), self.project)
            self.assertEqual(agent_erp.project_root(), self.project)
            self.assertEqual(image_budget.configuration()['daily_budget_rmb'], 321)


if __name__ == '__main__':
    unittest.main()
