import hashlib
import os
import re
import signal
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from gaffer import store as st
from gaffer.store import (
    ActiveJobExists,
    CaseResult,
    Conflict,
    Disposition,
    LiveJobExists,
    RemoteBranch,
    RepairLimitReached,
    SchemaTooNew,
    Store,
    StoreCorrupt,
)

H = {n: f"{n:x}" * 40 for n in range(1, 10)}  # distinct fake 40-hex SHAs
BASE, TESTER, HEAD1, HEAD2, FIX1 = H[1], H[2], H[3], H[4], H[5]
TESTS_DIR = Path(__file__).parent


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class FakeClock:
    """Wall and monotonic clocks the test moves by hand. sleep() advances the
    wall clock only, as macOS does while asleep (mach_absolute_time stops)."""

    def __init__(self, wall: float = 1_800_000_000.0, mono: float = 1000.0):
        self._wall, self._mono = wall, mono

    def wall(self) -> float:
        return self._wall

    def monotonic(self) -> float:
        return self._mono

    def advance(self, seconds: float) -> None:
        self._wall += seconds
        self._mono += seconds

    def sleep(self, seconds: float) -> None:
        self._wall += seconds

    def reboot(self, downtime: float) -> None:
        """Wall time passes; the monotonic clock restarts near zero."""
        self._wall += downtime
        self._mono = 5.0


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def db(tmp_path):
    return tmp_path / "gaffer" / "state.db"


@pytest.fixture
def s(db, clock):
    with Store(db, clock=clock) as store:
        yield store


# --- builders ------------------------------------------------------------


def published_job(s: Store, key: str = "ENG-1", *, state: str = "ROUND") -> st.Job:
    job = s.create_job(key, team_key="ENG", repo="mfullbrook/app")
    s.set_workspace(job.id, clone_path=f"/jobs/{key}", base_sha=BASE)
    s.set_tester_commit(job.id, TESTER, {"AC1": ["tests/test_a.py::test_a"]})
    s.set_writer_session(job.id, "11111111-2222-3333-4444-555555555555")
    s.set_head(job.id, HEAD1)
    s.set_pr(job.id, 7)
    return s.set_state(job.id, state)


def add(s: Store, job_id: int, n: int = 1, *, source: str = "coderabbit", round_id=None) -> list[st.FeedbackItem]:
    return [s.add_item(job_id, source=source, item_id=f"c{i}", content_hash=digest(f"body {i}"),
                       payload={"body": f"body {i}"}, head_sha=HEAD1, round_id=round_id)[0] for i in range(n)]


def packet(s: Store, job_id: int, items, content=None, **kw):
    return s.create_packet(job_id, kind=kw.pop("kind", "round"), head_sha=HEAD1, item_ids=[i.id for i in items],
                           content=content if content is not None else {"items": [i.item_id for i in items]},
                           round_id=kw.pop("round_id", None) or s.open_round(job_id, head_sha=HEAD1, deadline_seconds=1800)[0].id,
                           **kw)


def dump(path: Path) -> dict:
    """Every row of every table, read with plain sqlite3."""
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
        out = {t: conn.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall() for t in tables}
        out["user_version"] = conn.execute("PRAGMA user_version").fetchone()[0]
        return out
    finally:
        conn.close()


# --- schema, WAL, migration ----------------------------------------------


def test_new_db_is_wal_private_and_current(db, clock):
    with Store(db, clock=clock) as s:
        assert s.created
        assert s.schema_version == st.SCHEMA_VERSION
        assert s._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert s._conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
        assert s._conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert stat.S_IMODE(db.stat().st_mode) == 0o600
    assert stat.S_IMODE(db.parent.stat().st_mode) == 0o700
    with Store(db, clock=clock) as s:
        assert not s.created


def test_default_db_path_is_in_dot_gaffer(tmp_path):
    assert st.default_db_path(tmp_path) == tmp_path / ".gaffer" / "state.db"


def test_refuses_newer_schema_and_leaves_db_untouched(db, clock):
    Store(db, clock=clock).close()
    conn = sqlite3.connect(db)
    conn.execute(f"PRAGMA user_version = {st.SCHEMA_VERSION + 1}")
    conn.close()
    before = dump(db)
    with pytest.raises(SchemaTooNew) as e:
        Store(db, clock=clock)
    assert e.value.exit_code == 4
    assert dump(db) == before


def test_migrates_old_db_forward_keeping_data(db, clock):
    v2 = "ALTER TABLE jobs ADD COLUMN note TEXT;\nCREATE TABLE extra (id INTEGER PRIMARY KEY) STRICT;"
    with Store(db, clock=clock, migrations=st.MIGRATIONS) as s:
        published_job(s)
    with Store(db, clock=clock, migrations=(*st.MIGRATIONS, v2)) as s:
        assert s.schema_version == st.SCHEMA_VERSION + 1
        assert s.job_for_issue("ENG-1").head_sha == HEAD1
        assert s._conn.execute("SELECT note FROM jobs").fetchone()[0] is None


def test_failed_migration_rolls_back_whole_step(db, clock):
    Store(db, clock=clock).close()
    before = dump(db)
    bad = "CREATE TABLE half (id INTEGER) STRICT;\nTHIS IS NOT SQL;"
    with pytest.raises(sqlite3.OperationalError):
        Store(db, clock=clock, migrations=(*st.MIGRATIONS, bad))
    assert dump(db) == before


def test_corrupt_file_is_refused(db, clock):
    db.parent.mkdir(parents=True)
    db.write_bytes(b"this is not a database" * 100)
    with pytest.raises(StoreCorrupt) as e:
        Store(db, clock=clock)
    assert e.value.exit_code == 4


