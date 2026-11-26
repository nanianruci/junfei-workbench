"""Create and verify a local snapshot of an idle workbench data directory."""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile


MANIFEST = 'snapshot.json'
SQLITE = ('.sqlite', '.sqlite3', '.db')


def files(root):
    for path in sorted(root.rglob('*')):
        if path.is_symlink() or path.is_junction():
            raise ValueError(f'数据目录包含链接，无法确认快照范围 {path.relative_to(root)}')
        if path.is_file():
            yield path.relative_to(root)


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def fingerprint(root):
    return {path.as_posix(): digest(root / path) for path in files(root)}


def database_sidecar(path, source_names):
    name = path.as_posix()
    return any(name.endswith(suffix) and name[:-len(suffix)] in source_names
               and Path(name[:-len(suffix)]).suffix.lower() in SQLITE
               for suffix in ('-wal', '-shm', '-journal'))


def check_database(path):
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
        if db.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
            raise ValueError(f'SQLite 快照检查失败 {path.name}')


def verify(snapshot):
    snapshot = Path(snapshot).resolve(strict=True)
    data = snapshot / 'data'
    manifest_path = snapshot / MANIFEST
    if data.is_symlink() or data.is_junction() or manifest_path.is_symlink():
        raise ValueError('快照数据或清单包含链接')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    expected = manifest.get('files')
    if manifest.get('version') != 1 or not isinstance(expected, dict):
        raise ValueError('快照清单格式无效')
    actual = fingerprint(data)
    if actual != expected or not actual:
        raise ValueError('快照文件缺失、被修改或与清单不一致')
    for filename in actual:
        path = data / filename
        if path.suffix.lower() in SQLITE:
            check_database(path)
    return len(actual)


def backup(source, destination):
    source = Path(source).resolve(strict=True)
    destination = Path(destination).resolve()
    if not source.is_dir() or not destination.parent.is_dir() or destination.exists() or destination.is_relative_to(source):
        raise ValueError('请输入现有数据目录及其外部尚不存在的快照目录')
    before = fingerprint(source)
    if not before:
        raise ValueError('数据目录为空')
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix='.junfei-backup-') as temp:
        stage = Path(temp) / 'snapshot'
        staged_data = stage / 'data'
        staged_data.mkdir(parents=True)
        for filename in before:
            relative = Path(filename)
            if database_sidecar(relative, before):
                continue
            original, target = source / relative, staged_data / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if relative.suffix.lower() in SQLITE:
                with (closing(sqlite3.connect(original.as_uri() + '?mode=ro', uri=True)) as current,
                      closing(sqlite3.connect(target)) as captured):
                    current.backup(captured, pages=128, sleep=0.05)
            else:
                shutil.copy2(original, target)
        # Opening a WAL database can change its SHM sidecar without editing the database.
        before_data = {name: value for name, value in before.items() if not name.endswith('-shm')}
        after_data = {name: value for name, value in fingerprint(source).items() if not name.endswith('-shm')}
        if before_data != after_data:
            raise ValueError('备份期间数据发生变化，请结束工作台和原任务后重新运行')
        manifest = {'version': 1, 'files': fingerprint(staged_data)}
        (stage / MANIFEST).write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding='utf-8')
        count = verify(stage)
        stage.rename(destination)
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('create', help='结束工作台和业务任务后创建快照')
    create.add_argument('data_dir', type=Path)
    create.add_argument('new_snapshot_dir', type=Path)
    check = commands.add_parser('verify', help='检查快照清单和 SQLite 数据库')
    check.add_argument('snapshot_dir', type=Path)
    args = parser.parse_args()
    try:
        count = (backup(args.data_dir, args.new_snapshot_dir) if args.command == 'create'
                 else verify(args.snapshot_dir))
    except (ValueError, OSError, sqlite3.Error) as error:
        parser.exit(1, f'{error}\n')
    print(f'已校验 {count} 个文件')


if __name__ == '__main__':
    main()
