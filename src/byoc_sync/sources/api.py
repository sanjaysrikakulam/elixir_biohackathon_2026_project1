"""Read BYOC form data through Galaxy's admin API (Galaxy 26.1), using BioBlend.

Galaxy has no API that lists users by preference, so this lists every account (active and deleted, one
request each) and fetches each user's detailed view, whose ``preferences.extra_user_preferences`` holds the
form values. Detail calls run on a bounded thread pool; BioBlend retries failed GETs. If more than
``max_error_rate`` of the detail calls fail, the scan is marked incomplete and nothing may be written.
For large servers the gxadmin source answers the same question with a single SQL query.

On Galaxy 26.2 and later password inputs are no longer returned; such records are flagged
``password_unreadable`` (see docs/GAPS.md).
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urlparse

import requests
from bioblend import ConnectionError as BioblendConnectionError
from bioblend.galaxy import GalaxyInstance

from ..models import UserRecord, section_prefs
from . import SECTION, ScanResult

REDACTED_VALUES = ("__SECRET_PLACEHOLDER__",)
FIRST_REDACTING_VERSION = (26, 2)
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


class GalaxyApiError(RuntimeError):
    pass


def _version_tuple(version: str | None) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in str(version).split(".")[:2])
    except ValueError:
        return ()


class _Galaxy(GalaxyInstance):
    """BioBlend's client with two changes to how GETs are sent: a pooled session per thread (BioBlend uses a
    new connection per request, which matters when every user is fetched) and no redirects (requests would
    re-send the ``x-api-key`` header to wherever a redirect points)."""

    def __init__(self, url: str, key: str, timeout: float):
        super().__init__(url, key=key)
        self.timeout = timeout
        self._local = threading.local()

    def make_get_request(self, url: str, **kwargs: Any) -> requests.Response:
        session = getattr(self._local, "session", None)
        if session is None:
            session = self._local.session = requests.Session()
        kwargs.setdefault("timeout", self.timeout)
        kwargs.setdefault("verify", self.verify)
        kwargs["allow_redirects"] = False
        try:
            return session.get(url, headers=self.json_headers, **kwargs)
        except requests.ConnectionError:
            raise
        except requests.RequestException as exc:
            # BioBlend only retries ConnectionError; a read timeout must be retried and reported the same way.
            raise requests.ConnectionError(f"{type(exc).__name__} for {url}") from None


def _api_error(what: str, exc: BioblendConnectionError) -> GalaxyApiError:
    # BioBlend's message embeds the response body; only the status is reported.
    status = getattr(exc, "status_code", None)
    return GalaxyApiError(f"{what}: HTTP {status}" if status else f"{what}: connection failed")


class ApiSource:
    def __init__(
        self,
        url: str,
        api_key: str,
        *,
        section: str = SECTION,
        concurrency: int = 16,
        timeout: float = 30,
        retries: int = 3,
        retry_backoff: float = 1.0,
        max_error_rate: float = 0.01,
    ):
        parsed = urlparse(url)
        if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in LOOPBACK_HOSTS):
            raise ValueError(f"Galaxy URL must use https (plain http is only allowed for localhost): {url}")
        self.url = url.rstrip("/")
        self.section = section
        self.concurrency = max(1, concurrency)
        self.max_error_rate = max_error_rate
        self.gi = _Galaxy(self.url, api_key, timeout)
        self.gi.max_get_attempts = max(1, retries + 1)
        self.gi.get_retry_delay = retry_backoff
        self._admin_checked = False
        self._redacting: bool | None = None

    # -- checks -----------------------------------------------------------------------------------------------

    def _check(self) -> str | None:
        """Refuse non-admin keys (they silently see only their own account) and note the Galaxy version."""
        if not self._admin_checked:
            try:
                current = self.gi.users.get_current_user()
            except BioblendConnectionError as exc:
                raise _api_error("/api/users/current", exc) from None
            if not current.get("is_admin"):
                raise GalaxyApiError("the API key does not belong to a Galaxy admin")
            self._admin_checked = True
        try:
            version = self.gi.config.get_version().get("version_major")
        except BioblendConnectionError as exc:
            raise _api_error("/api/version", exc) from None
        self._redacting = _version_tuple(version) >= FIRST_REDACTING_VERSION
        return version

    # -- records ----------------------------------------------------------------------------------------------

    def _record(self, user: dict[str, Any], active: bool) -> tuple[UserRecord | None, str | None]:
        """Record for a detailed user view, or (None, error) when its preferences cannot be parsed. ``active``
        comes from the user list: the detailed view does not include it."""
        email = str(user.get("email") or "")
        raw = (user.get("preferences") or {}).get("extra_user_preferences")
        prefs: dict[str, Any] | None = {}
        if raw:
            try:
                prefs = section_prefs(json.loads(raw), self.section)
            except ValueError:
                prefs = None
            if prefs is None:
                return None, f"{email}: extra preferences are not valid JSON"
        password = (prefs or {}).get(f"{self.section}|password")
        unreadable = bool(prefs) and (
            bool(self._redacting) or isinstance(password, dict) or password in REDACTED_VALUES
        )
        record = UserRecord(
            galaxy_id=str(user["id"]),
            email=email,
            username=user.get("username"),
            deleted=bool(user.get("deleted")),
            purged=bool(user.get("purged")),
            active=active,
            prefs=prefs,
            source="api",
            password_unreadable=unreadable,
        )
        return record, None

    def _detail(self, user_id: str, deleted: bool) -> dict[str, Any]:
        try:
            user = self.gi.users.show_user(user_id, deleted=deleted)
        except BioblendConnectionError as exc:
            raise _api_error(f"/api/users/{user_id}", exc) from None
        if not isinstance(user, dict) or "id" not in user:
            raise GalaxyApiError(f"/api/users/{user_id}: unexpected response")
        return user

    def _list(self, **filters: Any) -> list[tuple[str, str, bool, bool]]:
        """(id, email, deleted, active) of every matching account, active ones and deleted ones."""
        seen: dict[str, tuple[str, str, bool, bool]] = {}
        for deleted in (False, True):
            try:
                users = self.gi.users.get_users(deleted=deleted, **filters)
            except BioblendConnectionError as exc:
                raise _api_error("/api/users", exc) from None
            for user in users:
                seen.setdefault(
                    str(user["id"]),
                    (str(user["id"]), str(user.get("email") or ""), deleted, bool(user.get("active", True))),
                )
        return list(seen.values())

    # -- Source interface ------------------------------------------------------------------------------------

    def scan(self) -> ScanResult:
        result = ScanResult(records=[], galaxy_version=self._check())
        users = self._list()

        def fetch(item: tuple[str, str, bool, bool]) -> tuple[str, str, bool, dict[str, Any] | None, str | None]:
            user_id, email, deleted, active = item
            try:
                return user_id, email, active, self._detail(user_id, deleted), None
            except GalaxyApiError as exc:
                return user_id, email, active, None, f"{email or 'user ' + user_id}: {exc}"

        failed = 0
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            for user_id, email, active, detail, error in pool.map(fetch, users):
                if error:
                    # Unknown state: this user's config must stay exactly as it is.
                    failed += 1
                    result.errors.append(error)
                    result.unreadable.add(email or user_id)
                    continue
                record, parse_error = self._record(detail, active)
                if parse_error:
                    result.errors.append(parse_error)
                    result.unreadable.add(str(detail.get("email") or user_id))
                elif record is not None and record.prefs:
                    result.records.append(record)
        result.records.sort(key=lambda r: r.email)
        if users and failed / len(users) > self.max_error_rate:
            result.complete = False
        return result

    def lookup(self, email: str) -> UserRecord | None:
        if self._redacting is None or not self._admin_checked:
            self._check()
        for user_id, found, deleted, active in self._list(f_email=email):
            if found == email:
                record, error = self._record(self._detail(user_id, deleted), active)
                if error:
                    raise GalaxyApiError(error)
                return record
        return None
