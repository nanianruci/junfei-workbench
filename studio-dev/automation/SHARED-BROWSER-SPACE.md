# Shared browser-space coordination

The native EGO adapter, workbench discovery, bound 1688 captures, publisher
desktop and image skill use the same live lock for a numeric TaskSpace on the
current Windows account. This prevents overlapping participating calls. Initial
1688 space creation and reconnects without a resolved target ID use the durable
1688 queue. Each native job owns its submission receipts and recovery rules.
The separate durable enrollment and fencing protocol documented below remains
available for isolated tests.

## Status and scope

`Coordinator` is the offline-tested durable protocol. Its enrollment, operation
fencing and terminal evidence rules are independent of the live lock. The
[adapter adoption inventory](SHARED-BROWSER-ADOPTION.md) records the original
dispatch boundaries and requirements for durable integration.

The implemented boundary is one complete operation in **cooperating processes under the same Windows user**, using one trusted enrollment and the same default root. It is not machine-global across OS users and does not control direct EGO CLI calls, unmodified scripts, malicious participants, or browser operations outside the guarded callback. It does not reserve a whole multi-step discovery run between calls.

The default root comes from Windows `SHGetKnownFolderPath(FOLDERID_LocalAppData)`, followed by `JunfeiListingWorkbench/browser-space-coordinator-v1`. It does not depend on working directory, installation path, environment `LOCALAPPDATA`, per-account `DATA`, account ID, CLI executable path, or tab label. The resource is conservatively keyed by the fixed `ego-lite` engine and numeric TaskSpace ID. Different aliases or installations therefore cannot intentionally subdivide a space; distinct actual runtimes with coincident IDs are overlocked rather than assumed independent. Different space IDs may proceed independently.

An explicit `root=` argument exists for synthetic tests and a future reviewed deployment flow. It must never come from an HTTP request, account setting, environment variable, or other per-client choice. All participating production clients must use the default root. UNC/device paths and non-fixed/unknown Windows drive types are rejected before filesystem traversal. Reparse/symlink paths are refused rather than silently creating a new scope. This is not a hostile-filesystem race defense.

## Enrollment is separate from runtime

- `provision(root)` creates exactly one fresh root and SQLite ledger, returning a random 32-hex coordinator identity. The parent directory must already exist. It refuses existing or partially initialized roots and does not repair or delete them.
- Provisioning is an explicit setup primitive, not a CLI/API endpoint, import effect, acquire fallback, or automatic startup step. There is no production enrollment workflow in this slice.
- A future reviewed installer must pin the returned identity in independently trusted enrollment, shared across workbench accounts/installations. A runtime must not adopt whichever identity a replacement database reports.
- `Coordinator(expected_identity, root=None)` opens existing state and validates the pinned identity. Construction and `inspect` use read-only SQLite mode. Writes use `mode=rw`, never create-on-open.
- Missing/unreadable files, partial/wrong schema, absent/wrong enrollment, unknown journal mode, path replacement within an instance, inconsistent operation identities, and missing history rows/counter mismatches block. Runtime does not reset state or pick another directory.
- Removing all coordinator state is not a valid reset. With retained enrollment, opening missing state blocks. If all state and the independent enrollment are deleted or an entire coherent old snapshot is restored, software cannot establish historical safety without another durable anchor. This implementation does not claim adversarial tamper resistance or rollback detection. Re-enrollment/recovery requires a separate reviewed procedure and reconciliation; none is provided here.

## Durable state and one-use authority

The ledger uses SQLite rollback journals, `synchronous=FULL`, and `BEGIN IMMEDIATE` for mutations. OS/SQLite lock abandonment alone never establishes that a browser action did not run. Durable operation rows are never deleted by this API.

Each operation has an immutable identity:

- coordinator identity, fixed engine, numeric TaskSpace ID;
- stable owner namespace and operation ID;
- request SHA-256, monotonically increasing per-space fence;
- random dispatch nonce for exact receipt binding.

The owner namespace distinguishes originating application/account identities, but **never** partitions the resource lock. Bare account integer IDs are insufficient across unrelated central account databases. Future adapters must derive a stable central-accounts identity plus authenticated account ID, not accept an owner from request JSON. They must not include secrets in identifiers or request material stored here. The shared ledger stores only the request digest, not request payloads or credentials.

`reserve` atomically persists `reserved` and returns an opaque in-process lease. The lease's secret is not included in normal repr or operation records; only its digest is stored. Local object identity, creator PID, immutable dispatch identity, persisted secret digest, active fence, and expected state are checked before mutation. PID is a capability check, **not** a liveness/recovery signal.

