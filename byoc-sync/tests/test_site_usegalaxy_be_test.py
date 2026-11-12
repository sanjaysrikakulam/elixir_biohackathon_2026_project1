"""The usegalaxy-be-test site: inline vault, owners by user id, and an older script's blocks left alone."""

from __future__ import annotations

import shutil

import pytest
import yaml

from byoc_sync.config import SiteConfig
from byoc_sync.planner import PlanOptions, Repo, materialize, plan
from byoc_sync.sources import ScanResult
from byoc_sync.vault import InlineVaultFile, open_vault

from .test_owner_by_galaxy_id import FakeIdSource
from .test_planner import FIXTURES, VAULT_PASSWORD, prefs, user

ALICE = "a1b2c3d4e5f60718"
H = "inventories/host_vars/test.usegalaxy.be"
FILES = {
    "mq": f"{H}/messaging.yml",
    "job_conf": f"{H}/galaxy_job_conf.yml",
    "destinations": "templates/galaxy/config/tpv/destinations.yml.j2",
    "users": "files/byoc/test.usegalaxy.be/byoc_owners.yml",
    "vault": f"{H}/byoc.yml",
}


@pytest.fixture
def site():
    return SiteConfig.load("usegalaxy-be-test")


@pytest.fixture
def repo(tmp_path, site):
    root = tmp_path / "playbook"
    shutil.copytree(FIXTURES / "usegalaxy-be-test", root)
    return Repo(root, site)


def alice(**kw):
    return user(email="alice@example.org", galaxy_id=ALICE, **kw)


def run(repo, records, **opts):
    scan = ScanResult(records=list(records), complete=True)
    source = FakeIdSource({r.galaxy_id: r for r in records})
    return plan(
        scan, repo, source, open_vault(repo.root / FILES["vault"], VAULT_PASSWORD, repo.site), PlanOptions(**opts)
    )


def apply_all(repo, run_plan):
    result = materialize(
        run_plan.publishable(), repo, open_vault(repo.root / FILES["vault"], VAULT_PASSWORD, repo.site)
    )
    for path, file_plan in result.files.items():
        (repo.root / path).write_text(file_plan.text)
    if result.vault.changed:
        (repo.root / FILES["vault"]).write_bytes(result.vault.ciphertext())
    return result


def read(repo, key):
    return (repo.root / FILES[key]).read_text()


def test_site_loads(site):
    assert site.vault_format == "inline" and site.vault_id == "byoc" and site.owner_key == "galaxy_id"


def test_new_endpoint_touches_all_files_and_leaves_the_old_blocks_alone(repo):
    before = {key: read(repo, key) for key in FILES}
    run_plan = run(repo, [alice(p=prefs(nfs="/data/share"))])
    assert {u.email: u.action for u in run_plan.users} == {ALICE: "create"}
    result = apply_all(repo, run_plan)
    assert set(result.files) == {FILES[k] for k in ("mq", "job_conf", "destinations", "users")}
    assert result.vault.changed

    for key in FILES:
        after = read(repo, key)
        assert "@example.org" not in after
        # Every original line is still there, in the same order.
        lines = iter(after.splitlines())
        assert all(any(line == old for line in lines) for old in before[key].splitlines()), key

    # New entries sit before the older script's BEGIN marker, not inside its block.
    for key, entry in [
        ("mq", "- user: galaxy_alice01_byoc"),
        ("job_conf", "pulsar_alice01_byoc:"),
        ("destinations", "pulsar_alice01_byoc_tpv:"),
        ("vault", "rabbitmq_password_galaxy_alice01_byoc: !vault |"),
    ]:
        text = read(repo, key)
        assert text.index(entry) < text.index("# BEGIN BYOC byoc_0e0fad4d103ae8ed"), key


def test_generated_entries(repo):
    apply_all(repo, run(repo, [alice(p=prefs(nfs="/data/share"))]))
    runner = yaml.safe_load(read(repo, "job_conf"))["galaxy_job_config"]["runners"]["pulsar_alice01_byoc"]
    assert runner["amqp_url"] == (
        "pyamqp://galaxy_alice01_byoc:{{ rabbitmq_password_galaxy_alice01_byoc }}"
        "@127.0.0.1:5671//pulsar/galaxy_alice01_byoc?ssl=1"
    )
    assert runner["galaxy_url"] == "https://test.usegalaxy.be"
    users = yaml.safe_load(read(repo, "mq"))["rabbitmq_users"]
    assert {
        "user": "galaxy_alice01_byoc",
        "password": "{{ rabbitmq_password_galaxy_alice01_byoc }}",
        "vhost": "/pulsar/galaxy_alice01_byoc",
    } in users
    text = read(repo, "destinations")
    start = text.index("  pulsar_alice01_byoc_tpv:")
    dest = yaml.safe_load(text[start : text.index("\n\n", start)].replace("\n  ", "\n")[2:])["pulsar_alice01_byoc_tpv"]
    assert dest["inherits"] == "_pulsar"
    assert dest["scheduling"]["require"] == ["alice01-byoc-pulsar"]
    assert dest["params"]["jobs_directory"] == "/data/share/staging"
    assert dest["env"]["APPTAINER_CACHEDIR"] == "/data/share/container_cache"
    assert dest["env"]["APPTAINER_TMPDIR"] == "$_GALAXY_JOB_TMP_DIR"
    assert "persistence_directory" not in dest["params"]
    assert ALICE in dest["rules"][0]["if"]
    owners = yaml.safe_load(read(repo, "users"))["byoc_owners"]
    assert owners[ALICE] == {"name": "alice01"}
    vault = InlineVaultFile(repo.root / FILES["vault"], VAULT_PASSWORD)
    assert vault.get("rabbitmq_password_galaxy_alice01_byoc") == prefs()["byoc_pulsar|password"]
    assert vault.get("rabbitmq_password_galaxy_byoc_0e0fad4d103ae8ed") == "dummy-surf-password"


def test_without_nfs_path_the_pulsar_defaults_stay(repo):
    apply_all(repo, run(repo, [alice()]))
    text = read(repo, "destinations")
    start = text.index("  pulsar_alice01_byoc_tpv:")
    dest = yaml.safe_load(text[start : text.index("\n\n", start)].replace("\n  ", "\n")[2:])["pulsar_alice01_byoc_tpv"]
    assert dest["env"] == {"SINGULARITY_TMPDIR": "$_GALAXY_JOB_TMP_DIR", "APPTAINER_TMPDIR": "$_GALAXY_JOB_TMP_DIR"}
    assert "params" not in dest


def test_second_run_changes_nothing(repo):
    apply_all(repo, run(repo, [alice()]))
    assert {u.email: u.action for u in run(repo, [alice()]).users} == {ALICE: "noop"}


def test_removal_restores_every_file(repo):
    before = {key: (repo.root / FILES[key]).read_bytes() for key in FILES}
    apply_all(repo, run(repo, [alice()]))
    cleared = alice(p={})
    scan = ScanResult(records=[], complete=True)
    vault = open_vault(repo.root / FILES["vault"], VAULT_PASSWORD, repo.site)
    run_plan = plan(scan, repo, FakeIdSource({ALICE: cleared}), vault, PlanOptions(allow_delete=True))
    assert {u.email: u.action for u in run_plan.users} == {ALICE: "delete"}
    apply_all(repo, run_plan)
    assert {key: (repo.root / FILES[key]).read_bytes() for key in FILES} == before
