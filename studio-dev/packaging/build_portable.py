"""Build a clean Windows portable app without production data or source paths."""
import argparse
import ast
import tempfile
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
MODULES = ('ego-existing-draft.mjs', 'ego-draft-images.mjs', 'ego-library-draft.mjs',
           'ego-library-images.mjs', 'ego-library-package.mjs', 'listing.mjs', 'existing-draft.mjs')
FORBIDDEN_NAMES = {'project-root.txt', '.env', 'auth.json', 'deployment.json'}
FORBIDDEN_SUFFIXES = ('.sqlite3', '.sqlite3-wal', '.sqlite3-shm', '.dpapi', '.fernet', '.key')
SOURCE_ASSET_DIRS = ('extension', 'assets')
SOURCE_IMAGE_SUFFIXES = {'.png', '.ico', '.jpg', '.jpeg', '.webp'}


def validate_defaults(config):
    if (config.get('images', {}).get('enabled') is not False
            or config['images'].get('codex_tools', {}).get('enabled') is not False
            or config.get('publishing', {}).get('mode') != 'draft'
            or config['publishing'].get('store')
            or config.get('profiles') != {}
            or config.get('execution', {}).get('scheduled') is not False):
        raise ValueError('Portable defaults must disable paid work, publishing and saved stores')
    browser = config.get('browser', {})
    if browser.get('task_space_id') is not None or any(browser.get(k) for k in ('cli', 'script_directory', 'task_space_name',
                                     'pages', 'erp_account_bindings', 'erp_library_bindings', 'discovery_account_bindings')):
        raise ValueError('Portable defaults contain machine or account bindings')
    if browser.get('fallback_to_edge') is not False:
        raise ValueError('Browser fallback must remain disabled')
    discovery = config.get('discovery_browser')
    if (not isinstance(discovery, dict)
            or set(discovery) != {'engine', 'cli', 'account_bindings'}
            or discovery.get('engine') != 'ego-lite'
            or discovery.get('cli') != ''
            or discovery.get('account_bindings') != {}):
        raise ValueError('Portable discovery defaults must have no CLI or account bindings')


