"""Per-user decisions of the planner, on a copy of usegalaxy.eu's files and the dummy vault."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from byoc_sync.config import SiteConfig
from byoc_sync.models import UserRecord
from byoc_sync.planner import PlanError, PlanOptions, Repo, materialize, plan
from byoc_sync.sources import ScanResult
from byoc_sync.vault import VaultFile
from byoc_sync.yamledit.doc import Doc

FIXTURES = Path(__file__).parent / "fixtures"
VAULT_PASSWORD = (FIXTURES / "vault-pass.txt").read_text().strip().encode()
PW = "MyPulS4rP4ssw0rd"
DEST = "files/galaxy/tpv/destinations.yml.j2"
USERS = "files/galaxy/tpv/users.yml"


def prefs(name="alice01", password=PW, cores="8", mem="31", nfs=None, **extra):
    values = {
        "byoc_pulsar|username": name,
        "byoc_pulsar|password": password,
        "byoc_pulsar|max_accepted_cores": cores,
        "byoc_pulsar|max_accepted_mem": mem,
        "byoc_pulsar|min_accepted_gpus": "0",
        "byoc_pulsar|max_accepted_gpus": "0",
    }
    if nfs:
        values["byoc_pulsar|nfs_path"] = nfs
    values.update(extra)
    return values


def user(email="alice@example.org", galaxy_id="1", p=None, **kw):
    return UserRecord(
        galaxy_id=galaxy_id,
        email=email,
        username=email.split("@")[0],
        deleted=kw.pop("deleted", False),
        purged=kw.pop("purged", False),
        active=kw.pop("active", True),
        prefs=prefs() if p is None else p,
        source="test",
        **kw,
    )


class FakeSource:
    """Answers lookups from a dict of e-mail -> record (None: no such account)."""

    def __init__(self, accounts=None):
        self.accounts = dict(accounts or {})
        self.lookups = []

    def scan(self):  # pragma: no cover - the planner only looks users up
        raise AssertionError("not used")

    def lookup(self, email):
        self.lookups.append(email)
        return self.accounts.get(email)


@pytest.fixture
def site():
    return SiteConfig.load("eu")


@pytest.fixture
def repo(tmp_path, eu_fixtures, site):
    root = tmp_path / "playbook"
    shutil.copytree(eu_fixtures, root)
    form = root / "files/galaxy/config/user_preferences_extra_conf.yml"
    form.parent.mkdir(parents=True, exist_ok=True)
    form.write_text((FIXTURES / "user_preferences_extra_conf.yml").read_text())
    return Repo(root, site)


def vault(repo):
    return VaultFile(repo.root / repo.site.vault_file, VAULT_PASSWORD)


def run(repo, records, source=None, *, complete=True, unreadable=(), **opts):
    scan = ScanResult(records=list(records), complete=complete, unreadable=set(unreadable))
    accounts = {r.email: r for r in records}
    return plan(scan, repo, source or FakeSource(accounts), vault(repo), PlanOptions(**opts))


def apply_all(repo, run_plan):
    result = materialize(run_plan.publishable(), repo, vault(repo))
    for path, file_plan in result.files.items():
        (repo.root / path).write_text(file_plan.text)
    if result.vault.changed:
        (repo.root / repo.site.vault_file).write_bytes(result.vault.ciphertext())
    return result


def actions(run_plan):
    return {u.email: u.action for u in run_plan.users}


# -- creating ------------------------------------------------------------------------------------------------


def test_new_user_is_created_in_every_file_and_the_vault(repo):
    run_plan = run(repo, [user()])
    assert actions(run_plan) == {"alice@example.org": "create"}
    result = materialize(run_plan.publishable(), repo, vault(repo))
    assert set(result.files) == {"group_vars/mq.yml", "templates/galaxy/config/job_conf.yml", DEST, USERS}
    assert [c.describe() for c in result.vault.changes] == ["create rabbitmq_password_galaxy_alice01_byoc: ********"]


def test_second_run_with_the_same_data_changes_nothing(repo):
    apply_all(repo, run(repo, [user()]))
    again = run(repo, [user()])
    assert actions(again) == {"alice@example.org": "noop"}
    result = materialize(again.publishable(), repo, vault(repo))
    assert not result.files and not result.vault.changed


def test_inactive_new_account_is_skipped(repo):
    assert actions(run(repo, [user(active=False)])) == {"alice@example.org": "skipped-inactive"}


def test_invalid_form_is_reported_and_nothing_changes(repo):
    run_plan = run(repo, [user(p=prefs(password="short"))])
    (u,) = run_plan.users
    assert u.action == "invalid" and "password" in " ".join(u.reasons) and "short" not in " ".join(u.reasons)
    assert not run_plan.publishable()


def test_password_unreadable_users_are_reported_and_left_alone(repo):
    assert actions(run(repo, [user(password_unreadable=True)])) == {"alice@example.org": "password-unreadable"}


def test_allow_list_limits_who_gets_entries(repo):
    run_plan = run(repo, [user(), user("bob@example.org", "2", prefs("bob01"))], allow_list={"bob@example.org"})
    assert actions(run_plan) == {"alice@example.org": "not-allowed", "bob@example.org": "create"}


def test_strict_mode_renders_the_strict_rule(repo):
    apply_all(repo, run(repo, [user()], strict=True))
    rule = Doc((repo.root / USERS).read_text()).data["users"]["alice@example.org"]["rules"][0]
    assert set(rule["scheduling"]) == {"require"}


# -- updating ------------------------------------------------------------------------------------------------


def test_changed_limits_update_only_the_destination(repo):
    apply_all(repo, run(repo, [user()]))
    run_plan = run(repo, [user(p=prefs(cores="16"))])
    assert actions(run_plan) == {"alice@example.org": "update"}
    result = materialize(run_plan.publishable(), repo, vault(repo))
    assert set(result.files) == {DEST} and not result.vault.changed


def test_changed_password_updates_only_the_vault(repo):
    apply_all(repo, run(repo, [user()]))
    run_plan = run(repo, [user(p=prefs(password="AnotherPassw0rd"))])
    assert actions(run_plan) == {"alice@example.org": "update"}
    result = materialize(run_plan.publishable(), repo, vault(repo))
    assert not result.files and [c.action for c in result.vault.changes] == ["update"]


def test_added_nfs_path_updates_the_destination(repo):
    apply_all(repo, run(repo, [user()]))
    result = materialize(run(repo, [user(p=prefs(nfs="/mnt/byoc"))]).publishable(), repo, vault(repo))
    entry = Doc(result.files[DEST].text).data["destinations"]["pulsar_alice01_byoc_tpv"]
    assert entry["params"]["jobs_directory"] == "/mnt/byoc/staging"


def test_invalid_change_by_the_owner_keeps_the_existing_entry(repo):
    apply_all(repo, run(repo, [user()]))
    run_plan = run(repo, [user(p=prefs(cores="lots"))])
    assert actions(run_plan) == {"alice@example.org": "invalid"} and not run_plan.publishable()


# -- deleting ------------------------------------------------------------------------------------------------


def configured(repo):
    apply_all(repo, run(repo, [user()]))
    return repo


@pytest.mark.parametrize(
    "record",
    [
        user(p={}),
        user(p={"byoc_pulsar|username": "", "byoc_pulsar|password": ""}),
        user(deleted=True),
        user(deleted=True, purged=True),
    ],
)
def test_removal_needs_allow_delete(repo, record):
    configured(repo)
    source = FakeSource({"alice@example.org": record})
    assert actions(run(repo, [], source)) == {"alice@example.org": "pending-delete"}
    run_plan = run(repo, [], source, allow_delete=True)
    assert actions(run_plan) == {"alice@example.org": "delete"}
    apply_all(repo, run_plan)
    for path in ("group_vars/mq.yml", "templates/galaxy/config/job_conf.yml", DEST, USERS):
        assert (repo.root / path).read_text() == (FIXTURES / "eu" / path).read_text()
    assert "rabbitmq_password_galaxy_alice01_byoc" not in vault(repo).keys()


def test_absence_from_the_scan_alone_never_deletes(repo):
    configured(repo)
    # The scan missed alice but a direct lookup shows her request is still there.
    source = FakeSource({"alice@example.org": user()})
    run_plan = run(repo, [], source, allow_delete=True)
    assert actions(run_plan) == {"alice@example.org": "noop"}
    assert source.lookups == ["alice@example.org"]


def test_incomplete_scan_never_deletes(repo):
    configured(repo)
    source = FakeSource({"alice@example.org": user(p={})})
    run_plan = run(repo, [], source, complete=False, allow_delete=True)
    assert actions(run_plan) == {"alice@example.org": "pending-delete"}
    assert any("incomplete" in r for r in run_plan.users[0].reasons)


def test_unreadable_owner_is_left_alone(repo):
    configured(repo)
    run_plan = run(repo, [], FakeSource({}), unreadable={"alice@example.org"}, allow_delete=True)
    assert actions(run_plan) == {"alice@example.org": "unreadable"}


def test_owner_that_no_longer_resolves_needs_a_human(repo):
    configured(repo)
    run_plan = run(repo, [], FakeSource({}), allow_delete=True)
    assert actions(run_plan) == {"alice@example.org": "needs-human"}


# -- names, ownership and conflicts --------------------------------------------------------------------------


def test_two_new_users_claiming_one_name_are_both_conflicts(repo):
    run_plan = run(repo, [user(), user("eve@example.org", "2", prefs())])
    assert actions(run_plan) == {"alice@example.org": "conflict", "eve@example.org": "conflict"}
    assert [u.reasons for u in run_plan.users] == [["the name 'alice01' is requested by several users"]] * 2


def test_a_name_stays_with_its_owner(repo):
    configured(repo)
    run_plan = run(repo, [user(), user("eve@example.org", "2", prefs())])
    assert actions(run_plan) == {"alice@example.org": "noop", "eve@example.org": "conflict"}


def test_conflict_reason_says_the_name_belongs_to_another_user(repo):
    configured(repo)
    run_plan = run(repo, [user(), user("eve@example.org", "2", prefs())])
    (eve,) = [u for u in run_plan.users if u.email == "eve@example.org"]
    assert eve.reasons == ["the name 'alice01' belongs to another user"]


def test_name_of_an_owner_missing_from_the_scan_stays_with_the_owner(repo):
    # alice owns alice01 but is not in this scan; the direct lookup finds her account, so eve cannot take it.
    configured(repo)
    eve = user("eve@example.org", "2", prefs())
    run_plan = run(repo, [eve], FakeSource({"alice@example.org": user(), "eve@example.org": eve}))
    (eve_plan,) = [u for u in run_plan.users if u.email == "eve@example.org"]
    assert (eve_plan.action, eve_plan.reasons) == ("conflict", ["the name 'alice01' belongs to another user"])


def test_name_taken_by_a_hand_written_entry_is_a_conflict(repo):
    path = repo.root / "group_vars/mq.yml"
    path.write_text(
        path.read_text().replace(
            "rabbitmq_plugins:", "  - user: galaxy_alice01_byoc\n    vhost: /pulsar/x\n\nrabbitmq_plugins:", 1
        )
    )
    run_plan = run(repo, [user()])
    assert actions(run_plan) == {"alice@example.org": "conflict"}
    assert "galaxy_alice01_byoc" in " ".join(run_plan.users[0].reasons)


def test_orphaned_entries_are_reported(repo):
    configured(repo)
    users_file = repo.root / USERS
    users_file.write_text((FIXTURES / "eu" / USERS).read_text())  # owner entry removed by hand
    run_plan = run(repo, [])
    assert "alice01" in run_plan.orphans


def test_rename_is_deferred_without_allow_delete(repo):
    configured(repo)
    run_plan = run(repo, [user(p=prefs("alice02"))])
    assert actions(run_plan) == {"alice@example.org": "rename-deferred"} and not run_plan.publishable()


def test_rename_with_allow_delete_replaces_every_entry(repo):
    configured(repo)
    run_plan = run(repo, [user(p=prefs("alice02"))], allow_delete=True)
    assert actions(run_plan) == {"alice@example.org": "rename"}
    apply_all(repo, run_plan)
    dest = Doc((repo.root / DEST).read_text()).data["destinations"]
    assert "pulsar_alice02_byoc_tpv" in dest and "pulsar_alice01_byoc_tpv" not in dest
    rules = Doc((repo.root / USERS).read_text()).data["users"]["alice@example.org"]["rules"]
    assert [r["id"] for r in rules] == ["byoc-pulsar-alice02"]
    assert vault(repo).keys() >= {"rabbitmq_password_galaxy_alice02_byoc"}
    assert "rabbitmq_password_galaxy_alice01_byoc" not in vault(repo).keys()


def test_email_change_proven_by_the_vault_password_moves_the_entry(repo):
    configured(repo)
    moved = user("alice@new.example.org", "1", prefs())
    run_plan = run(repo, [moved], FakeSource({"alice@new.example.org": moved}))
    assert actions(run_plan) == {"alice@new.example.org": "update"}
    apply_all(repo, run_plan)
    users = Doc((repo.root / USERS).read_text()).data["users"]
    assert "alice@new.example.org" in users and "alice@example.org" not in users
    guard = Doc((repo.root / DEST).read_text()).data["destinations"]["pulsar_alice01_byoc_tpv"]["rules"][0]["if"]
    assert "alice@new.example.org" in guard
    # the reason ends up in the pull request text, which must not name the previous owner
    assert all("@" not in reason for reason in run_plan.users[0].reasons)


def test_email_change_without_the_password_needs_a_human(repo):
    configured(repo)
    claimant = user("mallory@example.org", "9", prefs(password="Guessed12345"))
    run_plan = run(repo, [claimant], FakeSource({"mallory@example.org": claimant}))
    assert actions(run_plan) == {"mallory@example.org": "needs-human"}


def test_vault_backed_form_inputs_are_refused(repo):
    form = repo.root / repo.site.user_preferences_file
    text = form.read_text()
    marker = "label: RabbitMQ custom alphanumerical password, at least 10 characters long (e.g. MyPulS4rP4ssw0rd).\n"
    assert marker in text
    form.write_text(
        text.replace(
            marker + "              type: password\n",
            marker + "              type: password\n              store: vault\n",
            1,
        )
    )
    with pytest.raises(PlanError, match="store: vault"):
        run(repo, [user()])


def test_vault_inputs_of_other_sections_do_not_matter(repo):
    assert "store: vault" in (repo.root / repo.site.user_preferences_file).read_text()  # the Invenio token
    assert actions(run(repo, [user()])) == {"alice@example.org": "create"}


class FailingSource(FakeSource):
    def lookup(self, email):
        raise RuntimeError("Galaxy unreachable")


def test_failed_owner_lookup_never_moves_or_deletes(repo):
    configured(repo)
    moved = user("alice@new.example.org", "1", prefs())
    assert actions(run(repo, [moved], FailingSource(), allow_delete=True)) == {"alice@new.example.org": "unreadable"}
    assert actions(run(repo, [], FailingSource(), allow_delete=True)) == {"alice@example.org": "unreadable"}


def test_lookup_showing_a_different_name_needs_a_human(repo):
    configured(repo)
    source = FakeSource({"alice@example.org": user(p=prefs("other01"))})
    assert actions(run(repo, [], source, allow_delete=True)) == {"alice@example.org": "needs-human"}


def test_lookup_showing_an_invalid_form_keeps_the_entry(repo):
    configured(repo)
    source = FakeSource({"alice@example.org": user(p=prefs(cores="x"))})
    assert actions(run(repo, [], source, allow_delete=True)) == {"alice@example.org": "invalid"}


def test_unparseable_form_config_is_an_error(repo):
    (repo.root / repo.site.user_preferences_file).write_text("preferences: [\n")
    with pytest.raises(PlanError, match="invalid YAML"):
        run(repo, [user()])


def test_missing_form_config_is_tolerated(repo):
    (repo.root / repo.site.user_preferences_file).unlink()
    assert actions(run(repo, [user()])) == {"alice@example.org": "create"}


def test_missing_target_file_is_an_error(repo):
    (repo.root / "group_vars/mq.yml").unlink()
    with pytest.raises(PlanError, match="group_vars/mq.yml not found"):
        run(repo, [user()])


@pytest.mark.parametrize(
    "email",
    [
        "a{{7*7}}@x.org",
        "{{lookup('pipe','id')}}@y.zz",
        "a{%if%}@x.org",
        "a b@x.org",
        "älice@x.org",
        "a{#c#}@x.org",
        "alice+lab@x.org",
        "a*b@x.org",
    ],
)
def test_unsafe_emails_make_that_user_invalid_and_never_reach_a_file(repo, email):
    run_plan = run(repo, [user(email, "5", prefs("weird01")), user("bob@example.org", "2", prefs("bob01"))])
    assert actions(run_plan) == {email: "invalid", "bob@example.org": "create"}
    result = materialize(run_plan.publishable(), repo, vault(repo))
    assert all(email not in fp.text for fp in result.files.values())


def test_a_user_whose_entries_cannot_be_edited_does_not_stop_the_others(repo):
    configured(repo)
    dest = repo.root / DEST
    # alice's destination collapsed into a single flow-style line by hand
    text = dest.read_text()
    from byoc_sync.yamledit.doc import Doc as D

    span = D(text).span_of(["destinations", "pulsar_alice01_byoc_tpv"])
    lines = text.split("\n")
    lines[span.start : span.end] = [
        "  pulsar_alice01_byoc_tpv: {inherits: pulsar_default, runner: pulsar_eu_alice01_byoc}"
    ]
    dest.write_text("\n".join(lines))
    run_plan = run(repo, [user(p=prefs(cores="16")), user("bob@example.org", "2", prefs("bob01"))])
    assert actions(run_plan) == {"alice@example.org": "conflict", "bob@example.org": "create"}


def test_inactive_owner_is_not_updated(repo):
    configured(repo)
    assert actions(run(repo, [user(p=prefs(cores="16"), active=False)])) == {"alice@example.org": "skipped-inactive"}
