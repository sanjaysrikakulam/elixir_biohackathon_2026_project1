"""Decide, per Galaxy user, what has to change in the playbook.

``plan()`` turns a scan into one :class:`UserPlan` per user that matters: users with a BYOC request and
owners of existing entries. Ownership lives in the playbook itself: a BYOC name belongs to the e-mail whose
generated ``users.yml`` key holds the name's rule. The main rules:

* a name stays with its owner; anyone else claiming it is a conflict;
* a new name claimed by more than one user, or whose objects already exist without an owner, is a conflict;
* an owner whose e-mail no longer resolves (changed, or account purged) needs a human, unless a new account
  claims the name with the very password stored in the vault, which only the owner knows: then the entry
  moves to the new e-mail;
* removal is only ever planned after a direct lookup confirms the owner's form is empty or the account is
  deleted, never because a user was missing from a scan, never after an incomplete scan, and only applied
  with ``allow_delete``. Renames delete the old entries and are deferred without ``allow_delete`` as well.

``materialize()`` applies any set of user plans to the files: one user for a per-user PR, all for a single
PR or the dry-run report.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml

from .config import SiteConfig
from .models import ByocRequest, UserRecord, ValidationFailure
from .naming import Names
from .render import DesiredObject, Removal, Renderer, deep_union, email_key, is_safe_email
from .sources import ScanResult, Source
from .targets import FilePlan, Target
from .validate import validate
from .vault import VaultFile, VaultPlan
from .yamledit.doc import LocateError
from .yamledit.edit import EditError
from .yamledit.verify import VerificationError

FILE_TARGETS = ("mq", "job_conf", "destinations")
PUBLISHABLE = {"create", "update", "delete", "rename"}


class PlanError(RuntimeError):
    pass


@dataclass(frozen=True)
class PlanOptions:
    allow_delete: bool = False
    strict: bool = False
    allow_list: set[str] | frozenset[str] | None = None


class Repo:
    """Read access to the playbook checkout the plan is made against."""

    def __init__(self, root: str | Path, site: SiteConfig):
        self.root = Path(root)
        self.site = site

    def read(self, relative: str) -> str:
        path = self.root / relative
        if not path.is_file():
            raise PlanError(f"{relative} not found in {self.root}")
        # newline="": keep CRLF files CRLF (text mode would silently turn them into LF).
        with path.open(encoding="utf-8", newline="") as handle:
            return handle.read()


@dataclass
class UserPlan:
    email: str
    action: str
    galaxy_id: str | None = None
    name: str | None = None
    reasons: list[str] = field(default_factory=list)
    objects: dict[str, dict[str, DesiredObject | Removal]] = field(default_factory=dict)
    vault: dict[str, str | None] = field(default_factory=dict, repr=False)
    request: ByocRequest | None = None
    previous_name: str | None = None

    @property
    def publishable(self) -> bool:
        return self.action in PUBLISHABLE


@dataclass
class RunPlan:
    users: list[UserPlan]
    orphans: dict[str, list[str]] = field(default_factory=dict)
    complete: bool = True

    def publishable(self) -> list[UserPlan]:
        return [u for u in self.users if u.publishable]


@dataclass
class Materialized:
    files: dict[str, FilePlan]
    vault: VaultPlan
    conflicts: dict[str, list[str]] = field(default_factory=dict)  # e-mail -> reasons

    @property
    def changed(self) -> bool:
        return bool(self.files) or self.vault.changed


def check_form(repo: Repo) -> None:
    """byoc-sync reads form values from the preferences JSON; values kept in Galaxy's vault are invisible."""
    relative = repo.site.user_preferences_file
    if not relative or not (repo.root / relative).is_file():
        return
    try:
        form = yaml.safe_load(repo.read(relative)) or {}
    except yaml.YAMLError as exc:
        raise PlanError(f"{relative}: invalid YAML: {exc}") from None
    section = (form.get("preferences") or {}).get(repo.site.section) or {}
    vaulted = [i.get("name") for i in section.get("inputs") or [] if isinstance(i, dict) and i.get("store") == "vault"]
    if vaulted:
        raise PlanError(
            f"{relative}: inputs {', '.join(map(str, vaulted))} of '{repo.site.section}' use 'store: vault'; "
            "their values are encrypted inside Galaxy and cannot be read (see docs/GAPS.md)"
        )


