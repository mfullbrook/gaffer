# TODOS

## Scheduler

### Approach B: capacity scheduler daemon (the night shift)

**What:** Turn the v0.1 hand-run job runner into an always-on scheduler: a quota-window model built from measured usage, job admission that reserves tester, reviewer and repair budget, an overnight interval with daytime headroom, `agent-ready` pickup from Linear, launchd supervision, the Jasper/Telegram notifier, Linear write-back (deferred from v0.1 by eng review D1), and a two-job limit.

**Why:** It's the version picked as the "whoa" in office hours: spend use-it-or-lose-it subscription capacity while you sleep and keep headroom for the day. v0.1 proves the job; B makes it run unattended.

**Context:** Deferred on purpose in office hours (Approach A first) and in eng review (D1, T2). The Claude subscription is Pro, so the capacity model needs real per-job quota numbers from Experiment 1 and the v0.1 runs; don't model windows from guesses. The design's Approach B section and eng-review ledger (`docs/designs/gaffer-overnight-pair.md`) hold the reasoning. Start with its own /office-hours and /plan-eng-review.

**Effort:** XL
**Priority:** P1
**Depends on:** v0.1 demo passing SC2-SC5; Experiment 1 quota-per-job data

## Runner

### `gaffer stop <issue>`

**What:** A command that sends SIGTERM to the job's recorded process groups, SIGKILL after 30 s, releases the job and run locks, and marks the job BLOCKED: stopped.

**Why:** Children run in their own process groups, so Ctrl-C on gaffer doesn't stop them, and a rerun refuses while a child is alive. Today aborting a job means killing recorded process groups by hand.

**Context:** From spec review R2-30 and eng review T1 (D32). Process groups, PIDs and start times are recorded per child under eng review R9. Add a row for it to the table-driven `test_runner_states.py`.

**Effort:** S
**Priority:** P2
**Depends on:** v0.1 runner (R9 locks and liveness)

## Completed
