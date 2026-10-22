"""gaffer's SQLite state: jobs, attempts and leases, test results, Codex
reviews, rounds, feedback items, packets, repair cycles, CLI evidence and
doctor runs.

Decisions: R2 (CLI versions and auth status per job), R4 (per-test results
keyed by head SHA, base failures kept separately), R8 (repair cycles persist,
--more-cycles recorded, --fresh, --adopt-head), R9 (leases for every child
role, pre-assigned session IDs, idempotent writes, state_lost), R12 and R27
(feedback items keyed by source, item id and normalized content hash), R17
and R25 (round deadlines in supervised seconds; sleep gaps recorded), R18
(doctor passes stored with both CLI versions), R24 (local packets count as
repair cycles), R30 (state.db lives in ~/.gaffer; job clones live under
job_root).

Every write is one SQLite transaction (BEGIN IMMEDIATE ... COMMIT) on a WAL
database with synchronous=FULL, so a crash at any point leaves either the
state before the write or the state after it, never a mix.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Protocol

from gaffer.config import EXIT_REFUSED

# --- constants ------------------------------------------------------------

DB_FILENAME = "state.db"

# R8 / design "RESUME": three repair cycles, plus whatever --more-cycles grants.
BASE_REPAIR_CYCLES = 3

# R17 (recording kept by R25): a wall-clock vs monotonic gap over 2 min is a
# sleep gap, recorded in the job log and report.
SLEEP_GAP_THRESHOLD = 120.0

# Job steps, in flow order (amended Job flow). PACKET is the moment the packet
# is recorded; RESUME is the writer working on it.
STEPS = (
    "PREFLIGHT",
    "INTAKE",
    "WORKSPACE",
    "TESTER",
    "RED_FIRST",
    "WRITER",
    "VERIFY",
    "PUBLISH",
    "ROUND",
    "PACKET",
    "RESUME",
)
TERMINAL_STATES = ("READY", "BLOCKED", "CLOSED")
STATES = STEPS + TERMINAL_STATES

# Every BLOCKED reason code the design and ledger name. Validated in Python
# (not SQL) so adding one needs no migration.
BLOCKED_REASONS = frozenset({
    "acceptance_criteria_missing",  # design INTAKE
    "unmapped_team",  # design Configuration ("an unmapped team is BLOCKED"); code name is gaffer's
    "tester_invalid",  # R5, R6
    "tester_failed",  # design Tester
    "already_passing",  # R5
    "no_testable_criteria",  # design Tester
    "writer_no_result",  # design Writer, R10
    "writer_no_change",  # design Writer, R8
    "writer_dirty",  # R10
    "writer_blocked",  # R10
    "tester_commit_rewritten",  # design Writer
    "tester_change_unexplained",  # R7
    "forbidden_path",  # design Writer
    "secret_detected",  # R1, R15
    "verify_failed",  # R24
    "repair_limit",  # design RESUME, R8
    "not_ready",  # R8
    "review_timeout",  # design Waits, R8, R25
    "reviewer_failed",  # design Codex reviewer
    "required_check_missing",  # R11
    "foreign_head",  # design External changes
    "base_conflict",  # design External changes
    "usage_limit",  # design Limits
    "state_lost",  # R9
    "stopped",  # T1 (P2)
})

CHILD_ROLES = frozenset({"tester", "writer", "reviewer", "test_run", "install"})
ITEM_SOURCES = frozenset({"coderabbit", "operator", "codex", "check", "test"})
TEST_RUN_KINDS = ("base", "red_first", "verify")
# R4 outcomes plus R7's xfailed and missing (both count as failed for READY).
CASE_OUTCOMES = ("passed", "failed", "error", "skipped", "xfailed", "missing")
ROUND_OUTCOMES = ("complete", "timed_out", "superseded", "closed")
CODERABBIT_STATES = ("verified", "unverified")
DISPOSITIONS = ("fixed", "declined")
PACKET_KINDS = ("round", "local")

ISSUE_KEY = re.compile(r"[A-Z][A-Z0-9]*-\d+")  # Arch #3
_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")  # SHA-1 or SHA-256 object names
_HASH = re.compile(r"[0-9a-f]{64}")  # sha256 hex
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")


def default_db_path(home: Path | None = None) -> Path:
    """~/.gaffer/state.db. gaffer-only and denied to every child (R28, R30)."""
    return (home or Path.home()) / ".gaffer" / DB_FILENAME


def branch_name(issue_key: str, generation: int = 1) -> str:
    """R8: gaffer/<key>, and gaffer/<key>-2 etc. after --fresh."""
    return f"gaffer/{issue_key}" if generation == 1 else f"gaffer/{issue_key}-{generation}"


def branch_generation(issue_key: str, branch: str) -> int | None:
    """The generation a gaffer branch name encodes for this issue, or None."""
    if branch == f"gaffer/{issue_key}":
        return 1
    m = re.fullmatch(re.escape(f"gaffer/{issue_key}-") + r"([2-9]|[1-9]\d+)", branch)
    return int(m.group(1)) if m else None


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(value: Any) -> str:
    """sha256 over canonical JSON; used for packet and resume-result hashes."""
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


# --- errors ---------------------------------------------------------------


class StoreError(Exception):
    exit_code = 1  # R8: internal error


class SchemaTooNew(StoreError):
    """state.db was written by a newer gaffer. Refuse rather than guess."""

    exit_code = EXIT_REFUSED


class StoreCorrupt(StoreError):
    exit_code = EXIT_REFUSED


class ActiveJobExists(StoreError):
    """R9 / Approach A: one job at a time. Carries the active job."""

    exit_code = EXIT_REFUSED

    def __init__(self, job: Job):
        self.job = job
        super().__init__(
            f"job {job.ref} is still active (state {job.state}); gaffer runs one job at a time. "
            f"Rerun `gaffer run {job.issue_key}` to finish it first."
        )


class LiveJobExists(StoreError):
    def __init__(self, job: Job):
        self.job = job
        super().__init__(f"{job.issue_key} already has a job ({job.ref}, state {job.state}).")


class RepairLimitReached(StoreError):
    def __init__(self, job: Job):
        self.job = job
        super().__init__(
            f"{job.ref} has used {job.cycles_used} of {job.cycle_limit} repair cycles; "
            f"rerun with --more-cycles N to grant more (R8)."
        )


class Conflict(StoreError):
    """A write that contradicts what is already recorded (idempotency guard)."""


# --- clock ----------------------------------------------------------------


class Clock(Protocol):
    def wall(self) -> float: ...

    def monotonic(self) -> float: ...


class SystemClock:
    """time.time() and time.monotonic(). On macOS time.monotonic() is
    mach_absolute_time(), which does not advance while the Mac sleeps, so
    monotonic deltas are exactly supervised time (R25)."""

    def wall(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()


# --- records --------------------------------------------------------------

_JSON_FIELDS = {"criterion_map", "usage", "payload", "content", "result", "results", "detail",
                "claude_auth", "codex_auth", "extra"}
_BOOL_FIELDS = {"reconstructed", "admitted", "passed", "junit_found"}


def _from_row(cls, row: sqlite3.Row | None):
    if row is None:
        return None
    values = {}
    for f in fields(cls):
        v = row[f.name]
        if f.name in _JSON_FIELDS and v is not None:
            v = json.loads(v)
        elif f.name in _BOOL_FIELDS and v is not None:
            v = bool(v)
        values[f.name] = v
    return cls(**values)


@dataclass(frozen=True)
class Job:
    id: int
    issue_key: str
    generation: int
    team_key: str
    repo: str
    branch: str
    clone_path: str | None
    base_sha: str | None
    tester_commit_sha: str | None
    head_sha: str | None
    pr_number: int | None
    state: str
    blocked_reason: str | None
    blocked_message: str | None
    blocked_step: str | None
    criterion_map: Any
    writer_session_id: str | None
    cycles_used: int
    cycles_granted: int
    reconstructed: bool
    created_at: float
    updated_at: float
    archived_at: float | None

    @property
    def ref(self) -> str:
        """Stable job reference for markers (`gaffer-job: <ref>`, `gaffer-round: <ref>/...`).
        Not the SQLite rowid, which a lost state.db would reuse."""
        return self.issue_key if self.generation == 1 else f"{self.issue_key}-{self.generation}"

    @property
    def cycle_limit(self) -> int:
        return BASE_REPAIR_CYCLES + self.cycles_granted

    @property
    def cycles_remaining(self) -> int:
        return max(0, self.cycle_limit - self.cycles_used)

    @property
    def active(self) -> bool:
        return self.state not in TERMINAL_STATES


@dataclass(frozen=True)
class Attempt:
    id: int
    job_id: int
    role: str
    step: str
    session_id: str | None
    started_at: float
    ended_at: float | None
    outcome: str | None
    usage: Any


@dataclass(frozen=True)
class Lease:
    id: int
    job_id: int
    attempt_id: int | None
    role: str
    pid: int
    proc_start: str
    pgid: int
    session_id: str | None
    acquired_at: float
    released_at: float | None
    release_reason: str | None


@dataclass(frozen=True)
class CaseResult:
    """One JUnit testcase outcome. Identity is (file, classname, name) (R22)."""

    file: str
    classname: str
    name: str
    outcome: str
    failure_type: str | None = None
    message: str | None = None

    @property
    def identity(self) -> tuple[str, str, str]:
        return (self.file, self.classname, self.name)


@dataclass(frozen=True)
class RunRecord:
    id: int
    job_id: int
    head_sha: str
    kind: str
    attempt: int
    exit_code: int | None
    junit_found: bool
    duration_seconds: float | None
    recorded_at: float


@dataclass(frozen=True)
class Review:
    id: int
    job_id: int
    head_sha: str
    result: Any
    recorded_at: float


@dataclass(frozen=True)
class Round:
    id: int
    job_id: int
    repo: str
    pr_number: int
    head_sha: str
    seq: int
    opened_at: float
    deadline_seconds: int
    supervised_seconds: float
    last_tick_at: float | None
    closed_at: float | None
    outcome: str | None
    coderabbit: str | None
    coderabbit_note: str | None

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.deadline_seconds - self.supervised_seconds)

    @property
    def expired(self) -> bool:
        return self.supervised_seconds >= self.deadline_seconds

    def wall_elapsed(self, now: float) -> float:
        """Wall-clock time since the round opened, for the report (R25)."""
        return (self.closed_at if self.closed_at is not None else now) - self.opened_at

    @property
    def marker(self) -> str:
        return f"{self.head_sha}/{self.seq}"


def round_marker(job: Job, rnd: Round) -> str:
    """R9: the plain-text line in the round summary comment."""
    return f"gaffer-round: {job.ref}/{rnd.head_sha}/{rnd.seq}"


@dataclass(frozen=True)
class FeedbackItem:
    id: int
    job_id: int
    source: str
    item_id: str
    content_hash: str
    head_sha: str | None
    round_id: int | None
    payload: Any
    admitted: bool
    ignore_reason: str | None
    first_seen_at: float
    packet_id: int | None
    processed_at: float | None
    disposition: str | None
    fixed_sha: str | None
    decline_reason: str | None


@dataclass(frozen=True)
class Packet:
    id: int
    job_id: int
    kind: str
    round_id: int | None
    head_sha: str
    cycle: int
    packet_hash: str
    content: Any
    created_at: float
    resume_started_at: float | None
    resume_count: int
    result: Any
    result_hash: str | None
    result_recorded_at: float | None

    def verify(self) -> bool:
        """True when the stored content still hashes to the recorded hash."""
        return content_hash(self.content) == self.packet_hash


@dataclass(frozen=True)
class CliEvidence:
    id: int
    job_id: int
    recorded_at: float
    claude_version: str
    codex_version: str
    claude_auth: Any
    codex_auth: Any
    passed: bool
    extra: Any


@dataclass(frozen=True)
class DoctorRun:
    id: int
    claude_version: str
    codex_version: str
    ran_at: float
    passed: bool
    results: Any


@dataclass(frozen=True)
class Event:
    id: int
    job_id: int
    round_id: int | None
    at: float
    kind: str
    detail: Any


@dataclass(frozen=True)
class Disposition:
    """The writer's verdict on one packet item (design RESUME, R10)."""

    disposition: str
    fixed_sha: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class Tick:
    supervised_seconds: float
    deadline_seconds: int
    remaining_seconds: float
    expired: bool
    sleep_gap_seconds: float | None