def test_split_sql_keeps_semicolons_in_comments_and_strings():
    stmts = st._split_sql("-- a; b\nCREATE TABLE t (x TEXT DEFAULT 'a;b');\n-- c;\nCREATE INDEX i ON t (x);\n")
    assert stmts == ["CREATE TABLE t (x TEXT DEFAULT 'a;b');", "CREATE INDEX i ON t (x);"]
    with pytest.raises(ValueError):
        st._split_sql("CREATE TABLE t (x")


# --- jobs, one active job ------------------------------------------------


def test_create_job_defaults(s):
    job = s.create_job("ENG-12", team_key="ENG", repo="mfullbrook/app")
    assert (job.state, job.generation, job.branch, job.ref) == ("PREFLIGHT", 1, "gaffer/ENG-12", "ENG-12")
    assert (job.cycles_used, job.cycle_limit, job.active) == (0, 3, True)
    assert s.active_job() == job


@pytest.mark.parametrize("key", ["eng-1", "ENG", "ENG-", "ENG-1a", "1ENG-2", "ENG-1\n"])
def test_issue_key_is_validated(s, key):
    with pytest.raises(ValueError):
        s.create_job(key, team_key="ENG", repo="o/r")


def test_only_one_active_job_at_a_time(s):
    first = published_job(s, "ENG-1", state="WRITER")
    with pytest.raises(ActiveJobExists) as e:
        s.create_job("ENG-2", team_key="ENG", repo="o/r")
    assert e.value.job.id == first.id
    assert e.value.exit_code == 4

    s.block(first.id, "writer_blocked", "needs a decision")
    second = s.create_job("ENG-2", team_key="ENG", repo="o/r")
    # Rerunning the BLOCKED job while ENG-2 is active is refused too.
    with pytest.raises(ActiveJobExists):
        s.set_state(first.id, "WRITER")
    s.set_state(second.id, "READY")
    assert s.set_state(first.id, "WRITER").active


def test_one_active_job_is_enforced_by_the_schema(s, db):
    published_job(s, "ENG-1", state="ROUND")
    conn = sqlite3.connect(db)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO jobs (issue_key, team_key, repo, branch, state, created_at, updated_at)"
                     " VALUES ('ENG-2', 'ENG', 'o/r', 'gaffer/ENG-2', 'WRITER', 0, 0)")
    # A terminal job alongside is fine.
    conn.execute("INSERT INTO jobs (issue_key, team_key, repo, branch, state, created_at, updated_at)"
                 " VALUES ('ENG-3', 'ENG', 'o/r', 'gaffer/ENG-3', 'READY', 0, 0)")
    conn.commit()
    conn.close()


def test_one_live_job_per_issue_and_fresh_generations(s):
    job = published_job(s)
    s.block(job.id, "foreign_head", "someone pushed")
    with pytest.raises(LiveJobExists):
        s.create_job("ENG-1", team_key="ENG", repo="mfullbrook/app")
    with pytest.raises(Conflict):
        s.archive_job(job.id)  # --fresh only after CLOSED (R8)
    s.set_state(job.id, "CLOSED")
    s.archive_job(job.id)
    fresh = s.create_job("ENG-1", team_key="ENG", repo="mfullbrook/app")
    assert (fresh.generation, fresh.branch, fresh.ref) == (2, "gaffer/ENG-1-2", "ENG-1-2")
    assert s.job_for_issue("ENG-1").id == fresh.id
    assert [j.generation for j in s.jobs_for_issue("ENG-1")] == [1, 2]
    with pytest.raises(Conflict):
        s.set_state(job.id, "ROUND")  # archived jobs never come back


def test_block_records_reason_and_step_and_set_state_clears_it(s):
    job = published_job(s, state="VERIFY")
    blocked = s.block(job.id, "verify_failed", "3 cycles used")
    assert (blocked.state, blocked.blocked_reason, blocked.blocked_step, blocked.active) == (
        "BLOCKED", "verify_failed", "VERIFY", False)
    again = s.block(job.id, "repair_limit", "still failing")
    assert again.blocked_step == "VERIFY"  # still the step to re-validate from
    cleared = s.set_state(job.id, "VERIFY")
    assert (cleared.blocked_reason, cleared.blocked_message, cleared.blocked_step) == (None, None, None)
    with pytest.raises(ValueError):
        s.block(job.id, "made_up_reason", "x")
    with pytest.raises(ValueError):
        s.block(job.id, "not_ready", "  ")
    with pytest.raises(ValueError):
        s.set_state(job.id, "BLOCKED")
    with pytest.raises(ValueError):
        s.set_state(job.id, "DONE")


def test_job_fields_and_conflicting_pr(s):
    job = published_job(s)
    assert job.criterion_map == {"AC1": ["tests/test_a.py::test_a"]}
    assert (job.base_sha, job.tester_commit_sha, job.head_sha, job.pr_number) == (BASE, TESTER, HEAD1, 7)
    assert s.set_pr(job.id, 7).pr_number == 7  # same PR again is fine (PUBLISH reconcile)
    with pytest.raises(Conflict):
        s.set_pr(job.id, 8)
    with pytest.raises(ValueError):
        s.set_head(job.id, "abc123")
    adopted = s.adopt_head(job.id, head_sha=HEAD2, base_sha=H[6])
    assert (adopted.head_sha, adopted.base_sha) == (HEAD2, H[6])
    assert s.events(job.id, "adopt_head")[0].detail["previous_head"] == HEAD1


def test_more_cycles_are_recorded(s):
    job = published_job(s)
    s.grant_cycles(job.id, 2, "--more-cycles 2")
    job = s.grant_cycles(job.id, 1)
    assert (job.cycles_granted, job.cycle_limit) == (3, 6)
    assert [(n, note) for n, _, note in s.cycle_grants(job.id)] == [(2, "--more-cycles 2"), (1, None)]
    with pytest.raises(ValueError):
        s.grant_cycles(job.id, 0)


# --- repair cycles and packets -------------------------------------------


