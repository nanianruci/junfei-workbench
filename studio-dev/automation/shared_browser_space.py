"""Cooperative EGO space exclusion. No import-time I/O or auto-enrollment.

The live execution lock covers participating adapters. The durable coordinator
below still requires explicit enrollment and operation-specific evidence.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import threading
import uuid
from browser_space_lock import windows_local_app_data


SCHEMA_VERSION = 1
ENGINE = 'ego-lite'
SCOPE = 'participating_processes_same_windows_user_per_operation'
DATABASE = 'spaces.sqlite3'
_ACTIVE = frozenset(('reserved', 'dispatch_unknown'))
_STATES = _ACTIVE | {'completed', 'cancelled_before_dispatch'}
_HEX = re.compile(r'[0-9a-f]{64}\Z')
_UUID = re.compile(r'[0-9a-f]{32}\Z')
_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}\Z')
_SCHEMA = {
    'metadata': 'CREATE TABLE metadata (singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL, coordinator_id TEXT NOT NULL, root TEXT NOT NULL)',
    'spaces': 'CREATE TABLE spaces (space_id INTEGER PRIMARY KEY, last_fence INTEGER NOT NULL, active_fence INTEGER)',
    'operations': 'CREATE TABLE operations (owner TEXT NOT NULL, operation_id TEXT NOT NULL, space_id INTEGER NOT NULL, request_sha256 TEXT NOT NULL, fence INTEGER NOT NULL, dispatch_nonce TEXT NOT NULL, token_sha256 TEXT NOT NULL, identity_sha256 TEXT NOT NULL, status TEXT NOT NULL, outcome TEXT, evidence_sha256 TEXT, verifier TEXT, PRIMARY KEY(owner, operation_id), UNIQUE(space_id, fence))',
}


class CoordinationError(ValueError):
    """Fail closed; error strings contain neither credentials nor local paths."""


@dataclass(frozen=True)
class DispatchIdentity:
    coordinator_id: str
    engine: str
    space_id: int
    owner: str
    operation_id: str
    request_sha256: str
    fence: int
    dispatch_nonce: str


@dataclass(frozen=True)
class Operation:
    identity: DispatchIdentity
    status: str
    outcome: str | None = None
    evidence_sha256: str | None = None
    verifier: str | None = None


@dataclass(frozen=True)
class TerminalEvidence:
    """An assertion by a reviewed adapter, NOT a generic runtime exit receipt.

    The adapter must validate exact operation/nonce evidence, wait for all work
    to end, and durably persist its own result before returning this object.
    """
    identity: DispatchIdentity
    evidence_sha256: str
    verifier: str
    outcome: str = 'completed'


@dataclass(frozen=True, eq=False)
class _Lease:
    identity: DispatchIdentity
    _secret: str = field(repr=False)
    _pid: int = field(repr=False)


@dataclass(frozen=True)
class Reservation:
    record: Operation
    lease: _Lease | None = field(repr=False)


@dataclass(frozen=True)
class DispatchResult:
    value: object
    record: Operation


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=True).encode('ascii')).hexdigest()


def _identity_hash(identity):
    return _digest(vars(identity))


def _exact_identity(candidate, expected):
    # Dataclass equality aliases True/1 and 1.0/1, and can invoke a foreign
    # object's equality method. Terminal proof must not use those semantics.
    if type(candidate) is not DispatchIdentity or type(expected) is not DispatchIdentity:
        return False
    fields = {'coordinator_id': str, 'engine': str, 'space_id': int,
              'owner': str, 'operation_id': str, 'request_sha256': str,
              'fence': int, 'dispatch_nonce': str}
    values = vars(candidate)
    if set(values) != set(fields) or any(type(values[key]) is not kind
                                         for key, kind in fields.items()):
        return False
    return _identity_hash(candidate) == _identity_hash(expected)


def _name(value, label):
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise CoordinationError('invalid_' + label)
    return value


def _sha(value):
    if not isinstance(value, str) or not _HEX.fullmatch(value):
        raise CoordinationError('invalid_sha256')
    return value


def _space(value):
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise CoordinationError('invalid_space_id')
    return value


def default_root():
    """OS-known LocalAppData, independent of account DATA/install/environment.

    No fallback to cwd, TEMP, environment variables or a per-account directory.
    Other platforms must supply explicit synthetic roots in tests.
    """
    try:
        root = windows_local_app_data()
    except ValueError as error:
        raise CoordinationError(str(error)) from error
    return root / 'JunfeiListingWorkbench' / 'browser-space-coordinator-v1'


def _drive_type(root):
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    kernel.GetDriveTypeW.restype = wintypes.UINT
    return kernel.GetDriveTypeW(str(root.anchor))


def _path(value):
    result = Path(value)
    if os.name == 'nt' and str(result).startswith('\\\\'):
        raise CoordinationError('local_fixed_coordinator_drive_required')
    if not result.is_absolute():
        raise CoordinationError('absolute_coordinator_root_required')
    # Reject aliases/reparse points rather than silently creating a second scope.
    result = Path(os.path.abspath(result))
    if os.name == 'nt' and _drive_type(result) != 3:
        raise CoordinationError('local_fixed_coordinator_drive_required')
    for item in (result, *result.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise CoordinationError('coordinator_reparse_path_forbidden')
    return result


def _file_identity(path):
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise CoordinationError('coordinator_file_invalid')
    return info.st_dev, info.st_ino


def provision(root):
    """Explicit fresh setup ONLY. Never called by an adapter/acquire/import.

    Caller must record the returned ID in an independently trusted enrollment.
    Existing/partial roots are NEVER repaired, deleted, replaced or reopened.
    Parent directory must already exist. An interrupted setup remains blocked.
    """
    root = _path(root)
    try:
        root.mkdir(exist_ok=False)
    except OSError as error:
        raise CoordinationError('coordinator_fresh_root_required') from error
    identity = uuid.uuid4().hex
    path = root / DATABASE
    conn = None
    try:
        # Exclusive create precedes SQLite; there is deliberately no retry.
        with path.open('xb') as stream:
            stream.flush()
            os.fsync(stream.fileno())
        conn = sqlite3.connect(path.as_uri() + '?mode=rw', uri=True)
        conn.execute('PRAGMA journal_mode=DELETE')
        conn.execute('PRAGMA synchronous=FULL')
        conn.execute('BEGIN IMMEDIATE')
        for sql in _SCHEMA.values():
            conn.execute(sql)
        conn.execute('PRAGMA user_version=1')
        conn.execute('INSERT INTO metadata VALUES (1,?,?,?)',
                     (SCHEMA_VERSION, identity, str(root)))
        conn.commit()
        # Explicit file flush before returning the enrollment ID. SQLite FULL
        # and the filesystem must honor durable writes; faulty storage is not
        # a software guarantee. POSIX needs the new directory entry flushed too.
        with path.open('r+b') as stream:
            os.fsync(stream.fileno())
        if os.name != 'nt':
            for directory in (root, root.parent):
                fd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
    except (OSError, sqlite3.Error) as error:
        raise CoordinationError('coordinator_provision_incomplete') from error
    finally:
        if conn is not None:
            conn.close()
    return identity


class Coordinator:
    """Cooperative one-operation ownership; constructor and inspect are read-only.

    ``root`` is trusted test/deployment injection, never a user request option.
    Production enrollment must use default_root() for every participating app.
    There is intentionally no release/recovery/force-unlock API after dispatch.
    """
    def __init__(self, expected_identity, *, root=None):
        if not isinstance(expected_identity, str) or not _UUID.fullmatch(expected_identity):
            raise CoordinationError('trusted_enrollment_identity_required')
        self._identity = expected_identity
        self._root = _path(default_root() if root is None else root)
        self._db = self._root / DATABASE
        self._pid = os.getpid()
        self._caps = {}
        self._lock = threading.Lock()
        self._poisoned = False
        try:
            self._file_id = _file_identity(self._db)
        except OSError as error:
            raise CoordinationError('coordinator_unavailable') from error
        with self._transaction(False):
            pass

    def _check_path(self):
        if self._poisoned or os.getpid() != self._pid:
            raise CoordinationError('coordinator_instance_invalid')
        if _path(self._root) != self._root or _file_identity(self._db) != self._file_id:
            raise CoordinationError('coordinator_file_replaced')

    @contextmanager
    def _transaction(self, write):
        conn = None
        try:
            self._check_path()
            conn = sqlite3.connect(self._db.as_uri() + ('?mode=rw' if write else '?mode=ro'),
                                   uri=True, timeout=2, isolation_level=None)
            if write:
                conn.execute('PRAGMA synchronous=FULL')
            if conn.execute('PRAGMA journal_mode').fetchone() != ('delete',):
                raise CoordinationError('coordinator_journal_invalid')
            conn.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            self._validate(conn)
            self._check_path()
            yield conn
            self._check_path()
            if write:
                self._validate(conn)
            conn.commit()
        except (OSError, sqlite3.Error) as error:
            # A failed commit acknowledgement is not evidence of non-execution.
            # Invalidate local capabilities; new instances only inspect/replay.
            self._poisoned = True
            raise CoordinationError('coordinator_storage_unavailable') from error
        finally:
            if conn is not None:
                conn.close()  # Uncommitted transactions roll back, never retry.

    def _validate(self, conn):
        if conn.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
            raise CoordinationError('coordinator_corrupt')
        actual = dict(conn.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"))
        if actual != _SCHEMA or conn.execute("SELECT 1 FROM sqlite_master WHERE type IN ('trigger','view')").fetchone():
            raise CoordinationError('coordinator_schema_invalid')
        if conn.execute('PRAGMA user_version').fetchone() != (SCHEMA_VERSION,):
            raise CoordinationError('coordinator_schema_invalid')
        if conn.execute('SELECT * FROM metadata').fetchall() != [(1, SCHEMA_VERSION, self._identity, str(self._root))]:
            raise CoordinationError('coordinator_identity_mismatch')
        spaces = {}
        for sid, fence, active in conn.execute('SELECT * FROM spaces'):
            _space(sid)
            if type(fence) is not int or fence < 1 or (active is not None and (type(active) is not int or active != fence)):
                raise CoordinationError('coordinator_history_invalid')
            spaces[sid] = (fence, active)
        counts, maxima, active_rows = {}, {}, {}
        for row in conn.execute('SELECT * FROM operations'):
            record = self._record(row)
            sid, fence = record.identity.space_id, record.identity.fence
            counts[sid] = counts.get(sid, 0) + 1
            maxima[sid] = max(fence, maxima.get(sid, 0))
            if record.status in _ACTIVE:
                if sid in active_rows:
                    raise CoordinationError('coordinator_history_invalid')
                active_rows[sid] = fence
        if set(counts) != set(spaces):
            raise CoordinationError('coordinator_history_invalid')
        for sid, (fence, active) in spaces.items():
            # Unique contiguous fences detect missing rows/counter drift.
            if counts[sid] != fence or maxima[sid] != fence or active_rows.get(sid) != active:
                raise CoordinationError('coordinator_history_invalid')

    def _record(self, row):
        owner, op, sid, request, fence, nonce, token, ihash, state, outcome, evidence, verifier = row
        _name(owner, 'owner'); _name(op, 'operation_id'); _space(sid)
        _sha(request); _sha(nonce); _sha(token); _sha(ihash)
        if type(fence) is not int or not 1 <= fence <= 2**63 - 1 or state not in _STATES:
            raise CoordinationError('coordinator_history_invalid')
        identity = DispatchIdentity(self._identity, ENGINE, sid, owner, op, request, fence, nonce)
        if _identity_hash(identity) != ihash:
            raise CoordinationError('coordinator_history_invalid')
        if state == 'completed':
            if outcome not in ('completed', 'refused_before_effects'):
                raise CoordinationError('coordinator_history_invalid')
            _sha(evidence); _name(verifier, 'verifier')
        elif any(value is not None for value in (outcome, evidence, verifier)):
            raise CoordinationError('coordinator_history_invalid')
        return Operation(identity, state, outcome, evidence, verifier)

    def inspect(self, *, owner, operation_id):
        _name(owner, 'owner'); _name(operation_id, 'operation_id')
        with self._transaction(False) as conn:
            row = conn.execute('SELECT * FROM operations WHERE owner=? AND operation_id=?',
                               (owner, operation_id)).fetchone()
            return self._record(row) if row is not None else None

    def reserve(self, *, space_id, owner, operation_id, request_sha256):
        _space(space_id); _name(owner, 'owner'); _name(operation_id, 'operation_id'); _sha(request_sha256)
        with self._transaction(True) as conn:
            old = conn.execute('SELECT * FROM operations WHERE owner=? AND operation_id=?',
                               (owner, operation_id)).fetchone()
            if old is not None:
                record = self._record(old)
                if (record.identity.space_id, record.identity.request_sha256) != (space_id, request_sha256):
                    raise CoordinationError('conflicting_operation_reuse')
                # Replays never reissue or reconstruct a runnable capability.
                return Reservation(record, None)
            space = conn.execute('SELECT last_fence,active_fence FROM spaces WHERE space_id=?', (space_id,)).fetchone()
            if space is not None and space[1] is not None:
                raise CoordinationError('space_requires_review')
            fence = 1 if space is None else space[0] + 1
            if fence > 2**63 - 1:
                raise CoordinationError('coordinator_fence_exhausted')
            identity = DispatchIdentity(self._identity, ENGINE, space_id, owner, operation_id,
                                        request_sha256, fence, secrets.token_hex(32))
            secret = secrets.token_hex(32)
            conn.execute('INSERT INTO operations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                         (owner, operation_id, space_id, request_sha256, fence,
                          identity.dispatch_nonce, _digest(secret), _identity_hash(identity),
                          'reserved', None, None, None))
            if space is None:
                conn.execute('INSERT INTO spaces VALUES (?,?,?)', (space_id, fence, fence))
            else:
                conn.execute('UPDATE spaces SET last_fence=?,active_fence=? WHERE space_id=?', (fence, fence, space_id))
        lease = _Lease(identity, secret, self._pid)
        with self._lock:
            self._caps[id(lease)] = lease
        return Reservation(Operation(identity, 'reserved'), lease)

    def _consume(self, lease):
        with self._lock:
            if (type(lease) is not _Lease or lease._pid != os.getpid()
                    or self._caps.get(id(lease)) is not lease):
                raise CoordinationError('trusted_one_use_lease_required')
            del self._caps[id(lease)]
        return lease.identity

    def _fenced(self, conn, lease, state):
        identity = lease.identity
        row = conn.execute('SELECT * FROM operations WHERE owner=? AND operation_id=?',
                           (identity.owner, identity.operation_id)).fetchone()
        if row is None or self._record(row) != Operation(identity, state) or not secrets.compare_digest(row[6], _digest(lease._secret)):
            raise CoordinationError('coordinator_fence_mismatch')
        if conn.execute('SELECT active_fence FROM spaces WHERE space_id=?', (identity.space_id,)).fetchone() != (identity.fence,):
            raise CoordinationError('coordinator_fence_mismatch')

    def cancel_before_dispatch(self, lease):
        """Only the original live holder may cancel a never-dispatched reserve."""
        identity = self._consume(lease)
        with self._transaction(True) as conn:
            self._fenced(conn, lease, 'reserved')
            conn.execute("UPDATE operations SET status='cancelled_before_dispatch' WHERE owner=? AND operation_id=?",
                         (identity.owner, identity.operation_id))
            conn.execute('UPDATE spaces SET active_fence=NULL WHERE space_id=?', (identity.space_id,))
        return Operation(identity, 'cancelled_before_dispatch')

    def dispatch_once(self, lease, action, verify):
        """Own the complete action + validation + adapter-persistence span.

        action(identity) performs the operation. verify(value, identity) is
        trusted code returning exact TerminalEvidence AFTER durable adapter
        persistence. Neither callback may launch detached work. Any exception,
        invalid proof, timeout or crash retains dispatch_unknown indefinitely.
        No automatic retry, cancellation or finally-release occurs.
        """
        if not callable(action) or not callable(verify):
            raise CoordinationError('trusted_callbacks_required')
        identity = self._consume(lease)
        with self._transaction(True) as conn:
            self._fenced(conn, lease, 'reserved')
            conn.execute("UPDATE operations SET status='dispatch_unknown' WHERE owner=? AND operation_id=?",
                         (identity.owner, identity.operation_id))
        # Crash before this call is deliberately indistinguishable from dispatch.
        value = action(identity)
        evidence = verify(value, identity)
        if (type(evidence) is not TerminalEvidence or not _exact_identity(evidence.identity, identity)
                or evidence.outcome not in ('completed', 'refused_before_effects')):
            raise CoordinationError('terminal_evidence_required')
        _sha(evidence.evidence_sha256); _name(evidence.verifier, 'verifier')
        with self._transaction(True) as conn:
            self._fenced(conn, lease, 'dispatch_unknown')
            conn.execute("UPDATE operations SET status='completed',outcome=?,evidence_sha256=?,verifier=? WHERE owner=? AND operation_id=?",
                         (evidence.outcome, evidence.evidence_sha256, evidence.verifier,
                          identity.owner, identity.operation_id))
            conn.execute('UPDATE spaces SET active_fence=NULL WHERE space_id=?', (identity.space_id,))
        return DispatchResult(value, Operation(identity, 'completed', evidence.outcome,
                                               evidence.evidence_sha256, evidence.verifier))