@dataclass(frozen=True)
class RemoteBranch:
    """What GitHub shows for one gaffer branch of an issue (supplied by T7/T9)."""

    branch: str
    pr_number: int | None = None


@dataclass(frozen=True)
class StateLost:
    issue_key: str
    branches: tuple[RemoteBranch, ...]


class ProcessProbe(Protocol):
    """Implemented by children.py (T5). Returns the process start-time token
    for a PID, in the same format it was recorded in the lease, or None if no
    such process exists."""

    def start_time(self, pid: int) -> str | None: ...


# --- schema ---------------------------------------------------------------

_STATES_SQL = ", ".join(f"'{s}'" for s in STATES)
_STEPS_SQL = ", ".join(f"'{s}'" for s in STEPS)
_TERMINAL_SQL = ", ".join(f"'{s}'" for s in TERMINAL_STATES)


def _in(values: Iterable[str]) -> str:
    return ", ".join(f"'{v}'" for v in values)


SCHEMA_V1 = f"""
CREATE TABLE jobs (
    id INTEGER PRIMARY KEY,
    issue_key TEXT NOT NULL,
    generation INTEGER NOT NULL DEFAULT 1 CHECK (generation >= 1),
    team_key TEXT NOT NULL,
    repo TEXT NOT NULL,
    branch TEXT NOT NULL,
    clone_path TEXT,
    base_sha TEXT,
    tester_commit_sha TEXT,
    head_sha TEXT,
    pr_number INTEGER CHECK (pr_number > 0),
    state TEXT NOT NULL CHECK (state IN ({_STATES_SQL})),
    blocked_reason TEXT,
    blocked_message TEXT,
    blocked_step TEXT CHECK (blocked_step IS NULL OR blocked_step IN ({_STEPS_SQL})),
    criterion_map TEXT,
    writer_session_id TEXT,
    cycles_used INTEGER NOT NULL DEFAULT 0 CHECK (cycles_used >= 0),
    cycles_granted INTEGER NOT NULL DEFAULT 0 CHECK (cycles_granted >= 0),
    reconstructed INTEGER NOT NULL DEFAULT 0 CHECK (reconstructed IN (0, 1)),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    archived_at REAL,
    -- 1 while the job is being driven, NULL once it is READY, BLOCKED or CLOSED.
    active INTEGER GENERATED ALWAYS AS (CASE WHEN state IN ({_TERMINAL_SQL}) THEN NULL ELSE 1 END) VIRTUAL,
    CHECK ((state = 'BLOCKED') = (blocked_reason IS NOT NULL)),
    CHECK (state = 'BLOCKED' OR (blocked_message IS NULL AND blocked_step IS NULL)),
    CHECK (archived_at IS NULL OR state IN ({_TERMINAL_SQL})),
    CHECK (cycles_used <= {BASE_REPAIR_CYCLES} + cycles_granted)
) STRICT;
-- Approach A / R9: one job at a time.
CREATE UNIQUE INDEX jobs_one_active ON jobs (active);
-- design store.py: one live (unarchived) job per issue; --fresh archives it.
CREATE UNIQUE INDEX jobs_one_live_per_issue ON jobs (issue_key) WHERE archived_at IS NULL;
CREATE UNIQUE INDEX jobs_generation ON jobs (issue_key, generation);
CREATE UNIQUE INDEX jobs_branch ON jobs (repo, branch);

CREATE TABLE cycle_grants (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs (id),
    n INTEGER NOT NULL CHECK (n > 0),
    granted_at REAL NOT NULL,
    note TEXT
) STRICT;

CREATE TABLE attempts (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs (id),
    role TEXT NOT NULL,
    step TEXT NOT NULL CHECK (step IN ({_STEPS_SQL})),
    session_id TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    outcome TEXT,
    usage TEXT,
    CHECK ((ended_at IS NULL) = (outcome IS NULL))
) STRICT;

CREATE TABLE leases (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs (id),
    attempt_id INTEGER REFERENCES attempts (id),
    role TEXT NOT NULL,
    pid INTEGER NOT NULL CHECK (pid > 0),
    proc_start TEXT NOT NULL,
    pgid INTEGER NOT NULL CHECK (pgid > 0),
    session_id TEXT,
    acquired_at REAL NOT NULL,
    released_at REAL,
    release_reason TEXT,
    CHECK ((released_at IS NULL) = (release_reason IS NULL))
) STRICT;
CREATE UNIQUE INDEX leases_one_live_per_role ON leases (job_id, role) WHERE released_at IS NULL;

CREATE TABLE test_runs (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs (id),
    head_sha TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ({_in(TEST_RUN_KINDS)})),
    attempt INTEGER NOT NULL CHECK (attempt >= 1),
    exit_code INTEGER,
    junit_found INTEGER NOT NULL CHECK (junit_found IN (0, 1)),
    duration_seconds REAL,
    recorded_at REAL NOT NULL,
    UNIQUE (job_id, head_sha, kind, attempt)
) STRICT;

CREATE TABLE test_results (
    run_id INTEGER NOT NULL REFERENCES test_runs (id),
    file TEXT NOT NULL,
    classname TEXT NOT NULL,
    name TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ({_in(CASE_OUTCOMES)})),
    failure_type TEXT,
    message TEXT,
    PRIMARY KEY (run_id, file, classname, name)
) STRICT;

CREATE TABLE reviews (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs (id),
    head_sha TEXT NOT NULL,
    result TEXT NOT NULL,
    recorded_at REAL NOT NULL,
    UNIQUE (job_id, head_sha)
) STRICT;

CREATE TABLE rounds (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs (id),
    repo TEXT NOT NULL,
    pr_number INTEGER NOT NULL CHECK (pr_number > 0),
    head_sha TEXT NOT NULL,
    seq INTEGER NOT NULL CHECK (seq >= 1),
    opened_at REAL NOT NULL,
    deadline_seconds INTEGER NOT NULL CHECK (deadline_seconds > 0),
    supervised_seconds REAL NOT NULL DEFAULT 0 CHECK (supervised_seconds >= 0),
    last_tick_at REAL,
    closed_at REAL,
    outcome TEXT CHECK (outcome IS NULL OR outcome IN ({_in(ROUND_OUTCOMES)})),
    coderabbit TEXT CHECK (coderabbit IS NULL OR coderabbit IN ({_in(CODERABBIT_STATES)})),
    coderabbit_note TEXT,
    UNIQUE (repo, pr_number, head_sha, seq),
    CHECK ((closed_at IS NULL) = (outcome IS NULL))
) STRICT;
CREATE UNIQUE INDEX rounds_one_open_per_job ON rounds (job_id) WHERE closed_at IS NULL;

CREATE TABLE feedback_items (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs (id),
    source TEXT NOT NULL,
    item_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    head_sha TEXT,
    round_id INTEGER REFERENCES rounds (id),
    payload TEXT,
    admitted INTEGER NOT NULL CHECK (admitted IN (0, 1)),
    ignore_reason TEXT,
    first_seen_at REAL NOT NULL,
    packet_id INTEGER REFERENCES packets (id),
    processed_at REAL,
    disposition TEXT CHECK (disposition IS NULL OR disposition IN ({_in(DISPOSITIONS)})),
    fixed_sha TEXT,
    decline_reason TEXT,
    UNIQUE (job_id, source, item_id, content_hash),
    CHECK (admitted = 1 OR (ignore_reason IS NOT NULL AND packet_id IS NULL)),
    CHECK (processed_at IS NULL OR packet_id IS NOT NULL),
    CHECK (disposition IS NULL OR processed_at IS NOT NULL),
    CHECK (disposition IS NOT 'fixed' OR fixed_sha IS NOT NULL),
    CHECK (disposition IS NOT 'declined' OR decline_reason IS NOT NULL)
) STRICT;

CREATE TABLE packets (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs (id),
    kind TEXT NOT NULL CHECK (kind IN ({_in(PACKET_KINDS)})),
    round_id INTEGER REFERENCES rounds (id),
    head_sha TEXT NOT NULL,
    cycle INTEGER NOT NULL CHECK (cycle >= 1),
    packet_hash TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at REAL NOT NULL,
    resume_started_at REAL,
    resume_count INTEGER NOT NULL DEFAULT 0,
    result TEXT,
    result_hash TEXT,
    result_recorded_at REAL,
    UNIQUE (job_id, cycle),
    CHECK (kind = 'local' OR round_id IS NOT NULL),
    CHECK ((result_recorded_at IS NULL) = (result IS NULL)),
    CHECK ((result IS NULL) = (result_hash IS NULL))
) STRICT;
CREATE UNIQUE INDEX packets_one_open_per_job ON packets (job_id) WHERE result_recorded_at IS NULL;

CREATE TABLE cli_evidence (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs (id),
    recorded_at REAL NOT NULL,
    claude_version TEXT NOT NULL,
    codex_version TEXT NOT NULL,
    claude_auth TEXT NOT NULL,
    codex_auth TEXT NOT NULL,
    passed INTEGER NOT NULL CHECK (passed IN (0, 1)),
    extra TEXT
) STRICT;

CREATE TABLE doctor_runs (
    id INTEGER PRIMARY KEY,
    claude_version TEXT NOT NULL,
    codex_version TEXT NOT NULL,
    ran_at REAL NOT NULL,
    passed INTEGER NOT NULL CHECK (passed IN (0, 1)),
    results TEXT NOT NULL
) STRICT;
CREATE INDEX doctor_runs_versions ON doctor_runs (claude_version, codex_version, passed);

CREATE TABLE events (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs (id),
    round_id INTEGER REFERENCES rounds (id),
    at REAL NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT
) STRICT;
"""