def test_packet_counts_a_cycle_assigns_items_and_hash_is_recorded(s):
    job = published_job(s)
    items = add(s, job.id, 2)
    p, created = packet(s, job.id, items)
    assert created and p.cycle == 1 and p.verify()
    assert p.packet_hash == st.content_hash(p.content)
    assert s.get_job(job.id).cycles_used == 1
    assert [i.packet_id for i in s.packet_items(p.id)] == [p.id, p.id]
    assert s.pending_items(job.id) == []
    assert s.open_packet(job.id) == p


def test_recreating_the_same_open_packet_is_idempotent(s):
    job = published_job(s)
    items = add(s, job.id, 1)
    p1, _ = packet(s, job.id, items)
    p2, created = packet(s, job.id, items)
    assert (p2.id, created) == (p1.id, False)
    assert s.get_job(job.id).cycles_used == 1
    more = add(s, job.id, 2)[1:]
    with pytest.raises(Conflict):
        packet(s, job.id, more)


def test_resume_result_marks_items_processed_once(s):
    job = published_job(s)
    a, b = add(s, job.id, 2)
    p, _ = packet(s, job.id, [a, b])
    s.mark_resume_started(p.id)
    assert s.mark_resume_started(p.id).resume_count == 2
    result = {"status": "done", "items": {"c0": "fixed", "c1": "declined"}}
    done, created = s.record_resume_result(p.id, result=result, dispositions={
        a.id: Disposition("fixed", fixed_sha=FIX1), b.id: Disposition("declined", reason="intended behavior")})
    assert created and done.result == result and s.open_packet(job.id) is None
    a2, b2 = s.packet_items(p.id)
    assert (a2.disposition, a2.fixed_sha, a2.processed_at is not None) == ("fixed", FIX1, True)
    assert (b2.disposition, b2.decline_reason) == ("declined", "intended behavior")
    assert s.record_resume_result(p.id, result=result, dispositions={})[1] is False
    with pytest.raises(Conflict):
        s.record_resume_result(p.id, result={"status": "other"}, dispositions={})
    with pytest.raises(Conflict):
        s.mark_resume_started(p.id)


def test_resume_result_validation(s):
    job = published_job(s)
    a, b = add(s, job.id, 2)
    p, _ = packet(s, job.id, [a])
    with pytest.raises(ValueError):
        s.record_resume_result(p.id, result={}, dispositions={b.id: Disposition("declined", reason="x")})
    with pytest.raises(ValueError):
        s.record_resume_result(p.id, result={}, dispositions={a.id: Disposition("fixed")})
    with pytest.raises(ValueError):
        s.record_resume_result(p.id, result={}, dispositions={a.id: Disposition("declined")})
    with pytest.raises(ValueError):
        s.record_resume_result(p.id, result={}, dispositions={a.id: Disposition("ignored", reason="x")})
    assert s.open_packet(job.id).id == p.id
    # No disposition at all: processed, but with none (READY sees the gap).
    s.record_resume_result(p.id, result={}, dispositions={})
    assert s.get_item(a.id).processed_at is not None and s.get_item(a.id).disposition is None


def test_repair_limit_and_more_cycles(s):
    job = published_job(s)
    for n in range(3):
        (item,) = s.add_item(job.id, source="test", item_id=f"{HEAD1}:t{n}", content_hash=digest(str(n)),
                             payload={"n": n})[:1]
        p, _ = s.create_packet(job.id, kind="local", head_sha=HEAD1, item_ids=[item.id], content={"n": n})
        s.record_resume_result(p.id, result={"n": n}, dispositions={})
    (item,) = s.add_item(job.id, source="test", item_id=f"{HEAD1}:t9", content_hash=digest("9"), payload={})[:1]
    with pytest.raises(RepairLimitReached) as e:
        s.create_packet(job.id, kind="local", head_sha=HEAD1, item_ids=[item.id], content={"n": 9})
    assert "--more-cycles" in str(e.value)
    assert s.get_job(job.id).cycles_used == 3
    assert s.get_item(item.id).packet_id is None  # nothing half-written
    s.grant_cycles(job.id, 1)
    p, _ = s.create_packet(job.id, kind="local", head_sha=HEAD1, item_ids=[item.id], content={"n": 9})
    assert p.cycle == 4


def test_packet_item_rules(s):
    job = published_job(s)
    (a,) = add(s, job.id, 1)
    ignored, _ = s.add_item(job.id, source="coderabbit", item_id="walk", content_hash=digest("w"), payload={},
                            admitted=False, ignore_reason="walkthrough")
    with pytest.raises(ValueError):
        packet(s, job.id, [ignored])
    with pytest.raises(ValueError):
        packet(s, job.id, [])
    with pytest.raises(ValueError):
        packet(s, job.id, [a, a])
    with pytest.raises(ValueError):
        s.create_packet(job.id, kind="round", head_sha=HEAD1, item_ids=[9999], content={},
                        round_id=s.open_round(job.id, head_sha=HEAD1, deadline_seconds=60)[0].id)
    with pytest.raises(sqlite3.IntegrityError):  # a round packet needs its round
        s.create_packet(job.id, kind="round", head_sha=HEAD1, item_ids=[a.id], content={})


def test_cycles_used_can_never_exceed_the_limit_in_the_schema(s, db):
    job = published_job(s)
    conn = sqlite3.connect(db)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE jobs SET cycles_used = 4 WHERE id = ?", (job.id,))
    conn.close()


# --- feedback items -------------------------------------------------------


def test_item_insert_is_idempotent(s):
    job = published_job(s)
    kw = dict(source="coderabbit", item_id="2412345", content_hash=digest("fix the bug"), payload={"body": "fix the bug"})
    first, created = s.add_item(job.id, **kw)
    again, created_again = s.add_item(job.id, **kw)
    assert created and not created_again and again == first
    assert len(s.items(job.id)) == 1


