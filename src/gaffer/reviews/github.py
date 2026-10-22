"""GitHub read client: the interface the round poller needs, and a small urllib
implementation. Tests use fakes; nothing here runs in the test suite against
the network.

Experiment 2 section 6: per poll gaffer needs the pull, the status list and
check runs for the head, reviews, review comments, issue comments and the
GraphQL review threads; R13 adds annotations and job logs for failed checks.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from ..config import Secret

API = "https://api.github.com"
PER_PAGE = 100

# Only the thread root's databaseId is needed: replies carry in_reply_to_id = root (REST).
REVIEW_THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 50, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id isResolved isOutdated path line startLine originalLine
          resolvedBy { login }
          comments(first: 1) { nodes { databaseId author { login } } }
        }
      }
    }
  }
}
"""


class GitHubError(Exception):
    """Any failed read. Callers treat it as 'unknown', never as empty (design: Waits and timeouts)."""


class GitHubReader(Protocol):
    def pull(self, repo: str, number: int) -> Mapping[str, Any]: ...
    def commit_statuses(self, repo: str, sha: str) -> list[Mapping[str, Any]]: ...
    def check_runs(self, repo: str, sha: str) -> list[Mapping[str, Any]]: ...
    def check_run_annotations(self, repo: str, check_run_id: int) -> list[Mapping[str, Any]]: ...
    def reviews(self, repo: str, number: int) -> list[Mapping[str, Any]]: ...
    def review_comments(self, repo: str, number: int) -> list[Mapping[str, Any]]: ...
    def issue_comments(self, repo: str, number: int) -> list[Mapping[str, Any]]: ...
    def review_threads(self, repo: str, number: int) -> list[Mapping[str, Any]]: ...
    def job_log(self, repo: str, job_id: int) -> bytes: ...


Opener = Callable[[urllib.request.Request, float], Any]


class _NoAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    """Job logs redirect to blob storage. The token is sent as an unredirected
    header, so it is never forwarded; this handler only exists to make that explicit."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            new.remove_header("Authorization")
        return new


def _default_opener() -> Opener:
    opener = urllib.request.build_opener(_NoAuthOnRedirect())
    return lambda req, timeout: opener.open(req, timeout=timeout)


class UrllibGitHubReader:
    """GitHubReader over the REST and GraphQL APIs with gaffer's own token (R13)."""

    def __init__(self, token: Secret, *, opener: Opener | None = None, api: str = API, timeout: float = 30.0):
        self._token = token
        self._open = opener or _default_opener()
        self._api = api.rstrip("/")
        self._timeout = timeout

    # --- REST ---

    def pull(self, repo, number):
        return self._json(f"/repos/{repo}/pulls/{number}")

    def commit_statuses(self, repo, sha):
        return self._pages(f"/repos/{repo}/commits/{sha}/statuses")

    def check_runs(self, repo, sha):
        return self._pages(f"/repos/{repo}/commits/{sha}/check-runs", key="check_runs")

    def check_run_annotations(self, repo, check_run_id):
        return self._pages(f"/repos/{repo}/check-runs/{check_run_id}/annotations")

    def reviews(self, repo, number):
        return self._pages(f"/repos/{repo}/pulls/{number}/reviews")

    def review_comments(self, repo, number):
        return self._pages(f"/repos/{repo}/pulls/{number}/comments")

    def issue_comments(self, repo, number):
        return self._pages(f"/repos/{repo}/issues/{number}/comments")

    def job_log(self, repo, job_id):
        # R27: fetched raw over the API; ANSI codes are stripped by checks.log_tail.
        body, _ = self._request("GET", f"{self._api}/repos/{repo}/actions/jobs/{job_id}/logs")
        return body

    # --- GraphQL ---

    def review_threads(self, repo, number):
        owner, name = repo.split("/", 1)
        threads: list[Mapping[str, Any]] = []
        after = None
        while True:
            payload = {"query": REVIEW_THREADS_QUERY, "variables": {"owner": owner, "name": name, "number": number, "after": after}}
            body, _ = self._request("POST", f"{self._api}/graphql", json.dumps(payload).encode())
            data = json.loads(body)
            if data.get("errors"):
                raise GitHubError(f"GraphQL errors: {data['errors']}")
            try:
                conn = data["data"]["repository"]["pullRequest"]["reviewThreads"]
            except (KeyError, TypeError):
                raise GitHubError("GraphQL response has no reviewThreads") from None
            threads.extend(conn["nodes"])
            if not conn["pageInfo"]["hasNextPage"]:
                return threads
            after = conn["pageInfo"]["endCursor"]

    # --- plumbing ---

    def _json(self, path: str) -> Any:
        body, _ = self._request("GET", self._api + path)
        return json.loads(body)

    def _pages(self, path: str, *, key: str | None = None) -> list[Any]:
        sep = "&" if "?" in path else "?"
        url: str | None = f"{self._api}{path}{sep}per_page={PER_PAGE}"
        out: list[Any] = []
        while url:
            body, headers = self._request("GET", url)
            data = json.loads(body)
            out.extend(data[key] if key else data)
            url = _next_link(headers.get("Link") or headers.get("link") or "")
        return out

    def _request(self, method: str, url: str, data: bytes | None = None) -> tuple[bytes, Mapping[str, str]]:
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", "gaffer")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        # Unredirected: never forwarded to the blob-storage host a log download redirects to.
        req.add_unredirected_header("Authorization", f"Bearer {self._token.reveal()}")
        try:
            with self._open(req, self._timeout) as resp:
                return resp.read(), dict(resp.headers.items())
        except urllib.error.HTTPError as e:
            raise GitHubError(f"{method} {_path(url)} -> HTTP {e.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise GitHubError(f"{method} {_path(url)} failed: {e}") from None


_LINK_NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')


def _next_link(header: str) -> str | None:
    m = _LINK_NEXT.search(header)
    return m.group(1) if m else None


def _path(url: str) -> str:
    return urllib.parse.urlsplit(url).path