def create_source_archive(output, root):
    """Corresponding runtime source, UI source, build instructions and licenses only."""
    app = root / 'listing-workbench'
    selected = {}
    pending = ['desktop_launcher', 'account_runtime', 'agent_library', 'runtime_environment', 'portable_support', 'workspace_backup']
    visited = set()
    while pending:
        name = pending.pop()
        if name in visited:
            continue
        visited.add(name)
        source = app / (name + '.py')
        if not source.is_file():
            continue
        selected['listing-workbench/' + source.name] = source.read_bytes()
        tree = ast.parse(source.read_text(encoding='utf-8-sig'))
        for node in ast.walk(tree):
            names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or ''] if isinstance(node, ast.ImportFrom) else []
            pending.extend(n.split('.')[0] for n in names if (app / (n.split('.')[0] + '.py')).is_file())
    # Direct fixture imports also work with a minimal source tree in offline tests.
    for pattern in ('*.js', '*.css', '*prompt*.txt'):
        for source in app.glob(pattern):
            selected['listing-workbench/' + source.name] = source.read_bytes()
    for name in ('index.html', 'LICENSE-AGPL-3.0.txt', 'SOURCE-NOTICE.md', 'cloud_requirements.txt', 'CROSS-COMPUTER.md', 'AUTOMATION-RECOVERY.md', 'test_workflow_recovery.py', 'SELECTION-TO-COPY.md', 'test_agent_handoff.py', 'test_agent_handoff_ui.cjs', 'IMAGE-PREPARATION.md', 'test_agent_image_preparation.py', 'test_agent_image_preparation_ui.cjs', 'CYCLIC-DISCOVERY.md', 'test_cyclic_discovery.py', 'test_discovery_api.py', 'test_discovery_ui.cjs', 'test_agent_api.py', 'DISCOVERY-BROWSER.md', 'DISCOVERY-PLAN.md', 'test_discovery_browser.py', 'test_discovery_seller_collector.cjs', 'test_discovery_store_collector.cjs', 'test_discovery_search_collector.cjs', 'test_discovery_plan.py', 'test_discovery_family.py', 'test_discovery_family_capture.py', 'test_discovery_family_api.py', 'test_discovery_family_plan.py', 'test_discovery_family_collector.cjs', 'test_discovery_run.py', 'test_discovery_live.py', 'test_discovery_live_api.py', 'test_discovery_navigation.py', 'discovery_live_coordinated.py', 'test_discovery_live_coordinated.py', 'test_discovery_live_coordinated_adversarial.py', 'DISCOVERY-LIVE-COORDINATED.md', 'DISCOVERY-LIVE.md', 'DISCOVERY-FAMILY.md', 'DISCOVERY-RUN.md', 'test_discovery_browser_ui.cjs', 'test_supplier_detail_capture.cjs'):
        source = app / name
        if source.is_file():
            selected['listing-workbench/' + name] = source.read_bytes()
    allowed = {'.ts', '.tsx', '.js', '.jsx', '.css', '.json', '.md', '.html', '.svg', '.txt'}
    for directory in ('visual-ui', *SOURCE_ASSET_DIRS):
        suffixes = allowed | SOURCE_IMAGE_SUFFIXES if directory in SOURCE_ASSET_DIRS else allowed
        for source in (app / directory).rglob('*'):
            relative = source.relative_to(app)
            if not source.is_file() or source.is_symlink() or any(part.startswith('.') or part in ('node_modules', 'dist', '__pycache__') for part in relative.parts):
                continue
            if source.suffix.lower() in suffixes and source.name not in FORBIDDEN_NAMES:
                selected['listing-workbench/' + relative.as_posix()] = source.read_bytes()
    for relative in ('AmazonAgentPortable.spec', 'packaging/build_portable.py', 'packaging/smoke_portable.py', 'packaging/test_portable_build.py',
                     'packaging/portable-config.json', 'packaging/PORTABLE-README.txt', 'packaging/verify_live_discovery.py', 'packaging/verify_live_store.py', 'packaging/verify_live_search.py', 'automation/ego_browser.py', 'automation/shared_browser_space.py', 'automation/test_shared_browser_space.py', 'automation/SHARED-BROWSER-SPACE.md', 'automation/SHARED-BROWSER-ADOPTION.md', 'automation/pipeline.py', 'automation/codex_workbench.py', 'automation/test_budget_carryover.py', 'automation/test_pipeline.py', 'automation/test_ego_browser.py', 'automation/cyclic_family.py', 'automation/test_cyclic_family.py', 'automation/test_supplemental_rank_search.py',
                     'junfei-publisher/ego_link.py', 'junfei-publisher/package.json', 'junfei-publisher/package-lock.json'):
        source = root / relative
        if source.is_file():
            selected[relative] = source.read_bytes()
    for name in MODULES:
        selected['junfei-publisher/' + name] = (root / 'junfei-publisher' / name).read_bytes()
    for relative in ('listing-workbench/DISCOVERY-INTAKE.md', 'listing-workbench/test_discovery_intake.py',
                     'listing-workbench/test_listing_locks.py', 'listing-workbench/test_listing_workbench.cjs',
                     'listing-workbench/IMAGE-FACTORY.md', 'listing-workbench/test_local_image_provider.py',
                     'automation/sellersprite_date.py'):
        source = root / relative
        if source.is_file():
            selected[relative] = source.read_bytes()
    selected['automation/browser_space_lock.py'] = (root / 'automation/browser_space_lock.py').read_bytes()
    selected['automation/config.json'] = (root / 'packaging/portable-config.json').read_bytes()
    selected['README-BUILD.txt'] = (
        'Corresponding source for AmazonAgentPortable, AGPL-3.0-only.\n'
        'No production data, credentials, or original machine configuration is included.\n'
        'Requirements: Windows x64, Python 3.12, Node.js 20.9+, PyInstaller 6.22.3.\n'
        'Install Python dependencies from listing-workbench/cloud_requirements.txt.\n'
        'Restore npm dependencies: npm ci in junfei-publisher and listing-workbench/visual-ui.\n'
        'Build UI: npm run build in listing-workbench/visual-ui.\n'
        'Build portable app: python packaging/build_portable.py.\n'
        'Runtime assets in listing-workbench/assets and listing-workbench/extension are build inputs.\n'
        'Source run: python listing-workbench/server.py. New configuration is deliberately unconfigured.\n'
        'packaging/verify_live_discovery.py, verify_live_store.py and verify_live_search.py are optional source-only live read verifiers, never a startup step.\n'
        'Configure a dedicated discovery_browser route on this computer; never reuse the ERP task space.\n'
        'See LICENSE-AGPL-3.0.txt, SOURCE-NOTICE.md and visual-ui/NOTICE.md under listing-workbench.\n'
    ).encode()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name, raw in sorted(selected.items()):
            archive.writestr(name, raw)
    return {name: hashlib.sha256(raw).hexdigest() for name, raw in sorted(selected.items())}