def test_edited_comment_is_a_new_item_and_sources_are_separate(s):
    job = published_job(s)
    base = dict(item_id="2412345", payload={})
    a, _ = s.add_item(job.id, source="coderabbit", content_hash=digest("v1"), **base)
    b, created = s.add_item(job.id, source="coderabbit", content_hash=digest("v2"), **base)
    c, created_c = s.add_item(job.id, source="operator", content_hash=digest("v1"), **base)
    assert created and created_c and len({a.id, b.id, c.id}) == 3


def test_item_keys_are_per_job(s):
    job = published_job(s)
    s.set_state(job.id, "READY")
    other = s.create_job("ENG-2", team_key="ENG", repo="o/r")
    kw = dict(source="check", item_id="run-1", content_hash=digest("x"), payload={})
    assert s.add_item(job.id, **kw)[1] and s.add_item(other.id, **kw)[1]


def test_item_validation(s):
    job = published_job(s)
    ok = dict(source="coderabbit", item_id="1", content_hash=digest("x"), payload={})
    for bad in (dict(source="slack"), dict(item_id=""), dict(content_hash="ABC"), dict(head_sha="nope"),
                dict(admitted=False), dict(ignore_reason="why")):
        with pytest.raises(ValueError):
            s.add_item(job.id, **{**ok, **bad})
    ignored, _ = s.add_item(job.id, **{**ok, "admitted": False, "ignore_reason": "resolved thread"})
    assert (ignored.admitted, ignored.ignore_reason) == (False, "resolved thread")
    assert s.pending_items(job.id) == []


# --- Codex review per head, test results per head --------------------------


def test_codex_review_is_recorded_once_per_head(s):
    job = published_job(s)
    r1, created = s.record_review(job.id, head_sha=HEAD1, result={"verdict": "ok", "findings": []})
    r2, created2 = s.record_review(job.id, head_sha=HEAD1, result={"verdict": "different"})
    assert created and not created2 and r2 == r1 and r2.result["verdict"] == "ok"
    assert s.review_for_head(job.id, HEAD2) is None
    assert s.record_review(job.id, head_sha=HEAD2, result={})[1]


def test_test_results_are_keyed_by_head_and_kind(s):
    job = published_job(s)
    base = [CaseResult("tests/test_a.py", "tests.test_a", "test_old", "failed", "AssertionError", "boom")]
    run = s.record_test_run(job.id, head_sha=BASE, kind="base", results=base, exit_code=1, junit_found=True)
    verify = [CaseResult("tests/test_a.py", "tests.test_a", "test_a", "passed"),
              CaseResult("tests/test_a.py", "tests.test_a", "test_b", "skipped")]
    v1 = s.record_test_run(job.id, head_sha=HEAD1, kind="verify", results=verify, exit_code=0, junit_found=True,
                           duration_seconds=12.5)
    v2 = s.record_test_run(job.id, head_sha=HEAD1, kind="verify", results=verify[:1], exit_code=0, junit_found=True)
    assert (run.attempt, v1.attempt, v2.attempt) == (1, 1, 2)
    assert s.latest_test_run(job.id, head_sha=HEAD1, kind="verify") == v2
    assert s.latest_test_run(job.id, head_sha=HEAD1, kind="base") is None
    assert s.test_results(run.id) == base
    assert [r.outcome for r in s.test_results(v1.id)] == ["passed", "skipped"]


def test_test_run_is_all_or_nothing(s):
    job = published_job(s)
    dup = [CaseResult("f", "c", "n", "passed"), CaseResult("f", "c", "n", "failed")]
    with pytest.raises(ValueError):
        s.record_test_run(job.id, head_sha=HEAD1, kind="verify", results=dup, exit_code=1, junit_found=True)
    assert s.latest_test_run(job.id, head_sha=HEAD1, kind="verify") is None
    with pytest.raises(ValueError):
        s.record_test_run(job.id, head_sha=HEAD1, kind="verify", results=[CaseResult("f", "c", "n", "xpass")],
                          exit_code=1, junit_found=True)
    with pytest.raises(ValueError):
        s.record_test_run(job.id, head_sha=HEAD1, kind="lint", results=[], exit_code=1, junit_found=True)


# --- attempts and leases (R9) ---------------------------------------------


class FakeProbe:
    def __init__(self, procs: dict[int, str]):
        self.procs = procs

    def start_time(self, pid: int) -> str | None:
        return self.procs.get(pid)


def test_attempt_records_session_before_launch_and_usage_after(s):
    job = published_job(s, state="WRITER")
    a = s.start_attempt(job.id, role="writer", step="WRITER", session_id="sess-1")
    assert (a.session_id, a.ended_at, a.outcome) == ("sess-1", None, None)
    done = s.finish_attempt(a.id, outcome="done", usage={"input_tokens": 10})
    assert done.usage == {"input_tokens": 10} and done.ended_at is not None
    with pytest.raises(Conflict):
        s.finish_attempt(a.id, outcome="done")
    with pytest.raises(ValueError):
        s.start_attempt(job.id, role="linear", step="WRITER")


def test_lease_liveness_uses_pid_and_start_time(s):
    job = published_job(s, state="WRITER")
    writer = s.acquire_lease(job.id, role="writer", pid=100, proc_start="Mon 01:00:00.123", pgid=100, session_id="sess-1")
    tester = s.acquire_lease(job.id, role="tester", pid=200, proc_start="Mon 00:59:00.000", pgid=200)
    reviewer = s.acquire_lease(job.id, role="reviewer", pid=300, proc_start="Mon 00:58:00.000", pgid=300)
    with pytest.raises(Conflict):
        s.acquire_lease(job.id, role="writer", pid=101, proc_start="x", pgid=101)
    # 100 alive with the same start; 200 reused by another process; 300 gone.
    alive, dead = s.alive_leases(job.id, FakeProbe({100: "Mon 01:00:00.123", 200: "Tue 09:00:00.000"}))
    assert [l.id for l in alive] == [writer.id]
    assert sorted(l.id for l in dead) == [tester.id, reviewer.id]
    assert all(l.release_reason == "dead" for l in dead)
    assert [l.id for l in s.live_leases(job.id)] == [writer.id]
    released = s.release_lease(writer.id, "sigterm")
    assert released.release_reason == "sigterm"
    assert s.release_lease(writer.id, "exited").release_reason == "sigterm"  # first release wins
    assert s.acquire_lease(job.id, role="writer", pid=102, proc_start="y", pgid=102).pid == 102


