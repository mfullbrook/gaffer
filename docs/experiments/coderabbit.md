# Experiment 2: CodeRabbit fixtures (T2)

Run on 2026-10-18 (Phase 1, read-only harvest) and 2026-10-19/20 (Phase 2, live sandbox capture) from Mark's Mac, branch `gaffer-v0.1-design`, with `gh` logged in as `mfullbrook`. In scope: Open Question 1, R12 (feedback admission and PR opening), the round-marker row of R9, the timeouts in R11 and the round rules, and the CI-evidence path in R13.

**Questions:**
- Is there a reliable per-head-SHA signal that a CodeRabbit review is complete?
- Does each R12 admission rule hold against CodeRabbit's real traffic?
- What does CodeRabbit do with a draft PR?

**Verdicts**

| Question | Verdict | Basis |
|---|---|---|
| Open Question 1: completion signal | **Verified signal**: a commit status on the head SHA with context `CodeRabbit`, state `success` and description exactly `Review completed`. The fallback stays only for a missing or non-`Review completed` status. | 23 Phase 1 heads plus 3 sandbox heads. On all 21 heads that reached `Review completed`, no finding arrived after it. On the 16 heads that had findings, the last one came 4–8 s before the status. `success` is also used for `Review skipped…`, `Review paused` and `Review approved`, so the state alone is not enough. |
| R12 admission rules | **6 confirmed, 3 corrected, 1 new rule** | See [section 2](#2-r12-admission-rules). The corrections are: replies inside threads, deduplication of outside-diff entries, and excluding gaffer's own round comment, which carries Mark's author login. The new rule is to ignore CodeRabbit's edits to the PR body. |
| Draft PRs | **Skipped, with a misleading `success`** | A draft gets status `success` / `Review skipped: draft pull request` and a "Review skipped" comment, but no review. After `gh pr ready`, the status goes pending within 5 s and the review completes in 131 s. R12's "open PRs ready for review" is confirmed. |
| R9 round marker | **Confirmed** | `gaffer-round: exp2/<head>/1` came back byte-identical through REST and GraphQL. CodeRabbit didn't reply or react to it within 26 minutes. |
| R13 CI evidence | **Confirmed, with notes** | gaffer's token fetched the annotations and the job log. The failure text is only in the log. `gh api` needs `--allow-escape-sequences`, and the 200-line tail of a toy job is already 15.5 KB. |
| Timings | 60 s settle **OK**. 30 min round timeout **OK** (worst head was 14.2 min). 10 min `check_appear_timeout` **OK** for CI, but CodeRabbit needs its own "status never appeared" rule. | [Section 4](#4-timings) |

Nothing here edits the ledger. The proposed corrections are collected in [section 8](#8-proposed-ledger-corrections).

## 1. Completion signal (Open Question 1)

### Candidates examined

| Candidate | Result | Why |
|---|---|---|
| Check run | **Rejected** | CodeRabbit posts no check runs. GitHub creates an empty `coderabbitai` check suite on every head (status `queued`, 0 runs), and it never changes. Its presence only shows that the app covers the repo. |
| Commit status `CodeRabbit` | **Chosen** | Present on every reviewed head. Creator is `coderabbitai[bot]`. Its last state is the last thing CodeRabbit does for a head. |
| Review with `commit_id` = head | **Rejected** | Missing on heads with nothing to say: 2 Phase 1 heads ended `Review completed` with no review object. CodeRabbit also creates one-comment reviews for every reply inside a thread, and those can come long after the round. The review-body header "Actionable comments posted: N" is missing from reviews whose only findings are in the body (sandbox round 3). |
| Walkthrough edit naming the SHA | **Corroborating only** | The summary comment carries the HTML markers `change_assessment_commit:"<sha>"` and `final_review_risk_coverage:{"sourceCommitId","coveredCommitId","kind":"reviewed"}`. Each head's final edit names that head 2–3 s before the status flips (checked on the 3 sandbox heads and one Phase 1 head). On paused heads, `coveredCommitId` stays pinned to the last reviewed head. These markers are undocumented HTML comments, so gaffer can use them as a cross-check but shouldn't depend on them. |

### Status descriptions seen

| state | description | Count (P1 + sandbox) | Meaning for gaffer |
|---|---|---|---|
| pending | `Review in progress` | 23 + 3 | Not complete. On 2 superseded heads it stayed pending forever, because a new push cancels the review in progress. |
| success | `Review completed` | 18 + 3 | **Complete** |
| success | `Review approved` | 3 + 0 | Not complete yet. It always came ~2 s before `Review completed` on the same head. |
| success | `Review skipped` / `Review skipped: draft pull request` | 1 + 1 | Draft; nothing was reviewed. The wording changed between May and October 2026. |
| success | `Review paused` | 2 + 0 | Nothing was reviewed. It appeared on a long PR's next two pushes after 5 completed reviews, which fits an auto-pause after 5 reviewed commits (the org setting wasn't checked). |

Rule for gaffer: completion for head H means `GET /repos/{o}/{r}/commits/{H}/statuses` holds an entry with `context == "CodeRabbit"`, `creator.login == "coderabbitai[bot]"`, `state == "success"` and `description == "Review completed"`. Any other description is "not reviewed" and should be named in the report. Don't treat it as an empty review.

### Caveats

- **The descriptions are English strings that CodeRabbit can reword.** "Review skipped" became "Review skipped: draft pull request". Match `Review completed` exactly and treat anything unrecognized as not complete. Log it verbatim so a wording change shows up as `unverified`, not as a false READY.
- **`Review completed` doesn't mean every file was reviewed.** One Phase 1 head (P6) completed with a body section "Files not reviewed due to moderation or processing errors". gaffer should surface that section in the report.
- **The repo must be covered by the app install.** The first sandbox head got no status and no check suite, because the app was installed for selected repos only. Once Mark added the repo, the next push was reviewed. With the per-head signal, gaffer can tell "not covered" from "slow" by the missing `coderabbitai` check suite.
- **Paused reviews will hit gaffer.** Up to 3 repair cycles plus `--more-cycles` means 4+ reviewed heads per job, and the long Phase 1 PR paused after 5 completed reviews. Paused, skipped and "never appeared" each need a defined outcome ([section 8](#8-proposed-ledger-corrections)).

## 2. R12 admission rules

Each rule is checked against six Phase 1 PRs from three private repos (labelled P1–P6 here; the mapping is kept outside the repo) and against the sandbox capture. Phase 1 patterns are described in words only.

| R12 rule | Verdict | Evidence | Fixture |
|---|---|---|---|
| Admit CodeRabbit inline review comments on unresolved, non-outdated threads | **Corrected**: top-level comments only | Sandbox round 1 posted 3 top-level comments, each opening its own thread, one per seeded bug. In Phase 1, CodeRabbit also posted **30 replies inside threads**, every one answering a plain (non-@mention) reply by Mark in one of its threads. They arrive 9 s to 18 min after Mark's reply, whatever the round state (27 of 30 came after a `Review completed`), on threads that are still unresolved, so under the current wording each would open round `seq+1`. D2 means gaffer itself posts no thread replies, but Mark might. Proposal: a CodeRabbit comment with `in_reply_to_id` is context for its thread, not a new item. | `inline_finding_open_thread` |
| Admit review bodies only for listed actionable sections | **Confirmed, with a deduplication correction** | Actionable sections: `⚠️ Outside diff range comments (N)` (inside a `> [!CAUTION]` block; each entry is a `<details>` with severity · title · `path:lines`) and `🧹 Nitpick comments (N)` (Phase 1 only). Every other section is boilerplate: prompts for AI agents, review info, run configuration, commits, and files selected, skipped or ignored. Two corrections: (1) Sandbox round 2 restated the still-open off-by-one thread as an outside-diff entry because the push didn't touch that line, so the same finding would be admitted twice. Dedupe outside-diff entries against open CodeRabbit threads on the same path and overlapping lines. (2) Sandbox round 3's only finding was body-only, and that review had **no** "Actionable comments posted" line, so parse the sections and not the header. | `review_body_actionable` |
| Admit comments by Mark | **Corrected**: exclude gaffer's own comments | gaffer posts with Mark's token, so its round summary's author is `mfullbrook`, the same login as a real Mark comment. Admit Mark's comments **unless a line matches `^gaffer-round: `** (and the PR body's `gaffer-job:` line). The plain sandbox comment is admitted. | `mark_comment`, `round_marker_comment` |
| Ignore walkthrough/summary and status comments | **Confirmed** | There is exactly one CodeRabbit summary comment per PR, edited in place: 8–17 edits per Phase 1 PR and 10 on the sandbox. On a draft it starts as the "Review skipped" comment and later *becomes* the walkthrough. Each push produces the same three-edit pattern: a "Currently processing new changes" banner within ~10 s, the final walkthrough, then the coverage marker 6–8 s later. "Status comments" are banner versions of that one comment, not separate comments. Ignoring every CodeRabbit issue-comment version is safe; no finding ever appeared only in it. | `walkthrough_edit`, `status_edit` |
| **New:** ignore CodeRabbit's edits to the PR body | **New rule** | CodeRabbit appends a "Summary by CodeRabbit" block to the PR body between `<!-- This is an auto-generated comment: release notes by coderabbit.ai -->` markers and rewrites it on every push (3 edits on the sandbox). Mark's text above it, including the `gaffer-job: exp2` line, was left untouched. The PR-body hash is not a stable fingerprint, and R23's evidence record should read only the text outside those markers. | `walkthrough_edit` (`pull_body_edits.graphql.json`) |
| Ignore edits that only change status or resolution markers (hash over a normalized body) | **Confirmed** | On a fix, CodeRabbit edits its *original* inline comment, 13–50 s after the push (12–13 s on the sandbox): it appends `✅ Addressed in commit <sha7>` (also `Addressed in commits <a> to <b>`, or `✅ Confirmed as addressed by @user` after a human reply). It also flips its trailing HTML marker from `auto-generated comment` to `auto-generated reply` when a human replies in the thread. No other kind of inline-comment edit appeared across 32 edited comments (30 in Phase 1, 2 on the sandbox). Removing those lines before hashing makes the sandbox before/after hashes equal; the raw hashes differ. The regex is in the fixture. | `resolution_marker_edit` |
| Ignore resolved threads | **Confirmed** | CodeRabbit resolves its own threads when it judges them addressed: 30 of 32 Phase 1 threads, and 2 of 2 sandbox fixes. On the sandbox the thread was resolved within 9 s of the push; Phase 1 data has no resolution timestamps. Mark resolving a thread with an unfixed finding (sandbox) drew no reaction, and CodeRabbit didn't reopen it. | `resolved_thread` |
| Ignore outdated comments (context only) | **Confirmed** | Rewriting the lines under a finding without fixing it made the thread outdated (REST `line` became `null`). CodeRabbit raised the same finding again as a new thread on the new lines in the next round, so ignoring outdated threads loses nothing. | `outdated_comment` |
| Ignore comments by anyone else | **Confirmed** (synthetic fixture) | No third party can comment on the private sandbox. In Phase 1 the only other author was a deploy-preview bot that edited its single comment 8–13 times; the author rule drops it correctly. | `stranger_comment` (`"synthetic": true`) |
| Open PRs ready for review | **Confirmed** | See section 3. | `draft_pr` |

**Replies to Mark's comments.** CodeRabbit never replied to or reacted to a top-level PR comment without an @mention. That held for the 4 summary-style comments Mark had posted in Phase 1 and for both sandbox comments. It did reply to the one top-level comment that @mentioned it, and to every non-@mention reply Mark made **inside one of its threads**. So D2's single top-level round comment is safe. Any human reply inside a thread will draw a CodeRabbit reply.

## 3. Draft behavior

- **Phase 1 (P1, May 2026):** a draft PR got pending `Review in progress`, then within 2 s `success` / `Review skipped`, and a comment saying "Review skipped — Draft detected", with no review.
- **Sandbox (October 2026):** a draft head got `success` / `Review skipped: draft pull request` at once, with no pending, plus the skip comment. Nothing else happened during the 10 minutes it stayed a draft.
- After `gh pr ready`, it went pending within 5 s, the skip comment was rewritten into the walkthrough, and `Review completed` arrived 131 s later with 3 inline findings.

The skip shows up as a `success` status, so a draft PR would look "complete" to a state-only check. The exact-description rule in section 1 handles that.

## 4. Timings

All times are measured from the push (Phase 1: the earliest check-suite `created_at` on the head; sandbox: `git push` return time) or from `gh pr ready`.

| Interval | Phase 1 (n) | Sandbox |
|---|---|---|
| push → `CodeRabbit` pending | min 4 s, median 8.5 s, max 118 s (22) | 5 s, 8 s, 8 s |
| push → `Review completed` | min 136 s, median 319 s, max 852 s (17) | 131 s, 186 s, 110 s |
| last finding → `Review completed` | 4–8 s (13 heads with findings); none after on any of 18 | 6 s on all 3 |
| push → "Addressed" edit | 13–50 s (25 edits) | 12–13 s; self-resolve within 9 s |
| push → CI check run visible | (not measured) | within the first 30 s poll |
| Mark's thread reply → CodeRabbit reply | 9 s–18 min, median 7 min (30) | none (no thread replies posted) |

- **60 s settle: right.** No admissible item arrived after `Review completed` on any head. The settle guards against clock skew and polling order more than against CodeRabbit itself.
- **30-minute round timeout: right.** The worst head took 14.2 min, which leaves about 2× headroom. A long required CI job could still be the limit; that's outside this experiment.
- **10-minute `check_appear_timeout`: right for CI.** It's too blunt for CodeRabbit, which has no required-check entry. The pending status appeared within 118 s on every covered head, while an uncovered repo never gets a status or a `coderabbitai` check suite. CodeRabbit needs its own "status never appeared" outcome ([section 8](#8-proposed-ledger-corrections)).

## 5. Facts only available through GraphQL

| Fact | REST | GraphQL |
|---|---|---|
| Thread resolved, and by whom | not exposed | `reviewThreads.isResolved`, `resolvedBy` |
| Thread outdated | weak proxy only (`line: null`, `position` changes) | `reviewThreads.isOutdated` |
| Edit history and per-edit content | not exposed (only `updated_at`) | `userContentEdits { editedAt editor diff }` on comments, reviews and the PR body |
| Thread grouping | `in_reply_to_id` | `reviewThreads.comments` |

Commit statuses, check runs, reviews and comment bodies are all in REST. Before relying on the edit history, I checked that it returns data: 171 Phase 1 edits came back, every one with a non-empty `diff`, and editors were attributed. GraphQL refuses queries that might return more than 500,000 nodes. The first harvest query (100 threads × 50 comments × 100 edits) was rejected, and 50 × 20 × 30 works. Resolving a thread (`resolveReviewThread`) is also GraphQL-only.

## 6. API cost

The poller (`experiments/coderabbit_poll.py`) makes **10 calls per poll**: 9 REST (pull, reviews, review comments, issue comments, timeline, issue reactions, check runs for the head, combined status, status list) and 1 GraphQL query for threads (cost 1 point). Over 127 polls at 30 s, the core limit never fell below 4,507 of 5,000.

For gaffer, 7 calls per poll are enough: pull, status list for the head, check runs for the head, reviews, review comments, issue comments and GraphQL threads. Timeline and reactions are experiment-only. At gaffer's 60 s interval that is **~420 REST calls/hour, 8% of the 5,000/hour core limit**, plus 60 GraphQL points/hour. Every list call used `per_page=100`. A PR with more than 100 review comments needs extra pages, and the poller doesn't paginate. Conditional requests with ETags might cut this further, but I didn't test them.

The Phase 1 harvest of 6 PRs (54 SHAs, 3 per-SHA endpoints each, plus a status-history pass) took about 200 REST calls.

## 7. CI evidence path (R13)

On sandbox head `73d6429`, the failed check run `test` was read with gaffer's (Mark's) token:

- `output.title` and `output.summary` are **null** for GitHub Actions check runs.
- There are 3 annotations: a Node 20 deprecation warning, `Process completed with exit code 1` (on `.github`, line 44), and a runner-label notice. **None of them carries the test failure.**
- `GET /repos/{o}/{r}/actions/jobs/{check_run_id}/logs` works with the check-run ID as the job ID. Plain `gh api` exits with "the response contains terminal escape sequences" and writes nothing; it needs `--allow-escape-sequences`. After removing the 22 bytes of ANSI codes, the 223-line log's last 200 lines held the failing assertion and `1 failed, 2 passed`.
- Secrets in the log were already masked by Actions (`token: ***`, `AUTHORIZATION: basic ***`). The R1 scanner still runs.
- The 200-line tail of this tiny job is 15.5 KB, against R13's 16 KB per-check cap. Real jobs will hit the cap, so the cap should cut from the **end** of the log, keeping the last lines.

This supports R13's option A over B: annotations alone would have told the writer nothing.

## 8. Proposed ledger corrections

These are proposals only. Neither the ledger nor the design has been edited.

- **R12, inline comments:** admit only CodeRabbit inline comments with no `in_reply_to_id`. Keep CodeRabbit replies inside threads as context on their thread, so a human thread reply can't open round `seq+1`.
- **R12, review bodies:** the actionable sections are `⚠️ Outside diff range comments` and `🧹 Nitpick comments`. Parse the sections, not the "Actionable comments posted" header. Dedupe an outside-diff entry against an open CodeRabbit thread on the same path with overlapping lines. Name a `Files not reviewed due to moderation or processing errors` section in the report.
- **R12, comments by Mark:** exclude any comment with a `gaffer-round:` line, because gaffer's own comments share Mark's login. "Mark" should be the configured operator login, not a hard-coded name.
- **R12, new ignore rule:** ignore CodeRabbit's edits to the PR body. Its release-notes block sits between the `release notes by coderabbit.ai` markers. R23's reader should parse only the text outside them.
- **R12 normalization:** drop lines matching `✅ Addressed in commit(s) …`, `✅ Confirmed as addressed by @…` and `<!-- This is an auto-generated (comment|reply) by CodeRabbit -->` before hashing.
- **Rounds rule (design "Rounds and packets"), per-head completion:** the signal is the exact `Review completed` description. `Review paused` or `Review skipped…` on the current head means CodeRabbit won't review it. Take the fallback path and mark the round **unverified**; don't wait out the 30-minute timeout.
- **R11-style appear timeout for CodeRabbit:** no `CodeRabbit` status on the head 10 minutes after the push should give a named outcome. One option is to reuse the unverified fallback, with the report saying "CodeRabbit never started; the repo may not be covered by the app". It shouldn't fall through to `review_timeout`.
- **R13:** fetch logs with `--allow-escape-sequences`, or call the API directly. Strip ANSI codes and cap from the end.
- **R9 round marker:** confirmed as specified; no change.

## 9. Artifacts

- `experiments/coderabbit_poll.py`: the stdlib-plus-`gh` poller that recorded Phase 2.
- `tests/fixtures/coderabbit/<rule>/`: 13 directories built **only** from the sandbox PR (`mfullbrook/gaffer-coderabbit-sandbox#1`, toy code). Each holds API JSON as GitHub returned it plus an `expected.json` with the verdict and reason. `stranger_comment` and three cases inside `completion_signal` (paused, approved-then-completed, the old skip wording) are marked `"synthetic": true`. They copy real shapes from the sandbox and Phase 1, with no Phase 1 text.
- Not committed: Phase 1 raw JSON (private repos), the P1–P6 mapping and the full sandbox snapshots are in `~/.gaffer/experiments/coderabbit-raw/` and `~/.gaffer/experiments/coderabbit-sandbox/`.
- The sandbox repo is still there for Mark to delete: `gh repo delete mfullbrook/gaffer-coderabbit-sandbox` (needs the `delete_repo` scope). PR #1 is closed and unmerged.

### Sandbox run log (UTC)

| Time | Step |
|---|---|
| 10-19 17:28:14 | Seeded commit `47a28a8` (mutable default, bare except, off-by-one, failing test). PR opened as a draft. CI failed. |
| 17:28–17:50 | Draft window, then ready at 17:39:38. **No CodeRabbit activity:** no status and no `coderabbitai` check suite. Stopped and asked Mark. Mark added the repo to the CodeRabbit install. |
| 10-20 07:57:28 | Converted back to draft and pushed an empty commit `cfc9e38`. Got `Review skipped: draft pull request` and the skip comment. Quiet for 10 min. |
| 08:08:45 | `gh pr ready`. Pending at +5 s, `Review completed` at +131 s, with 3 inline findings, one per seeded bug. |
| 08:15:44 | Commit 2 `73d6429`: fixed the bare except and rewrote the `add_item` lines (bug kept). Within ~14 s the bare-except thread was marked addressed and self-resolved, and the mutable-default thread went outdated. `Review completed` at +186 s: the mutable default re-raised as a new thread, and the off-by-one repeated as an outside-diff body entry. |
| 08:21:47 | Plain comment and round-summary comment posted as Mark. Both came back byte-identical, with no reply or reaction. |
| 08:27:17 | Resolved the re-raised mutable-default thread with `resolveReviewThread`. No CodeRabbit reaction. |
| ~08:30 | Fetched the failed run's annotations and log tail (section 7). |
| 08:30:52 | Commit 3 `7cbe022` fixed the off-by-one, and CI passed. That thread was marked addressed and self-resolved. `Review completed` at +110 s, with one new body-only finding (clamp `n`). |
| 08:32:55–08:48:12 | 15 minutes with no events. Poller stopped and PR closed unmerged. |
