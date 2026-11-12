"""A minimal GitHub REST API (pulls and issue comments) on 127.0.0.1, with merge/close helpers for tests."""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

TOKEN = "ghp_test_token_for_byoc_sync"


@dataclass
class FakePR:
    number: int
    head: str
    base: str
    title: str
    body: str
    state: str = "open"
    merged_at: str | None = None
    comments: list[str] = field(default_factory=list)
    head_repo: str = "owner/playbook"

    def view(self, owner: str, repo: str, api: str = "") -> dict:
        return {
            "url": f"{api}/repos/{owner}/{repo}/pulls/{self.number}",
            "issue_url": f"{api}/repos/{owner}/{repo}/issues/{self.number}",
            "number": self.number,
            "html_url": f"https://github.com/{owner}/{repo}/pull/{self.number}",
            "state": self.state,
            "merged_at": self.merged_at,
            "title": self.title,
            "body": self.body,
            "head": {
                "ref": self.head,
                "label": f"{self.head_repo.split('/')[0]}:{self.head}",
                "repo": {"full_name": self.head_repo},
            },
            "base": {"ref": self.base},
            "created_at": f"2026-11-10T00:00:{self.number:02d}Z",
        }


@dataclass
class FakeGitHub:
    owner: str = "owner"
    repo: str = "playbook"
    prs: list[FakePR] = field(default_factory=list)
    calls: list[tuple[str, str]] = field(default_factory=list)
    auth: set[str | None] = field(default_factory=set)
    api: str = ""

    def by_head(self, head: str) -> list[FakePR]:
        return [p for p in self.prs if p.head == head]


def _handler(gh: FakeGitHub):
    prefix = f"/repos/{gh.owner}/{gh.repo}"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, status, body, link=None):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            if link:
                self.send_header("Link", link)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(length) or b"{}")

        def _authorised(self):
            gh.auth.add(self.headers.get("Authorization"))
            if self.headers.get("Authorization") not in (f"Bearer {TOKEN}", f"token {TOKEN}"):
                self._send(401, {"message": "Bad credentials"})
                return False
            return True

        def do_GET(self):
            url = urlparse(self.path)
            gh.calls.append(("GET", self.path))
            if not self._authorised():
                return
            query = {k: v[-1] for k, v in parse_qs(url.query).items()}
            if url.path == f"{prefix}/pulls":
                prs = gh.prs
                state = query.get("state", "open")
                if state != "all":
                    prs = [p for p in prs if p.state == state]
                if "head" in query:
                    prs = [p for p in prs if f"{p.head_repo.split('/')[0]}:{p.head}" == query["head"]]
                page, per_page = int(query.get("page", 1)), int(query.get("per_page", 30))
                ordered = sorted(prs, key=lambda p: -p.number)
                chunk = ordered[(page - 1) * per_page : page * per_page]
                link = None
                if page * per_page < len(ordered):
                    params = {**query, "page": str(page + 1)}
                    next_url = f"http://{self.headers['Host']}{url.path}?" + "&".join(
                        f"{k}={v}" for k, v in params.items()
                    )
                    link = f'<{next_url}>; rel="next"'
                return self._send(200, [p.view(gh.owner, gh.repo, gh.api) for p in chunk], link)
            match = re.fullmatch(rf"{prefix}/pulls/(\d+)", url.path)
            if match and int(match.group(1)) <= len(gh.prs):
                return self._send(200, gh.prs[int(match.group(1)) - 1].view(gh.owner, gh.repo, gh.api))
            return self._send(404, {"message": "Not Found"})

        def do_POST(self):
            url = urlparse(self.path)
            gh.calls.append(("POST", self.path))
            if not self._authorised():
                return
            body = self._body()
            if url.path == f"{prefix}/pulls":
                if any(
                    p.head == body["head"] and p.state == "open" and p.head_repo == f"{gh.owner}/{gh.repo}"
                    for p in gh.prs
                ):
                    return self._send(422, {"message": "A pull request already exists"})
                pr = FakePR(len(gh.prs) + 1, body["head"], body["base"], body["title"], body["body"])
                gh.prs.append(pr)
                return self._send(201, pr.view(gh.owner, gh.repo, gh.api))
            match = re.fullmatch(rf"{prefix}/issues/(\d+)/comments", url.path)
            if match:
                gh.prs[int(match.group(1)) - 1].comments.append(body["body"])
                return self._send(201, {"id": 1})
            return self._send(404, {"message": "Not Found"})

        def do_PATCH(self):
            url = urlparse(self.path)
            gh.calls.append(("PATCH", self.path))
            if not self._authorised():
                return
            match = re.fullmatch(rf"{prefix}/pulls/(\d+)", url.path)
            if not match:
                return self._send(404, {"message": "Not Found"})
            pr = gh.prs[int(match.group(1)) - 1]
            for key, value in self._body().items():
                setattr(pr, key, value)
            return self._send(200, pr.view(gh.owner, gh.repo, gh.api))

    return Handler


class FakeGitHubServer:
    def __init__(self, gh: FakeGitHub):
        self.gh = gh
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _handler(gh))
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        gh.api = self.url
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
