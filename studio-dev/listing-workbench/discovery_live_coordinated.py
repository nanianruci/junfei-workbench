"""OFFLINE ONLY: whole-run coordination of the actual isolated discovery controller.

No HTTP route, production enrollment, real browser transport, arbitrary callback,
external script, live input or user-supplied workspace is supported. See the
companion document for the deliberately cooperative and synthetic boundary.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

_SOURCE = Path(__file__).resolve()
_AUTOMATION = _SOURCE.parent.parent / 'automation'
if str(_AUTOMATION) not in sys.path:
    sys.path.insert(0, str(_AUTOMATION))
import shared_browser_space as shared

MODE = 'offline_coordinated_discovery_fixture_v1'
SCENARIOS = frozenset(('complete', 'blocked', 'unknown', 'worker_exception',
    'worker_baseexception', 'stop_during_dispatch', 'exit_without_proof',
    'crash_after_first', 'pause_between_steps', 'pause_after_terminal'))
_NAMES = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}\Z')
_ID_TYPES = {'coordinator_id': str, 'engine': str, 'space_id': int, 'owner': str,
             'operation_id': str, 'request_sha256': str, 'fence': int,
             'dispatch_nonce': str}
_EVIDENCE_SQL = 'CREATE TABLE evidence (stage TEXT PRIMARY KEY, payload TEXT NOT NULL)'
_STAGES = ('search_capture', 'seller_capture', 'store_capture',
           'title_phrase_capture', 'search_capture')
_SOURCE_CONFIG = {'browser': {'task_space_id': 1}, 'discovery_browser': {'account_bindings': {}}}


class FixtureError(ValueError):
    """Unknown results retain the coordinator barrier; never retry automatically."""


@dataclass(frozen=True)
class FixtureResult:
    record: shared.Operation
    workspace: Path | None
    summary: dict | None
    replayed: bool


@dataclass(frozen=True)
class _WorkerResult:
    workspace: Path
    returncode: int
    pid: int
    job: dict


def _json(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode('ascii')).hexdigest()


def _same(actual, expected):
    # Canonical JSON distinguishes boolean/integer and integer/float aliases.
    return _json(actual) == _json(expected)


def _require(ok, reason):
    if not ok:
        raise FixtureError(reason)


def _object(value, fields, reason):
    _require(type(value) is dict and set(value) == set(fields), reason)
    _require(all(type(value[k]) is t for k, t in fields.items()), reason)


def _temp_path(value):
    raw = Path(value)
    _require(raw.is_absolute(), 'absolute_temp_path_required')
    root = shared._path(raw)  # Same fixed-drive/reparse policy as coordinator.
    temp = Path(tempfile.gettempdir()).resolve()
    _require(root != temp and temp in root.parents, 'fixture_temp_root_required')
    return root


def _write_new(path, value):
    with path.open('x', encoding='ascii', newline='\n') as stream:
        stream.write(_json(value))
        stream.flush()
        os.fsync(stream.fileno())


def _loads(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result, 'duplicate_json_key')
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(FixtureError('invalid_json_number')))


def _read_json(path):
    _temp_path(path)
    _require(path.is_file() and path.stat().st_size <= 4_000_000,
             'fixture_file_missing_or_large')
    return _loads(path.read_text(encoding='ascii'))


def _connect(path, *, write=False):
    _temp_path(path)
    conn = sqlite3.connect(path.as_uri() + ('?mode=rw' if write else '?mode=ro'),
                           uri=True, timeout=3)
    try:
        if write:
            conn.execute('PRAGMA synchronous=FULL')
        _require(conn.execute('PRAGMA journal_mode').fetchone() == ('delete',),
                 'fixture_journal_invalid')
        _require(conn.execute('PRAGMA quick_check').fetchall() == [('ok',)],
                 'fixture_database_invalid')
        _require(not conn.execute("SELECT 1 FROM sqlite_master WHERE type IN ('trigger','view')").fetchone(),
                 'fixture_database_schema_invalid')
        return conn
    except BaseException:
        conn.close()
        raise


def _persist_evidence(workspace, stage, value):
    path = workspace / 'evidence.sqlite3'
    conn = _connect(path, write=True)
    try:
        with conn:
            _require(dict(conn.execute("SELECT name,sql FROM sqlite_master WHERE type='table'"))
                     == {'evidence': _EVIDENCE_SQL}, 'fixture_evidence_schema_invalid')
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('INSERT INTO evidence VALUES (?,?)', (stage, _json(value)))
    finally:
        conn.close()


def _read_evidence(workspace):
    conn = _connect(workspace / 'evidence.sqlite3')
    try:
        _require(dict(conn.execute("SELECT name,sql FROM sqlite_master WHERE type='table'"))
                 == {'evidence': _EVIDENCE_SQL}, 'fixture_evidence_schema_invalid')
        return {stage: _loads(raw) for stage, raw in
                conn.execute('SELECT stage,payload FROM evidence ORDER BY stage')}
    finally:
        conn.close()


def _spec(namespace, account_id, space_id, run_id, scenario):
    _require(type(namespace) is str and _NAMES.fullmatch(namespace), 'invalid_fixture_namespace')
    _require(type(run_id) is str and _NAMES.fullmatch(run_id), 'invalid_fixture_run_id')
    _require(type(account_id) is int and 1 <= account_id <= 2**31-1, 'invalid_fixture_account')
    # High synthetic IDs cannot target the blocked original pilot (space 6).
    _require(type(space_id) is int and 10000 <= space_id <= 2**31-1, 'synthetic_space_required')
    _require(type(scenario) is str and scenario in SCENARIOS, 'fixed_fixture_scenario_required')
    return dict(mode=MODE, namespace=namespace, account_id=account_id,
                space_id=space_id, run_id=run_id, scenario=scenario)


def run_fixture(*, coordinator_root, expected_identity, namespace, account_id,
                space_id, run_id, scenario='complete'):
    """Trusted source/test entry. Enrollment is supplied, never auto-created.

    Only fresh synthetic data is generated. Callers cannot supply URLs, scripts,
    callbacks, transports, enrollment via environment, or existing run DATA.
    Replays return ledger history and do not recreate a child or workspace.
    """
    _require(not getattr(sys, 'frozen', False) and
             os.environ.get('JUNFEI_WORKER_USER', '0') in ('', '0'),
             'source_non_account_worker_required')
    spec = _spec(namespace, account_id, space_id, run_id, scenario)
    coordinator = shared.Coordinator(expected_identity, root=_temp_path(coordinator_root))
    reservation = coordinator.reserve(space_id=space_id,
        owner='fixture:' + namespace + ':' + str(account_id),
        operation_id=run_id, request_sha256=_digest(spec))
    if reservation.lease is None:
        return FixtureResult(reservation.record, None, None, True)

    def action(identity):
        workspace = _temp_path(Path(tempfile.mkdtemp(prefix='discovery-coordinated-fixture-')))
        # Retain all artifacts even on crash or unknown. No finally-unlock/delete.
        job = dict(version=1, identity=vars(identity), spec=spec, workspace=str(workspace))
        _write_new(workspace / 'input.json', job)
        db = sqlite3.connect(workspace / 'evidence.sqlite3')
        try:
            db.execute('PRAGMA journal_mode=DELETE')
            db.execute('PRAGMA synchronous=FULL')
            db.execute(_EVIDENCE_SQL)
            db.execute('INSERT INTO evidence VALUES (?,?)', ('intent', _json(job)))
            db.commit()
        finally:
            db.close()
        return _invoke_worker(workspace, job)

    result = coordinator.dispatch_once(reservation.lease, action, _verify_terminal)
    value = result.value
    verified = _read_evidence(value.workspace)['parent_verified']
    return FixtureResult(result.record, value.workspace, verified['summary'], False)


def _invoke_worker(workspace, job):
    # Fixed reviewed Python program; source/frozen restriction applies above.
    env = {k: v for k, v in os.environ.items()
           if not k.startswith('JUNFEI_') and k not in ('PYTHONPATH', 'PYTHONHOME')}
    env.update(JUNFEI_ACCOUNT_DATA=str(workspace / 'data'),
               JUNFEI_PROJECT_ROOT=str(workspace / 'project'),
               JUNFEI_DISCOVERY_LIVE='isolated-read-only', JUNFEI_WORKER_USER='0')
    child = subprocess.Popen([sys.executable, '-I', '-B', '-X', 'utf8', str(_SOURCE),
                              '--offline-worker'], stdin=subprocess.PIPE,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             cwd=str(workspace), env=env,
                             creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    try:
        child.communicate(_json(job).encode('ascii'), timeout=35)
    except BaseException:
        # Reaping this Python fixture is not proof about a real remote browser.
        # Regardless of termination outcome, dispatch_unknown remains retained.
        child.kill()
        child.wait(timeout=5)
        raise
    return _WorkerResult(workspace, child.returncode, child.pid, job)


def _snapshot(workspace):
    data = workspace / 'data'
    browser = _connect(data / 'discovery_browser.sqlite3')
    graph = None
    try:
        graph = _connect(data / 'cyclic_discovery.sqlite3')
        browser.execute('BEGIN')
        graph.execute('BEGIN')
        result = {
            'runs': [list(r[:5]) + [_loads(r[5])] for r in browser.execute(
                'SELECT owner,id,space,status,request_hash,payload FROM live_runs ORDER BY owner,id')],
            'steps': [list(r[:6]) + [_loads(r[6])] for r in browser.execute(
                'SELECT owner,run_id,number,logical,token_hash,status,payload FROM live_steps ORDER BY owner,run_id,number')],
            'attempts': [list(r[:4]) + [_loads(r[4])] for r in browser.execute(
                'SELECT owner,id,digest,status,receipt FROM attempts ORDER BY owner,id')],
            'cycles': [list(r) for r in graph.execute(
                'SELECT owner,cycle,revision FROM discovery_cycles ORDER BY owner,cycle')],
            'events': [list(r[:3]) + [_loads(r[3])] for r in graph.execute(
                'SELECT owner,cycle,id,payload FROM discovery_events ORDER BY rowid')],
        }
        return result
    finally:
        browser.close()
        if graph is not None:
            graph.close()


def _verify_terminal(value, identity):
    _require(type(value) is _WorkerResult and type(value.returncode) is int
             and value.returncode == 0 and type(value.pid) is int and value.pid > 0,
             'fixture_child_not_verified_exited')
    workspace = _temp_path(value.workspace)
    job = value.job
    _object(job, {'version': int, 'identity': dict, 'spec': dict, 'workspace': str}, 'fixture_job_invalid')
    _object(job['identity'], _ID_TYPES, 'fixture_identity_invalid')
    _require(_same(job['identity'], vars(identity)) and job['version'] == 1
             and job['workspace'] == str(workspace), 'fixture_identity_mismatch')
    spec = job['spec']
    _object(spec, {'mode': str, 'namespace': str, 'account_id': int,
        'space_id': int, 'run_id': str, 'scenario': str}, 'fixture_spec_invalid')
    _require(_same(spec, _spec(spec['namespace'], spec['account_id'], spec['space_id'],
                            spec['run_id'], spec['scenario'])) and
             _digest(spec) == identity.request_sha256, 'fixture_request_mismatch')
    _require(_same(_read_json(workspace / 'input.json'), job), 'fixture_input_changed')
    evidence = _read_evidence(workspace)
    _require(set(evidence) == {'intent', 'worker_terminal', 'transport_1', 'transport_2',
        'transport_3', 'transport_5'} and _same(evidence['intent'], job),
             'fixture_terminal_missing_or_reused')
    proof = evidence['worker_terminal']
    _object(proof, {'version': int, 'mode': str, 'identity': dict, 'pid': int,
        'request': dict, 'joined': bool, 'workerErrors': list, 'transportCalls': int,
        'graphReport': dict, 'snapshotHash': str, 'status': dict}, 'fixture_terminal_schema_invalid')
    _object(proof['identity'], _ID_TYPES, 'fixture_identity_invalid')
    _require(proof['version'] == 1 and proof['mode'] == MODE and proof['pid'] == value.pid
        and proof['joined'] is True and proof['workerErrors'] == []
        and proof['transportCalls'] == 4 and _same(proof['identity'], vars(identity)),
        'fixture_terminal_identity_or_join_invalid')
    request = proof['request']
    _object(request, {'runId': str, 'cycleId': str, 'expectedRevision': int,
        'sourceEventId': str, 'maxActions': int, 'maxRounds': int,
        'maxSeconds': int, 'planFingerprint': str}, 'fixture_run_request_invalid')
    _require(request['runId'] == spec['run_id'] and request['cycleId'] == 'fixture-cycle'
        and request['sourceEventId'] == 'seed' and request['expectedRevision'] == 1
        and request['maxActions'] == 5 and request['maxRounds'] == 3
        and request['maxSeconds'] == 120 and re.fullmatch('[0-9a-f]{64}', request['planFingerprint']),
        'fixture_run_request_mismatch')
    snapshot = _snapshot(workspace)
    _require(_digest(snapshot) == proof['snapshotHash'], 'fixture_persisted_snapshot_changed')
    _validate_snapshot(snapshot, proof, spec, workspace)
    _validate_artifacts(evidence, snapshot, identity, workspace)
    # Exact post-child bytes bind final proof to immutable input and both ledgers.
    hashes = {name: hashlib.sha256((workspace / name).read_bytes()).hexdigest()
              for name in ('input.json', 'data/discovery_browser.sqlite3',
                           'data/cyclic_discovery.sqlite3', 'data/discovery-live-isolation.json',
                           'project/automation/config.json', 'unused-cli.exe')}
    summary = dict(mode=MODE, synthetic=True, realBrowserCalls=0,
        observedStages={'search_capture': 2, 'seller_capture': 1,
                        'store_capture': 1, 'title_phrase_capture': 1},
        syntheticBrowserCaptures=4, qualifiedCount=0, importedCount=0,
        paidCallsPerformed=0, uploadsPerformed=0, publicationsPerformed=0,
        productionExecutionAllowed=False, legacyEntryProtected=False)
    terminal = dict(version=1, identity=vars(identity), workerProof=proof,
                    transports={k:v for k,v in evidence.items() if k.startswith('transport_')},
                    files=hashes, summary=summary)
    _persist_terminal(workspace, terminal)
    return shared.TerminalEvidence(identity, _digest(terminal),
                                   'offline-discovery-whole-run-v1')


def _persist_terminal(workspace, terminal):
    _persist_evidence(workspace, 'parent_verified', terminal)
    _require(_same(_read_evidence(workspace)['parent_verified'], terminal),
             'fixture_terminal_readback_failed')


def _validate_snapshot(snapshot, proof, spec, workspace):
    owner, run_id = spec['account_id'], spec['run_id']
    _require(len(snapshot['runs']) == 1 and len(snapshot['steps']) == 5
        and len(snapshot['attempts']) == 4 and len(snapshot['events']) == 6
        and _same(snapshot['cycles'], [[owner, 'fixture-cycle', 6]]), 'fixture_history_count_invalid')
    row = snapshot['runs'][0]
    run = row[-1]
    _require(type(run) is dict and set(run) == {'schemaVersion','owner','runId','cycleId',
        'status','reason','revision','cursor','limits','usedActions','createdAt','deadline',
        'stopRequested','binding','bindingFingerprint','configFingerprint','graphFingerprint',
        'historyFingerprint','planFingerprint','stopPath'} and type(run['schemaVersion']) is int
        and run['schemaVersion']==1, 'fixture_run_schema_invalid')
    _require(_same(row[:5], [owner, run_id, spec['space_id'], 'stopped', _digest(proof['request'])]),
             'fixture_run_row_invalid')
    _require(type(run) is dict and run.get('owner') is not True
        and _same({k: run.get(k) for k in ('owner','runId','cycleId','status','reason','revision',
            'usedActions','stopRequested','limits','planFingerprint')},
            dict(owner=owner, runId=run_id, cycleId='fixture-cycle', status='stopped',
            reason='action_limit', revision=6, usedActions=5, stopRequested=False,
            limits={'maxActions':5,'maxRounds':3,'maxSeconds':120},
            planFingerprint=proof['request']['planFingerprint'])), 'fixture_run_not_clean_terminal')
    stop_path = workspace/'data'/'discovery-live-control'/(_digest([owner,run_id])+'.stop')
    _require(run.get('stopPath') == str(stop_path) and not stop_path.exists(),
             'fixture_stop_control_changed')
    expected_binding = {'spaceId':spec['space_id'], 'name':'Synthetic fixture only',
                        'label':'fixture-page', 'cli':str(workspace/'unused-cli.exe')}
    config = _read_json(workspace/'project'/'automation'/'config.json')
    marker = _read_json(workspace/'data'/'discovery-live-isolation.json')
    expected_config = {'browser':{'task_space_id':1}, 'discovery_browser':{'engine':'ego-lite',
        'cli':str(workspace/'unused-cli.exe'), 'account_bindings':{str(owner):{
        'enabled':True,'task_space_id':spec['space_id'],
        'task_space_name':'Synthetic fixture only','page_label':'fixture-page'}}}}
    _require(_same(config,expected_config) and _same(marker, {'schemaVersion':1,
        'mode':'isolated-read-only','owner':owner,'taskSpaceId':spec['space_id'],
        'dedicatedSpace':True}) and (workspace/'unused-cli.exe').read_bytes() ==
        b'OFFLINE FIXTURE: NEVER EXECUTE', 'fixture_configuration_changed')
    original = _SOURCE_CONFIG
    binding_fingerprint = hashlib.sha256(json.dumps({'engine':'ego-lite',
        'account':str(owner),'enabled':True,**expected_binding},sort_keys=True,
        ensure_ascii=True).encode()).hexdigest()
    _require(_same(run.get('binding'), expected_binding)
        and run.get('bindingFingerprint') == binding_fingerprint
        and run.get('configFingerprint') == _digest({'config':config,'production':original,'marker':marker})
        and type(run.get('createdAt')) in (int,float) and type(run.get('deadline')) in (int,float)
        and math.isfinite(run['createdAt']) and run['deadline'] == run['createdAt'] + 120,
        'fixture_run_binding_invalid')
    graph = proof['graphReport']
    _require(type(graph.get('revision')) is int and graph['revision'] == 6
        and graph.get('cycleId') == 'fixture-cycle'
        and _digest(graph) == run.get('graphFingerprint'), 'fixture_graph_fingerprint_invalid')
    events = {}
    for event_row in snapshot['events']:
        _require(_same(event_row[:2], [owner, 'fixture-cycle']) and type(event_row[2]) is str
            and type(event_row[3]) is dict and event_row[2] == event_row[3].get('id')
            and event_row[2] not in events, 'fixture_event_row_invalid')
        events[event_row[2]] = event_row[3]
    reported_events = {eid: {**event, 'eventId': eid, 'sourceURL': event['url'],
        'trust': 'unverified_observation', 'round': 1 if n < 4 else 2}
        for n, (eid, event) in enumerate(events.items())}
    _require(type(graph.get('events')) is list and len(graph['events']) == len(events)
        and _same({e['id']:e for e in graph['events']}, reported_events), 'fixture_graph_events_invalid')
    attempts, attempt_digests = {}, {}
    for account, cid, digest, state, receipt in snapshot['attempts']:
        _require(type(account) is int and account == owner and type(cid) is str
            and cid not in attempts and type(digest) is str and re.fullmatch('[0-9a-f]{64}', digest)
            and state == 'captured' and type(receipt) is dict
            and receipt.get('status') == 'captured' and receipt.get('captureId') == cid,
            'fixture_attempt_invalid')
        attempts[cid] = receipt
        attempt_digests[cid] = digest
    used_attempts = set()
    previous = 'seed'
    for number, row in enumerate(snapshot['steps'], 1):
        account, rid, n, logical, token, state, step = row
        _require(_same([account,rid,n,state], [owner,run_id,number,'observed'])
            and type(logical) is str and re.fullmatch('[0-9a-f]{64}', logical)
            and type(token) is str and re.fullmatch('[0-9a-f]{64}', token)
            and type(step) is dict and type(step.get('number')) is int and step['number'] == number
            and step.get('status') == 'observed', 'fixture_step_invalid')
        step_fields = {'number','status','action','request','reservedAt','graphFingerprint',
            'planFingerprint','historyFingerprint','observationId','finishedAt',
            'browserCapturePerformed','titlePolicy'}
        if number != 4:
            step_fields.add('dispatchedAt')
        _require(set(step) == step_fields, 'fixture_step_schema_invalid')
        action, request = step.get('action'), step.get('request')
        _require(type(action) is dict and action.get('kind') == _STAGES[number-1]
            and action.get('sourceEventId') == previous and type(request) is dict,
            'fixture_step_target_invalid')
        target = action.get('normalizedQuery') if action['kind']=='search_capture' else (
            action.get('sellerId') if action['kind']=='store_capture' else
            action.get('familyGroupId',action.get('asin')))
        _require(logical == _digest([action['kind'],action.get('marketplace'),target]),
                 'fixture_step_logical_invalid')
        capture_id = 'live-' + _digest([owner,run_id,number])[:48]
        expected = dict(captureId=capture_id, cycleId='fixture-cycle',
                        expectedRevision=number, parentId=previous)
        if number == 2:
            expected['asin'] = 'B000000004'
        if number == 3:
            expected['sellerId'] = 'FIXTURESELLER'
        _require(_same(request, expected), 'fixture_step_request_invalid')
        observation = step.get('observationId')
        _require(type(observation) is str and observation in events
            and events[observation].get('parentId') == previous
            and events[observation].get('kind') == ('keyword' if number == 4 else _STAGES[number-1].removesuffix('_capture')),
            'fixture_step_observation_invalid')
        if number == 4:
            _require(step.get('browserCapturePerformed') is False and capture_id not in attempts
                and events[observation].get('phrase') == 'New Linen Dress Long Sleeve',
                'fixture_title_step_invalid')
        else:
            receipt = attempts.get(capture_id)
            _require(step.get('browserCapturePerformed') is True and type(receipt) is dict
                and type(receipt.get('runtimeExitCode')) is int and receipt['runtimeExitCode'] == 0
                and receipt.get('phase') == 'runtime_dispatched'
                and receipt.get('navigationDispatched') is True
                and receipt.get('collectorDispatched') is True
                and receipt.get('navigationPhase') == 'finished'
                and receipt.get('captureKind') == _STAGES[number-1].removesuffix('_capture')
                and _same(receipt.get('target'), {'taskSpaceId': spec['space_id'],
                    'taskSpaceName':'Synthetic fixture only','pageLabel':'fixture-page'})
                and _same(receipt.get('preparedBatch'), {'cycleId':'fixture-cycle',
                    'expectedRevision':number, 'events':[events[observation]]}),
                'fixture_browser_receipt_invalid')
            kind = _STAGES[number-1].removesuffix('_capture')
            browser_digest = lambda value: hashlib.sha256(json.dumps(value,sort_keys=True,
                ensure_ascii=True).encode()).hexdigest()
            collector = _SOURCE.with_name('discovery-'+kind+'-collector.js')
            _require(attempt_digests[capture_id] == browser_digest({'captureMode':'navigate_read',
                'captureKind':kind,'request':request}) and
                receipt.get('bindingDigest') == browser_digest(expected_binding) and
                receipt.get('collectorSha256') == hashlib.sha256(collector.read_bytes()).hexdigest(),
                'fixture_attempt_fingerprint_invalid')
            receipt_fields = set(request) | {'captureKind','schemaVersion','status','reason','trust',
                'qualificationEvaluated','qualifiedCount','executionAllowed','publicationAllowed',
                'paidExternalCallsPerformed','startedAt','captureMode','marketplace','sourceURL',
                'bindingDigest','collectorSha256','target','runtimeExitCode','navigationDispatched',
                'collectorDispatched','navigationPhase','evidence','navigationEvidence','preparedBatch',
                'phase','finishedAt'}
            _require(set(receipt) == receipt_fields and
                all(_same(receipt.get(k),v) for k,v in request.items()) and
                _same({k:receipt.get(k) for k in ('schemaVersion','qualificationEvaluated',
                    'qualifiedCount','executionAllowed','publicationAllowed','paidExternalCallsPerformed')},
                    dict(schemaVersion=1,qualificationEvaluated=False,qualifiedCount=0,
                         executionAllowed=False,publicationAllowed=False,paidExternalCallsPerformed=0)),
                'fixture_attempt_request_invalid')
            used_attempts.add(capture_id)
        previous = observation
    _require(used_attempts == set(attempts) and run.get('cursor') == previous,
             'fixture_unused_attempt_or_cursor_invalid')
    status = proof['status']
    _require(_same({k: status.get(k) for k in ('runId','cycleId','status','reason','usedActions',
        'unresolvedStepCount','browserCapturesRecorded','completedObservationCount','revision')},
        dict(runId=run_id,cycleId='fixture-cycle',status='stopped',reason='action_limit',
             usedActions=5,unresolvedStepCount=0,browserCapturesRecorded=4,
             completedObservationCount=5,revision=6)), 'fixture_public_status_invalid')


def _validate_artifacts(evidence, snapshot, identity, workspace):
    seen = set()
    for number in (1,2,3,5):
        proof = evidence['transport_'+str(number)]
        _object(proof, {'identity':dict,'number':int,'captureId':str,'directory':str,
                        'scriptSha256':str,'resultSha256':str}, 'fixture_transport_schema_invalid')
        _object(proof['identity'], _ID_TYPES, 'fixture_identity_invalid')
        step = snapshot['steps'][number-1][-1]
        relative = Path(proof['directory'])
        _require(_same(proof['identity'],vars(identity)) and proof['number']==number
            and proof['captureId']==step['request']['captureId']
            and not relative.is_absolute() and len(relative.parts)==4
            and relative.parts[:3]==('data','discovery-captures',str(snapshot['runs'][0][0]))
            and re.fullmatch('[0-9a-f]{32}',relative.parts[3])
            and proof['directory'] not in seen, 'fixture_transport_binding_invalid')
        directory = _temp_path(workspace/relative)
        _require(set(p.name for p in directory.iterdir())=={'capture.mjs','result.json'},
                 'fixture_transport_artifacts_invalid')
        for filename, key in (('capture.mjs','scriptSha256'),('result.json','resultSha256')):
            path = _temp_path(directory/filename)
            _require(path.is_file() and path.stat().st_size<=1_000_000 and
                hashlib.sha256(path.read_bytes()).hexdigest()==proof[key],
                'fixture_transport_artifact_changed')
        seen.add(proof['directory'])
    root = workspace/'data'/'discovery-captures'/str(snapshot['runs'][0][0])
    _require({str(p.relative_to(workspace)) for p in root.iterdir()} == seen,
             'fixture_unused_capture_artifact')


def _pause(workspace, stage):
    _write_new(workspace / (stage + '.ready'), {'stage':stage})
    release = workspace / (stage + '.release')
    deadline = time.monotonic() + 20
    while not release.exists():
        _require(time.monotonic() < deadline, 'fixture_pause_timed_out')
        time.sleep(.02)


def _worker(job):
    # No discovery/app_paths import until exact newly-created TEMP scope exists.
    _object(job, {'version':int,'identity':dict,'spec':dict,'workspace':str}, 'fixture_job_invalid')
    _object(job['identity'], _ID_TYPES, 'fixture_identity_invalid')
    workspace = _temp_path(job['workspace'])
    _require(workspace.name.startswith('discovery-coordinated-fixture-')
        and _same(_read_json(workspace/'input.json'),job)
        and set(p.name for p in workspace.iterdir()) == {'input.json','evidence.sqlite3'},
        'fresh_fixture_workspace_required')
    spec = job['spec']
    _object(spec, {'mode':str,'namespace':str,'account_id':int,'space_id':int,
                  'run_id':str,'scenario':str}, 'fixture_spec_invalid')
    _require(_same(spec, _spec(spec['namespace'],spec['account_id'],spec['space_id'],
        spec['run_id'],spec['scenario'])) and _digest(spec)==job['identity']['request_sha256'],
        'fixture_request_invalid')
    _require(os.environ.get('JUNFEI_ACCOUNT_DATA') == str(workspace/'data')
        and os.environ.get('JUNFEI_PROJECT_ROOT') == str(workspace/'project')
        and os.environ.get('JUNFEI_WORKER_USER') == '0'
        and os.environ.get('JUNFEI_DISCOVERY_LIVE') == 'isolated-read-only'
        and not getattr(sys,'frozen',False), 'fixture_import_isolation_required')
    owner, run_id = spec['account_id'], spec['run_id']
    data, project = workspace/'data', workspace/'project'
    data.mkdir(); (project/'automation').mkdir(parents=True)
    (workspace/'unused-cli.exe').write_bytes(b'OFFLINE FIXTURE: NEVER EXECUTE')
    config = {'browser':{'task_space_id':1}, 'discovery_browser':{'engine':'ego-lite',
        'cli':str(workspace/'unused-cli.exe'), 'account_bindings':{str(owner):{
        'enabled':True,'task_space_id':spec['space_id'],
        'task_space_name':'Synthetic fixture only','page_label':'fixture-page'}}}}
    _write_new(project/'automation'/'config.json',config)
    baseline = workspace / 'source-config.json'
    _write_new(baseline, _SOURCE_CONFIG)
    _write_new(data/'discovery-live-isolation.json',{'schemaVersion':1,
        'mode':'isolated-read-only','owner':owner,'taskSpaceId':spec['space_id'],
        'dedicatedSpace':True})
    sys.path.insert(0,str(_SOURCE.parent))
    from unittest.mock import patch
    # Deny external calls during import as well as during the worker lifetime.
    def denied(*args, **kwargs):
        raise FixtureError('offline_external_operation_forbidden')
    with patch('socket.socket',side_effect=denied), patch('subprocess.Popen',side_effect=denied), \
         patch('os.system',side_effect=denied), patch('subprocess.run',side_effect=denied):
        import discovery_live as live
    errors, calls = [], []
    def thread_error(args):
        errors.append(type(args.exc_value).__name__)
    seed = dict(id='seed',kind='keyword',parentId=None,marketplace='US',
        observedAt=datetime.now(timezone.utc).isoformat(),sourceType='browser_visible',
        asin='B000000001',sourceTitle='Cotton Shirt Women',phrase='Cotton Shirt',
        url='https://amazon.com/dp/B000000001')
    def fake_run(args, **kwargs):
        directory = _temp_path(Path(kwargs['cwd']))
        _require(data in directory.parents and directory.parent.parent == data/'discovery-captures'
            and args == [str(workspace/'unused-cli.exe'),'nodejs','-e',
                'await import(' + json.dumps((directory/'capture.mjs').as_uri()) + ')'],
            'unexpected_fixture_subprocess')
        snap = _snapshot(workspace)
        active = [row[-1] for row in snap['steps'] if row[5]=='dispatch_unknown']
        _require(len(active)==1, 'fixture_dispatch_intent_required')
        step = active[0]; calls.append(step['number'])
        if spec['scenario']=='worker_exception':
            raise RuntimeError('fixed synthetic failure')
        if spec['scenario']=='worker_baseexception':
            raise KeyboardInterrupt('fixed synthetic failure')
        if spec['scenario']=='stop_during_dispatch':
            live.stop(owner,run_id)
        if spec['scenario']=='unknown':
            return subprocess.CompletedProcess(args,9)
        raw = _raw_capture(live,step,live.ledger.report(owner,'fixture-cycle'),spec['space_id'])
        if spec['scenario']=='blocked':
            raw = {'schemaVersion':1,'status':'blocked','reason':'captcha_or_challenge',
                   'navigationDispatched':True,'collectorDispatched':True,'navigationPhase':'finished'}
        _write_new(directory/'result.json',raw)
        _persist_evidence(workspace,'transport_'+str(step['number']),dict(
            identity=job['identity'],number=step['number'],captureId=step['request']['captureId'],
            directory=str(directory.relative_to(workspace)),
            scriptSha256=hashlib.sha256((directory/'capture.mjs').read_bytes()).hexdigest(),
            resultSha256=hashlib.sha256((directory/'result.json').read_bytes()).hexdigest()))
        return subprocess.CompletedProcess(args,0)
    original_reserve = live._reserve
    def reserve(account, rid):
        state = live.status(account,rid)
        if state['usedActions']==1 and state['completedObservationCount']==1:
            if spec['scenario']=='crash_after_first':
                os._exit(91)
            if spec['scenario']=='pause_between_steps':
                _pause(workspace,'between_steps')
        return original_reserve(account,rid)
    from browser_space_lock import active_space
    with patch('socket.socket',side_effect=denied), patch('subprocess.Popen',side_effect=denied), \
         patch('os.system',side_effect=denied), patch('subprocess.run',side_effect=fake_run), \
         patch.object(live.browser.runtime_environment, 'ego_space',
                      side_effect=lambda space: active_space(space, root=workspace)), \
         patch.object(live, '_ORIGINAL_CONFIG', baseline), \
         patch.object(threading,'excepthook',thread_error), patch.object(live,'_reserve',reserve):
        live.ledger.ingest(owner,{'cycleId':'fixture-cycle','expectedRevision':0,'events':[seed]})
        request = dict(runId=run_id,cycleId='fixture-cycle',expectedRevision=1,
                       sourceEventId='seed',maxActions=5,maxRounds=3,maxSeconds=120)
        request['planFingerprint'] = live.planner.prepare(owner,{k:request[k] for k in
            ('cycleId','expectedRevision','maxActions','maxRounds')})['planFingerprint']
        live.start(owner,request)
        worker = live._threads.get((owner,run_id))
        _require(type(worker) is threading.Thread, 'exact_fixture_worker_required')
        worker.join(25)
        _require(not worker.is_alive() and live._threads.get((owner,run_id)) is worker,
                 'fixture_worker_not_joined')
        _require(not any(t is not threading.current_thread() and t.is_alive()
                         for t in threading.enumerate()), 'fixture_extra_worker_alive')
        if spec['scenario']=='exit_without_proof':
            return
        proof = dict(version=1,mode=MODE,identity=job['identity'],pid=os.getpid(),
            request=request,joined=True,workerErrors=errors,transportCalls=len(calls),
            graphReport=live.ledger.report(owner,'fixture-cycle'),
            snapshotHash=_digest(_snapshot(workspace)),status=live.status(owner,run_id))
        _persist_evidence(workspace,'worker_terminal',proof)
        if spec['scenario']=='pause_after_terminal':
            _pause(workspace,'after_terminal')


def _raw_capture(live, step, graph, space_id):
    kind = step['action']['kind'].removesuffix('_capture')
    request = step['request']
    parent = next(e for e in graph['events'] if e['id']==request['parentId'])
    destination = live.browser._destination(kind,request,parent)
    now = datetime.now(timezone.utc).isoformat()
    raw = dict(schemaVersion=1,status='captured',marketplace='US',url=destination,
        capturedAt=now,navigationDispatched=True,collectorDispatched=True,navigationPhase='finished',
        navigationEvidence={'spaceId':space_id,'name':'Synthetic fixture only',
            'label':'fixture-page','destination':destination,'selectedURL':destination})
    if kind=='seller':
        raw.update(title='First visible product',asin=request['asin'],sellerId='FIXTURESELLER',
            sellerName='Fixture Seller',sellerLabel='Sold by Fixture Seller',
            sellerLink='https://amazon.com/sp?seller=FIXTURESELLER',sellerLinkSource='visible_sold_by_link',
            evidence={'selector':'#merchant-info','labelText':'Sold by Fixture Seller',
                'anchorText':'Fixture Seller','anchorHref':'https://amazon.com/sp?seller=FIXTURESELLER'})
    else:
        asin = 'B000000006' if kind=='store' else 'B000000004' if step['number']==1 else 'B000000007'
        product = {'asin':asin,'title':'New Linen Dress Long Sleeve' if kind=='store' else 'First visible product',
                   'productURL':'https://amazon.com/dp/'+asin}
        target = {'sellerId':request['sellerId']} if kind=='store' else {'query':parent['phrase']}
        card = {**product,'cardAsin':asin,'anchorHref':product['productURL'],
            'cardSelector':live.browser.SEARCH_CARD_SELECTOR,
            'titleSelector':live.browser.STORE_TITLE_SELECTOR if kind=='store' else live.browser.SEARCH_TITLE_SELECTOR,
            'visible':True,'sponsored':False}
        if kind=='search': card['directMainSlot']=True
        raw.update(**target,products=[product],evidence={'selector':'.s-main-slot',**target,
            'marketplace':'US','capturedAt':now,'sourceURL':destination,'cards':[card]})
    return raw


if __name__ == '__main__':
    if sys.argv[1:] != ['--offline-worker']:
        raise SystemExit('Offline fixture worker only; no live CLI is provided.')
    raw = sys.stdin.buffer.read(16385)
    if len(raw)>16384:
        raise SystemExit('Invalid fixture input')
    _worker(_loads(raw))
