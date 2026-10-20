#!/usr/bin/env python3
"""Experiment 2 poller: record everything CodeRabbit and CI do on one PR.

Stdlib only; all GitHub access goes through the `gh` CLI, so the token never
passes through this script. Every interval it fetches the PR's REST and
GraphQL state, writes a snapshot, and appends an event for every new item,
body edit (by body hash) and state change.

    python3 experiments/coderabbit_poll.py OWNER/REPO PR OUT_DIR [--interval 30] [--quiet-exit 900]

Output:
    OUT_DIR/snapshots/<utc>-<head8>.json   raw API responses for that poll
    OUT_DIR/events.jsonl                   one JSON object per detected change
    OUT_DIR/ratelimit.jsonl                rate-limit headers seen per poll
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

THREADS_QUERY = """
query($owner: String!, $name: String!, $n: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $n) {
      isDraft state headRefOid
      reviewThreads(first: 100) {
        nodes {
          id isResolved isOutdated resolvedBy { login } path line originalLine
          comments(first: 50) {
            nodes { databaseId author { login } createdAt lastEditedAt outdated
                    userContentEdits { totalCount } }
          }
        }
      }
    }
  }
  rateLimit { cost remaining resetAt }
}
"""


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def gh_api(args):
    """Run `gh api -i ...`; return (status, headers, body). Errors are data, not empty results."""
    r = subprocess.run(["gh", "api", "-i", *args], capture_output=True, text=True)
    head, _, body = r.stdout.replace("\r\n", "\n").partition("\n\n")
    lines = head.splitlines()
    status = int(lines[0].split()[1]) if lines and lines[0].startswith("HTTP") else None
    headers = {}
    for line in lines[1:]:
        k, _, v = line.partition(":")
        headers[k.strip().lower()] = v.strip()
    try:
        data = json.loads(body) if body.strip() else None
    except json.JSONDecodeError:
        data = {"_unparsed": body[:2000]}
    if r.returncode and status is None:
        data = {"_error": r.stderr.strip()[:2000]}
    return status, headers, data


def body_hash(text):
    return hashlib.sha256((text or "").encode()).hexdigest()[:16]


def fetch(repo, pr):
    owner, name = repo.split("/")
    base = f"repos/{repo}"
    out, limits = {}, []

    def get(key, path):
        status, headers, data = gh_api([path])
        out[key] = {"http_status": status, "data": data}
        limits.append({k: headers.get(f"x-ratelimit-{k}") for k in ("resource", "remaining", "used", "reset")} | {"call": key})
        return data

    pull = get("pull", f"{base}/pulls/{pr}")
    head = (pull or {}).get("head", {}).get("sha") if isinstance(pull, dict) else None
    get("reviews", f"{base}/pulls/{pr}/reviews?per_page=100")
    get("review_comments", f"{base}/pulls/{pr}/comments?per_page=100")
    get("issue_comments", f"{base}/issues/{pr}/comments?per_page=100")
    get("timeline", f"{base}/issues/{pr}/timeline?per_page=100")
    get("reactions", f"{base}/issues/{pr}/reactions?per_page=100")
    if head:
        get("check_runs", f"{base}/commits/{head}/check-runs?per_page=100")
        get("status", f"{base}/commits/{head}/status")
        get("statuses", f"{base}/commits/{head}/statuses?per_page=100")
    status, headers, data = gh_api(["graphql", "-f", f"query={THREADS_QUERY}",
                                    "-F", f"owner={owner}", "-F", f"name={name}", "-F", f"n={pr}"])
    out["graphql_threads"] = {"http_status": status, "data": data}
    limits.append({k: headers.get(f"x-ratelimit-{k}") for k in ("resource", "remaining", "used", "reset")} | {"call": "graphql_threads"})
    return head, out, limits


def items(snap):
    """Flatten a snapshot into {key: fingerprint} for diffing between polls."""
    d = lambda k: (snap.get(k) or {}).get("data")
    found = {}
    pull = d("pull")
    if isinstance(pull, dict) and "head" in pull:
        found["pull"] = {"state": pull["state"], "draft": pull["draft"], "head": pull["head"]["sha"],
                         "merged": pull.get("merged"), "mergeable_state": pull.get("mergeable_state"),
                         "body": body_hash(pull.get("body"))}
    for kind in ("reviews", "review_comments", "issue_comments"):
        for x in d(kind) or []:
            if not isinstance(x, dict):
                continue
            fp = {"author": x["user"]["login"], "body": body_hash(x.get("body"))}
            if kind == "reviews":
                fp |= {"state": x["state"], "commit": x.get("commit_id")}
            else:
                fp |= {"reactions": (x.get("reactions") or {}).get("total_count")}
            if kind == "review_comments":
                fp |= {"reply_to": x.get("in_reply_to_id"), "commit": x.get("commit_id"),
                       "line": x.get("line"), "position": x.get("position")}
            found[f"{kind}:{x['id']}"] = fp
    for x in d("timeline") or []:
        if isinstance(x, dict):
            key = x.get("id") or x.get("node_id") or x.get("sha") or body_hash(json.dumps(x, sort_keys=True))
            found[f"timeline:{key}"] = {"event": x.get("event"), "actor": (x.get("actor") or x.get("user") or {}).get("login")}
    for x in d("reactions") or []:
        if isinstance(x, dict):
            found[f"reaction:{x['id']}"] = {"user": x["user"]["login"], "content": x["content"]}
    runs = d("check_runs")
    for x in (runs or {}).get("check_runs", []) if isinstance(runs, dict) else []:
        found[f"check_run:{x['id']}"] = {"name": x["name"], "app": x["app"]["slug"], "head": x["head_sha"],
                                         "status": x["status"], "conclusion": x["conclusion"]}
    for x in d("statuses") or []:
        if isinstance(x, dict):
            found[f"status:{x['id']}"] = {"context": x["context"], "state": x["state"], "description": x["description"]}
    gq = d("graphql_threads")
    try:
        threads = gq["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
    except (KeyError, TypeError):
        threads = []
    for t in threads:
        found[f"thread:{t['id']}"] = {"resolved": t["isResolved"], "outdated": t["isOutdated"],
                                      "resolved_by": (t["resolvedBy"] or {}).get("login"),
                                      "comments": [c["databaseId"] for c in t["comments"]["nodes"]]}
    return found


def diff(prev, cur):
    events = []
    for k, fp in cur.items():
        if k not in prev:
            events.append({"type": "new", "key": k, "now": fp})
        elif prev[k] != fp:
            changed = {f: [prev[k].get(f), fp.get(f)] for f in fp if prev[k].get(f) != fp.get(f)}
            kind = "edit" if set(changed) == {"body"} else "change"
            events.append({"type": kind, "key": k, "changed": changed, "now": fp})
    for k in prev:
        if k not in cur:
            events.append({"type": "gone", "key": k, "was": prev[k]})
    return events


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("repo")
    p.add_argument("pr", type=int)
    p.add_argument("out")
    p.add_argument("--interval", type=int, default=30)
    p.add_argument("--quiet-exit", type=int, default=0, help="exit after this many seconds with no events (0 = never)")
    a = p.parse_args()

    os.makedirs(os.path.join(a.out, "snapshots"), exist_ok=True)
    prev, last_event = {}, time.time()
    while True:
        t = now()
        head, snap, limits = fetch(a.repo, a.pr)
        name = f"{t.replace(':', '')}-{(head or 'nohead')[:8]}.json"
        with open(os.path.join(a.out, "snapshots", name), "w") as f:
            json.dump({"polled_at": t, "head": head, "responses": snap}, f, indent=1)
        cur = items(snap)
        errors = [k for k, v in snap.items() if v["http_status"] != 200]
        events = diff(prev, cur) if prev else [{"type": "baseline", "count": len(cur)}]
        if errors:
            events.append({"type": "api_error", "calls": errors})
            cur = prev | cur  # an API error never counts as items disappearing
        with open(os.path.join(a.out, "events.jsonl"), "a") as f:
            for e in events:
                f.write(json.dumps({"at": t, "head": head, **e}) + "\n")
        with open(os.path.join(a.out, "ratelimit.jsonl"), "a") as f:
            f.write(json.dumps({"at": t, "calls": len(limits), "limits": limits}) + "\n")
        core = [l for l in limits if l.get("resource") == "core"]
        print(f"{t} head={(head or '-')[:8]} events={len(events)} calls={len(limits)} "
              f"core_remaining={core[-1]['remaining'] if core else '?'}", flush=True)
        if events:
            last_event = time.time()
        prev = cur
        if a.quiet_exit and time.time() - last_event >= a.quiet_exit:
            print(f"{now()} quiet for {a.quiet_exit}s, exiting", flush=True)
            return 0
        if core and core[-1]["remaining"] and int(core[-1]["remaining"]) < 200:
            print("core rate limit below 200, exiting", file=sys.stderr, flush=True)
            return 2
        time.sleep(a.interval)


if __name__ == "__main__":
    sys.exit(main())
