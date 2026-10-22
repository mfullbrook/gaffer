"""Fakes for the reviews tests: a GitHub reader, a controllable clock and a secret scanner.
No test touches the network."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from gaffer.reviews.github import GitHubError
from gaffer.reviews.packet import SecretFinding

FIXTURES = Path(__file__).parent / "fixtures"
CODERABBIT = FIXTURES / "coderabbit"
JUNIT = FIXTURES / "junit"


def load(path: Path | str) -> Any:
    return json.loads((CODERABBIT / path).read_text() if not isinstance(path, Path) else path.read_text())


class FakeGitHub:
    """Satisfies gaffer.reviews.github.GitHubReader from in-memory data."""

    def __init__(self, **data: Any):
        self.data: dict[str, Any] = {
            "pull": {"head": {"sha": ""}, "state": "open", "draft": False},
            "statuses": {},
            "check_runs": {},
            "annotations": {},
            "reviews": [],
            "review_comments": [],
            "issue_comments": [],
            "review_threads": [],
            "logs": {},
        }
        self.data.update(data)
        self.calls: list[str] = []
        self.fail: set[str] = set()

    def _get(self, name: str) -> Any:
        self.calls.append(name)
        if name in self.fail:
            raise GitHubError(f"{name}: HTTP 502")
        return self.data[name]

    def pull(self, repo: str, number: int) -> Mapping[str, Any]:
        return self._get("pull")

    def commit_statuses(self, repo: str, sha: str):
        return list(self._get("statuses").get(sha, []))

    def check_runs(self, repo: str, sha: str):
        return list(self._get("check_runs").get(sha, []))

    def check_run_annotations(self, repo: str, check_run_id: int):
        return list(self._get("annotations").get(check_run_id, []))

    def reviews(self, repo: str, number: int):
        return list(self._get("reviews"))

    def review_comments(self, repo: str, number: int):
        return list(self._get("review_comments"))

    def issue_comments(self, repo: str, number: int):
        return list(self._get("issue_comments"))

    def review_threads(self, repo: str, number: int):
        return list(self._get("review_threads"))

    def job_log(self, repo: str, job_id: int) -> bytes:
        logs = self._get("logs")
        if job_id not in logs:
            raise GitHubError(f"GET /repos/{repo}/actions/jobs/{job_id}/logs -> HTTP 404")
        return logs[job_id]


class FakeClock:
    """monotonic() stops while 'asleep'; wall() always runs."""

    def __init__(self, mono: float = 1000.0, wall: float = 1_760_000_000.0):
        self.mono = mono
        self.wall_time = wall

    def monotonic(self) -> float:
        return self.mono

    def wall(self) -> float:
        return self.wall_time

    def advance(self, seconds: float) -> None:
        self.mono += seconds
        self.wall_time += seconds

    def sleep_mac(self, seconds: float) -> None:
        self.wall_time += seconds


class FakeScanner:
    """Flags any text containing one of `needles`; records what it saw."""

    def __init__(self, *needles: str):
        self.needles = needles
        self.seen: list[str] = []

    def scan(self, text: str):
        self.seen.append(text)
        return [SecretFinding("generic-api-key", "redacted") for n in self.needles if n in text]
