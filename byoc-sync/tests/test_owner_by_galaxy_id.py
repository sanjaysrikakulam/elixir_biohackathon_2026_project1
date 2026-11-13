"""owner_key: galaxy_id: ownership by Galaxy user id, no e-mail address in any file (docs/routing-by-user-id.md)."""

from __future__ import annotations

import shutil
import sys
import types
from pathlib import Path

import pytest
import yaml

from byoc_sync.config import ConfigError, SiteConfig
from byoc_sync.planner import PlanOptions, Repo, materialize, plan
from byoc_sync.sources import ScanResult
from byoc_sync.sources.gxadmin import GxadminError, lookup_id_sql
from byoc_sync.targets import Target
from byoc_sync.vault import VaultFile

from .test_planner import FIXTURES, VAULT_PASSWORD, prefs, user

ROOT = Path(__file__).parents[1]
EU_TEMPLATES = ROOT / "src" / "byoc_sync" / "templates" / "eu"
ID_TEMPLATES = ROOT / "galaxy-side" / "site-templates"
OWNERS = "files/galaxy/tpv/byoc_owners.yml"
DEST = "files/galaxy/tpv/destinations.yml.j2"
ALICE = "0e0fad4d103ae8ed"
BOB = "a1b2c3d4e5f60718"
# The editor only adds entries next to an existing one, so the file starts with a placeholder it ignores.
OWNERS_START = (
    "---\nbyoc_owners:\n  # keeps the mapping non-empty so byoc-sync can add owners below it\n  _placeholder: {}\n"
)


def id_site_raw() -> dict:
    raw = yaml.safe_load((ROOT / "src" / "byoc_sync" / "sites" / "eu.yml").read_text())
    raw["owner_key"] = "galaxy_id"
    raw["templates"] = "templates"
    raw["targets"]["users"] = {
        "file": OWNERS,
        "path": ["byoc_owners"],
        "id": "owner_key",
        "template": "owner.yml.j2",
        "anchor": "$^",
    }
    return raw


@pytest.fixture
def site(tmp_path):
    templates = tmp_path / "site" / "templates"
    templates.mkdir(parents=True)
    for name in ("runner.yml.j2", "mq_user.yml.j2"):
        shutil.copy(EU_TEMPLATES / name, templates / name)
    for name in ("destination.yml.j2", "owner.yml.j2"):
        shutil.copy(ID_TEMPLATES / name, templates / name)
    return SiteConfig.from_dict(id_site_raw(), base=tmp_path / "site", source="id-site")


@pytest.fixture
def repo(tmp_path, eu_fixtures, site):
    root = tmp_path / "playbook"
    shutil.copytree(eu_fixtures, root)
    (root / OWNERS).write_text(OWNERS_START)
    form = root / "files/galaxy/config/user_preferences_extra_conf.yml"
    form.parent.mkdir(parents=True, exist_ok=True)
    form.write_text((FIXTURES / "user_preferences_extra_conf.yml").read_text())
    return Repo(root, site)


class FakeIdSource:
    """Answers lookups by user id (None: no such account); e-mail lookups must not happen."""

    def __init__(self, accounts=None):
        self.accounts = dict(accounts or {})
        self.lookups = []

    def scan(self):  # pragma: no cover
        raise AssertionError("not used")

    def lookup(self, email):  # pragma: no cover
        raise AssertionError("looked up by e-mail with owner_key: galaxy_id")

    def lookup_id(self, galaxy_id):
        self.lookups.append(galaxy_id)
        return self.accounts.get(galaxy_id)


def vault(repo):
    return VaultFile(repo.root / repo.site.vault_file, VAULT_PASSWORD)


def run(repo, records, source=None, **opts):
    scan = ScanResult(records=list(records), complete=True)
    return plan(scan, repo, source or FakeIdSource({r.galaxy_id: r for r in records}), vault(repo), PlanOptions(**opts))


def apply_all(repo, run_plan):
    result = materialize(run_plan.publishable(), repo, vault(repo))
    for path, file_plan in result.files.items():
        (repo.root / path).write_text(file_plan.text)
    if result.vault.changed:
        (repo.root / repo.site.vault_file).write_bytes(result.vault.ciphertext())
    return result


def actions(run_plan):
    return {u.email: u.action for u in run_plan.users}


def why(run_plan):
    return {u.email: u.reasons for u in run_plan.users}


def alice(**kw):
    return user(email="alice@example.org", galaxy_id=ALICE, **kw)


def bob(**kw):
    return user(email="bob@example.org", galaxy_id=BOB, **kw)


# -- configuration -------------------------------------------------------------------------------------------


def test_galaxy_id_needs_the_users_target_keyed_by_owner_key(tmp_path):
    raw = id_site_raw()
    raw["targets"]["users"]["id"] = "email_key"
    (tmp_path / "templates").mkdir()
    with pytest.raises(ConfigError, match="owner_key"):
        SiteConfig.from_dict(raw, base=tmp_path, source="bad")


def test_unknown_owner_key_is_refused(tmp_path):
    raw = id_site_raw()
    raw["owner_key"] = "username"
    (tmp_path / "templates").mkdir()
    with pytest.raises(ConfigError, match="owner_key must be one of"):
        SiteConfig.from_dict(raw, base=tmp_path, source="bad")


def test_eu_still_uses_email(tmp_path):
    assert SiteConfig.load("eu").owner_key == "email"


# -- planning --------------------------------------------------------------------------------------------------