# MIGRATIONS[i] takes the schema from user_version i to i + 1.
MIGRATIONS: tuple[str, ...] = (SCHEMA_V1,)
SCHEMA_VERSION = len(MIGRATIONS)


# --- store ----------------------------------------------------------------


class Store:
    """One connection to state.db. Not thread-safe; gaffer has one driver (R9)."""

    def __init__(
        self,
        path: Path | str,
        *,
        clock: Clock | None = None,
        migrations: Sequence[str] = MIGRATIONS,
        before_commit: Callable[[str], None] | None = None,
    ):
        """Open (creating if needed) and migrate state.db.

        `before_commit` is a fault-injection hook for tests: it runs inside
        every write transaction just before COMMIT, with the operation name.
        """
        self.path = Path(path)
        self.clock = clock or SystemClock()
        self._before_commit = before_commit
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.created = not self.path.exists()
        self._conn = sqlite3.connect(self.path, autocommit=True, timeout=10.0)
        try:
            self._conn.row_factory = sqlite3.Row
            if self.created:
                os.chmod(self.path, 0o600)
            self._check_integrity()
            mode = self._conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if mode.lower() != "wal":
                raise StoreError(f"{self.path}: could not enable WAL (journal_mode={mode}).")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._migrate(migrations)
        except BaseException:
            self._conn.close()
            raise

    # -- lifecycle --

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def schema_version(self) -> int:
        return self._conn.execute("PRAGMA user_version").fetchone()[0]

    def _check_integrity(self) -> None:
        try:
            result = self._conn.execute("PRAGMA quick_check").fetchone()[0]
        except sqlite3.DatabaseError as e:
            raise StoreCorrupt(f"{self.path} is not a readable SQLite database: {e}.") from None
        if result != "ok":
            raise StoreCorrupt(f"{self.path} failed SQLite quick_check: {result}.")

    def _migrate(self, migrations: Sequence[str]) -> None:
        current = self.schema_version
        if current > len(migrations):
            raise SchemaTooNew(
                f"{self.path} has schema version {current}, but this gaffer knows only up to "
                f"{len(migrations)}. Upgrade gaffer; the database was left untouched."
            )
        for version in range(current, len(migrations)):
            with self._tx(f"migrate_{version + 1}") as conn:
                # Re-read inside the write lock: another process may have migrated.
                if conn.execute("PRAGMA user_version").fetchone()[0] != version:
                    continue
                for statement in _split_sql(migrations[version]):
                    conn.execute(statement)
                conn.execute(f"PRAGMA user_version = {version + 1}")

    @contextmanager
    def _tx(self, op: str) -> Iterator[sqlite3.Connection]:
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            if self._before_commit is not None:
                self._before_commit(op)
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    def _now(self) -> float:
        return self.clock.wall()

    def _one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        return self._conn.execute(sql, params).fetchone()

    # -- jobs --

    def _get(self, cls, table: str, pk: int):
        record = _from_row(cls, self._one(f"SELECT * FROM {table} WHERE id = ?", (pk,)))
        if record is None:
            raise StoreError(f"no {table} row with id {pk}.")
        return record

    def get_job(self, job_id: int) -> Job:
        return self._get(Job, "jobs", job_id)

    def job_for_issue(self, issue_key: str) -> Job | None:
        """The issue's live (unarchived) job, if any."""
        return _from_row(Job, self._one(
            "SELECT * FROM jobs WHERE issue_key = ? AND archived_at IS NULL", (issue_key,)))

    def jobs_for_issue(self, issue_key: str) -> list[Job]:
        """Every job for the issue, archived ones included, oldest first."""
        rows = self._conn.execute("SELECT * FROM jobs WHERE issue_key = ? ORDER BY generation", (issue_key,))
        return [_from_row(Job, r) for r in rows]

    def active_job(self) -> Job | None:
        return _from_row(Job, self._one("SELECT * FROM jobs WHERE active = 1"))

    def create_job(self, issue_key: str, *, team_key: str, repo: str, state: str = "PREFLIGHT") -> Job:
        """Create the issue's job. Its generation is one past any archived job (R8 --fresh)."""
        _check_issue_key(issue_key)
        _check_state(state)
        if state in TERMINAL_STATES:
            raise ValueError(f"a new job starts in a step, not {state}; use block() after creating it.")
        with self._tx("create_job") as conn:
            if (live := self.job_for_issue(issue_key)) is not None:
                raise LiveJobExists(live)
            if (active := self.active_job()) is not None:
                raise ActiveJobExists(active)
            generation = conn.execute(
                "SELECT COALESCE(MAX(generation), 0) + 1 FROM jobs WHERE issue_key = ?", (issue_key,)).fetchone()[0]
            now = self._now()
            cur = conn.execute(
                "INSERT INTO jobs (issue_key, generation, team_key, repo, branch, state, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (issue_key, generation, team_key, repo, branch_name(issue_key, generation), state, now, now),
            )
            return self.get_job(cur.lastrowid)

    def set_state(self, job_id: int, state: str) -> Job:
        """Move a job to a step, READY or CLOSED. Clears any BLOCKED reason.

        Raises ActiveJobExists if this would make a second job active."""
        _check_state(state)
        if state == "BLOCKED":
            raise ValueError("use block() to record a BLOCKED reason.")
        with self._tx("set_state"):
            self._require_activatable(job_id, state)
            self._update_job(job_id, state=state, blocked_reason=None, blocked_message=None, blocked_step=None)
        return self.get_job(job_id)

    def block(self, job_id: int, reason: str, message: str, *, step: str | None = None) -> Job:
        """BLOCKED with a reason code and message (design Exits). `step` defaults
        to the step the job was in, which is where a rerun re-validates from."""
        if reason not in BLOCKED_REASONS:
            raise ValueError(f"unknown BLOCKED reason {reason!r}; known: {sorted(BLOCKED_REASONS)}.")
        if not message.strip():
            raise ValueError("a BLOCKED job needs a message.")
        with self._tx("block"):
            job = self._job_in_tx(job_id)
            if step is None:
                step = job.state if job.state in STEPS else job.blocked_step
            if step is not None and step not in STEPS:
                raise ValueError(f"unknown step {step!r}.")
            self._update_job(job_id, state="BLOCKED", blocked_reason=reason, blocked_message=message, blocked_step=step)
        return self.get_job(job_id)

    def set_workspace(self, job_id: int, *, clone_path: Path | str, base_sha: str) -> Job:
        """WORKSPACE: the job clone (under job_root, R30) and the job's base SHA."""
        _check_sha(base_sha)
        return self._set(job_id, "set_workspace", clone_path=str(clone_path), base_sha=base_sha)

    def set_criterion_map(self, job_id: int, criterion_map: Any) -> Job:
        return self._set(job_id, "set_criterion_map", criterion_map=canonical_json(criterion_map))

    def set_tester_commit(self, job_id: int, sha: str, criterion_map: Any | None = None) -> Job:
        """RED-FIRST passed and the tester files are committed."""
        _check_sha(sha)
        values: dict[str, Any] = {"tester_commit_sha": sha}
        if criterion_map is not None:
            values["criterion_map"] = canonical_json(criterion_map)
        return self._set(job_id, "set_tester_commit", **values)

    def set_writer_session(self, job_id: int, session_id: str) -> Job:
        """R9: the pre-assigned --session-id, recorded before the writer launches."""
        if not session_id:
            raise ValueError("empty session id.")
        return self._set(job_id, "set_writer_session", writer_session_id=session_id)

    def set_pr(self, job_id: int, pr_number: int) -> Job:
        if pr_number <= 0:
            raise ValueError(f"bad PR number {pr_number}.")
        with self._tx("set_pr"):
            job = self._job_in_tx(job_id)
            if job.pr_number is not None and job.pr_number != pr_number:
                raise Conflict(f"{job.ref} already has PR #{job.pr_number}; refusing to record #{pr_number}.")
            self._update_job(job_id, pr_number=pr_number)
        return self.get_job(job_id)

    def set_head(self, job_id: int, head_sha: str) -> Job:
        """The head gaffer last pushed (PUBLISH)."""
        _check_sha(head_sha)
        return self._set(job_id, "set_head", head_sha=head_sha)

    def adopt_head(self, job_id: int, *, head_sha: str, base_sha: str) -> Job:
        """R8 --adopt-head: accept the remote head, base = merge-base. Recorded."""
        _check_sha(head_sha)
        _check_sha(base_sha)
        with self._tx("adopt_head"):
            job = self._job_in_tx(job_id)
            self._update_job(job_id, head_sha=head_sha, base_sha=base_sha)
            self._event(job_id, "adopt_head", {"head_sha": head_sha, "base_sha": base_sha,
                                               "previous_head": job.head_sha, "previous_base": job.base_sha})
        return self.get_job(job_id)

    def archive_job(self, job_id: int) -> Job:
        """R8 --fresh on a CLOSED job: archive it so the issue can start generation n+1."""
        with self._tx("archive_job"):
            job = self._job_in_tx(job_id)
            if job.state != "CLOSED":
                raise Conflict(f"{job.ref} is {job.state}; --fresh archives only CLOSED jobs (R8).")
            self._update_job(job_id, archived_at=self._now())
        return self.get_job(job_id)

    def grant_cycles(self, job_id: int, n: int, note: str | None = None) -> Job:
        """R8 --more-cycles N: N more repair cycles, recorded."""
        if n <= 0:
            raise ValueError("--more-cycles needs a positive N.")
        with self._tx("grant_cycles") as conn:
            job = self._job_in_tx(job_id)
            conn.execute("INSERT INTO cycle_grants (job_id, n, granted_at, note) VALUES (?, ?, ?, ?)",
                         (job_id, n, self._now(), note))
            self._update_job(job_id, cycles_granted=job.cycles_granted + n)
        return self.get_job(job_id)

    def cycle_grants(self, job_id: int) -> list[tuple[int, float, str | None]]:
        rows = self._conn.execute("SELECT n, granted_at, note FROM cycle_grants WHERE job_id = ? ORDER BY id", (job_id,))
        return [(r["n"], r["granted_at"], r["note"]) for r in rows]

    def _set(self, job_id: int, op: str, **values: Any) -> Job:
        with self._tx(op):
            self._job_in_tx(job_id)
            self._update_job(job_id, **values)
        return self.get_job(job_id)

    def _job_in_tx(self, job_id: int) -> Job:
        return self.get_job(job_id)

    def _update_job(self, job_id: int, **values: Any) -> None:
        values["updated_at"] = self._now()
        cols = ", ".join(f"{k} = ?" for k in values)
        self._conn.execute(f"UPDATE jobs SET {cols} WHERE id = ?", (*values.values(), job_id))

    def _require_activatable(self, job_id: int, state: str) -> None:
        job = self._job_in_tx(job_id)
        if job.archived_at is not None:
            raise Conflict(f"{job.ref} is archived.")
        if state not in TERMINAL_STATES:
            active = self.active_job()
            if active is not None and active.id != job_id:
                raise ActiveJobExists(active)

    # -- state lost (R9) --

    def check_state_lost(self, issue_key: str, remote: Iterable[RemoteBranch]) -> StateLost | None:
        """R9: gaffer branches (and their PRs) that exist on GitHub for this issue
        but that no job record owns mean state.db was lost or replaced.

        The caller lists the remote branches matching gaffer/<key> and
        gaffer/<key>-N, each with its PR number if one exists."""
        _check_issue_key(issue_key)
        owned = {j.branch for j in self.jobs_for_issue(issue_key)}
        unknown = tuple(b for b in remote if branch_generation(issue_key, b.branch) is not None and b.branch not in owned)
        return StateLost(issue_key, unknown) if unknown else None

    def record_state_lost(self, lost: StateLost, *, team_key: str, repo: str) -> Job:
        """Create the job as BLOCKED: state_lost on the newest lost branch, so a
        rerun with --adopt-head can reconstruct it (R9)."""
        newest = max(lost.branches, key=lambda b: branch_generation(lost.issue_key, b.branch) or 0)
        generation = branch_generation(lost.issue_key, newest.branch)
        assert generation is not None
        names = ", ".join(b.branch + (f" (PR #{b.pr_number})" if b.pr_number else "") for b in lost.branches)
        with self._tx("record_state_lost") as conn:
            if (live := self.job_for_issue(lost.issue_key)) is not None:
                raise LiveJobExists(live)
            if conn.execute("SELECT 1 FROM jobs WHERE issue_key = ? AND generation = ?",
                            (lost.issue_key, generation)).fetchone():
                raise Conflict(f"{newest.branch} is already recorded for an archived job.")
            now = self._now()
            cur = conn.execute(
                "INSERT INTO jobs (issue_key, generation, team_key, repo, branch, pr_number, state,"
                " blocked_reason, blocked_message, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, 'BLOCKED', 'state_lost', ?, ?, ?)",
                (lost.issue_key, generation, team_key, repo, newest.branch, newest.pr_number,
                 f"gaffer has no record of {names}; state.db was lost or replaced. "
                 f"Rerun with --adopt-head to rebuild the job from the branch (R9).", now, now),
            )
            self._event(cur.lastrowid, "state_lost", {"branches": [b.branch for b in lost.branches],
                                                      "prs": [b.pr_number for b in lost.branches]})
            return self.get_job(cur.lastrowid)

    def reconstruct_job(self, job_id: int, *, head_sha: str, base_sha: str, tester_commit_sha: str,
                        clone_path: Path | str | None = None, cycles_used: int = 0) -> Job:
        """R9 --adopt-head after state_lost: base = merge-base, tester commit =
        first commit, inferred by the caller. Marks the job reconstructed so the
        report says it ran on inferred evidence. The writer session is not
        recoverable; the runner starts a fresh writer with a handoff."""
        for sha in (head_sha, base_sha, tester_commit_sha):
            _check_sha(sha)
        with self._tx("reconstruct_job"):
            job = self._job_in_tx(job_id)
            if job.blocked_reason != "state_lost":
                raise Conflict(f"{job.ref} is not BLOCKED: state_lost.")
            if cycles_used > BASE_REPAIR_CYCLES + job.cycles_granted:
                raise ValueError(f"cycles_used {cycles_used} exceeds the limit.")
            values: dict[str, Any] = dict(head_sha=head_sha, base_sha=base_sha, tester_commit_sha=tester_commit_sha,
                                          reconstructed=1, cycles_used=cycles_used, writer_session_id=None)
            if clone_path is not None:
                values["clone_path"] = str(clone_path)
            self._update_job(job_id, **values)
            self._event(job_id, "reconstructed", {"head_sha": head_sha, "base_sha": base_sha,
                                                  "tester_commit_sha": tester_commit_sha, "cycles_used": cycles_used})
        return self.get_job(job_id)

    # -- CLI evidence (R2) and doctor (R18) --

    def record_cli_evidence(self, job_id: int, *, claude_version: str, codex_version: str,
                            claude_auth: Any, codex_auth: Any, passed: bool, extra: Any = None) -> CliEvidence:
        """R2: both CLI versions and status output, email/org already redacted by
        the caller. Refuses anything that still looks like an email address."""
        for label, value in (("claude_auth", claude_auth), ("codex_auth", codex_auth), ("extra", extra)):
            if value is not None and _EMAIL.search(canonical_json(value)):
                raise ValueError(f"{label} still contains an email address; redact it before recording (R2).")
        with self._tx("record_cli_evidence") as conn:
            self._job_in_tx(job_id)
            cur = conn.execute(
                "INSERT INTO cli_evidence (job_id, recorded_at, claude_version, codex_version, claude_auth,"
                " codex_auth, passed, extra) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (job_id, self._now(), claude_version, codex_version, canonical_json(claude_auth),
                 canonical_json(codex_auth), int(passed), None if extra is None else canonical_json(extra)),
            )
        return _from_row(CliEvidence, self._one("SELECT * FROM cli_evidence WHERE id = ?", (cur.lastrowid,)))

    def cli_evidence(self, job_id: int) -> list[CliEvidence]:
        rows = self._conn.execute("SELECT * FROM cli_evidence WHERE job_id = ? ORDER BY id", (job_id,))
        return [_from_row(CliEvidence, r) for r in rows]

    def record_doctor_run(self, *, claude_version: str, codex_version: str, passed: bool, results: Any) -> DoctorRun:
        with self._tx("record_doctor_run") as conn:
            cur = conn.execute(
                "INSERT INTO doctor_runs (claude_version, codex_version, ran_at, passed, results) VALUES (?, ?, ?, ?, ?)",
                (claude_version, codex_version, self._now(), int(passed), canonical_json(results)),
            )
        return _from_row(DoctorRun, self._one("SELECT * FROM doctor_runs WHERE id = ?", (cur.lastrowid,)))

    def last_doctor_pass(self) -> DoctorRun | None:
        return _from_row(DoctorRun, self._one("SELECT * FROM doctor_runs WHERE passed = 1 ORDER BY id DESC LIMIT 1"))

    def doctor_needed(self, claude_version: str, codex_version: str) -> bool:
        """R18: run doctor when either version differs from the last pass."""
        last = self.last_doctor_pass()
        return last is None or (last.claude_version, last.codex_version) != (claude_version, codex_version)

    # -- attempts and leases (R9) --

    def start_attempt(self, job_id: int, *, role: str, step: str, session_id: str | None = None) -> Attempt:
        """Record a child launch before it starts (so a pre-assigned session ID
        survives a crash between launch and capture)."""
        _check_role(role)
        if step not in STEPS:
            raise ValueError(f"unknown step {step!r}.")
        with self._tx("start_attempt") as conn:
            self._job_in_tx(job_id)
            cur = conn.execute("INSERT INTO attempts (job_id, role, step, session_id, started_at) VALUES (?, ?, ?, ?, ?)",
                               (job_id, role, step, session_id, self._now()))
        return self.get_attempt(cur.lastrowid)

    def finish_attempt(self, attempt_id: int, *, outcome: str, usage: Any = None) -> Attempt:
        """Design "Seed data for B": whatever usage the CLI reported."""
        if not outcome:
            raise ValueError("empty outcome.")
        with self._tx("finish_attempt") as conn:
            attempt = self.get_attempt(attempt_id)
            if attempt.ended_at is not None:
                raise Conflict(f"attempt {attempt_id} already finished ({attempt.outcome}).")
            conn.execute("UPDATE attempts SET ended_at = ?, outcome = ?, usage = ? WHERE id = ?",
                         (self._now(), outcome, None if usage is None else canonical_json(usage), attempt_id))
        return self.get_attempt(attempt_id)

    def get_attempt(self, attempt_id: int) -> Attempt:
        return self._get(Attempt, "attempts", attempt_id)

    def attempts(self, job_id: int) -> list[Attempt]:
        return [_from_row(Attempt, r) for r in
                self._conn.execute("SELECT * FROM attempts WHERE job_id = ? ORDER BY id", (job_id,))]

    def acquire_lease(self, job_id: int, *, role: str, pid: int, proc_start: str, pgid: int,
                      session_id: str | None = None, attempt_id: int | None = None) -> Lease:
        """Record a live child: PID, process start time and process group."""
        _check_role(role)
        if not proc_start:
            raise ValueError("empty process start time.")
        with self._tx("acquire_lease") as conn:
            self._job_in_tx(job_id)
            try:
                cur = conn.execute(
                    "INSERT INTO leases (job_id, attempt_id, role, pid, proc_start, pgid, session_id, acquired_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (job_id, attempt_id, role, pid, str(proc_start), pgid, session_id, self._now()),
                )
            except sqlite3.IntegrityError:
                live = self.live_leases(job_id, role=role)
                if live:
                    raise Conflict(f"job {job_id} already has a live {role} lease (pid {live[0].pid}).") from None
                raise
        return _from_row(Lease, self._one("SELECT * FROM leases WHERE id = ?", (cur.lastrowid,)))

    def release_lease(self, lease_id: int, reason: str) -> Lease:
        """Reasons are free text, e.g. "exited", "dead", "sigterm" (R9: recorded)."""
        if not reason:
            raise ValueError("empty release reason.")
        with self._tx("release_lease") as conn:
            conn.execute("UPDATE leases SET released_at = ?, release_reason = ? WHERE id = ? AND released_at IS NULL",
                         (self._now(), reason, lease_id))
        return _from_row(Lease, self._one("SELECT * FROM leases WHERE id = ?", (lease_id,)))

    def live_leases(self, job_id: int, *, role: str | None = None) -> list[Lease]:
        """Unreleased leases. Whether each process is really alive is alive_leases()."""
        sql, params = "SELECT * FROM leases WHERE job_id = ? AND released_at IS NULL", [job_id]
        if role is not None:
            sql += " AND role = ?"
            params.append(role)
        return [_from_row(Lease, r) for r in self._conn.execute(sql + " ORDER BY id", params)]

    def alive_leases(self, job_id: int, probe: ProcessProbe) -> tuple[list[Lease], list[Lease]]:
        """R9 liveness: a child is alive when its PID exists with the same start
        time. Returns (alive, dead); dead ones are released as "dead"."""
        alive, dead = [], []
        for lease in self.live_leases(job_id):
            if probe.start_time(lease.pid) == lease.proc_start:
                alive.append(lease)
            else:
                dead.append(self.release_lease(lease.id, "dead"))
        return alive, dead

    # -- test results (R4) --

    def record_test_run(self, job_id: int, *, head_sha: str, kind: str, results: Iterable[CaseResult],
                        exit_code: int | None, junit_found: bool, duration_seconds: float | None = None) -> RunRecord:
        """Store one gaffer test run and every testcase in it, atomically.
        `kind`: "base" (base failures, kept separately), "red_first", "verify"."""
        _check_sha(head_sha)
        if kind not in TEST_RUN_KINDS:
            raise ValueError(f"unknown test run kind {kind!r}.")
        results = list(results)
        for r in results:
            if r.outcome not in CASE_OUTCOMES:
                raise ValueError(f"unknown outcome {r.outcome!r} for {r.identity}.")
        with self._tx("record_test_run") as conn:
            self._job_in_tx(job_id)
            attempt = conn.execute("SELECT COALESCE(MAX(attempt), 0) + 1 FROM test_runs WHERE job_id = ? AND head_sha = ?"
                                   " AND kind = ?", (job_id, head_sha, kind)).fetchone()[0]
            cur = conn.execute(
                "INSERT INTO test_runs (job_id, head_sha, kind, attempt, exit_code, junit_found, duration_seconds,"
                " recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (job_id, head_sha, kind, attempt, exit_code, int(junit_found), duration_seconds, self._now()),
            )
            try:
                conn.executemany(
                    "INSERT INTO test_results (run_id, file, classname, name, outcome, failure_type, message)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [(cur.lastrowid, r.file, r.classname, r.name, r.outcome, r.failure_type, r.message) for r in results],
                )
            except sqlite3.IntegrityError as e:
                raise ValueError(f"duplicate test identity in one run: {e}.") from None
        return _from_row(RunRecord, self._one("SELECT * FROM test_runs WHERE id = ?", (cur.lastrowid,)))

    def latest_test_run(self, job_id: int, *, head_sha: str, kind: str) -> RunRecord | None:
        return _from_row(RunRecord, self._one(
            "SELECT * FROM test_runs WHERE job_id = ? AND head_sha = ? AND kind = ? ORDER BY attempt DESC LIMIT 1",
            (job_id, head_sha, kind)))

    def test_results(self, run_id: int) -> list[CaseResult]:
        rows = self._conn.execute("SELECT * FROM test_results WHERE run_id = ? ORDER BY file, classname, name", (run_id,))
        return [CaseResult(r["file"], r["classname"], r["name"], r["outcome"], r["failure_type"], r["message"])
                for r in rows]

    # -- Codex review, once per head --

    def record_review(self, job_id: int, *, head_sha: str, result: Any) -> tuple[Review, bool]:
        """Record the Codex review for a head. A head has exactly one; a second
        call returns the recorded one and False (a rerun reuses it)."""
        _check_sha(head_sha)
        with self._tx("record_review") as conn:
            self._job_in_tx(job_id)
            cur = conn.execute("INSERT INTO reviews (job_id, head_sha, result, recorded_at) VALUES (?, ?, ?, ?)"
                               " ON CONFLICT (job_id, head_sha) DO NOTHING",
                               (job_id, head_sha, canonical_json(result), self._now()))
            created = cur.rowcount == 1
        review = self.review_for_head(job_id, head_sha)
        assert review is not None
        return review, created

    def review_for_head(self, job_id: int, head_sha: str) -> Review | None:
        return _from_row(Review, self._one("SELECT * FROM reviews WHERE job_id = ? AND head_sha = ?", (job_id, head_sha)))

    # -- rounds (R8, R25) --

    def open_round(self, job_id: int, *, head_sha: str, deadline_seconds: int) -> tuple[Round, bool]:
        """Open round (repo, PR, head, seq) with seq one past the last round on
        this head. If a round is already open on this head it is returned with
        False (rerun resumes it, supervised time intact). `deadline_seconds` is
        the repo's round_timeout, snapshotted."""
        _check_sha(head_sha)
        if deadline_seconds <= 0:
            raise ValueError("deadline_seconds must be positive.")
        with self._tx("open_round") as conn:
            job = self._job_in_tx(job_id)
            if job.pr_number is None:
                raise Conflict(f"{job.ref} has no PR yet; a round needs one.")
            if (current := self.open_round_for(job_id)) is not None:
                if current.head_sha == head_sha:
                    return current, False
                raise Conflict(f"round {current.marker} is still open on another head; close it first.")
            seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM rounds WHERE repo = ? AND pr_number = ?"
                               " AND head_sha = ?", (job.repo, job.pr_number, head_sha)).fetchone()[0]
            cur = conn.execute(
                "INSERT INTO rounds (job_id, repo, pr_number, head_sha, seq, opened_at, deadline_seconds)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (job_id, job.repo, job.pr_number, head_sha, seq, self._now(), deadline_seconds),
            )
        return self.get_round(cur.lastrowid), True

    def get_round(self, round_id: int) -> Round:
        return self._get(Round, "rounds", round_id)

    def open_round_for(self, job_id: int) -> Round | None:
        return _from_row(Round, self._one("SELECT * FROM rounds WHERE job_id = ? AND closed_at IS NULL", (job_id,)))

    def rounds(self, job_id: int) -> list[Round]:
        return [_from_row(Round, r) for r in self._conn.execute("SELECT * FROM rounds WHERE job_id = ? ORDER BY id", (job_id,))]

    def close_round(self, round_id: int, *, outcome: str, coderabbit: str | None = None,
                    coderabbit_note: str | None = None) -> Round:
        """complete | timed_out (BLOCKED: review_timeout) | superseded | closed.
        `coderabbit` is verified or unverified (R27), with the reason in the note."""
        if outcome not in ROUND_OUTCOMES:
            raise ValueError(f"unknown round outcome {outcome!r}.")
        if coderabbit is not None and coderabbit not in CODERABBIT_STATES:
            raise ValueError(f"unknown CodeRabbit state {coderabbit!r}.")
        if coderabbit == "unverified" and not coderabbit_note:
            raise ValueError("an unverified CodeRabbit round needs the reason (R27).")
        with self._tx("close_round") as conn:
            rnd = self.get_round(round_id)
            if rnd.closed_at is not None:
                if rnd.outcome == outcome:
                    return rnd
                raise Conflict(f"round {rnd.marker} already closed as {rnd.outcome}.")
            conn.execute("UPDATE rounds SET closed_at = ?, outcome = ?, coderabbit = ?, coderabbit_note = ? WHERE id = ?",
                         (self._now(), outcome, coderabbit, coderabbit_note, round_id))
        return self.get_round(round_id)

    def supervise(self, round_id: int, *, sleep_gap_threshold: float = SLEEP_GAP_THRESHOLD) -> RoundSupervisor:
        """Start counting supervised time for an open round from now (R25)."""
        rnd = self.get_round(round_id)
        if rnd.closed_at is not None:
            raise Conflict(f"round {rnd.marker} is closed.")
        return RoundSupervisor(self, round_id, sleep_gap_threshold)

    def _credit_round(self, round_id: int, seconds: float, sleep_gap: tuple[float, float, float] | None) -> Round:
        with self._tx("tick_round") as conn:
            rnd = self.get_round(round_id)
            if rnd.closed_at is not None:
                raise Conflict(f"round {rnd.marker} is closed.")
            now = self._now()
            conn.execute("UPDATE rounds SET supervised_seconds = supervised_seconds + ?, last_tick_at = ? WHERE id = ?",
                         (seconds, now, round_id))
            if sleep_gap is not None:
                gap, wall_delta, mono_delta = sleep_gap
                self._event(rnd.job_id, "sleep_gap",
                            {"gap_seconds": gap, "wall_seconds": wall_delta, "monotonic_seconds": mono_delta,
                             "ended_at": now}, round_id=round_id)
        return self.get_round(round_id)

    # -- feedback items (R12, R27) --

    def add_item(self, job_id: int, *, source: str, item_id: str, content_hash: str, payload: Any,
                 head_sha: str | None = None, round_id: int | None = None, admitted: bool = True,
                 ignore_reason: str | None = None) -> tuple[FeedbackItem, bool]:
        """Insert a feedback item keyed (source, item id, normalized content
        hash) within the job. Idempotent: a repeat returns the existing item and
        False. An edited comment has a new hash, so it is a new item.

        `content_hash` is the sha256 hex of the body after R27 normalization
        (computed by reviews.py). Items for per-head evidence (failed checks,
        failing tests) must put the head in `item_id`. Ignored items are stored
        with admitted=False and their reason, for the report (R12)."""
        if source not in ITEM_SOURCES:
            raise ValueError(f"unknown item source {source!r}; known: {sorted(ITEM_SOURCES)}.")
        if not item_id:
            raise ValueError("empty item id.")
        if not _HASH.fullmatch(content_hash):
            raise ValueError("content_hash must be a lowercase sha256 hex digest.")
        if head_sha is not None:
            _check_sha(head_sha)
        if admitted and ignore_reason is not None:
            raise ValueError("an admitted item has no ignore reason.")
        if not admitted and not ignore_reason:
            raise ValueError("an ignored item needs its reason (R12: listed in the report).")
        with self._tx("add_item") as conn:
            self._job_in_tx(job_id)
            cur = conn.execute(
                "INSERT INTO feedback_items (job_id, source, item_id, content_hash, head_sha, round_id, payload,"
                " admitted, ignore_reason, first_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (job_id, source, item_id, content_hash) DO NOTHING",
                (job_id, source, item_id, content_hash, head_sha, round_id, canonical_json(payload),
                 int(admitted), ignore_reason, self._now()),
            )
            created = cur.rowcount == 1
        item = _from_row(FeedbackItem, self._one(
            "SELECT * FROM feedback_items WHERE job_id = ? AND source = ? AND item_id = ? AND content_hash = ?",
            (job_id, source, item_id, content_hash)))
        return item, created

    def get_item(self, item_pk: int) -> FeedbackItem:
        return self._get(FeedbackItem, "feedback_items", item_pk)

    def items(self, job_id: int) -> list[FeedbackItem]:
        return [_from_row(FeedbackItem, r) for r in
                self._conn.execute("SELECT * FROM feedback_items WHERE job_id = ? ORDER BY id", (job_id,))]

    def pending_items(self, job_id: int) -> list[FeedbackItem]:
        """Admitted items in no packet yet: what the next packet would hold."""
        return [_from_row(FeedbackItem, r) for r in self._conn.execute(
            "SELECT * FROM feedback_items WHERE job_id = ? AND admitted = 1 AND packet_id IS NULL ORDER BY id", (job_id,))]

    # -- packets and resume results --

    def create_packet(self, job_id: int, *, kind: str, head_sha: str, item_ids: Sequence[int], content: Any,
                      round_id: int | None = None) -> tuple[Packet, bool]:
        """Record a packet (and its hash) before the resume, assign its items and
        count one repair cycle, atomically (R8, R24).

        If the job already has an unresolved packet with the same hash it is
        returned with False (rerun resumes the same packet); a different one
        raises Conflict. Raises RepairLimitReached when no cycles are left."""
        if kind not in PACKET_KINDS:
            raise ValueError(f"unknown packet kind {kind!r}.")
        _check_sha(head_sha)
        if not item_ids:
            raise ValueError("a packet needs at least one item.")
        if len(set(item_ids)) != len(item_ids):
            raise ValueError("duplicate item in packet.")
        packet_hash = content_hash(content)
        with self._tx("create_packet") as conn:
            job = self._job_in_tx(job_id)
            if (current := self.open_packet(job_id)) is not None:
                if current.packet_hash == packet_hash:
                    return current, False
                raise Conflict(f"packet {current.id} (cycle {current.cycle}) has no recorded result yet; resume it first.")
            if job.cycles_used >= job.cycle_limit:
                raise RepairLimitReached(job)
            for pk in item_ids:
                item = _from_row(FeedbackItem, self._one("SELECT * FROM feedback_items WHERE id = ?", (pk,)))
                if item is None or item.job_id != job_id:
                    raise ValueError(f"item {pk} does not belong to job {job_id}.")
                if not item.admitted:
                    raise ValueError(f"item {pk} was not admitted ({item.ignore_reason}).")
                if item.packet_id is not None:
                    raise Conflict(f"item {pk} is already in packet {item.packet_id}.")
            cycle = job.cycles_used + 1
            cur = conn.execute(
                "INSERT INTO packets (job_id, kind, round_id, head_sha, cycle, packet_hash, content, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (job_id, kind, round_id, head_sha, cycle, packet_hash, canonical_json(content), self._now()),
            )
            conn.executemany("UPDATE feedback_items SET packet_id = ? WHERE id = ?", [(cur.lastrowid, pk) for pk in item_ids])
            self._update_job(job_id, cycles_used=cycle)
        return self.get_packet(cur.lastrowid), True

    def get_packet(self, packet_id: int) -> Packet:
        return self._get(Packet, "packets", packet_id)

    def open_packet(self, job_id: int) -> Packet | None:
        """The packet whose resume result isn't recorded yet (design recovery table)."""
        return _from_row(Packet, self._one("SELECT * FROM packets WHERE job_id = ? AND result_recorded_at IS NULL", (job_id,)))

    def packets(self, job_id: int) -> list[Packet]:
        return [_from_row(Packet, r) for r in self._conn.execute("SELECT * FROM packets WHERE job_id = ? ORDER BY id", (job_id,))]

    def packet_items(self, packet_id: int) -> list[FeedbackItem]:
        return [_from_row(FeedbackItem, r) for r in
                self._conn.execute("SELECT * FROM feedback_items WHERE packet_id = ? ORDER BY id", (packet_id,))]

    def mark_resume_started(self, packet_id: int) -> Packet:
        with self._tx("mark_resume_started") as conn:
            packet = self.get_packet(packet_id)
            if packet.result_recorded_at is not None:
                raise Conflict(f"packet {packet_id} already has a result.")
            conn.execute("UPDATE packets SET resume_started_at = COALESCE(resume_started_at, ?),"
                         " resume_count = resume_count + 1 WHERE id = ?", (self._now(), packet_id))
        return self.get_packet(packet_id)

    def record_resume_result(self, packet_id: int, *, result: Any, dispositions: Mapping[int, Disposition]) -> tuple[Packet, bool]:
        """Record the resume result and mark the packet's items processed, in
        one transaction. Items without a disposition are processed with none
        (READY's "every item has a disposition" check sees them). A repeat with
        the same result returns (packet, False)."""
        result_hash = content_hash(result)
        for pk, d in dispositions.items():
            if d.disposition not in DISPOSITIONS:
                raise ValueError(f"unknown disposition {d.disposition!r} for item {pk}.")
            if d.disposition == "fixed":
                _check_sha(d.fixed_sha or "")
            elif not d.reason:
                raise ValueError(f"declined item {pk} needs a reason.")
        with self._tx("record_resume_result") as conn:
            packet = self.get_packet(packet_id)
            if packet.result_recorded_at is not None:
                if packet.result_hash == result_hash:
                    return packet, False
                raise Conflict(f"packet {packet_id} already has a different result.")
            member_ids = {i.id for i in self.packet_items(packet_id)}
            if stray := set(dispositions) - member_ids:
                raise ValueError(f"dispositions for items not in packet {packet_id}: {sorted(stray)}.")
            now = self._now()
            conn.execute("UPDATE packets SET result = ?, result_hash = ?, result_recorded_at = ? WHERE id = ?",
                         (canonical_json(result), result_hash, now, packet_id))
            conn.execute("UPDATE feedback_items SET processed_at = ? WHERE packet_id = ?", (now, packet_id))
            conn.executemany(
                "UPDATE feedback_items SET disposition = ?, fixed_sha = ?, decline_reason = ? WHERE id = ?",
                [(d.disposition, d.fixed_sha if d.disposition == "fixed" else None,
                  d.reason if d.disposition == "declined" else None, pk) for pk, d in dispositions.items()],
            )
        return self.get_packet(packet_id), True

    # -- events (job log) --

    def log_event(self, job_id: int, kind: str, detail: Any = None, *, round_id: int | None = None) -> None:
        """Recorded facts for the report: SIGTERM of a writer group (R9), fresh
        writer fallback, and so on."""
        with self._tx("log_event"):
            self._job_in_tx(job_id)
            self._event(job_id, kind, detail, round_id=round_id)

    def events(self, job_id: int, kind: str | None = None) -> list[Event]:
        sql, params = "SELECT * FROM events WHERE job_id = ?", [job_id]
        if kind is not None:
            sql += " AND kind = ?"
            params.append(kind)
        return [_from_row(Event, r) for r in self._conn.execute(sql + " ORDER BY id", params)]

    def _event(self, job_id: int, kind: str, detail: Any, *, round_id: int | None = None) -> None:
        self._conn.execute("INSERT INTO events (job_id, round_id, at, kind, detail) VALUES (?, ?, ?, ?, ?)",
                           (job_id, round_id, self._now(), kind, None if detail is None else canonical_json(detail)))


