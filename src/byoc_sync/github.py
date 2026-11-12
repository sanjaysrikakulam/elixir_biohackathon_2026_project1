"""Pull request operations on top of PyGithub.

Only the handful of calls byoc-sync needs, mapped onto a small :class:`PullRequest` record. PyGithub handles
pagination, retries and GitHub's request pacing for writes. The token goes into the Authorization header
only and never into error messages.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from github import Auth, Github, GithubException

SLUG_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
T = TypeVar("T")


class GitHubError(RuntimeError):
    pass


@dataclass(frozen=True)
class PullRequest:
    number: int
    url: str
    state: str
    merged: bool
    head: str
    title: str
    body: str
    head_repo: str = ""

    @property
    def is_open(self) -> bool:
        return self.state == "open"

    @property
    def rejected(self) -> bool:
        """Closed without being merged."""
        return self.state == "closed" and not self.merged

    @classmethod
    def from_github(cls, pr: Any) -> PullRequest:
        # Only fields present in list responses are read, so no extra request is made per pull request.
        return cls(
            number=pr.number,
            url=pr.html_url or "",
            state=pr.state,
            merged=pr.merged_at is not None,
            head=pr.head.ref,
            title=pr.title or "",
            body=pr.body or "",
            head_repo=pr.head.repo.full_name if pr.head.repo is not None else "",
        )


class GitHub:
    def __init__(
        self,
        repo: str,
        token: str,
        *,
        api_url: str = "https://api.github.com",
        timeout: int = 30,
        pace_requests: bool = True,
    ):
        if not SLUG_RE.fullmatch(repo):
            raise ValueError(f"GitHub repository must look like OWNER/REPO, got {repo!r}")
        pacing = {} if pace_requests else {"seconds_between_requests": None, "seconds_between_writes": None}
        self._github = Github(
            auth=Auth.Token(token), base_url=api_url, timeout=timeout, per_page=100, user_agent="byoc-sync", **pacing
        )
        self._repo = self._github.get_repo(repo, lazy=True)
        self.repo = repo
        self.owner = repo.split("/")[0]

    @staticmethod
    def _call(what: str, func: Callable[[], T]) -> T:
        try:
            return func()
        except GithubException as exc:
            message = exc.data.get("message", "") if isinstance(exc.data, dict) else ""
            raise GitHubError(f"GitHub {what}: HTTP {exc.status} {message}".strip()) from None

    def prs_for_branch(self, branch: str) -> list[PullRequest]:
        """All pull requests from ``branch`` (any state), newest first."""
        pulls = self._call(
            f"list pull requests of {branch}",
            lambda: [
                PullRequest.from_github(p) for p in self._repo.get_pulls(state="all", head=f"{self.owner}:{branch}")
            ],
        )
        return sorted((p for p in pulls if self._ours(p)), key=lambda p: -p.number)

    def _ours(self, pr: PullRequest) -> bool:
        return pr.head_repo.lower() == self.repo.lower()

    def create_pr(self, branch: str, base: str, title: str, body: str) -> PullRequest:
        return self._call(
            f"open a pull request for {branch}",
            lambda: PullRequest.from_github(self._repo.create_pull(base=base, head=branch, title=title, body=body)),
        )

    def update_pr(self, number: int, **fields: str) -> None:
        self._call(f"update pull request #{number}", lambda: self._repo.get_pull(number).edit(**fields))

    def comment(self, number: int, text: str) -> None:
        self._call(f"comment on pull request #{number}", lambda: self._repo.get_pull(number).create_issue_comment(text))

    def close_pr(self, number: int) -> None:
        self.update_pr(number, state="closed")
