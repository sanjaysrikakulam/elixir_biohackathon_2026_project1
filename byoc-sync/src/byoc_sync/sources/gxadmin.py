"""Read BYOC form data straight from Galaxy's database through gxadmin.

Galaxy stores all of a user's extra preferences as one JSON string in ``user_preference`` (row name
``extra_user_preferences``). A single query returns every user that has the BYOC section filled in, however
many accounts the server has. ``gxadmin rawquery`` prints the value unmodified (``psql -At``), unlike
``jsonquery``, whose CSV quoting doubles spaces inside values.

The table has no unique constraint on (user_id, name), so duplicates are possible; like Galaxy itself we use
the newest row (highest id).
"""

from __future__ import annotations

import json
import re
import subprocess
from typing import Any

from ..models import UserRecord, section_prefs
from . import SECTION, ScanResult


class GxadminError(RuntimeError):
    pass


_USER_COLUMNS = """
    u.id, u.email, u.username, u.deleted, u.purged, u.active, newest.value AS extra_user_preferences
"""

_NEWEST_PREFERENCES = """
    SELECT DISTINCT ON (up.user_id) up.user_id, up.value
    FROM user_preference up
    WHERE up.name = 'extra_user_preferences'
    ORDER BY up.user_id, up.id DESC
"""


def scan_sql(section: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]+", section):
        raise GxadminError(f"invalid preferences section name {section!r}")
    return f"""
SELECT COALESCE(json_agg(t ORDER BY t.id), '[]'::json) FROM (
    SELECT {_USER_COLUMNS}
    FROM ({_NEWEST_PREFERENCES}) newest
    JOIN galaxy_user u ON u.id = newest.user_id
    WHERE newest.value LIKE '%{section}|%'
) t
""".strip()


def _sql_string(value: str) -> str:
    # Standard SQL string literal. Backslashes are refused so the literal is safe even on a server running
    # with standard_conforming_strings=off.
    if any(ord(c) < 32 for c in value) or "\\" in value:
        raise GxadminError(f"cannot look up e-mail {value!r}: unsupported characters")
    return "'" + value.replace("'", "''") + "'"


def lookup_sql(email: str) -> str:
    return f"""
SELECT COALESCE(json_agg(t ORDER BY t.id), '[]'::json) FROM (
    SELECT {_USER_COLUMNS}
    FROM galaxy_user u
    LEFT JOIN ({_NEWEST_PREFERENCES}) newest ON newest.user_id = u.id
    WHERE u.email = {_sql_string(email)}
) t
""".strip()


def lookup_id_sql(galaxy_id: str) -> str:
    if not galaxy_id.isdigit():
        raise GxadminError(f"cannot look up user id {galaxy_id!r}: gxadmin reports database ids (digits)")
    return f"""
SELECT COALESCE(json_agg(t ORDER BY t.id), '[]'::json) FROM (
    SELECT {_USER_COLUMNS}
    FROM galaxy_user u
    LEFT JOIN ({_NEWEST_PREFERENCES}) newest ON newest.user_id = u.id
    WHERE u.id = {int(galaxy_id)}
) t
""".strip()


class GxadminSource:
    def __init__(self, cmd: list[str], section: str = SECTION, timeout: int = 600):
        self.cmd = list(cmd)
        self.section = section
        self.timeout = timeout

    def _rows(self, sql: str) -> list[dict[str, Any]]:
        try:
            proc = subprocess.run(
                [*self.cmd, "rawquery", "q", sql], capture_output=True, text=True, timeout=self.timeout
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GxadminError(f"could not run gxadmin ({self.cmd[0]}): {exc}") from exc
        if proc.returncode != 0:
            raise GxadminError(f"gxadmin exited with {proc.returncode}: {proc.stderr.strip()[-500:]}")
        # The query always yields exactly one JSON array. Anything else (typically empty output: psql reading
        # from stdin exits 0 on SQL errors) must not be mistaken for "no users". The output itself is never
        # echoed: it contains other users' form values.
        stderr = proc.stderr.strip()[-500:]
        try:
            rows = json.loads(proc.stdout)
        except ValueError:
            raise GxadminError(
                f"gxadmin did not return a JSON document ({len(proc.stdout)} bytes on stdout); stderr: {stderr}"
            ) from None
        if not isinstance(rows, list):
            raise GxadminError(f"gxadmin returned a {type(rows).__name__}, not a list of users; stderr: {stderr}")
        return rows

    def _record(self, row: dict[str, Any], result: ScanResult | None) -> UserRecord | None:
        email = str(row.get("email") or "")
        raw = row.get("extra_user_preferences")
        prefs: dict[str, Any] | None = {}
        if raw:
            try:
                prefs = section_prefs(json.loads(raw), self.section)
            except ValueError:
                prefs = None
            if prefs is None:
                if result is None:
                    raise GxadminError(f"{email}: extra preferences are not valid JSON")
                result.unreadable.update(k for k in (email, str(row.get("id") or "")) if k)
                result.errors.append(f"{email}: extra preferences are not valid JSON")
                return None
        return UserRecord(
            galaxy_id=str(row["id"]),
            email=email,
            username=row.get("username"),
            deleted=bool(row.get("deleted")),
            purged=bool(row.get("purged")),
            active=bool(row.get("active")),
            prefs=prefs,
            source="gxadmin",
        )

    def scan(self) -> ScanResult:
        result = ScanResult(records=[])
        for row in self._rows(scan_sql(self.section)):
            record = self._record(row, result)
            if record is not None and record.prefs:
                result.records.append(record)
        return result

    def lookup(self, email: str) -> UserRecord | None:
        rows = self._rows(lookup_sql(email))
        for row in rows:
            if row.get("email") == email:
                return self._record(row, None)
        return None

    def lookup_id(self, galaxy_id: str) -> UserRecord | None:
        for row in self._rows(lookup_id_sql(galaxy_id)):
            if str(row.get("id")) == galaxy_id:
                return self._record(row, None)
        return None


#: The scan query for the default section, for documentation and tests.
BYOC_QUERY_SQL = scan_sql(SECTION)
