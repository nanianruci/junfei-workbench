"""Synthetic coordinator tests: new TEMP roots, Python children, no browser/network."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import FrozenInstanceError, replace
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import shared_browser_space as shared
from browser_space_lock import active_space


DIGEST = hashlib.sha256(b'synthetic request').hexdigest()
EVIDENCE = hashlib.sha256(b'synthetic validated terminal receipt').hexdigest()


class ActiveSpaceTests(unittest.TestCase):
    def test_default_adapter_lease_rejects_a_second_process_on_same_space(self):
        script = """import sys
from browser_space_lock import active_space
try:
    with active_space(int(sys.argv[2]), root=sys.argv[1]):
        print('entered')
except ValueError:
    print('busy')
"""
        with tempfile.TemporaryDirectory() as root:
            def observe(space):
                result = subprocess.run([sys.executable, '-c', script, root, str(space)],
                                        cwd=Path(__file__).parent, capture_output=True, text=True, check=True)
                return result.stdout.strip()
            with active_space(7, root=root):
                self.assertEqual(observe(7), 'busy')
                self.assertEqual(observe(8), 'entered')
            self.assertEqual(observe(7), 'entered')


def proof(value, identity):
    return shared.TerminalEvidence(identity, EVIDENCE, 'synthetic.fixture.v1')


def child(mode, root, enrollment, owner, operation, marker):
    root, marker = Path(root), Path(marker)
    coordinator = shared.Coordinator(enrollment, root=root)
    if mode == 'race':
        marker.with_suffix('.ready').write_text('ready', encoding='ascii')
        deadline = time.monotonic() + 20
        while not marker.parent.joinpath('go').exists():
            if time.monotonic() > deadline:
                return 91
            time.sleep(.01)
    try:
        reservation = coordinator.reserve(space_id=7, owner=owner,
            operation_id=operation, request_sha256=DIGEST)
    except shared.CoordinationError as error:
        print(json.dumps({'error': str(error)}), flush=True)
        return 0
    if mode in ('reserve-crash', 'race'):
        print(json.dumps({'status': reservation.record.status,
                          'hasLease': reservation.lease is not None}), flush=True)
        return 23 if mode == 'reserve-crash' else 0
    def action(identity):
        # Durable external-action stand-in. Child exits without a terminal proof.
        with marker.open('x', encoding='ascii') as stream:
            stream.write(identity.dispatch_nonce)
            stream.flush()
            os.fsync(stream.fileno())
        os._exit(24)
    coordinator.dispatch_once(reservation.lease, action, proof)
    return 92


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='shared-space-synthetic-')
        self.base = Path(self.temporary.name)
        self.root = self.base / 'coordinator'
        self.enrollment = shared.provision(self.root)
        self.coordinator = shared.Coordinator(self.enrollment, root=self.root)
        self.db = self.root / shared.DATABASE

    def tearDown(self):
        self.temporary.cleanup()

    def reserve(self, *, owner='install-a.account-1', operation='op-1', space=7,
                digest=DIGEST, coordinator=None):
        return (coordinator or self.coordinator).reserve(space_id=space, owner=owner,
            operation_id=operation, request_sha256=digest)

    def inspect(self, operation='op-1', owner='install-a.account-1'):
        return self.coordinator.inspect(owner=owner, operation_id=operation)

    def mutate(self, sql, args=()):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute(sql, args)
            conn.commit()

    def assert_blocked(self):
        with self.assertRaises(shared.CoordinationError):
            self.reserve(operation='new-operation')
        with self.assertRaises(shared.CoordinationError):
            self.inspect()

    def worker(self, mode, owner, operation, marker, **environment):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', **environment)
        return subprocess.Popen([sys.executable, '-B', '-X', 'utf8',
            str(Path(__file__).resolve()), '--worker', mode, str(self.root),
            self.enrollment, owner, operation, str(marker)],
            cwd=self.base, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding='utf-8', creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))

    def finish_child(self, process):
        stdout, stderr = process.communicate(timeout=30)
        self.assertEqual(stderr, '')
        return process.returncode, stdout.strip()

    def test_import_has_no_io_and_no_environment_namespace(self):
        script = ('from unittest.mock import patch; import sys; '
                  'sys.path.insert(0, ' + repr(str(Path(__file__).resolve().parent)) + '); '
                  '\nwith patch("pathlib.Path.mkdir", side_effect=AssertionError("mkdir")), '
                  'patch("sqlite3.connect", side_effect=AssertionError("sqlite")):\n '
                  'import shared_browser_space\n')
        result = subprocess.run([sys.executable, '-B', '-X', 'utf8', '-c', script],
            cwd=self.base, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('JUNFEI_ACCOUNT_DATA', Path(shared.__file__).read_text(encoding='utf-8-sig'))

    @unittest.skipUnless(os.name == 'nt', 'Windows known-folder scope')
    def test_default_root_ignores_environment_and_install(self):
        first = shared.default_root()
        with patch.dict(os.environ, {'LOCALAPPDATA': str(self.base / 'fake'),
                'JUNFEI_ACCOUNT_DATA': str(self.base / 'account'),
                'JUNFEI_PROJECT_ROOT': str(self.base / 'install')}):
            self.assertEqual(shared.default_root(), first)
        self.assertTrue(first.is_absolute())
        self.assertNotIn(str(self.base), str(first))

    @unittest.skipUnless(os.name == 'nt', 'Windows local-drive policy')
    def test_unc_rejected_before_filesystem_or_drive_lookup(self):
        with patch.object(Path, 'lstat', side_effect=AssertionError('No UNC I/O')):
            with patch.object(shared, '_drive_type', side_effect=AssertionError('No UNC lookup')):
                for path in (chr(92) * 2 + 'synthetic-invalid-host' + chr(92) + 'share' + chr(92) + 'coordinator',
                             '//synthetic-invalid-host/share/coordinator',
                             chr(92) * 2 + '?' + chr(92) + 'C:' + chr(92) + 'coordinator'):
                    with self.subTest(path=path), self.assertRaisesRegex(
                            shared.CoordinationError, 'local_fixed'):
                        shared.Coordinator(self.enrollment, root=path)

    @unittest.skipUnless(os.name == 'nt', 'Windows local-drive policy')
    def test_network_removable_unknown_drives_rejected_before_traversal(self):
        for drive_type in (0, 1, 2, 4, 5, 6):
            with self.subTest(drive_type=drive_type):
                with patch.object(shared, '_drive_type', return_value=drive_type):
                    with patch.object(Path, 'lstat', side_effect=AssertionError('No drive I/O')):
                        with self.assertRaisesRegex(shared.CoordinationError, 'local_fixed'):
                            shared.Coordinator(self.enrollment, root=self.root)

    def test_fresh_provision_only_and_readonly_inspection(self):
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.iterdir()}
        self.assertIsNone(self.inspect())
        self.assertIsNone(shared.Coordinator(self.enrollment, root=self.root).inspect(
            owner='install-a.account-1', operation_id='op-1'))
        self.assertEqual(before, {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.iterdir()})
        with self.assertRaises(shared.CoordinationError):
            shared.provision(self.root)
        missing = self.base / 'missing'
        with self.assertRaises(shared.CoordinationError):
            shared.Coordinator(self.enrollment, root=missing)
        self.assertFalse(missing.exists())

    def test_identity_is_pinned_not_self_reported(self):
        with self.assertRaisesRegex(shared.CoordinationError, 'identity'):
            shared.Coordinator('f' * 32, root=self.root)
        with self.assertRaises(shared.CoordinationError):
            shared.Coordinator('', root=self.root)
        self.mutate('UPDATE metadata SET coordinator_id=?', ('e' * 32,))
        self.assert_blocked()

    def test_reservation_identity_immutable_and_duplicate_readonly(self):
        first = self.reserve()
        self.assertEqual(first.record.identity.fence, 1)
        with self.assertRaises(FrozenInstanceError):
            first.record.identity.space_id = 9
        replay = self.reserve()
        self.assertIsNone(replay.lease)
        self.assertEqual(replay.record, first.record)
        with self.assertRaisesRegex(shared.CoordinationError, 'conflicting'):
            self.reserve(space=8)
        with self.assertRaisesRegex(shared.CoordinationError, 'conflicting'):
            self.reserve(digest='f' * 64)
        self.assertEqual(self.inspect().status, 'reserved')

    def test_same_space_different_accounts_profiles_cli_paths_conflict(self):
        self.reserve(owner='accounts-db-a.uid-1')
        with patch.dict(os.environ, {'JUNFEI_ACCOUNT_DATA': str(self.base / 'other-data'),
                'JUNFEI_PROJECT_ROOT': str(self.base / 'other-install'),
                'EGO_CLI': str(self.base / 'different-cli.exe')}):
            other = shared.Coordinator(self.enrollment, root=self.root)
            with self.assertRaisesRegex(shared.CoordinationError, 'requires_review'):
                self.reserve(owner='accounts-db-b.uid-1', coordinator=other)
            self.assertIsNotNone(self.reserve(owner='accounts-db-b.uid-1', space=8,
                                             coordinator=other).lease)

    def test_dispatch_unknown_committed_before_action_and_verify(self):
        reservation = self.reserve()
        called = []
        def action(identity):
            other = shared.Coordinator(self.enrollment, root=self.root)
            record = other.inspect(owner=identity.owner, operation_id=identity.operation_id)
            self.assertEqual(record.status, 'dispatch_unknown')
            self.assertEqual(record.identity, identity)
            called.append('action')
            with self.assertRaises(shared.CoordinationError):
                self.reserve(owner='other')
            return {'synthetic': True}
        def verify(value, identity):
            self.assertEqual(self.inspect().status, 'dispatch_unknown')
            with self.assertRaises(shared.CoordinationError):
                self.reserve(owner='other')
            called.append('verify')
            return proof(value, identity)
        result = self.coordinator.dispatch_once(reservation.lease, action, verify)
        self.assertEqual(called, ['action', 'verify'])
        self.assertEqual(result.record.status, 'completed')
        next_reservation = self.reserve(operation='op-2')
        self.assertEqual(next_reservation.record.identity.fence, 2)
        replay = self.reserve()
        self.assertIsNone(replay.lease)
        self.assertEqual(replay.record.status, 'completed')
        with self.assertRaises(shared.CoordinationError):
            self.coordinator.dispatch_once(reservation.lease, action, verify)
        self.assertEqual(called, ['action', 'verify'])

    def test_exception_and_runtime_exit_zero_do_not_release(self):
        for scenario in ('exception', 'exit-zero', 'blocked', 'bad-proof'):
            with self.subTest(scenario=scenario):
                space = {'exception': 7, 'exit-zero': 8, 'blocked': 9, 'bad-proof': 10}[scenario]
                reservation = self.reserve(operation=scenario, space=space)
                def action(identity):
                    if scenario == 'exception':
                        raise TimeoutError('synthetic dispatch interruption')
                    return {'returncode': 0, 'status': 'blocked'}
                def verify(value, identity):
                    if scenario == 'bad-proof':
                        return proof(value, replace(identity, dispatch_nonce='f' * 64))
                    return value if scenario == 'blocked' else True
                with self.assertRaises((TimeoutError, shared.CoordinationError)):
                    self.coordinator.dispatch_once(reservation.lease, action, verify)
                self.assertEqual(self.inspect(scenario).status, 'dispatch_unknown')
                with self.assertRaises(shared.CoordinationError):
                    self.reserve(operation='later-' + scenario, space=space)
                self.assertIsNone(self.reserve(operation=scenario, space=space).lease)

    def assert_malformed_identity_stays_unknown(self, transform, *, space=1):
        # Fence=1 and space=1 are deliberate: bool aliases would compare equal
        # with the original dataclass == check. Each test uses a fresh TEMP DB.
        reservation = self.reserve(space=space)
        def verify(value, identity):
            return shared.TerminalEvidence(transform(identity), EVIDENCE, 'synthetic.fixture.v1')
        with self.assertRaisesRegex(shared.CoordinationError, 'terminal_evidence'):
            self.coordinator.dispatch_once(reservation.lease, lambda i: 0, verify)
        self.assertEqual(self.inspect().status, 'dispatch_unknown')
        self.assertIsNone(self.reserve(space=space).lease)
        with self.assertRaisesRegex(shared.CoordinationError, 'requires_review'):
            self.reserve(operation='next', space=space)

    def test_terminal_identity_bool_space_alias_rejected(self):
        self.assert_malformed_identity_stays_unknown(lambda i: replace(i, space_id=True))

    def test_terminal_identity_bool_fence_alias_rejected(self):
        self.assert_malformed_identity_stays_unknown(lambda i: replace(i, fence=True))

    def test_terminal_identity_float_space_alias_rejected(self):
        self.assert_malformed_identity_stays_unknown(lambda i: replace(i, space_id=1.0))

    def test_terminal_identity_float_fence_alias_rejected(self):
        self.assert_malformed_identity_stays_unknown(lambda i: replace(i, fence=1.0))

    def test_terminal_identity_foreign_equality_spoof_rejected(self):
        class AlwaysEqual:
            def __eq__(self, other):
                raise AssertionError('Foreign equality must not be called')
            def __ne__(self, other):
                raise AssertionError('Foreign inequality must not be called')
        self.assert_malformed_identity_stays_unknown(lambda i: AlwaysEqual())

    def test_terminal_identity_subclass_rejected(self):
        class IdentitySubclass(shared.DispatchIdentity):
            pass
        self.assert_malformed_identity_stays_unknown(lambda i: IdentitySubclass(**vars(i)))

    def test_terminal_identity_subclass_equality_spoof_rejected(self):
        class IdentitySubclass(shared.DispatchIdentity):
            def __eq__(self, other):
                raise AssertionError('Subclass equality must not be called')
            def __ne__(self, other):
                raise AssertionError('Subclass inequality must not be called')
        self.assert_malformed_identity_stays_unknown(lambda i: IdentitySubclass(**vars(i)))

    def test_terminal_identity_string_subclass_rejected(self):
        class StringSubclass(str):
            def __eq__(self, other):
                raise AssertionError('Field equality must not be called')
        self.assert_malformed_identity_stays_unknown(lambda i:
            replace(i, owner=StringSubclass(i.owner)))

    def test_terminal_identity_extra_field_rejected(self):
        def with_extra(identity):
            candidate = replace(identity)
            object.__setattr__(candidate, 'unexpected', True)
            return candidate
        self.assert_malformed_identity_stays_unknown(with_extra)

    def test_terminal_identity_missing_field_rejected(self):
        def without_field(identity):
            candidate = replace(identity)
            object.__delattr__(candidate, 'fence')
            return candidate
        self.assert_malformed_identity_stays_unknown(without_field)

    def test_verifier_exception_or_failure_persisting_adapter_result_stays_unknown(self):
        reservation = self.reserve()
        def verify(value, identity):
            raise OSError('synthetic adapter receipt fsync failed')
        with self.assertRaises(OSError):
            self.coordinator.dispatch_once(reservation.lease, lambda i: 0, verify)
        self.assertEqual(self.inspect().status, 'dispatch_unknown')

    def test_only_original_capability_can_cancel_before_dispatch(self):
        reservation = self.reserve()
        for bad in (None, {'identity': reservation.record.identity}, replace(reservation.lease)):
            with self.assertRaises(shared.CoordinationError):
                self.coordinator.cancel_before_dispatch(bad)
        other = shared.Coordinator(self.enrollment, root=self.root)
        with self.assertRaises(shared.CoordinationError):
            other.cancel_before_dispatch(reservation.lease)
        record = self.coordinator.cancel_before_dispatch(reservation.lease)
        self.assertEqual(record.status, 'cancelled_before_dispatch')
        self.assertIsNone(self.reserve().lease)
        newer = self.reserve(operation='new')
        self.assertEqual(newer.record.identity.fence, 2)
        with self.assertRaises(shared.CoordinationError):
            self.coordinator.cancel_before_dispatch(reservation.lease)

    def test_concurrent_same_capability_dispatch_once(self):
        reservation = self.reserve()
        calls = []
        def run():
            try:
                self.coordinator.dispatch_once(reservation.lease, lambda i: calls.append(i), proof)
                return True
            except shared.CoordinationError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(pool.map(lambda _: run(), range(2))), 1)
        self.assertEqual(len(calls), 1)

    def test_pid_change_invalidates_capability_not_reclaims_space(self):
        reservation = self.reserve()
        with patch.object(shared.os, 'getpid', return_value=os.getpid() + 1):
            with self.assertRaises(shared.CoordinationError):
                self.coordinator.dispatch_once(reservation.lease, lambda i: 0, proof)
            with self.assertRaises(shared.CoordinationError):
                self.inspect()
        self.assertEqual(self.inspect().status, 'reserved')

    def test_missing_database_never_recreated(self):
        self.reserve()
        self.db.rename(self.base / 'preserved.sqlite3')
        self.assert_blocked()
        self.assertFalse(self.db.exists())
        with self.assertRaises(shared.CoordinationError):
            shared.Coordinator(self.enrollment, root=self.root)
        self.assertFalse(self.db.exists())

    def test_empty_replacement_and_same_uuid_missing_metadata_fail(self):
        self.db.rename(self.base / 'preserved.sqlite3')
        self.db.write_bytes(b'')
        self.assert_blocked()
        with self.assertRaises(shared.CoordinationError):
            shared.Coordinator(self.enrollment, root=self.root)
        self.assertEqual(self.db.read_bytes(), b'')

    def test_missing_metadata_or_corrupt_partial_schema_fails(self):
        self.mutate('DELETE FROM metadata')
        self.assert_blocked()
        with self.assertRaises(shared.CoordinationError):
            shared.Coordinator(self.enrollment, root=self.root)

    def test_partial_schema_fails_without_repair(self):
        self.mutate('DROP TABLE operations')
        before = self.db.read_bytes()
        self.assert_blocked()
        self.assertEqual(self.db.read_bytes(), before)

    def test_corrupt_database_fails_without_repair(self):
        self.db.write_bytes(b'not a sqlite database')
        before = self.db.read_bytes()
        self.assert_blocked()
        self.assertEqual(self.db.read_bytes(), before)

    def test_missing_operation_or_space_rows_detected_by_fences(self):
        self.reserve()
        self.mutate('DELETE FROM operations')
        self.assert_blocked()

    def test_missing_space_rows_detected(self):
        self.reserve()
        self.mutate('DELETE FROM spaces')
        self.assert_blocked()

    def test_changed_request_hash_or_status_detected(self):
        self.reserve()
        self.mutate('UPDATE operations SET request_sha256=?', ('a' * 64,))
        self.assert_blocked()

    def test_unknown_status_blocks(self):
        self.reserve()
        self.mutate("UPDATE operations SET status='free'")
        self.assert_blocked()

    def test_nonrollback_journal_blocks(self):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute('PRAGMA journal_mode=WAL')
        self.assert_blocked()

    def test_coordinator_mutation_after_dispatch_blocks_release(self):
        reservation = self.reserve()
        def action(identity):
            self.mutate('UPDATE metadata SET coordinator_id=?', ('b' * 32,))
            return 0
        with self.assertRaises(shared.CoordinationError):
            self.coordinator.dispatch_once(reservation.lease, action, proof)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT status FROM operations').fetchone(), ('dispatch_unknown',))

    def test_multiprocess_race_different_accounts_install_paths_and_data(self):
        children = []
        for n in range(2):
            marker = self.base / ('worker-' + str(n))
            process = self.worker('race', 'accounts-db-' + str(n) + '.uid-1', 'same-op', marker,
                JUNFEI_ACCOUNT_DATA=str(self.base / ('data-' + str(n))),
                JUNFEI_PROJECT_ROOT=str(self.base / ('install-' + str(n))),
                EGO_CLI=str(self.base / ('cli-' + str(n) + '.exe')))
            children.append((process, marker))
        try:
            deadline = time.monotonic() + 20
            while not all(marker.with_suffix('.ready').exists() for _, marker in children):
                self.assertLess(time.monotonic(), deadline, 'child readiness timeout')
                time.sleep(.02)
            self.base.joinpath('go').write_text('go', encoding='ascii')
            outputs = []
            for process, _ in children:
                code, stdout = self.finish_child(process)
                self.assertEqual(code, 0)
                outputs.append(json.loads(stdout))
            self.assertEqual(sum(v.get('status') == 'reserved' for v in outputs), 1)
            self.assertEqual(sum(v.get('error') == 'space_requires_review' for v in outputs), 1)
        finally:
            for process, _ in children:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=10)

    def test_crashed_reservation_is_not_released_or_replayed(self):
        process = self.worker('reserve-crash', 'child-account', 'crash-op', self.base / 'unused')
        code, stdout = self.finish_child(process)
        self.assertEqual(code, 23)
        self.assertEqual(json.loads(stdout)['status'], 'reserved')
        other = shared.Coordinator(self.enrollment, root=self.root)
        replay = self.reserve(owner='child-account', operation='crash-op', coordinator=other)
        self.assertIsNone(replay.lease)
        with self.assertRaises(shared.CoordinationError):
            self.reserve(coordinator=other)

    def test_process_death_after_dispatch_keeps_unknown_and_never_repeats(self):
        marker = self.base / 'external-call-marker'
        process = self.worker('dispatch-crash', 'child-account', 'crash-op', marker)
        code, stdout = self.finish_child(process)
        self.assertEqual(code, 24)
        self.assertEqual(stdout, '')
        before = marker.read_bytes()
        other = shared.Coordinator(self.enrollment, root=self.root)
        replay = self.reserve(owner='child-account', operation='crash-op', coordinator=other)
        self.assertEqual(replay.record.status, 'dispatch_unknown')
        self.assertIsNone(replay.lease)
        with self.assertRaises(shared.CoordinationError):
            other.dispatch_once(replay.lease, lambda i: marker.write_text('repeated'), proof)
        with self.assertRaises(shared.CoordinationError):
            self.reserve(coordinator=other)
        self.assertEqual(before, marker.read_bytes())

    def test_dispatch_commit_failure_never_invokes_action_or_reissues_lease(self):
        reservation = self.reserve()
        real_connect = sqlite3.connect
        class FailedCommit(sqlite3.Connection):
            def commit(self):
                raise sqlite3.OperationalError('synthetic pre-commit failure')
        def connect(*args, **kwargs):
            return real_connect(*args, **kwargs, factory=FailedCommit)
        calls = []
        with patch.object(shared.sqlite3, 'connect', side_effect=connect):
            with self.assertRaises(shared.CoordinationError):
                self.coordinator.dispatch_once(reservation.lease, lambda i: calls.append(i), proof)
        self.assertEqual(calls, [])
        other = shared.Coordinator(self.enrollment, root=self.root)
        replay = self.reserve(coordinator=other)
        self.assertEqual(replay.record.status, 'reserved')
        self.assertIsNone(replay.lease)
        with self.assertRaises(shared.CoordinationError):
            self.coordinator.dispatch_once(reservation.lease, lambda i: calls.append(i), proof)

    def test_dispatch_commit_ack_lost_leaves_unknown_without_calling_action(self):
        reservation = self.reserve()
        real_connect = sqlite3.connect
        class LostAcknowledgement(sqlite3.Connection):
            def commit(self):
                super().commit()
                raise sqlite3.OperationalError('synthetic lost commit acknowledgement')
        calls = []
        with patch.object(shared.sqlite3, 'connect', side_effect=lambda *a, **kw:
                          real_connect(*a, **kw, factory=LostAcknowledgement)):
            with self.assertRaises(shared.CoordinationError):
                self.coordinator.dispatch_once(reservation.lease, lambda i: calls.append(i), proof)
        self.assertEqual(calls, [])
        other = shared.Coordinator(self.enrollment, root=self.root)
        replay = self.reserve(coordinator=other)
        self.assertEqual(replay.record.status, 'dispatch_unknown')
        self.assertIsNone(replay.lease)
        with self.assertRaises(shared.CoordinationError):
            self.reserve(operation='new', coordinator=other)

    def test_terminal_commit_ack_lost_never_replays_old_operation(self):
        reservation = self.reserve()
        real_connect = sqlite3.connect
        commits = []
        class LostTerminalAcknowledgement(sqlite3.Connection):
            def commit(self):
                super().commit()
                commits.append(True)
                if len(commits) == 2:
                    raise sqlite3.OperationalError('synthetic lost terminal acknowledgement')
        calls = []
        with patch.object(shared.sqlite3, 'connect', side_effect=lambda *a, **kw:
                          real_connect(*a, **kw, factory=LostTerminalAcknowledgement)):
            with self.assertRaises(shared.CoordinationError):
                self.coordinator.dispatch_once(reservation.lease, lambda i: calls.append(i), proof)
        self.assertEqual(len(calls), 1)
        other = shared.Coordinator(self.enrollment, root=self.root)
        replay = self.reserve(coordinator=other)
        self.assertEqual(replay.record.status, 'completed')
        self.assertIsNone(replay.lease)
        with self.assertRaises(shared.CoordinationError):
            self.coordinator.dispatch_once(reservation.lease, lambda i: calls.append(i), proof)
        self.assertEqual(len(calls), 1)

    def test_terminal_commit_failure_retains_unknown(self):
        reservation = self.reserve()
        real_connect = sqlite3.connect
        commits = []
        class FailedTerminalCommit(sqlite3.Connection):
            def commit(self):
                commits.append(True)
                if len(commits) == 2:
                    raise sqlite3.OperationalError('synthetic terminal commit failure')
                super().commit()
        with patch.object(shared.sqlite3, 'connect', side_effect=lambda *a, **kw:
                          real_connect(*a, **kw, factory=FailedTerminalCommit)):
            with self.assertRaises(shared.CoordinationError):
                self.coordinator.dispatch_once(reservation.lease, lambda i: 0, proof)
        other = shared.Coordinator(self.enrollment, root=self.root)
        replay = self.reserve(coordinator=other)
        self.assertEqual(replay.record.status, 'dispatch_unknown')
        self.assertIsNone(replay.lease)
        with self.assertRaises(shared.CoordinationError):
            self.reserve(operation='new', coordinator=other)

    def test_elapsed_time_cannot_free_unknown_operation(self):
        reservation = self.reserve()
        with self.assertRaises(TimeoutError):
            self.coordinator.dispatch_once(reservation.lease,
                lambda i: (_ for _ in ()).throw(TimeoutError('synthetic')), proof)
        with patch('time.time', return_value=10**14):
            other = shared.Coordinator(self.enrollment, root=self.root)
            with self.assertRaises(shared.CoordinationError):
                self.reserve(operation='much-later', coordinator=other)
            self.assertIsNone(self.reserve(coordinator=other).lease)

    def test_strict_fields_do_not_accept_bool_or_arbitrary_namespace(self):
        for change in ({'space': True}, {'space': -1}, {'owner': ''},
                       {'operation': '../escape'}, {'digest': 'not-a-digest'}):
            with self.subTest(change=change), self.assertRaises(shared.CoordinationError):
                self.reserve(**change)
        self.assertIsNone(self.inspect())


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--worker':
        raise SystemExit(child(*sys.argv[2:]))
    unittest.main()