# --- CLI evidence (R2) and doctor (R18) -----------------------------------


def test_cli_evidence_per_job_refuses_unredacted_email(s):
    job = published_job(s, state="PREFLIGHT")
    auth = {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty", "email": "<redacted>"}
    ev = s.record_cli_evidence(job.id, claude_version="2.1.304", codex_version="0.160.0", claude_auth=auth,
                               codex_auth="Logged in using ChatGPT", passed=True, extra={"skills": {"a": digest("a")}})
    assert s.cli_evidence(job.id) == [ev] and ev.claude_auth == auth and ev.passed
    with pytest.raises(ValueError):
        s.record_cli_evidence(job.id, claude_version="1", codex_version="1", claude_auth={"email": "mark@example.com"},
                              codex_auth="", passed=True)


SRT = "0.0.79"


def test_doctor_runs_when_either_version_differs_from_the_last_pass(s):
    assert s.doctor_needed("2.1.304", "0.160.0", srt_version=SRT)
    s.record_doctor_run(claude_version="2.1.304", codex_version="0.160.0", srt_version=SRT, passed=True,
                        results={"probes": 12})
    assert not s.doctor_needed("2.1.304", "0.160.0", srt_version=SRT)
    assert s.doctor_needed("2.1.305", "0.160.0", srt_version=SRT)
    assert s.doctor_needed("2.1.304", "0.161.0", srt_version=SRT)
    s.record_doctor_run(claude_version="2.1.305", codex_version="0.160.0", srt_version=SRT, passed=False,
                        results={"failed": "B4"})
    assert s.doctor_needed("2.1.305", "0.160.0", srt_version=SRT)  # a failure is not a pass
    assert s.last_doctor_pass().claude_version == "2.1.304"
    s.record_doctor_run(claude_version="2.1.305", codex_version="0.160.0", srt_version=SRT, passed=True, results={})
    assert s.doctor_needed("2.1.304", "0.160.0", srt_version=SRT)  # compared with the *last* pass only


def test_doctor_reruns_when_the_srt_pin_changes(s):
    """R31: a changed sandbox-runtime pin triggers a doctor rerun."""
    s.record_doctor_run(claude_version="2.1.304", codex_version="0.160.0", srt_version=SRT, passed=True, results={})
    assert not s.doctor_needed("2.1.304", "0.160.0", srt_version=SRT)
    assert s.doctor_needed("2.1.304", "0.160.0", srt_version="0.0.80")
    s.record_doctor_run(claude_version="2.1.304", codex_version="0.160.0", srt_version="0.0.80", passed=True, results={})
    assert not s.doctor_needed("2.1.304", "0.160.0", srt_version="0.0.80")
    assert s.doctor_needed("2.1.304", "0.160.0", srt_version=SRT)
    assert s.last_doctor_pass().srt_version == "0.0.80"
    for bad in ("", "  ", None):
        with pytest.raises(ValueError, match="srt_version"):
            s.doctor_needed("2.1.304", "0.160.0", srt_version=bad)
        with pytest.raises(ValueError, match="srt_version"):
            s.record_doctor_run(claude_version="2.1.304", codex_version="0.160.0", srt_version=bad, passed=True,
                                results={})


def test_doctor_pass_from_before_the_srt_key_reruns_doctor(db, clock):
    """A v1 database's doctor pass has no srt version: after migrating, doctor runs again."""
    with Store(db, clock=clock, migrations=st.MIGRATIONS[:1]) as old:
        old._conn.execute("INSERT INTO doctor_runs (claude_version, codex_version, ran_at, passed, results)"
                          " VALUES ('2.1.304', '0.160.0', 1.0, 1, '{}')")
        assert old.schema_version == 1
    with Store(db, clock=clock) as s:
        assert s.schema_version == st.SCHEMA_VERSION == 2
        last = s.last_doctor_pass()
        assert last.claude_version == "2.1.304" and last.srt_version is None
        assert s.doctor_needed("2.1.304", "0.160.0", srt_version=SRT)


# --- rounds and supervised time (R8, R17, R25) ------------------------------


def test_rounds_are_keyed_repo_pr_head_seq(s):
    job = published_job(s)
    r1, created = s.open_round(job.id, head_sha=HEAD1, deadline_seconds=1800)
    assert created and (r1.repo, r1.pr_number, r1.head_sha, r1.seq) == ("mfullbrook/app", 7, HEAD1, 1)
    same, created = s.open_round(job.id, head_sha=HEAD1, deadline_seconds=1800)
    assert (same.id, created) == (r1.id, False)
    with pytest.raises(Conflict):
        s.open_round(job.id, head_sha=HEAD2, deadline_seconds=1800)
    s.close_round(r1.id, outcome="timed_out")
    r2, _ = s.open_round(job.id, head_sha=HEAD1, deadline_seconds=1800)  # rerun after review_timeout
    assert r2.seq == 2 and r2.supervised_seconds == 0
    s.close_round(r2.id, outcome="superseded")
    r3, _ = s.open_round(job.id, head_sha=HEAD2, deadline_seconds=1800)
    assert r3.seq == 1
    assert st.round_marker(job, r3) == f"gaffer-round: ENG-1/{HEAD2}/1"


def test_close_round_rules(s):
    job = published_job(s)
    r, _ = s.open_round(job.id, head_sha=HEAD1, deadline_seconds=60)
    with pytest.raises(ValueError):
        s.close_round(r.id, outcome="complete", coderabbit="unverified")
    closed = s.close_round(r.id, outcome="complete", coderabbit="unverified", coderabbit_note="Review paused")
    assert (closed.outcome, closed.coderabbit, closed.coderabbit_note) == ("complete", "unverified", "Review paused")
    assert s.close_round(r.id, outcome="complete") == closed
    with pytest.raises(Conflict):
        s.close_round(r.id, outcome="timed_out")
    with pytest.raises(Conflict):
        s.supervise(r.id)


def test_round_needs_a_pr(s):
    job = s.create_job("ENG-1", team_key="ENG", repo="o/r")
    with pytest.raises(Conflict):
        s.open_round(job.id, head_sha=HEAD1, deadline_seconds=60)


def test_supervised_seconds_count_monotonic_time(s, clock):
    job = published_job(s)
    r, _ = s.open_round(job.id, head_sha=HEAD1, deadline_seconds=1800)
    sup = s.supervise(r.id)
    clock.advance(60)
    t = sup.tick()
    assert (t.supervised_seconds, t.remaining_seconds, t.expired, t.sleep_gap_seconds) == (60, 1740, False, None)
    clock.advance(61.5)
    assert sup.tick().supervised_seconds == 121.5
    assert s.get_round(r.id).supervised_seconds == 121.5  # persisted


def test_sleep_is_not_supervised_and_gap_over_two_minutes_is_recorded(s, clock):
    job = published_job(s)
    r, _ = s.open_round(job.id, head_sha=HEAD1, deadline_seconds=1800)
    sup = s.supervise(r.id)
    clock.advance(30)
    clock.sleep(3600)  # lid closed for an hour
    clock.advance(30)
    t = sup.tick()
    assert t.supervised_seconds == 60 and t.sleep_gap_seconds == 3600
    (gap,) = s.events(job.id, "sleep_gap")
    assert gap.round_id == r.id and gap.detail["gap_seconds"] == 3600 and gap.detail["monotonic_seconds"] == 60
    # A short wall/monotonic skew (NTP, a 2-minute nap) is not a recorded gap.
    clock.sleep(120)
    clock.advance(10)
    assert sup.tick().sleep_gap_seconds is None
    assert len(s.events(job.id, "sleep_gap")) == 1
    assert s.get_round(r.id).wall_elapsed(clock.wall()) == 30 + 3600 + 30 + 120 + 10


def test_time_while_gaffer_is_stopped_is_not_counted(db, clock):
    with Store(db, clock=clock) as s:
        job = published_job(s)
        r, _ = s.open_round(job.id, head_sha=HEAD1, deadline_seconds=1800)
        sup = s.supervise(r.id)
        clock.advance(600)
        sup.tick()
        clock.advance(45)  # worked 45 s more, then crashed before the next tick
    clock.reboot(downtime=7200)  # two hours down; monotonic clock restarts
    with Store(db, clock=clock) as s:
        r, created = s.open_round(job.id, head_sha=HEAD1, deadline_seconds=1800)  # rerun resumes the round
        assert not created and r.supervised_seconds == 600
        sup = s.supervise(r.id)
        clock.advance(60)
        t = sup.tick()
        assert t.supervised_seconds == 660 and t.remaining_seconds == 1140
        assert t.sleep_gap_seconds is None  # baseline is per process: downtime is not a sleep gap


def test_round_expires_at_the_supervised_deadline(s, clock):
    job = published_job(s)
    r, _ = s.open_round(job.id, head_sha=HEAD1, deadline_seconds=1800)
    sup = s.supervise(r.id)
    for _ in range(29):
        clock.advance(60)
        assert not sup.tick().expired
    clock.advance(59.9)
    assert not sup.tick().expired
    clock.advance(0.1)
    t = sup.tick()
    assert t.expired and t.remaining_seconds == 0
    clock.advance(30)
    t = sup.tick()
    assert t.supervised_seconds == 1830 and t.remaining_seconds == 0


def test_monotonic_never_credits_negative_time(s, clock):
    job = published_job(s)
    r, _ = s.open_round(job.id, head_sha=HEAD1, deadline_seconds=1800)
    sup = s.supervise(r.id)
    clock._mono -= 50
    assert sup.tick().supervised_seconds == 0


def test_failed_tick_is_retried_not_lost(db, clock):
    calls = []

    def fail_once(op):
        if op == "tick_round" and not calls:
            calls.append(op)
            raise OSError("disk hiccup")

    with Store(db, clock=clock, before_commit=fail_once) as s:
        job = published_job(s)
        r, _ = s.open_round(job.id, head_sha=HEAD1, deadline_seconds=1800)
        sup = s.supervise(r.id)
        clock.advance(60)
        with pytest.raises(OSError):
            sup.tick()
        assert s.get_round(r.id).supervised_seconds == 0
        clock.advance(60)
        assert sup.tick().supervised_seconds == 120


# --- state_lost (R9) --------------------------------------------------------


def test_branch_helpers():
    assert st.branch_name("ENG-1") == "gaffer/ENG-1" and st.branch_name("ENG-1", 3) == "gaffer/ENG-1-3"
    assert st.branch_generation("ENG-1", "gaffer/ENG-1") == 1
    assert st.branch_generation("ENG-1", "gaffer/ENG-1-2") == 2
    assert st.branch_generation("ENG-1", "gaffer/ENG-12") is None
    assert st.branch_generation("ENG-1", "gaffer/ENG-1-1") is None
    assert st.branch_generation("ENG-1", "gaffer/ENG-1-02") is None
    assert st.branch_generation("ENG-1", "feature/ENG-1") is None


def test_no_remote_branch_is_not_state_lost(s):
    assert s.check_state_lost("ENG-1", []) is None
    assert s.check_state_lost("ENG-1", [RemoteBranch("gaffer/ENG-12", 3), RemoteBranch("main")]) is None


def test_known_branches_are_not_state_lost(s):
    job = published_job(s)
    assert s.check_state_lost("ENG-1", [RemoteBranch("gaffer/ENG-1", 7)]) is None
    s.set_state(job.id, "CLOSED")
    s.archive_job(job.id)
    s.create_job("ENG-1", team_key="ENG", repo="mfullbrook/app")
    assert s.check_state_lost("ENG-1", [RemoteBranch("gaffer/ENG-1", 7), RemoteBranch("gaffer/ENG-1-2")]) is None


def test_lost_db_with_existing_branch_and_pr_is_state_lost(db, clock):
    with Store(db, clock=clock) as s:
        published_job(s)
    for suffix in ("", "-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)
    with Store(db, clock=clock) as s:
        assert s.created
        remote = [RemoteBranch("gaffer/ENG-1", 7)]
        lost = s.check_state_lost("ENG-1", remote)
        assert lost == st.StateLost("ENG-1", tuple(remote))
        job = s.record_state_lost(lost, team_key="ENG", repo="mfullbrook/app")
        assert (job.state, job.blocked_reason, job.pr_number, job.branch, job.active) == (
            "BLOCKED", "state_lost", 7, "gaffer/ENG-1", False)
        assert "--adopt-head" in job.blocked_message
        assert s.check_state_lost("ENG-1", remote) is None
        with pytest.raises(LiveJobExists):
            s.record_state_lost(lost, team_key="ENG", repo="mfullbrook/app")

        rebuilt = s.reconstruct_job(job.id, head_sha=HEAD2, base_sha=BASE, tester_commit_sha=TESTER,
                                    clone_path="/jobs/ENG-1")
        assert rebuilt.reconstructed and rebuilt.writer_session_id is None
        assert (rebuilt.head_sha, rebuilt.base_sha, rebuilt.tester_commit_sha) == (HEAD2, BASE, TESTER)
        assert s.set_state(job.id, "ROUND").reconstructed
        assert [e.kind for e in s.events(job.id)] == ["state_lost", "reconstructed"]


def test_state_lost_picks_the_newest_generation(s):
    lost = s.check_state_lost("ENG-1", [RemoteBranch("gaffer/ENG-1", 3), RemoteBranch("gaffer/ENG-1-2", 9)])
    job = s.record_state_lost(lost, team_key="ENG", repo="o/r")
    assert (job.generation, job.branch, job.pr_number, job.ref) == (2, "gaffer/ENG-1-2", 9, "ENG-1-2")


def test_reconstruct_needs_state_lost(s):
    job = published_job(s)
    with pytest.raises(Conflict):
        s.reconstruct_job(job.id, head_sha=HEAD2, base_sha=BASE, tester_commit_sha=TESTER)


# --- crash at any step leaves consistent state ------------------------------
#
# Each write runs in a child Python process that is SIGKILLed either inside
# the transaction just before COMMIT, or right after the write returns. The
# reopened DB must pass integrity_check and hold exactly the state before
# (killed before commit) or a changed state (killed after).


def _setup_job(s):
    published_job(s)


def _setup_round(s):
    job = published_job(s)
    s.open_round(job.id, head_sha=HEAD1, deadline_seconds=1800)


def _setup_items(s):
    job = published_job(s)
    rnd, _ = s.open_round(job.id, head_sha=HEAD1, deadline_seconds=1800)
    add(s, job.id, 2, round_id=rnd.id)


def _setup_packet(s):
    _setup_items(s)
    job = s.job_for_issue("ENG-1")
    packet(s, job.id, s.pending_items(job.id), round_id=s.open_round_for(job.id).id)


def _setup_lease(s):
    job = published_job(s, state="WRITER")
    a = s.start_attempt(job.id, role="writer", step="WRITER", session_id="sess")
    s.acquire_lease(job.id, role="writer", pid=4242, proc_start="t0", pgid=4242, attempt_id=a.id)


def _setup_closed(s):
    job = published_job(s)
    s.set_state(job.id, "CLOSED")


def _setup_state_lost(s):
    lost = s.check_state_lost("ENG-1", [RemoteBranch("gaffer/ENG-1", 7)])
    s.record_state_lost(lost, team_key="ENG", repo="mfullbrook/app")


def _job(s):
    return s.job_for_issue("ENG-1")


def _tick(s):
    sup = s.supervise(s.open_round_for(_job(s).id).id)
    s.clock.advance(30)
    s.clock.sleep(600)
    sup.tick()


CRASH_OPS = {
    "migrate": (None, lambda s: None),
    "migrate_v2": (None, lambda s: None),  # v1 -> v2 (R31 doctor srt_version)
    "create_job": (None, lambda s: s.create_job("ENG-1", team_key="ENG", repo="o/r")),
    "set_state": (_setup_job, lambda s: s.set_state(_job(s).id, "PACKET")),
    "block": (_setup_job, lambda s: s.block(_job(s).id, "review_timeout", "30 min supervised")),
    "set_workspace": (_setup_job, lambda s: s.set_workspace(_job(s).id, clone_path="/x", base_sha=H[7])),
    "set_criterion_map": (_setup_job, lambda s: s.set_criterion_map(_job(s).id, {"AC2": []})),
    "set_tester_commit": (_setup_job, lambda s: s.set_tester_commit(_job(s).id, H[7], {"AC9": []})),
    "set_writer_session": (_setup_job, lambda s: s.set_writer_session(_job(s).id, "new-session")),
    "set_pr": (lambda s: s.create_job("ENG-1", team_key="ENG", repo="o/r"), lambda s: s.set_pr(_job(s).id, 9)),
    "set_head": (_setup_job, lambda s: s.set_head(_job(s).id, H[7])),
    "adopt_head": (_setup_job, lambda s: s.adopt_head(_job(s).id, head_sha=H[7], base_sha=H[8])),
    "archive_job": (_setup_closed, lambda s: s.archive_job(_job(s).id)),
    "grant_cycles": (_setup_job, lambda s: s.grant_cycles(_job(s).id, 2, "--more-cycles 2")),
    "record_state_lost": (None, lambda s: s.record_state_lost(
        s.check_state_lost("ENG-1", [RemoteBranch("gaffer/ENG-1", 7)]), team_key="ENG", repo="o/r")),
    "reconstruct_job": (_setup_state_lost, lambda s: s.reconstruct_job(
        _job(s).id, head_sha=HEAD2, base_sha=BASE, tester_commit_sha=TESTER)),
    "record_cli_evidence": (_setup_job, lambda s: s.record_cli_evidence(
        _job(s).id, claude_version="2.1.304", codex_version="0.160.0", claude_auth={"authMethod": "claude.ai"},
        codex_auth="Logged in using ChatGPT", passed=True)),
    "record_doctor_run": (None, lambda s: s.record_doctor_run(
        claude_version="2.1.304", codex_version="0.160.0", srt_version="0.0.79", passed=True, results={"ok": 1})),
    "start_attempt": (_setup_job, lambda s: s.start_attempt(_job(s).id, role="reviewer", step="ROUND")),
    "finish_attempt": (_setup_lease, lambda s: s.finish_attempt(s.attempts(_job(s).id)[0].id, outcome="done",
                                                                usage={"tokens": 5})),
    "acquire_lease": (_setup_job, lambda s: s.acquire_lease(_job(s).id, role="reviewer", pid=77, proc_start="t",
                                                            pgid=77)),
    "release_lease": (_setup_lease, lambda s: s.release_lease(s.live_leases(_job(s).id)[0].id, "sigterm")),
    "record_test_run": (_setup_job, lambda s: s.record_test_run(
        _job(s).id, head_sha=HEAD1, kind="verify", exit_code=1, junit_found=True,
        results=[CaseResult("t.py", "t", f"test_{i}", "failed" if i % 2 else "passed") for i in range(50)])),
    "record_review": (_setup_job, lambda s: s.record_review(_job(s).id, head_sha=HEAD1, result={"verdict": "ok"})),
    "open_round": (_setup_job, lambda s: s.open_round(_job(s).id, head_sha=HEAD1, deadline_seconds=1800)),
    "close_round": (_setup_round, lambda s: s.close_round(s.open_round_for(_job(s).id).id, outcome="complete",
                                                          coderabbit="verified")),
    "tick_round": (_setup_round, _tick),
    "add_item": (_setup_round, lambda s: s.add_item(_job(s).id, source="operator", item_id="99",
                                                    content_hash=digest("please"), payload={"body": "please"})),
    "create_packet": (_setup_items, lambda s: packet(s, _job(s).id, s.pending_items(_job(s).id),
                                                     round_id=s.open_round_for(_job(s).id).id)),
    "mark_resume_started": (_setup_packet, lambda s: s.mark_resume_started(s.open_packet(_job(s).id).id)),
    "record_resume_result": (_setup_packet, lambda s: s.record_resume_result(
        s.open_packet(_job(s).id).id, result={"status": "done"},
        dispositions={i.id: Disposition("fixed", fixed_sha=FIX1) for i in s.packet_items(s.open_packet(_job(s).id).id)})),
    "log_event": (_setup_job, lambda s: s.log_event(_job(s).id, "writer_sigterm", {"pgid": 4242})),
}

# Ops whose transaction has a different name from the op.
_TX_NAME = {"migrate": "migrate_1", "migrate_v2": "migrate_2", "tick_round": "tick_round"}


def _child_main(db: str, op: str, mode: str) -> None:
    """Runs in the child process: perform `op`, then die by SIGKILL."""

    def kill(name: str) -> None:
        if name == _TX_NAME.get(op, op):
            os.kill(os.getpid(), signal.SIGKILL)

    store = Store(db, clock=FakeClock(wall=1_900_000_000.0, mono=50.0),
                  before_commit=kill if mode == "before_commit" else None)
    CRASH_OPS[op][1](store)
    os.kill(os.getpid(), signal.SIGKILL)


def _run_child(db: Path, op: str, mode: str) -> None:
    code = (f"import sys; sys.path.insert(0, {str(TESTS_DIR)!r}); import test_store; "
            f"test_store._child_main({str(db)!r}, {op!r}, {mode!r})")
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert proc.returncode == -signal.SIGKILL, proc.stderr


@pytest.mark.parametrize("op", sorted(CRASH_OPS))
def test_crash_and_reopen(op, db, clock):
    setup, _ = CRASH_OPS[op]
    if op == "migrate_v2":
        Store(db, clock=clock, migrations=st.MIGRATIONS[:1]).close()
    elif op != "migrate":
        with Store(db, clock=clock) as s:
            if setup:
                setup(s)
    before = dump(db) if db.exists() else None

    _run_child(db, op, "before_commit")
    if before is None:  # crashed while creating the schema
        assert dump(db)["user_version"] == 0
    else:
        assert dump(db) == before
    with Store(db, clock=clock) as s:  # reopens cleanly and stays usable
        assert s.schema_version == st.SCHEMA_VERSION
    before = dump(db)

    if op in ("migrate", "migrate_v2"):
        return
    _run_child(db, op, "after_commit")
    after = dump(db)
    assert after != before
    with Store(db, clock=clock):
        pass


def test_crash_op_table_covers_every_write():
    source = Path(st.__file__).read_text()
    tx_names = set(re.findall(r'_tx\("([a-z_]+)"\)', source)) | set(re.findall(r'_set\(job_id, "([a-z_]+)"', source))
    covered = {_TX_NAME.get(op, op) for op in CRASH_OPS}
    assert tx_names - covered == set()