class RoundSupervisor:
    """Counts supervised seconds for one round inside one gaffer process (R25).

    Each tick() adds the monotonic delta since the previous tick (or since
    supervise() was called) to the round's persisted supervised_seconds. Time
    before supervise() -- gaffer stopped, crashed or rebooting -- is never
    counted. When the wall clock advanced more than the monotonic clock by over
    SLEEP_GAP_THRESHOLD, the Mac slept: the gap is recorded as a sleep_gap event
    (R17) and, since the monotonic clock stood still, it is not counted."""

    def __init__(self, store: Store, round_id: int, sleep_gap_threshold: float):
        self._store = store
        self.round_id = round_id
        self._threshold = sleep_gap_threshold
        self._last_mono = store.clock.monotonic()
        self._last_wall = store.clock.wall()

    def tick(self) -> Tick:
        mono, wall = self._store.clock.monotonic(), self._store.clock.wall()
        mono_delta = max(0.0, mono - self._last_mono)
        wall_delta = wall - self._last_wall
        gap = wall_delta - mono_delta
        sleep_gap = (gap, wall_delta, mono_delta) if gap > self._threshold else None
        rnd = self._store._credit_round(self.round_id, mono_delta, sleep_gap)
        # Advance the baseline only after the credit is committed, so a failed
        # write is retried on the next tick rather than lost.
        self._last_mono, self._last_wall = mono, wall
        return Tick(rnd.supervised_seconds, rnd.deadline_seconds, rnd.remaining_seconds, rnd.expired,
                    gap if sleep_gap else None)


