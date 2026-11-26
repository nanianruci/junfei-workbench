"""Run trusted project browser scripts inside the configured Ego Lite task space.

Never launches Edge or copies browser login data. Scripts use Ego's documented API.
Every run has a persistent receipt; the same file/content cannot silently replay.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid
from browser_space_lock import active_space

ROOT = Path(__file__).resolve().parent
APPLICATION_ERROR_MARKER = '__JUNFEI_EGO_APPLICATION_ERROR__'


def settings(binding=None):
    config = json.loads((ROOT / 'config.json').read_text(encoding='utf-8'))['browser']
    if binding is not None:
        selected = config.get('task_bindings', {}).get(binding)
        if not isinstance(selected, dict):
            raise ValueError('未配置指定的Ego任务绑定，不回退共享工作区')
        required = ('task_space_id', 'task_space_name', 'script_directory')
        if any(key not in selected for key in required):
            raise ValueError('Ego任务绑定不完整，不继承共享工作区身份')
        config = {**config, **selected, 'binding': binding}
    if config.get('engine') != 'ego-lite':
        raise ValueError('专属浏览器未配置为ego-lite')
    if type(config.get('task_space_id')) is not int or config['task_space_id'] < 0:
        raise ValueError('缺少已确认的Ego TaskSpace ID')
    cli = Path(config.get('cli') or shutil.which('ego-browser') or '')
    if not cli.is_file():
        raise ValueError('ego-browser未找到；请完成ego安装/引导，不会回退Edge')
    directory = Path(config['script_directory'])
    if not directory.is_absolute():
        raise ValueError('Ego脚本目录必须为绝对路径')
    directory.mkdir(parents=True, exist_ok=True)
    return config, cli, directory


def persist(path, record):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(record, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def application_error(result, binding, script_sha256):
    """Only a complete marker belonging to this invocation can change its result."""
    for output in (result.stdout, result.stderr):
        if isinstance(output, bytes):
            output = output.decode('utf-8', errors='replace')
        for line in (output or '').splitlines():
            if not line.startswith(APPLICATION_ERROR_MARKER):
                continue
            try:
                value = json.loads(line[len(APPLICATION_ERROR_MARKER):])
            except (ValueError, TypeError):
                continue
            if (isinstance(value, dict) and type(value.get('version')) is int and value.get('version') == 1
                    and value.get('kind') == 'application_job_error'
                    and value.get('binding') == binding and value.get('scriptSha256') == script_sha256
                    and isinstance(value.get('message'), str) and value['message']
                    and isinstance(value.get('stack'), str)):
                return value
    return None


def execute(script=None, timeout=60, resume=False, binding=None, application_job=False):
    config, cli, directory = settings(binding)
    with active_space(config['task_space_id']):
        return execute_bound(config, cli, directory, script, timeout, resume, binding, application_job)


def execute_bound(config, cli, directory, script, timeout, resume, binding, application_job):
    method = 'takeOverTaskSpace' if resume else 'taskSpace'
    bootstrap = f'globalThis.amazonTask = await {method}({config["task_space_id"]});\n'
    if application_job:
        if not binding or config.get('application_owned') is not True or not script:
            raise ValueError('此绑定未授权工作台按钮复用专属采集浏览器')
        expected=config.get('task_space_name')
        bootstrap=(
            'const spaces=await listTaskSpaces();\n'
            f'const connection=spaces.find(s=>s.id==={config["task_space_id"]});\n'
            f'if(!connection||connection.name!=={json.dumps(expected)})throw Error("专属采集浏览器连接已变化，请重新连接");\n'
            'if(!["user","agent"].includes(connection.ownership))throw Error("浏览器已交还或中断，请完成验证后继续");\n'
            f'globalThis.amazonTask=connection.ownership==="user"?await claimTaskSpace({config["task_space_id"]}):await taskSpace({config["task_space_id"]});\n'
        )
    expected_name = config.get('task_space_name')
    bootstrap += (
        f'if (amazonTask.spaceId !== {config["task_space_id"]} || amazonTask.ownership !== "agent") '
        'throw new Error("专属工作区归属不符，停止操作");\n'
    )
    if expected_name:
        bootstrap += (
            f'if (amazonTask.name !== {json.dumps(expected_name)}) '
            'throw new Error("专属工作区名称不符，停止操作");\n'
        )
    if application_job:
        label=config.get('pages',{}).get('erp')
        if not isinstance(label,str) or not __import__('re').fullmatch(r'p\d+',label):raise ValueError('采集连接缺少ERP页面')
        bootstrap+=(
            f'if(!(await amazonTask.pages()).some(p=>p.label==={json.dumps(label)})){{\n'
            'const tabs=(await amazonTask.tabs()).filter(t=>!t.label&&t.url.startsWith("https://www.junfeikuajing.com/"));\n'
            'if(tabs.length!==1)throw Error("专属ERP页面不唯一，请重新连接");\n'
            f'await amazonTask.adopt(tabs[0].page,{{as:{json.dumps(label)}}});\n'
            '}\n'
        )
    receipt = None
    if script is None:
        bootstrap += 'console.log(JSON.stringify({spaceId:amazonTask.spaceId,name:amazonTask.name,tabs:(await amazonTask.tabs()).map(t=>({label:t.label,url:t.url,title:t.title,openedBy:t.openedBy}))},null,2));\n'
    else:
        script = Path(script).resolve(strict=True)
        if script.suffix != '.mjs':
            raise ValueError('仅支持已审阅的Ego .mjs脚本；不能直接运行Playwright脚本')
        digest = hashlib.sha256(script.read_bytes()).hexdigest()
        key = hashlib.sha256((str(script) + ':' + digest).encode('utf-8')).hexdigest()
        ledger = ROOT / 'data' / 'ego-executions'
        ledger.mkdir(parents=True, exist_ok=True)
        receipt = ledger / (key + '.json')
        record = {'script': str(script), 'sha256': digest,
                  'taskSpaceId': config['task_space_id'], 'status': 'execution_unknown'}
        if binding is not None:
            record['browserBinding'] = binding
        # Exclusive creation is the replay barrier, before invoking the runtime.
        try:
            with receipt.open('x', encoding='utf-8') as stream:
                json.dump(record, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            raise ValueError(f'该脚本已有执行记录，先核对实际结果，不重复执行：{receipt}') from None
        bootstrap += f'await import({json.dumps(script.as_uri())});\n'
    if application_job:
        # Keep JS failures inside this invocation. No ownership or page cleanup
        # is performed: Python still records an unknown/failed application run.
        bootstrap = ('try {\n' + bootstrap + '} catch (applicationError) {\n'
            + 'let message="Unknown application error",stack="";\n'
            + 'try{message=String(applicationError?.message??applicationError);}catch{}\n'
            + 'try{if(typeof applicationError?.stack==="string")stack=applicationError.stack;}catch{}\n'
            + 'console.log(' + json.dumps(APPLICATION_ERROR_MARKER)
            + '+JSON.stringify({version:1,kind:"application_job_error",binding:' + json.dumps(binding)
            + ',scriptSha256:' + json.dumps(digest)
            + ',message:message.slice(0,2000)||"Unknown application error",stack:stack.slice(0,20000)}));\n'
            + '}\n')
    entry = directory / ('round-' + uuid.uuid4().hex + '.mjs')
    entry.write_text(bootstrap, encoding='utf-8')
    try:
        result = subprocess.run([str(cli), 'nodejs', '-e', f"await import('{entry.as_uri()}')"],
                                capture_output=True, encoding='utf-8', timeout=timeout)
    except Exception as error:
        if receipt:
            record.update(error=type(error).__name__, entry=str(entry))
            persist(receipt, record)
        raise
    failure = application_error(result, binding, digest) if application_job else None
    node_returncode = result.returncode
    if failure:
        # The caught Node script returns normally; the application must still
        # fail closed and retain its original one-shot execution receipt.
        result = subprocess.CompletedProcess(result.args, 1, result.stdout, result.stderr)
    if receipt:
        # Runtime success is not proof of a supplier send, ERP save, or live listing.
        record.update(status='runtime_returned' if result.returncode == 0 else 'execution_unknown',
                      exitCode=result.returncode, entry=str(entry))
        if failure:
            record.update(applicationError=failure, nodeProcessReturnCode=node_returncode)
        persist(receipt, record)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    status = sub.add_parser('status')
    resume = sub.add_parser('resume', help='用户确认完成手动操作后，恢复同一工作区并读取状态')
    run = sub.add_parser('run')
    for command in (status, resume, run):
        command.add_argument('--binding', help='使用config.browser.task_bindings中的独立任务工作区，不改变默认刊登绑定')
    run.add_argument('script', help='A new, reviewed Ego API .mjs script')
    run.add_argument('--timeout', type=int, default=60)
    run.add_argument('--application-job',action='store_true',help='工作台用户按钮触发，复用明确授权的专属采集连接')
    args = parser.parse_args()
    try:
        result = execute(getattr(args, 'script', None), getattr(args, 'timeout', 60), args.action == 'resume', args.binding,getattr(args,'application_job',False))
        print(result.stdout, end='')
        if result.stderr:
            print(result.stderr, end='', file=__import__('sys').stderr)
        return result.returncode
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        print(str(error), file=__import__('sys').stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
