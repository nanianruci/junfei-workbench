"""Portable path discovery and read-only diagnostics; never starts external tools."""
import argparse
from functools import lru_cache
import json
import os
from pathlib import Path
import shutil
import sys
import runpy

PROJECT_ENV = 'JUNFEI_PROJECT_ROOT'
PYTHON_ENV = 'JUNFEI_PYTHON'
DEFAULT_PORT = 18769


def workbench_port(environ=None):
    env = os.environ if environ is None else environ
    try:
        port = int(env.get('JUNFEI_PORT', DEFAULT_PORT))
    except (TypeError, ValueError):
        port = 0
    if not 1024 <= port <= 65535:
        raise ValueError('JUNFEI_PORT 必须是 1024 至 65535 之间的端口号')
    return port


def _validated_root(value, origin):
    root = Path(value).expanduser()
    if not root.is_absolute():
        raise ValueError(f'{origin} 必须是绝对路径')
    root = root.resolve()
    if not (root / 'automation/config.json').is_file():
        raise FileNotFoundError(f'{origin} 未找到 automation/config.json；请设置 {PROJECT_ENV} 为完整项目目录')
    return root


def project_root(app_dir=None, environ=None):
    """Explicit configuration wins; stale pointers fail closed, never use old budgets."""
    env = os.environ if environ is None else environ
    app = Path(app_dir or Path(__file__).resolve().parent).resolve()
    configured = env.get(PROJECT_ENV, '').strip()
    if configured:
        return _validated_root(configured, PROJECT_ENV)
    pointer = app / 'project-root.txt'
    if pointer.is_file():
        value = pointer.read_text(encoding='utf-8-sig').strip()
        if not value:
            raise ValueError(f'project-root.txt 为空；请设置 {PROJECT_ENV}')
        return _validated_root(value, 'project-root.txt')
    for candidate in (app, app.parent):
        if (candidate / 'automation/config.json').is_file():
            return candidate
    raise ValueError(f'未找到项目配置；请设置 {PROJECT_ENV}，不能只复制 EXE')


def resource_root(app_dir, default, environ=None):
    """An explicit root selects matching code resources as well as operating policy."""
    env = os.environ if environ is None else environ
    if env.get(PROJECT_ENV, '').strip():
        return project_root(app_dir, env)
    return Path(default)


def configuration(app_dir=None, environ=None):
    path = project_root(app_dir, environ) / 'automation/config.json'
    config = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(config, dict):
        raise ValueError('项目配置必须是 JSON 对象')
    return config


def python_command(environ=None):
    """Return an interpreter command, not the frozen GUI executable or py launcher."""
    env = os.environ if environ is None else environ
    configured = env.get(PYTHON_ENV, '').strip()
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute() or not path.is_file():
            raise ValueError(f'{PYTHON_ENV} 必须指向本机 Python 可执行文件的绝对路径')
        return [str(path.resolve())]
    if not getattr(sys, 'frozen', False):
        return [sys.executable]
    for name in ('python', 'python3'):
        located = shutil.which(name)
        if located and Path(located).is_file():
            return [located]
    raise ValueError(f'未找到 Python 运行时；请安装 Python 3.10+ 并设置 {PYTHON_ENV}，不会启动外部工具')


def ego_command(project=None, environ=None):
    env = os.environ if environ is None else environ
    root = Path(project) if project is not None else project_root(environ=env)
    script = root / 'automation/ego_browser.py'
    if not script.is_file():
        raise FileNotFoundError('Missing local Ego adapter; no browser action was started')
    if getattr(sys, 'frozen', False) and not env.get(PYTHON_ENV, '').strip():
        return [sys.executable, '--workbench-tool', 'ego-browser']
    return python_command(env) + ['-X', 'utf8', str(script)]


@lru_cache(maxsize=1)
def _ego_lock():
    bundled = Path(__file__).resolve().parent.parent / 'automation/browser_space_lock.py'
    script = bundled if bundled.is_file() else project_root() / 'automation/browser_space_lock.py'
    return runpy.run_path(str(script))['active_space']


def ego_space(space_id):
    return _ego_lock()(space_id)


def node_command(app_dir=None, root=None, environ=None):
    """Use the same Node resolution for diagnostics and actual local preflight."""
    app = Path(app_dir or Path(__file__).resolve().parent)
    project = Path(root) if root is not None else project_root(app, environ)
    for path in (project / 'node.exe', app / 'node.exe'):
        if path.is_file():
            return [str(path.resolve())]
    located = shutil.which('node')
    if located and Path(located).is_file():
        return [located]
    raise FileNotFoundError('未找到 Node.js 或随包 Node 运行时；未执行预检或浏览器操作')


