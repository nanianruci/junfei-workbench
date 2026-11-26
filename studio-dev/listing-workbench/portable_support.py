"""Clean portable-package bootstrap and fixed CLI entry points. No external calls."""
import io
import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil
import sys
import tempfile
import runtime_environment

MARKER = 'portable-release.json'
DEFAULT_PORT = '18768'


def release(base):
    marker = Path(base) / MARKER
    if not marker.is_file():
        return None
    value = json.loads(marker.read_text(encoding='utf-8'))
    if value.get('schemaVersion') != 1 or not isinstance(value.get('buildId'), str):
        raise ValueError('Invalid portable release manifest')
    return value


def bundled_project(base):
    root = Path(base) / 'portable-runtime'
    if not (root / 'automation/config.json').is_file():
        raise FileNotFoundError('Portable runtime is incomplete')
    return root


def configure(base, environ=None):
    """Copy clean runtime to one writable user profile; never migrate existing data."""
    env = os.environ if environ is None else environ
    meta = release(base)
    if meta is None:
        return False
    env.setdefault('JUNFEI_ACCOUNT_DATA', str(Path.home() / 'AmazonWorkbenchPortableData'))
    env.setdefault('JUNFEI_PORT', DEFAULT_PORT)
    data = Path(env['JUNFEI_ACCOUNT_DATA']).expanduser().resolve()
    env['JUNFEI_ACCOUNT_DATA'] = str(data)
    if env.get('JUNFEI_PROJECT_ROOT', '').strip():
        runtime_environment.project_root(base, env)
        return True
    source = bundled_project(base)
    data.mkdir(parents=True, exist_ok=True)
    target = data / 'portable-runtime'
    installed = target / '.installed-release.json'
    if not target.exists():
        with tempfile.TemporaryDirectory(prefix='.portable-install-', dir=data) as temp:
            staged = Path(temp) / 'runtime'
            shutil.copytree(source, staged)
            (staged / '.installed-release.json').write_text(json.dumps({'buildId': meta['buildId']}), encoding='utf-8')
            try:
                staged.rename(target)
            except FileExistsError:
                pass  # A concurrent first launch may have installed the same build.
    if not installed.is_file():
        raise ValueError('Existing portable runtime has no install marker; no files were replaced')
    previous = json.loads(installed.read_text(encoding='utf-8'))
    if previous.get('buildId') != meta['buildId']:
        raise ValueError('This data directory belongs to another build. Preserve its records and use a new test data directory or an explicit reviewed migration; no overwrite performed')
    for relative, expected in meta.get('runtimeFiles', {}).items():
        if relative == 'automation/config.json':
            continue  # Operator configuration is mutable; execution receipts are never replaced.
        path = (target / relative).resolve()
        if target.resolve() not in path.parents or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError('Installed runtime file changed or is missing: ' + relative)
    env['JUNFEI_PROJECT_ROOT'] = str(target)
    return True


def readonly_project(base, env):
    data = Path(env.get('JUNFEI_ACCOUNT_DATA') or Path.home() / 'AmazonWorkbenchPortableData').expanduser().resolve()
    candidate = data / 'portable-runtime'
    marker = candidate / '.installed-release.json'
    if candidate.exists():
        if not marker.is_file() or json.loads(marker.read_text(encoding='utf-8')).get('buildId') != release(base)['buildId']:
            raise ValueError('Existing profile runtime does not match this build; no fallback or overwrite')
        return candidate
    return bundled_project(base)


def attach_cli_streams():
    # Windowed Python has None streams even when Popen supplied valid OS pipes.
    for name, selector in (('stdout', -11), ('stderr', -12)):
        if getattr(sys, name) is not None:
            continue
        stream = None
        if os.name == 'nt':
            import ctypes
            import msvcrt
            get_handle = ctypes.windll.kernel32.GetStdHandle
            get_handle.argtypes = [ctypes.c_ulong]
            get_handle.restype = ctypes.c_void_p
            handle = get_handle(selector & 0xffffffff)
            if handle not in (None, 0, ctypes.c_void_p(-1).value):
                try:
                    fd = msvcrt.open_osfhandle(handle, os.O_WRONLY)
                    stream = os.fdopen(fd, 'w', encoding='utf-8', buffering=1, closefd=False)
                except OSError:
                    pass
        setattr(sys, name, stream or io.StringIO())


def handle_cli(base, argv=None):
    """Handle only fixed diagnostics and Ego adapter entry points before server import."""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in ('--self-check', '--workbench-tool'):
        return False
    env = os.environ.copy()
    if release(base):
        env.setdefault('JUNFEI_PORT', DEFAULT_PORT)
        if not env.get('JUNFEI_PROJECT_ROOT', '').strip():
            env['JUNFEI_PROJECT_ROOT'] = str(readonly_project(base, env))
    if args[0] == '--self-check':
        if len(args) != 2:
            raise ValueError('Usage: --self-check NEW_REPORT_FILE.json')
        report = runtime_environment.diagnose(base, env)
        with Path(args[1]).open('x', encoding='utf-8') as output:
            json.dump(report, output, ensure_ascii=False, indent=2)
        return True
    if len(args) < 2 or args[1] != 'ego-browser':
        raise ValueError('Only the fixed ego-browser tool is supported')
    project = runtime_environment.project_root(base, env)
    script = project / 'automation/ego_browser.py'
    if not script.is_file():
        raise FileNotFoundError('Ego adapter is missing')
    # run_path preserves __file__, so the adapter reads config and receipts from
    # this same project, including an explicitly selected writable runtime.
    previous_argv = sys.argv
    sys.argv = [str(script), *args[2:]]
    attach_cli_streams()
    sys.path.insert(0, str(script.parent))
    try:
        runpy.run_path(str(script), run_name='__main__')
    finally:
        sys.path.remove(str(script.parent))
        sys.argv = previous_argv
    return True
