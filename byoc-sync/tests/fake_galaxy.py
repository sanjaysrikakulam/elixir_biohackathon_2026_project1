"""A small HTTP server that answers the Galaxy 26.1 admin API calls byoc-sync makes.

It is a real server on 127.0.0.1, so the sources are tested through requests, retries and threads exactly as
they run against Galaxy. Behaviour knobs (failures, HTML pages, a redacting version) live on ``FakeGalaxy``.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

API_KEY = "admin-key-for-tests"
USER_KEY = "plain-user-key-for-tests"


@dataclass
class FakeUser:
    id: str
    email: str
    username: str
    prefs: dict[str, Any] | str | None = None  # dict -> JSON-encoded like Galaxy; str -> stored verbatim
    deleted: bool = False
    purged: bool = False
    active: bool = True

    def index_view(self) -> dict:
        return {
            "id": self.id,
            "email": self.email,
            "username": self.username,
            "deleted": self.deleted,
            "active": self.active,
            "last_password_change": None,
        }

    def detailed_view(self) -> dict:
        """The keys Galaxy 26.1 returns for GET /api/users/{id} (note: no "active", unlike the list view)."""
        preferences: dict[str, Any] = {"localization|locale": "en"}
        if isinstance(self.prefs, dict):
            preferences["extra_user_preferences"] = json.dumps(self.prefs)
        elif isinstance(self.prefs, str):
            preferences["extra_user_preferences"] = self.prefs
        return {
            "id": self.id,
            "email": self.email,
            "username": self.username,
            "deleted": self.deleted,
            "purged": self.purged,
            "preferences": preferences,
            "is_admin": False,
            "quota": "unlimited",
            "total_disk_usage": 0.0,
            "nice_total_disk_usage": "0 bytes",
            "quota_percent": None,
            "quota_bytes": None,
            "preferred_object_store_id": None,
        }


@dataclass
class FakeGalaxy:
    users: list[FakeUser] = field(default_factory=list)
    version: str = "26.1"
    # user id -> list of statuses to return before answering normally (e.g. [429, 503])
    flaky: dict[str, list[int]] = field(default_factory=dict)
    always_fail: set[str] = field(default_factory=set)
    html_for: set[str] = field(default_factory=set)
    redirect_for: set[str] = field(default_factory=set)
    slow: dict[str, float] = field(default_factory=dict)  # user id -> seconds to wait before answering
    requests: list[str] = field(default_factory=list)
    seen_keys: list[str | None] = field(default_factory=list)

    def add(self, *users: FakeUser) -> None:
        self.users.extend(users)


def _handler(galaxy: FakeGalaxy):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, status: int, body: Any, content_type: str = "application/json") -> None:
            data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            if status == 429:
                self.send_header("Retry-After", "0")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            url = urlparse(self.path)
            query = {k: v[-1] for k, v in parse_qs(url.query).items()}
            galaxy.requests.append(self.path)
            galaxy.seen_keys.append(self.headers.get("x-api-key"))
            key = self.headers.get("x-api-key")
            if key not in (API_KEY, USER_KEY):
                return self._send(403, {"err_msg": "Provided API key is not valid."})
            if url.path == "/api/users/current":
                return self._send(200, {"id": "admin", "email": "admin@example.org", "is_admin": key == API_KEY})

            if url.path == "/api/version":
                return self._send(200, {"version_major": galaxy.version, "version_minor": "0"})
            if url.path in ("/api/users", "/api/users/deleted"):
                deleted = url.path.endswith("/deleted") or query.get("deleted", "false") == "true"
                users = sorted((u for u in galaxy.users if u.deleted == deleted), key=lambda u: u.email)
                if "f_email" in query:
                    users = [u for u in users if query["f_email"] in u.email]
                offset = int(query.get("offset", 0))
                limit = int(query["limit"]) if "limit" in query else None
                page = users[offset : offset + limit if limit else None]
                return self._send(200, [u.index_view() for u in page])
            if url.path.startswith("/api/users/"):
                user_id = url.path.rsplit("/", 1)[-1]
                if user_id in galaxy.slow:
                    time.sleep(galaxy.slow[user_id])
                if user_id in galaxy.redirect_for:
                    self.send_response(302)
                    self.send_header("Location", "http://example.invalid/steal")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return None
                pending = galaxy.flaky.get(user_id)
                if pending:
                    return self._send(pending.pop(0), {"err_msg": "try again"})
                if user_id in galaxy.always_fail:
                    return self._send(500, {"err_msg": "boom"})
                if user_id in galaxy.html_for:
                    return self._send(200, "<html>proxy error</html>", "text/html")
                deleted = "/api/users/deleted/" in url.path or query.get("deleted", "false") == "true"
                for u in galaxy.users:
                    if u.id == user_id and u.deleted == deleted:
                        return self._send(200, u.detailed_view())
                return self._send(400, {"err_msg": "user not found"})
            return self._send(404, {"err_msg": "no route"})

    return Handler


class FakeGalaxyServer:
    def __init__(self, galaxy: FakeGalaxy):
        self.galaxy = galaxy
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _handler(galaxy))
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> FakeGalaxyServer:
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