def diagnose(app_dir=None, environ=None):
    """Inspect local files only. No database writes, credentials, network, or subprocesses."""
    checks = []
    result = {
        'schemaVersion': 1, 'readOnly': True, 'runtimeFilesReady': False,
        'readyForUnattendedRun': False, 'checks': checks,
        'notes': [
            '自检只验证文件和配置，不验证登录、授权、网络连通性或实际商品结果。',
            '完整自动选品、生图总控、刊登及在售核对尚未全部接通。',
            '更换电脑后需核对本机 EGO 路径、专属工作区、页面和店铺绑定；不复制浏览器登录资料。',
            '本机与云端数据库没有双向同步，迁移前须确定唯一生产数据来源。',
        ],
    }
    def check(key, ok, message):
        checks.append({'id': key, 'ok': bool(ok), 'message': message})
    env = os.environ if environ is None else environ
    try:
        result['workbenchUrl'] = f'http://127.0.0.1:{workbench_port(env)}'
    except ValueError as error:
        result['connectionIssue'] = str(error)
    if env.get('JUNFEI_ACCOUNT_DATA'):
        result['accountDataDirectory'] = str(Path(env['JUNFEI_ACCOUNT_DATA']).expanduser().resolve())
    try:
        root = project_root(app_dir, environ)
        result['projectRoot'] = str(root)
        config = configuration(app_dir, environ)
        check('project-config', True, '已找到有效的项目配置文件')
    except (OSError, ValueError, TypeError) as error:
        check('project-config', False, str(error))
        return result
    if result.get('connectionIssue'):
        check('workbench-port', False, result['connectionIssue'])
    for relative in ('automation/ego_browser.py', 'automation/browser_space_lock.py', 'junfei-publisher/ego_link.py',
                     'junfei-publisher/ego-existing-draft.mjs',
                     'junfei-publisher/ego-library-package.mjs',
                     'junfei-publisher/listing.mjs'):
        check(relative, (root / relative).is_file(), relative)
    try:
        env = os.environ if environ is None else environ
        if getattr(sys, 'frozen', False) and not env.get(PYTHON_ENV, '').strip():
            check('python', True, '软件包内置 Python，仅提供固定工具入口；实际入口仍需隔离验收')
        else:
            command = python_command(environ)
            check('python', Path(command[0]).is_file(), 'Python 文件存在；版本与依赖仍需离线测试验证')
    except ValueError as error:
        check('python', False, str(error))
    app = Path(app_dir or Path(__file__).resolve().parent)
    try:
        node_command(app, root, environ)
        check('node', True, '已找到 Node.js 或随包 Node 运行时')
    except FileNotFoundError as error:
        check('node', False, str(error))
    browser = config.get('browser')
    if not isinstance(browser, dict):
        browser = {}
    cli = browser.get('cli')
    check('ego-runtime', isinstance(cli, str) and Path(cli).is_absolute() and Path(cli).is_file(),
          'EGO 路径必须指向此电脑已安装的运行时；不自动安装或改动浏览器')
    directory = browser.get('script_directory')
    check('ego-script-directory', isinstance(directory, str) and Path(directory).is_absolute() and Path(directory).is_dir(),
          '脚本目录必须是此电脑已存在的绝对目录；本自检不创建目录')
    space = browser.get('task_space_id')
    check('ego-task-space', type(space) is int and space >= 0 and browser.get('engine') == 'ego-lite',
          '仅验证 EGO 工作区配置格式；实际归属及店铺登录尚未验证')
    bindings = browser.get('task_bindings')
    result['egoBindings'] = [{'binding': name, 'taskSpaceId': value.get('task_space_id'),
                              'configured': type(value.get('task_space_id')) is int
                              and value['task_space_id'] >= 0 and bool(value.get('task_space_name'))}
                             for name, value in (bindings.items() if isinstance(bindings, dict) else ())
                             if isinstance(value, dict)]
    assets = root / 'listing-workbench'
    try:
        templates = json.loads((assets / 'official-image-templates/manifest.json').read_text(encoding='utf-8'))
        counts = (templates.get('layoutCount'), templates.get('verifiedImageCount'), templates.get('repairCount'))
        files = templates.get('files') or {}
        missing = sum(not (assets / 'official-image-templates' / name).is_file() for name in files)
        result['imageResources'] = {'templateFilesPresent': counts == (240, 240, 24) and bool(files) and missing == 0,
                                    'missingManifestFiles': missing}
    except (OSError, ValueError, TypeError, AttributeError):
        result['imageResources'] = {'templateFilesPresent': False}
    result['imageResources']['localModelRuntimePresent'] = (assets / 'models/local-instance/runtime.json').is_file()
    result['runtimeFilesReady'] = all(item['ok'] for item in checks)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', help='本次自检使用的完整项目绝对路径，不保存设置')
    parser.add_argument('--json', action='store_true', help='输出不含密钥的 JSON')
    args = parser.parse_args()
    env = os.environ.copy()
    if args.project_root:
        env[PROJECT_ENV] = args.project_root
    report = diagnose(environ=env)
    if args.json:
        print(json.dumps(report, ensure_ascii=True, indent=2))
    else:
        for item in report['checks']:
            print(('OK ' if item['ok'] else 'MISSING ') + item['id'] + ': ' + item['message'])
        for note in report['notes']:
            print(note)
    return 0 if report['runtimeFilesReady'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
