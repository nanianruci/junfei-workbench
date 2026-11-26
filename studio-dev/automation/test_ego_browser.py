import json
from pathlib import Path
import subprocess
import shutil
import tempfile
import unittest
from unittest.mock import patch
import ego_browser


class EgoRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {'task_space_id': 1}
        self.cli = self.root / 'ego-browser.exe'
        self.scripts = self.root / 'scripts'
        self.scripts.mkdir()
        self.settings = patch.object(ego_browser, 'settings', return_value=(self.config,self.cli,self.scripts))
        self.settings.start()
        self.addCleanup(self.settings.stop)
        self.root_patch = patch.object(ego_browser,'ROOT',self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.script = self.root / 'task.mjs'
        self.script.write_text('console.log(await amazonTask.page("p1").url());',encoding='utf-8')

    def test_receipt_exists_before_runtime_and_prevents_replay(self):
        def run(*args,**kwargs):
            records=list((self.root/'data/ego-executions').glob('*.json'))
            self.assertEqual(len(records),1)
            self.assertEqual(json.loads(records[0].read_text())['status'],'execution_unknown')
            self.assertEqual(args[0][0],str(self.cli))
            return subprocess.CompletedProcess(args[0],0,stdout='ok',stderr='')
        with patch.object(ego_browser.subprocess,'run',side_effect=run) as call:
            ego_browser.execute(self.script)
            with self.assertRaisesRegex(ValueError,'已有执行记录'):
                ego_browser.execute(self.script)
        self.assertEqual(call.call_count,1)

    def test_timeout_remains_unknown_and_is_not_reissued(self):
        with patch.object(ego_browser.subprocess,'run',side_effect=subprocess.TimeoutExpired('ego',1)) as call:
            with self.assertRaises(subprocess.TimeoutExpired):ego_browser.execute(self.script)
            with self.assertRaisesRegex(ValueError,'已有执行记录'):ego_browser.execute(self.script)
        self.assertEqual(call.call_count,1)
        record=json.loads(next((self.root/'data/ego-executions').glob('*.json')).read_text())
        self.assertEqual(record['status'],'execution_unknown')

    def test_repeated_status_is_observation_without_mutation_receipt(self):
        with patch.object(ego_browser.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'{}','')) as call:
            ego_browser.execute()
            ego_browser.execute()
        self.assertEqual(call.call_count,2)
        self.assertFalse((self.root/'data/ego-executions').exists())

    def test_application_button_can_only_reuse_explicitly_owned_connection(self):
        with self.assertRaisesRegex(ValueError,'未授权'):ego_browser.execute(self.script,binding='app',application_job=True)
        self.config.update(application_owned=True,task_space_name='App collection',pages={'erp':'p1'})
        with patch.object(ego_browser.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'{}','')):
            ego_browser.execute(self.script,binding='app',application_job=True)
        text=next(self.scripts.glob('round-*.mjs')).read_text(encoding='utf-8')
        self.assertIn('connection.name',text);self.assertIn('claimTaskSpace(1)',text);self.assertIn('tabs.length!==1',text)
        self.assertIn('["user","agent"]',text)

    def test_application_error_marker_requires_exact_schema_binding_and_script_sha(self):
        value={'version':1,'kind':'application_job_error','binding':'app','scriptSha256':'a'*64,'message':'controlled','stack':'Error: controlled'}
        def parsed(data):
            result=subprocess.CompletedProcess([],0,ego_browser.APPLICATION_ERROR_MARKER+json.dumps(data)+'\n','')
            return ego_browser.application_error(result,'app','a'*64)
        self.assertEqual(parsed(value),value)
        for field,replacement in [('version',True),('kind','other'),('binding','other'),('scriptSha256','b'*64),('message',''),('stack',None)]:
            self.assertIsNone(parsed({**value,field:replacement}))
        self.assertIsNone(ego_browser.application_error(subprocess.CompletedProcess([],0,ego_browser.APPLICATION_ERROR_MARKER+'{invalid',''),'app','a'*64))

    @unittest.skipUnless(shutil.which('node'), 'Isolated controlled-throw verification needs Node')
    def test_caught_business_throw_returns_node_zero_but_application_unknown_and_cannot_replay(self):
        self.config.update(application_owned=True,task_space_name='App collection',pages={'erp':'p1'})
        self.script.write_text("throw new Error('controlled business failure');",encoding='utf-8')
        real_run=subprocess.run
        def run_isolated(*args,**kwargs):
            entry=next(self.scripts.glob('round-*.mjs'))
            harness='globalThis.listTaskSpaces=async()=>[{id:1,name:"App collection",ownership:"agent"}];\n'
            harness+='globalThis.taskSpace=async()=>({spaceId:1,name:"App collection",ownership:"agent",pages:async()=>[{label:"p1"}]});\n'
            harness+='await import('+json.dumps(entry.as_uri())+');'
            result=real_run([shutil.which('node'),'--input-type=module','-e',harness],capture_output=True,encoding='utf-8',timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
            return result
        with patch.object(ego_browser.subprocess,'run',side_effect=run_isolated) as call:
            result=ego_browser.execute(self.script,binding='app',application_job=True)
            self.assertEqual(result.returncode,1)
            with self.assertRaisesRegex(ValueError,'已有执行记录'):ego_browser.execute(self.script,binding='app',application_job=True)
        self.assertEqual(call.call_count,1)
        record=json.loads(next((self.root/'data/ego-executions').glob('*.json')).read_text(encoding='utf-8'))
        self.assertEqual(record['status'],'execution_unknown');self.assertEqual(record['exitCode'],1)
        self.assertEqual(record['nodeProcessReturnCode'],0)
        self.assertEqual(record['applicationError']['message'],'controlled business failure')
        source=next(self.scripts.glob('round-*.mjs')).read_text(encoding='utf-8')
        for forbidden in ['process.exit','process.exitCode','.finish(','.close(','.handOff(']:self.assertNotIn(forbidden,source)

    @unittest.skipUnless(shutil.which('node'), 'Isolated controlled-throw verification needs Node')
    def test_caught_guard_keeps_original_message_stack_and_does_not_import_business(self):
        self.config.update(application_owned=True,task_space_name='App collection',pages={'erp':'p1'})
        self.script.write_text("throw new Error('business should not run');",encoding='utf-8')
        real_run=subprocess.run
        def run_isolated(*args,**kwargs):
            entry=next(self.scripts.glob('round-*.mjs'))
            harness='globalThis.listTaskSpaces=async()=>[];await import('+json.dumps(entry.as_uri())+');'
            result=real_run([shutil.which('node'),'--input-type=module','-e',harness],capture_output=True,encoding='utf-8',timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
            return result
        with patch.object(ego_browser.subprocess,'run',side_effect=run_isolated):
            result=ego_browser.execute(self.script,binding='app',application_job=True)
        self.assertEqual(result.returncode,1)
        failure=ego_browser.application_error(result,'app',__import__('hashlib').sha256(self.script.read_bytes()).hexdigest())
        self.assertEqual(failure['message'],'专属采集浏览器连接已变化，请重新连接')
        from urllib.parse import unquote
        self.assertIn(unquote(next(self.scripts.glob('round-*.mjs')).as_uri()), unquote(failure['stack']))
        self.assertNotIn('business should not run',failure['message'])