def test_new_user_gets_an_owner_entry_and_no_address_anywhere(repo):
    run_plan = run(repo, [alice()])
    assert actions(run_plan) == {ALICE: "create"}, why(run_plan)
    result = materialize(run_plan.publishable(), repo, vault(repo))
    assert set(result.files) == {"group_vars/mq.yml", "templates/galaxy/config/job_conf.yml", DEST, OWNERS}
    for file_plan in result.files.values():
        assert "@example.org" not in file_plan.text
    owners = yaml.safe_load(result.files[OWNERS].text)["byoc_owners"]
    assert owners == {"_placeholder": {}, ALICE: {"name": "alice01"}}
    guard = f'if: "user is None or \\"{ALICE}\\" not in (str(user.id), app.security.encode_id(user.id))"'
    assert guard in result.files[DEST].text
    assert all(u.email != "alice@example.org" for u in run_plan.users)


def test_second_run_changes_nothing(repo):
    apply_all(repo, run(repo, [alice()]))
    assert actions(run(repo, [alice()])) == {ALICE: "noop"}


def test_a_name_owned_by_another_user_id_is_refused(repo):
    apply_all(repo, run(repo, [alice()]))
    again = run(repo, [alice(), bob()])
    assert actions(again)[ALICE] == "noop"
    assert actions(again)[BOB] == "conflict"


def test_owner_is_looked_up_by_id_when_the_form_is_cleared(repo):
    apply_all(repo, run(repo, [alice()]))
    cleared = alice(p={})
    source = FakeIdSource({ALICE: cleared})
    run_plan = run(repo, [], source, allow_delete=True)
    assert source.lookups == [ALICE]
    assert actions(run_plan) == {ALICE: "delete"}
    result = apply_all(repo, run_plan)
    assert yaml.safe_load(result.files[OWNERS].text)["byoc_owners"] == {"_placeholder": {}}


def test_rename_moves_the_owner_entry(repo):
    apply_all(repo, run(repo, [alice()]))
    renamed = alice(p=prefs(name="alice02"))
    run_plan = run(repo, [renamed], allow_delete=True)
    assert actions(run_plan) == {ALICE: "rename"}
    result = apply_all(repo, run_plan)
    assert yaml.safe_load(result.files[OWNERS].text)["byoc_owners"] == {"_placeholder": {}, ALICE: {"name": "alice02"}}


def test_a_hand_written_numeric_id_is_still_an_owner(site):
    owners = Target(site, "users").owners("byoc_owners:\n  42:\n    name: carol01\n")
    assert owners == {"carol01": "42"}


def test_gxadmin_looks_up_database_ids_only():
    assert "WHERE u.id = 42" in lookup_id_sql("42")
    with pytest.raises(GxadminError):
        lookup_id_sql("0e0fad4d103ae8ed")


# -- the Galaxy side: owner guard and routing rule --------------------------------------------------------------


class _Tag:
    def __init__(self, value):
        self.value = value


class _Tags:
    def __init__(self, values=()):
        self.tags = [_Tag(v) for v in values]
        self.required = []

    def filter(self, tag_value):
        return [t for t in self.tags if t.value == tag_value]

    def combine(self, other):
        self.required += other.require
        return self


class _Destination:
    def __init__(self, tags):
        self.tpv_dest_tags = _Tags(tags)


class _User:
    def __init__(self, db_id, byoc_name=None):
        self.id = db_id
        self.extra_preferences = {"byoc_pulsar|username": byoc_name} if byoc_name else {}


class _Security:
    @staticmethod
    def encode_id(db_id):
        return {7: ALICE, 8: BOB}[db_id]


class _App:
    security = _Security()


def _guard_condition(galaxy_id: str) -> str:
    text = (ID_TEMPLATES / "destination.yml.j2").read_text()
    line = next(line for line in text.splitlines() if line.strip().startswith("if:"))
    assert "galaxy_id | pystr" in line
    return f"user is None or {galaxy_id!r} not in (str(user.id), app.security.encode_id(user.id))"


@pytest.mark.parametrize(
    ("owner", "job_user", "refused"),
    [(ALICE, _User(7), False), ("7", _User(7), False), (ALICE, _User(8), True), (ALICE, None, True)],
)
def test_owner_guard_accepts_the_owner_by_encoded_or_database_id(owner, job_user, refused):
    assert eval(_guard_condition(owner), {"user": job_user, "app": _App()}) is refused  # noqa: S307


class _SchedulingTags:
    def __init__(self, require):
        self.require = require


@pytest.fixture(autouse=True)
def _fake_tpv(monkeypatch):
    """The rule's execute block imports SchedulingTags; give it a stand-in without needing TPV."""
    module = types.ModuleType("tpv.core.entities")
    module.SchedulingTags = _SchedulingTags
    monkeypatch.setitem(sys.modules, "tpv.core.entities", module)


def _rule():
    data = yaml.safe_load((ROOT / "galaxy-side" / "tpv" / "byoc_rule.yml").read_text())
    return data["tools"]["default"]["rules"][0]


@pytest.mark.parametrize(
    ("byoc_name", "destinations", "routed"),
    [
        ("alice01", ["alice01-byoc-pulsar"], True),
        ("alice01", [], False),  # PR not merged yet: no destination with the tag
        (None, ["alice01-byoc-pulsar"], False),  # BYOC not filled in
    ],
)
def test_galaxy_side_rule_requires_the_users_own_tag(byoc_name, destinations, routed):
    rule = _rule()
    scope = {
        "user": _User(7, byoc_name),
        "entity": type("Entity", (), {"tpv_tags": _Tags()})(),
        "mapper": type("Mapper", (), {"destinations": {"d": _Destination(destinations)}})(),
        "job_wrapper": object(),
    }
    exec(rule["if"], scope)  # noqa: S102
    assert scope["retval"] is routed
    if routed:
        exec(rule["execute"], scope)  # noqa: S102
        assert scope["entity"].tpv_tags.required == ["alice01-byoc-pulsar"]