The only state transitions are:

1. `reserved -> cancelled_before_dispatch`, by the original live holder before dispatch;
2. `reserved -> dispatch_unknown`, durably committed before calling any action;
3. `dispatch_unknown -> completed`, only after reviewed adapter verification and durable adapter-result persistence.

A crash after reservation leaves `reserved` blocking the space. A crash even just before the external call, a timeout, invalid receipt, verifier error, or lost dispatch-commit acknowledgement leaves `dispatch_unknown` blocking the space. No TTL, dead-PID cleanup, stale-lock deletion, retry, resume, force-release, or reconcile endpoint exists.

The same `(owner, operation_id)` with the same space/request returns the previous immutable record and **no lease**, regardless of state. Conflicting identity reuse fails. No reconstructed capability is issued after a crash. IDs remain one-use even after completion or pre-dispatch cancellation. A new operation after a verified terminal state advances the fence.

## Trusted adapter interface

- `reserve(space_id=..., owner=..., operation_id=..., request_sha256=...) -> Reservation(record, lease)`
- `inspect(owner=..., operation_id=...) -> Operation | None`
- `cancel_before_dispatch(lease) -> Operation`
- `dispatch_once(lease, action, verify) -> DispatchResult(value, record)`

`action(identity)` receives a frozen `DispatchIdentity`. It may invoke the real adapter only after the durable `dispatch_unknown` commit succeeds. The reservation remains held throughout the callback **and** `verify(value, identity)`. The callbacks must wait for all work they start and must not launch detached browser work that outlives verification. Nested acquisition of the same space is refused, not delegated implicitly.

The reviewed verifier returns `TerminalEvidence(identity, evidence_sha256, verifier, outcome)`. The identity must have exactly the `DispatchIdentity` class and expected field set, strict built-in field types, and the same canonical identity digest (boolean/float numeric aliases and equality spoofing are rejected), the digest must be a SHA-256, the verifier must identify reviewed code, and the outcome is `completed` or `refused_before_effects`. This typed object is an assertion by trusted code, not cryptographic proof that an arbitrary caller behaved correctly. Never accept it or a lease from a browser page or HTTP JSON.

Before producing terminal evidence, a real adapter must:

1. Validate exact operation/fence/nonce, target space, expected route and result schema.
2. Establish that the operation is finished with no outstanding browser work.
3. Validate application-specific terminal evidence, including a narrowly proven pre-effect refusal where relevant.
4. Durably persist its own result/evidence so a replay can read it without redispatch.
5. Return the evidence only after all those checks and persistence succeed.

Runtime exit code zero is insufficient. A raw `blocked` receipt is insufficient: the current discovery adapter can mark ownership/scope/evidence failures blocked after navigation. Nonzero exit, timeout, malformed/missing output, binding change, unknown effects, or failed receipt persistence must not yield terminal evidence. Completion of a coordinated operation also does not by itself prove ERP submission success or an Amazon listing being live.

The primitive does not accept generic `finish()` calls or expose an unlock operation. The verifier is supplied by trusted application code and must itself undergo review before adoption.

A failed storage operation poisons that in-memory coordinator instance. A lost final commit acknowledgement does not return success and never grants reuse of the old capability. A new instance may read whichever state durably committed: either unresolved and blocked, or a terminal record with replay-only behavior. Never infer that an exception authorizes repeating the external action.

## Validation

Run from the repository root:

`python -B -X utf8 -m unittest discover -s automation -p test_shared_browser_space.py -v`

The 44 focused tests use only new `TEMP` synthetic roots and ordinary Python child processes. They cover concurrent cross-account/process reservation despite distinct account/install/CLI environment paths; independent spaces; dispatch and verifier span ownership; immutable identity and strict terminal-proof schema/type matching; same-ID replay; stale/copied/wrong-process capabilities; crashed reservations; process death after dispatch; missing/corrupt/partial state; row/fence consistency; environment-independent Windows known-folder resolution; offline-mocked UNC/network/unknown-drive rejection; elapsed-time non-recovery; failed commits and lost commit acknowledgements. No browser, network, paid service, upload, publishing or production-state operation is exercised.

This does not test production adapter integration, multi-step run ownership, another Windows user, complete-ledger rollback, power-loss durability on faulty storage, or a hostile actor rewriting files. These remain outside this slice's claims.
