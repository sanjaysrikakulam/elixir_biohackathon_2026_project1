"""Where BYOC form data comes from.

Both sources return the same :class:`ScanResult`, so everything downstream is independent of how the data
was read:

* :class:`~byoc_sync.sources.gxadmin.GxadminSource` runs one SQL query through gxadmin (fast at any scale);
* :class:`~byoc_sync.sources.api.ApiSource` walks every user through Galaxy's admin API (no DB access needed).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..models import UserRecord

SECTION = "byoc_pulsar"


@dataclass
class ScanResult:
    records: list[UserRecord]
    # False when too many users could not be read; callers must not write or delete anything then.
    complete: bool = True
    errors: list[str] = field(default_factory=list)
    # E-mails and Galaxy user ids of users whose preferences mention the section but could not be parsed.
    # Never treat them as "removed": their config stays as it is.
    unreadable: set[str] = field(default_factory=set)
    galaxy_version: str | None = None


class Source(Protocol):
    def scan(self) -> ScanResult: ...

    def lookup(self, email: str) -> UserRecord | None: ...

    def lookup_id(self, galaxy_id: str) -> UserRecord | None: ...
