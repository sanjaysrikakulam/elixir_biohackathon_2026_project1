"""Site configuration: where the BYOC entries live in a playbook and how they are named.

A site config is a YAML file. ``eu`` (bundled, ``byoc_sync/sites/eu.yml``) describes usegalaxy.eu's
infrastructure-playbook; other deployments pass the path of their own file. See README "Site configuration".
"""

from __future__ import annotations

import re
import string
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

from .naming import SINGLE_BRANCH, NamingConfig

SITES_DIR = Path(__file__).parent / "sites"
TEMPLATES_DIR = Path(__file__).parent / "templates"
TARGET_KEYS = ("mq", "job_conf", "destinations", "users")
# How byoc-sync knows who owns an endpoint: by e-mail (TPV users.yml keys) or by Galaxy user id (a file only
# byoc-sync reads, routing by a fixed TPV rule). See docs/routing-by-user-id.md.
OWNER_KEYS = ("email", "galaxy_id")


class ConfigError(ValueError):
    pass


# What a rendered branch name may look like: lowercase letters, digits, "_" and "-" in "/"-separated parts. Git
# accepts all of these (no "..", ".lock", spaces, ~^:?*[\, empty parts or leading "-").
_SAFE_BRANCH = re.compile(r"[a-z0-9][a-z0-9_-]*(?:/[a-z0-9][a-z0-9_-]*)*")


def _branch_pattern_ok(pattern: str) -> bool:
    """Exactly one plain ``{x}`` or ``{x_dash}`` field plus fixed text, so that every BYOC name gets its own branch
    and no BYOC name on its own (such as "master") is ever a branch name; the result must be a safe branch name."""
    try:
        parts = list(string.Formatter().parse(pattern))
    except ValueError:
        return False
    found = [(name, spec, conv) for _, name, spec, conv in parts if name is not None]
    fixed = "".join(literal for literal, *_ in parts)
    if not fixed or len(found) != 1 or found[0][0] not in ("x", "x_dash") or any(found[0][1:]):
        return False
    return _SAFE_BRANCH.fullmatch(pattern.format(x="abc", x_dash="abc")) is not None


def _clash(pattern: str, ref: str) -> str | None:
    """Whose branch would be ``ref`` or share a path with it (git cannot hold ``a`` and ``a/b`` at once):
    "the BYOC name '...'", "every BYOC name", or None. ``pattern`` must have passed ``_branch_pattern_ok``."""
    parts, ref_parts = pattern.split("/"), ref.split("/")
    shared = min(len(parts), len(ref_parts))
    head, ref_head = "/".join(parts[:shared]), "/".join(ref_parts[:shared])
    if "{" not in head:  # the name comes after the parts both have in common
        return "every BYOC name" if head == ref_head else None
    name = NamingConfig(branch=head).name_from("branch", ref_head)
    return f"the BYOC name {name!r}" if name else None


@dataclass(frozen=True)
class TargetSpec:
    key: str
    file: str
    path: list[str]
    id: str  # attribute of Names (or "email_key") that names the entry
    template: str
    anchor: str  # new entries go after the last sibling matching this regex
    field: str | None = None  # for lists: the item field holding the id


@dataclass(frozen=True)
class SiteConfig:
    section: str
    base_branch: str
    templates_dir: Path
    site: dict[str, Any]
    naming: NamingConfig
    targets: dict[str, TargetSpec]
    vault_file: str
    user_preferences_file: str | None = None
    owner_key: str = "email"
    source: str = field(default="", compare=False)

    def __post_init__(self) -> None:
        # Runs again on dataclasses.replace (the --base-branch override).
        where = f"{self.source}: " if self.source else ""
        for key, value in [("base_branch", self.base_branch)] + [
            (f"naming.{f.name}", getattr(self.naming, f.name)) for f in fields(self.naming)
        ]:
            if not isinstance(value, str) or not value:
                raise ConfigError(f"{where}{key} must be non-empty text, got {value!r}")
        branch = self.naming.branch
        if not _branch_pattern_ok(branch):
            raise ConfigError(
                f"{where}naming.branch must contain {{x}} or {{x_dash}} exactly once plus a fixed prefix or suffix "
                f"made of lowercase letters, digits, '_', '-' and '/' (e.g. '{{x}}_byoc_ci'), got {branch!r}"
            )
        for fixed, what in ((self.base_branch, "base_branch"), (SINGLE_BRANCH, "the --single-pr branch")):
            who = _clash(branch, fixed)
            if who:
                raise ConfigError(
                    f"{where}{what} {fixed!r} clashes with the branch of {who} (naming.branch {branch!r}): "
                    "the same name, or one is a folder of the other"
                )
        if self.base_branch == SINGLE_BRANCH or self.base_branch.startswith(SINGLE_BRANCH + "/"):
            raise ConfigError(f"{where}base_branch {self.base_branch!r} clashes with the --single-pr branch")
        if self.owner_key not in OWNER_KEYS:
            raise ConfigError(f"{where}owner_key must be one of {', '.join(OWNER_KEYS)}, got {self.owner_key!r}")
        users_id = self.targets["users"].id if "users" in self.targets else None
        if self.owner_key == "galaxy_id" and users_id != "owner_key":
            raise ConfigError(f"{where}with owner_key: galaxy_id, targets.users.id must be owner_key")
        if self.owner_key == "email" and users_id not in (None, "email_key", "owner_key"):
            raise ConfigError(f"{where}targets.users.id must be email_key or owner_key")

    @staticmethod
    def builtin_path(name: str) -> Path:
        return SITES_DIR / f"{name}.yml"

    @classmethod
    def load(cls, name_or_path: str) -> SiteConfig:
        path = Path(name_or_path)
        if not path.suffix:
            path = cls.builtin_path(name_or_path)
        if not path.is_file():
            raise ConfigError(f"site config {name_or_path!r} not found")
        try:
            raw = yaml.safe_load(path.read_text()) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
        return cls.from_dict(raw, base=path.parent, source=str(path))

    @classmethod
    def from_dict(cls, raw: dict[str, Any], base: Path, source: str = "") -> SiteConfig:
        targets = {}
        for key in TARGET_KEYS:
            spec = (raw.get("targets") or {}).get(key)
            if not isinstance(spec, dict):
                raise ConfigError(f"{source}: targets.{key} is missing")
            missing = [k for k in ("file", "path", "id", "template", "anchor") if k not in spec]
            if missing:
                raise ConfigError(f"{source}: targets.{key} needs {', '.join(missing)}")
            targets[key] = TargetSpec(
                key=key, **{k: spec[k] for k in ("file", "path", "id", "template", "anchor")}, field=spec.get("field")
            )
        if not (raw.get("vault") or {}).get("file"):
            raise ConfigError(f"{source}: vault.file is missing")
        templates = raw.get("templates", "eu")
        templates_dir = TEMPLATES_DIR / templates if (TEMPLATES_DIR / templates).is_dir() else base / templates
        if not templates_dir.is_dir():
            raise ConfigError(f"{source}: templates directory {templates!r} not found")
        try:
            naming = NamingConfig(**(raw.get("naming") or {}))
        except TypeError as exc:
            raise ConfigError(f"{source}: naming: {exc}") from exc
        return cls(
            section=raw.get("section", "byoc_pulsar"),
            base_branch=raw.get("base_branch", "main"),
            templates_dir=templates_dir,
            site=dict(raw.get("site") or {}),
            naming=naming,
            targets=targets,
            vault_file=raw["vault"]["file"],
            user_preferences_file=raw.get("user_preferences_file"),
            owner_key=raw.get("owner_key", "email"),
            source=source,
        )