def prepare_stage(destination, root=ROOT):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    template = root / 'packaging/portable-config.json'
    config = json.loads(template.read_text(encoding='utf-8'))
    validate_defaults(config)
    runtime = destination / 'portable-runtime'
    (runtime / 'automation').mkdir(parents=True)
    (runtime / 'junfei-publisher').mkdir()
    (runtime / 'automation/config.json').write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
    shutil.copy2(root / 'automation/ego_browser.py', runtime / 'automation/ego_browser.py')
    shutil.copy2(root / 'automation/browser_space_lock.py', runtime / 'automation/browser_space_lock.py')
    shutil.copy2(root / 'junfei-publisher/ego_link.py', runtime / 'junfei-publisher/ego_link.py')
    for name in MODULES:
        shutil.copy2(root / 'junfei-publisher' / name, runtime / 'junfei-publisher' / name)
    shutil.copytree(root / 'junfei-publisher/node_modules', runtime / 'junfei-publisher/node_modules',
                    ignore=shutil.ignore_patterns('.cache', '.git', '.env', '*.log'))
    (runtime / 'junfei-publisher/recipe.json').write_text(json.dumps({'calibrated': False, 'note': 'New computer: no live publisher calibration or authorization'}), encoding='utf-8')
    hashes = {}
    for p in sorted(runtime.rglob('*')):
        if p.is_file():
            if p.name in FORBIDDEN_NAMES or p.name.endswith(FORBIDDEN_SUFFIXES):
                raise ValueError('Forbidden file in stage: ' + str(p.relative_to(runtime)))
            hashes[p.relative_to(runtime).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    source_hashes = create_source_archive(destination / 'corresponding-source.zip', root)
    build_id = hashlib.sha256(json.dumps([hashes, source_hashes], sort_keys=True).encode()).hexdigest()[:20]
    manifest = {'schemaVersion': 1, 'buildId': build_id, 'createdAtUtc': datetime.now(timezone.utc).isoformat(),
                'platform': 'windows-x64', 'runtimeFiles': hashes,
                'sourceArchiveSha256': hashlib.sha256((destination / 'corresponding-source.zip').read_bytes()).hexdigest(),
                'paidGenerationEnabled': False, 'publishEnabled': False,
                'productionDataIncluded': False, 'sourceProjectPointerIncluded': False}
    (destination / 'portable-release.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    return manifest


def verify_artifact(folder):
    folder = Path(folder)
    internal = folder / '_internal'
    if not (folder / 'AmazonAgentPortable.exe').is_file() or not (internal / 'node.exe').is_file():
        raise ValueError('Portable executable or bundled Node is missing')
    manifest = json.loads((internal / 'portable-release.json').read_text(encoding='utf-8'))
    runtime = internal / 'portable-runtime'
    source_archive = internal / 'corresponding-source.zip'
    if not source_archive.is_file() or hashlib.sha256(source_archive.read_bytes()).hexdigest() != manifest['sourceArchiveSha256']:
        raise ValueError('Corresponding source archive is missing or changed')
    with zipfile.ZipFile(source_archive) as archive:
        for directory in SOURCE_ASSET_DIRS:
            for asset in sorted((internal / directory).rglob('*')):
                if not asset.is_file():
                    continue
                name = 'listing-workbench/' + asset.relative_to(internal).as_posix()
                if name not in archive.namelist() or archive.read(name) != asset.read_bytes():
                    raise ValueError('Corresponding source asset missing or changed: ' + name)
    for relative, expected in manifest['runtimeFiles'].items():
        path = (runtime / relative).resolve()
        if runtime.resolve() not in path.parents or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError('Runtime hash mismatch: ' + relative)
    for path in folder.rglob('*'):
        if path.is_file() and (path.name in FORBIDDEN_NAMES or path.name.endswith(FORBIDDEN_SUFFIXES)):
            raise ValueError('Forbidden distributed file: ' + str(path.relative_to(folder)))
    validate_defaults(json.loads((runtime / 'automation/config.json').read_text(encoding='utf-8')))
    return {'buildId': manifest['buildId'], 'runtimeFileCount': len(manifest['runtimeFiles']),
            'sourceProjectPointerIncluded': False, 'productionDataIncluded': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='New build directory; never overwrites an existing directory')
    parser.add_argument('--stage-only', action='store_true')
    args = parser.parse_args()
    output = (args.output or Path(tempfile.gettempdir()) / 'AmazonBuilds' / datetime.now().strftime('%Y%m%d-%H%M%S')).resolve()
    output.mkdir(parents=True, exist_ok=False)
    stage = output / 'stage'
    manifest = prepare_stage(stage)
    if args.stage_only:
        print(json.dumps({'stage': str(stage), 'buildId': manifest['buildId']}))
        return 0
    if os.name != 'nt':
        raise RuntimeError('This build currently targets Windows only')
    longest = max(len(str(output / 'dist/AmazonAgentPortable/_internal/portable-runtime' / name)) for name in manifest['runtimeFiles'])
    if longest >= 250:
        raise ValueError('Build output path is too long for portable Windows use; choose a shorter --output directory')
    env = os.environ.copy()
    env['JUNFEI_PORTABLE_STAGE'] = str(stage)
    command = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--workpath', str(output / 'work'),
               '--distpath', str(output / 'dist'), str(ROOT / 'AmazonAgentPortable.spec')]
    with (output / 'build.log').open('w', encoding='utf-8') as log:
        completed = subprocess.run(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if completed.returncode:
        raise RuntimeError('Build failed; inspect ' + str(output / 'build.log'))
    app = output / 'dist/AmazonAgentPortable'
    shutil.copy2(ROOT / 'packaging/PORTABLE-README.txt', app / '使用说明.txt')
    result = verify_artifact(app)
    result.update(folder=str(app), buildLog=str(output / 'build.log'), smokeTested=False,
                  secondComputerTested=False)
    (output / 'build-result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