# --- helpers --------------------------------------------------------------


def _split_sql(script: str) -> list[str]:
    """Split a migration into statements (sqlite3.complete_statement aware), so
    each runs inside the migration's transaction via execute()."""
    statements, buf = [], ""
    for line in script.splitlines(keepends=True):
        if line.strip().startswith("--") and not buf.strip():
            continue
        buf += line
        if sqlite3.complete_statement(buf):
            if buf.strip():
                statements.append(buf.strip())
            buf = ""
    if buf.strip():
        raise ValueError(f"incomplete SQL statement in migration: {buf.strip()[:80]!r}")
    return statements


def _check_issue_key(issue_key: str) -> None:
    if not ISSUE_KEY.fullmatch(issue_key):
        raise ValueError(f"{issue_key!r} is not a Linear issue key like ENG-123.")


def _check_state(state: str) -> None:
    if state not in STATES:
        raise ValueError(f"unknown state {state!r}.")


def _check_sha(sha: str) -> None:
    if not _SHA.fullmatch(sha):
        raise ValueError(f"{sha!r} is not a full lowercase commit SHA.")


def _check_role(role: str) -> None:
    if role not in CHILD_ROLES:
        raise ValueError(f"unknown child role {role!r}; known: {sorted(CHILD_ROLES)}.")