def materialize(
    users: list[UserPlan], repo: Repo, vault: VaultFile, texts: dict[str, str] | None = None
) -> Materialized:
    site = repo.site
    owner_of: dict[tuple[str, str], str] = {}
    objects: dict[str, dict[str, DesiredObject | Removal]] = {key: {} for key in site.targets}
    wanted: dict[str, str | None] = {}
    for user in users:
        for key, entries in user.objects.items():
            for object_id, obj in entries.items():
                objects[key][object_id] = obj
                owner_of[(key, object_id)] = user.email
        wanted.update(user.vault)
    files: dict[str, FilePlan] = {}
    conflicts: dict[str, list[str]] = {}
    for key, spec in site.targets.items():
        if not objects[key]:
            continue
        text = texts[key] if texts and key in texts else repo.read(spec.file)
        file_plan = Target(site, key).plan(text, objects[key], file=spec.file)
        for conflict in file_plan.conflicts:
            email = owner_of[(key, conflict.object_id)]
            conflicts.setdefault(email, []).append(f"{spec.file}: {conflict.object_id}: {conflict.reason}")
        if file_plan.changed:
            files[spec.file] = file_plan
    return Materialized(files, vault.plan(wanted), conflicts)


class _Planner:
    def __init__(self, scan: ScanResult, repo: Repo, source: Source, vault: VaultFile, opts: PlanOptions):
        self.scan, self.repo, self.source, self.vault, self.opts = scan, repo, source, vault, opts
        self.site = repo.site
        self.renderer = Renderer(self.site)
        self.texts = {key: repo.read(spec.file) for key, spec in self.site.targets.items()}
        self.owners = Target(self.site, "users").owners(self.texts["users"])
        self.emails_owning = {}
        for name, email in self.owners.items():
            self.emails_owning.setdefault(email, set()).add(name)
        self.present = self._present()
        self.users: list[UserPlan] = []
        self.done_emails: set[str] = set()
        self.done_names: set[str] = set()

    # -- what exists ------------------------------------------------------------------------------------------

    def _present(self) -> dict[str, list[str]]:
        """BYOC name -> where objects with that name exist outside users.yml."""
        found: dict[str, list[str]] = {}
        for key in FILE_TARGETS:
            spec = self.site.targets[key]
            for name in Target(self.site, key).names(self.texts[key]):
                found.setdefault(name, []).append(
                    f"{spec.file} ({getattr(Names.for_(name, self.site.naming), spec.id)})"
                )
        for var in self.vault.keys():
            name = self.site.naming.name_from("vault_var", var)
            if name:
                found.setdefault(name, []).append(f"{self.site.vault_file} ({var})")
        return found

    # -- building intents -------------------------------------------------------------------------------------

    def _add(self, plan: UserPlan) -> None:
        self.users.append(plan)
        self.done_emails.add(plan.email)

    def _intent(
        self,
        record: UserRecord,
        req: ByocRequest,
        action: str,
        *,
        rename_from: str | None = None,
        moved_from: str | None = None,
    ) -> UserPlan:
        names = Names.for_(req.name, self.site.naming)
        email = record.email
        objects: dict[str, dict[str, DesiredObject | Removal]] = {key: {} for key in self.site.targets}
        wanted: dict[str, str | None] = {names.vault_var: req.password}
        for key in FILE_TARGETS:
            obj = self.renderer.desired(key, req, names, email, strict=self.opts.strict)
            objects[key][obj.id] = obj
        users_obj = self.renderer.desired("users", req, names, email, strict=self.opts.strict)
        if rename_from:
            old = Names.for_(rename_from, self.site.naming)
            users_obj = replace(
                users_obj, universe=deep_union(users_obj.universe, self.renderer.universe("users", old, email))
            )
            for key in FILE_TARGETS:
                removal = self.renderer.removal(key, old, email)
                objects[key][removal.id] = removal
            wanted[old.vault_var] = None
        objects["users"][users_obj.id] = users_obj
        if moved_from:
            removal = self.renderer.removal("users", names, moved_from)
            objects["users"][removal.id] = removal
        return UserPlan(
            email=email,
            action=action,
            galaxy_id=record.galaxy_id,
            name=req.name,
            objects=objects,
            vault=wanted,
            request=req,
            previous_name=rename_from,
        )

    def _removal(self, email: str, name: str, galaxy_id: str | None) -> UserPlan:
        names = Names.for_(name, self.site.naming)
        objects = {key: {} for key in self.site.targets}
        for key in self.site.targets:
            removal = self.renderer.removal(key, names, email)
            objects[key][removal.id] = removal
        return UserPlan(
            email=email, action="delete", galaxy_id=galaxy_id, name=name, objects=objects, vault={names.vault_var: None}
        )

    def _finish(self, plan: UserPlan) -> UserPlan:
        """Turn create/update/delete/rename into noop or conflict depending on what the files say."""
        try:
            result = materialize([plan], self.repo, self.vault, self.texts)
        except (LocateError, EditError, VerificationError, ValueError) as exc:
            # Something about this user's entries cannot be edited safely; the others go on.
            plan.reasons.append(f"cannot edit the entries automatically: {exc}")
            plan.action = "conflict"
            return plan
        if result.conflicts.get(plan.email):
            plan.reasons += result.conflicts[plan.email]
            plan.action = "conflict"
        elif not result.changed:
            plan.action = "noop"
        return plan

    # -- the rules --------------------------------------------------------------------------------------------

    def run(self) -> RunPlan:
        claims: dict[str, list[tuple[UserRecord, ByocRequest]]] = {}
        for record in sorted(self.scan.records, key=lambda r: r.email):
            email = record.email
            allowed = self.opts.allow_list is None or email in self.opts.allow_list or email in self.emails_owning
            if not allowed:
                self._add(UserPlan(email, "not-allowed", record.galaxy_id, reasons=["not on the allow list"]))
                continue
            if not is_safe_email(email):
                self._add(
                    UserPlan(
                        email,
                        "invalid",
                        record.galaxy_id,
                        reasons=[
                            "e-mail cannot be used as a TPV users key: it contains braces, whitespace, non-ASCII "
                            "characters or regular expression operators such as + ? * | ^ $"
                        ],
                    )
                )
                continue
            if record.password_unreadable:
                self._add(
                    UserPlan(
                        email,
                        "password-unreadable",
                        record.galaxy_id,
                        reasons=["Galaxy did not return the password (Galaxy 26.2+ redacts it)"],
                    )
                )
                continue
            if record.deleted or record.purged:
                continue  # handled with the owners below
            outcome = validate(record, self.site.section)
            if outcome is None:
                continue
            if isinstance(outcome, ValidationFailure):
                self._add(
                    UserPlan(
                        email,
                        "invalid",
                        record.galaxy_id,
                        reasons=[f"{k}: {v}" for k, v in sorted(outcome.errors.items())],
                    )
                )
                continue
            if not record.active:
                self._add(
                    UserPlan(
                        email,
                        "skipped-inactive",
                        record.galaxy_id,
                        name=outcome.name,
                        reasons=["account is not activated"],
                    )
                )
                continue
            claims.setdefault(outcome.name, []).append((record, outcome))

        for name, claimants in claims.items():
            self._resolve_claim(name, claimants)
        for name, email in sorted(self.owners.items()):
            if name not in self.done_names and email not in self.done_emails:
                self._resolve_unclaimed(name, email)
        orphans = {
            name: places
            for name, places in self.present.items()
            if name not in self.owners and name not in self.done_names
        }
        return RunPlan(users=self.users, orphans=orphans, complete=self.scan.complete)

    def _resolve_claim(self, name: str, claimants: list[tuple[UserRecord, ByocRequest]]) -> None:
        owner = self.owners.get(name)
        self.done_names.add(name)
        if owner:
            mine = [c for c in claimants if c[0].email == owner]
            others = [c for c in claimants if c[0].email != owner]
            if mine:
                self._add(self._finish(self._intent(*mine[0], "update")))
                self._conflicts(others, f"the name '{name}' belongs to another user")
                return
            self._resolve_move(name, owner, others)
            return
        record, req = claimants[0]
        if len(claimants) > 1:
            self._conflicts(claimants, f"the name '{name}' is requested by several users")
            return
        if name in self.present:
            self._conflicts(
                claimants,
                f"entries named for '{name}' already exist without an owner: " + ", ".join(self.present[name]),
            )
            return
        old_names = self.emails_owning.get(record.email, set()) - {name}
        if old_names:
            old = sorted(old_names)[0]
            self.done_names.add(old)
            if not self.opts.allow_delete:
                self._add(
                    UserPlan(
                        record.email,
                        "rename-deferred",
                        record.galaxy_id,
                        name=name,
                        reasons=[f"rename from '{old}' removes the old entries; needs --allow-delete"],
                    )
                )
                return
            self._add(self._finish(self._intent(record, req, "rename", rename_from=old)))
            return
        self._add(self._finish(self._intent(record, req, "create")))

    def _resolve_move(self, name: str, owner: str, claimants: list[tuple[UserRecord, ByocRequest]]) -> None:
        """The owner did not claim the name in this scan; maybe they changed their e-mail."""
        try:
            current = self._lookup(owner, raise_errors=True)
        except Exception as exc:  # noqa: BLE001 - unknown owner state: never move anything
            for record, _req in claimants:
                self._add(
                    UserPlan(
                        record.email,
                        "unreadable",
                        record.galaxy_id,
                        name=name,
                        reasons=[f"could not look up the owner {owner}: {exc}"],
                    )
                )
            self.done_emails.add(owner)
            return
        if current is not None:
            # The owner account still exists: the name stays theirs.
            self._conflicts(claimants, f"the name '{name}' belongs to another user")
            if owner not in self.done_emails:
                self._resolve_unclaimed(name, owner, record=current)
            return
        stored = self.vault.get(Names.for_(name, self.site.naming).vault_var)
        proven = [c for c in claimants if stored is not None and c[1].password == stored]
        if len(proven) == 1 and len(claimants) == 1:
            record, req = proven[0]
            plan = self._intent(record, req, "update", moved_from=owner)
            plan.reasons.append("owner e-mail changed; ownership proven by the stored password")
            self._add(self._finish(plan))
            self.done_emails.add(owner)
            return
        for record, _req in claimants:
            self._add(
                UserPlan(
                    record.email,
                    "needs-human",
                    record.galaxy_id,
                    name=name,
                    reasons=[
                        f"the name '{name}' belongs to {owner}, which no longer resolves; "
                        "the request does not carry the stored password, so it cannot be moved automatically"
                    ],
                )
            )
        self.done_emails.add(owner)

    def _resolve_unclaimed(self, name: str, email: str, record: UserRecord | None = None) -> None:
        """An owner who did not request their name in this scan: confirm before planning any removal."""
        self.done_names.add(name)
        if email in self.scan.unreadable:
            self._add(
                UserPlan(email, "unreadable", name=name, reasons=["the user's data could not be read; left as is"])
            )
            return
        if record is None:
            try:
                record = self._lookup(email, raise_errors=True)
            except Exception as exc:  # noqa: BLE001 - any failure means: we do not know, so we keep
                self._add(UserPlan(email, "unreadable", name=name, reasons=[f"lookup failed: {exc}"]))
                return
        if record is None:
            self._add(
                UserPlan(
                    email,
                    "needs-human",
                    name=name,
                    reasons=[
                        "owner account not found (e-mail changed or account purged); remove or move the entries by hand"
                    ],
                )
            )
            return
        outcome = None if (record.deleted or record.purged) else validate(record, self.site.section)
        if isinstance(outcome, ByocRequest):
            if outcome.name == name:
                plan = self._finish(self._intent(record, outcome, "update"))
                plan.reasons.append("found by direct lookup (missing from the scan)")
                self._add(plan)
            else:
                self._add(
                    UserPlan(
                        email,
                        "needs-human",
                        record.galaxy_id,
                        name=name,
                        reasons=[f"lookup shows a request for '{outcome.name}' that the scan did not"],
                    )
                )
            return
        if isinstance(outcome, ValidationFailure):
            self._add(
                UserPlan(
                    email,
                    "invalid",
                    record.galaxy_id,
                    name=name,
                    reasons=[f"{k}: {v}" for k, v in sorted(outcome.errors.items())],
                )
            )
            return
        why = "account deleted" if record.deleted or record.purged else "BYOC form cleared"
        if not self.scan.complete:
            self._add(
                UserPlan(
                    email,
                    "pending-delete",
                    record.galaxy_id,
                    name=name,
                    reasons=[why, "scan incomplete: removals are not planned"],
                )
            )
            return
        if not self.opts.allow_delete:
            self._add(
                UserPlan(email, "pending-delete", record.galaxy_id, name=name, reasons=[why, "needs --allow-delete"])
            )
            return
        plan = self._removal(email, name, record.galaxy_id)
        plan.reasons.append(why)
        self._add(self._finish(plan))

    def _lookup(self, email: str, raise_errors: bool = False) -> UserRecord | None:
        try:
            return self.source.lookup(email)
        except Exception:
            if raise_errors:
                raise
            return None

    def _conflicts(self, claimants: list[tuple[UserRecord, ByocRequest]], reason: str) -> None:
        for record, req in claimants:
            self._add(UserPlan(record.email, "conflict", record.galaxy_id, name=req.name, reasons=[reason]))


def plan(scan: ScanResult, repo: Repo, source: Source, vault: VaultFile, opts: PlanOptions | None = None) -> RunPlan:
    check_form(repo)
    return _Planner(scan, repo, source, vault, opts or PlanOptions()).run()


__all__ = [
    "Materialized",
    "PlanError",
    "PlanOptions",
    "Repo",
    "RunPlan",
    "UserPlan",
    "email_key",
    "materialize",
    "plan",
]
