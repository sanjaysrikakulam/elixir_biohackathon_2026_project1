"""End-to-end runs of the command line: fake Galaxy, local bare origin, fake GitHub."""

from __future__ import annotations

import itertools
import json
import shutil
import stat
import textwrap
from pathlib import Path

import pytest

from byoc_sync.cli import main
from byoc_sync.yamledit.doc import Doc
from tests.fake_galaxy import API_KEY, FakeGalaxy, FakeGalaxyServer, FakeUser
from tests.fake_github import TOKEN, FakeGitHub, FakeGitHubServer
from tests.gitrepo import git, make_origin, merge_branch

FIXTURES = Path(__file__).parent / "fixtures"
VAULT_PASSWORD = (FIXTURES / "vault-pass.txt").read_text().strip()
PW = "MyPulS4rP4ssw0rd"


def prefs(name="alice01", password=PW, cores="8", **extra):
    return {
        "byoc_pulsar|username": name,
        "byoc_pulsar|password": password,
        "byoc_pulsar|max_accepted_cores": cores,
        "byoc_pulsar|max_accepted_mem": "31",
        "byoc_pulsar|min_accepted_gpus": "0",
        "byoc_pulsar|max_accepted_gpus": "0",
        **extra,
    }


@pytest.fixture
def world(tmp_path, eu_fixtures, monkeypatch):
    content = tmp_path / "content"
    shutil.copytree(eu_fixtures, content)
    form = content / "files/galaxy/config/user_preferences_extra_conf.yml"
    form.parent.mkdir(parents=True, exist_ok=True)
    form.write_text((FIXTURES / "user_preferences_extra_conf.yml").read_text())
    origin, clone = make_origin(tmp_path, content)
    galaxy = FakeGalaxy()
    galaxy.add(FakeUser("1", "alice@example.org", "alice", prefs()))
    gh = FakeGitHub()
    monkeypatch.setenv("GALAXY_API_KEY", API_KEY)
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    monkeypatch.setenv("BYOC_SYNC_VAULT_PASSWORD", VAULT_PASSWORD)
    monkeypatch.delenv("BYOC_SYNC_FINGERPRINT_KEY", raising=False)
    with FakeGalaxyServer(galaxy) as g, FakeGitHubServer(gh) as h:
        w = type("World", (), {})()
        w.tmp, w.origin, w.clone, w.galaxy, w.gh, w.galaxy_url, w.github_url = (
            tmp_path,
            origin,
            clone,
            galaxy,
            gh,
            g.url,
            h.url,
        )
        yield w


def args(w, *extra, source=True):
    base = [
        "--repo-path",
        str(w.clone),
        "--workdir",
        str(w.tmp / "work"),
        "--github-repo",
        "owner/playbook",
        "--github-api-url",
        w.github_url,
    ]
    if source:
        base += ["--galaxy-url", w.galaxy_url]
    return base + list(extra)


def branches(w):
    return {line.split()[-1] for line in git(w.origin, "branch").splitlines()} - {"master"}


# -- basic runs ----------------------------------------------------------------------------------------------


def test_dry_run_shows_the_plan_and_changes_nothing(world, capsys):
    head = git(world.origin, "rev-parse", "master")
    assert main(args(world, "--dry-run")) == 0
    out = capsys.readouterr().out
    assert "alice@example.org" in out and "create" in out
    assert "+  - user: galaxy_alice01_byoc" in out
    assert "create rabbitmq_password_galaxy_alice01_byoc: ********" in out
    assert PW not in out and VAULT_PASSWORD not in out and API_KEY not in out
    assert git(world.origin, "rev-parse", "master") == head and branches(world) == set() and not world.gh.prs


FINGERPRINT_WARNING = (
    "warning: BYOC_SYNC_FINGERPRINT_KEY is not set, so the fingerprint in each pull request body is a plain hash "
    "that includes the owner's e-mail; anyone who can read the pull request can check a guessed address against it"
)


def test_publishing_without_a_fingerprint_key_warns(world, capsys):
    assert main(args(world)) == 0
    assert FINGERPRINT_WARNING in capsys.readouterr().err
    assert len(world.gh.prs) == 1


def test_publishing_with_a_fingerprint_key_does_not_warn(world, capsys, monkeypatch):
    monkeypatch.setenv("BYOC_SYNC_FINGERPRINT_KEY", "a-secret")
    assert main(args(world)) == 0
    assert "BYOC_SYNC_FINGERPRINT_KEY" not in capsys.readouterr().err


def test_dry_run_does_not_warn_about_the_fingerprint_key(world, capsys):
    assert main(args(world, "--dry-run")) == 0
    assert "BYOC_SYNC_FINGERPRINT_KEY" not in capsys.readouterr().err


def test_run_opens_a_pull_request_and_writes_a_report(world, capsys):
    report = world.tmp / "report.json"
    assert main(args(world, "--report-json", str(report))) == 0
    assert branches(world) == {"alice01_byoc_ci"} and len(world.gh.prs) == 1
    data = json.loads(report.read_text())
    assert data["users"][0] == {
        "email": "alice@example.org",
        "galaxy_id": "1",
        "name": "alice01",
        "action": "create",
        "reasons": [],
    }
    assert data["published"][0]["outcome"] == "opened" and data["scan"]["complete"] is True
    assert PW not in report.read_text()


def test_second_run_is_quiet(world):
    main(args(world))
    head = git(world.origin, "rev-parse", "alice01_byoc_ci")
    assert main(args(world)) == 0
    assert git(world.origin, "rev-parse", "alice01_byoc_ci") == head and len(world.gh.prs) == 1


# -- options -------------------------------------------------------------------------------------------------


def test_api_key_comes_from_the_named_environment_variable(world, monkeypatch, capsys):
    monkeypatch.delenv("GALAXY_API_KEY")
    assert main(args(world, "--dry-run")) == 1
    assert "GALAXY_API_KEY" in capsys.readouterr().err
    monkeypatch.setenv("MY_KEY", API_KEY)
    assert main(args(world, "--dry-run", "--api-key-env", "MY_KEY")) == 0


def test_vault_password_from_a_file(world, monkeypatch):
    monkeypatch.delenv("BYOC_SYNC_VAULT_PASSWORD")
    assert main(args(world, "--dry-run")) == 1
    assert main(args(world, "--dry-run", "--vault-password-file", str(FIXTURES / "vault-pass.txt"))) == 0


def test_wrong_vault_password_stops_before_galaxy_is_contacted(world, monkeypatch, capsys):
    monkeypatch.setenv("BYOC_SYNC_VAULT_PASSWORD", "wrong")
    assert main(args(world, "--dry-run")) == 1
    assert "decrypt" in capsys.readouterr().err and world.galaxy.requests == []


def test_vault_file_option(world):
    target = world.clone / "other_vault.yml"
    shutil.copy(world.clone / "secret_group_vars/pulsar.yml", target)
    git(world.clone, "add", "other_vault.yml")
    git(world.clone, "commit", "-q", "-m", "second vault")
    git(world.clone, "push", "-q", "origin", "master")
    assert main(args(world, "--vault-file", "other_vault.yml")) == 0
    changed = git(world.origin, "diff", "--name-only", "master", "alice01_byoc_ci").split()
    assert "other_vault.yml" in changed and "secret_group_vars/pulsar.yml" not in changed


def test_missing_vault_file_is_fatal(world, capsys):
    assert main(args(world, "--dry-run", "--vault-file", "nope.yml")) == 1
    assert "nope.yml" in capsys.readouterr().err


def test_github_repo_is_required_unless_dry_run(world, capsys):
    argv = [a for a in args(world) if a not in ("--github-repo", "owner/playbook")]
    assert main(argv) == 1 and "--github-repo" in capsys.readouterr().err
    assert main([*argv, "--dry-run"]) == 0


def test_github_token_is_required_unless_dry_run(world, monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_TOKEN")
    assert main(args(world)) == 1 and "GITHUB_TOKEN" in capsys.readouterr().err
    assert main(args(world, "--dry-run")) == 0


def test_repo_url_clone_mode(world):
    argv = args(world)
    argv[argv.index("--repo-path")] = "--repo-url"
    argv[argv.index(str(world.clone))] = str(world.origin)
    assert main(argv) == 0 and branches(world) == {"alice01_byoc_ci"}


def test_repo_path_and_url_are_exclusive(world, capsys):
    with pytest.raises(SystemExit):
        main(args(world, "--repo-url", str(world.origin)))
    assert "not allowed with" in capsys.readouterr().err


def test_base_branch_option(world, capsys):
    assert main(args(world, "--dry-run", "--base-branch", "does-not-exist")) == 1
    assert "does-not-exist" in capsys.readouterr().err


def test_base_branch_option_that_byoc_sync_could_write_is_refused(world, capsys):
    head = git(world.origin, "rev-parse", "master")
    assert main(args(world, "--base-branch", "alice01_byoc_ci")) == 1
    assert "base_branch 'alice01_byoc_ci'" in capsys.readouterr().err
    assert git(world.origin, "rev-parse", "master") == head and branches(world) == set() and not world.gh.prs


def test_gxadmin_source(world, tmp_path):
    rows = [
        {
            "id": 1,
            "email": "alice@example.org",
            "username": "alice",
            "deleted": False,
            "purged": False,
            "active": True,
            "extra_user_preferences": json.dumps(prefs()),
        }
    ]
    script = tmp_path / "gxadmin"
    script.write_text(
        textwrap.dedent(f"""\
        #!/usr/bin/env python3
        import json, sys
        sql = sys.argv[3]
        print(json.dumps({rows!r} if "LIKE" in sql else [r for r in {rows!r} if r["email"] in sql]))
    """)
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    argv = args(world, "--source", "gxadmin", "--gxadmin-cmd", str(script), source=False)
    assert main(argv) == 0 and branches(world) == {"alice01_byoc_ci"}


def test_allow_list_option(world, tmp_path):
    allow = tmp_path / "allow.txt"
    allow.write_text("# approved BYOC users\nbob@example.org\n")
    assert main(args(world, "--allow-list", str(allow))) == 0 and branches(world) == set()


def test_incomplete_scan_publishes_nothing_and_exits_3(world):
    world.galaxy.always_fail.add("1")
    assert main(args(world, "--max-error-rate", "0", "--retries", "0")) == 3
    assert branches(world) == set()


def test_validate_tpv_runs_the_linter(world, tmp_path, capsys):
    bad = tmp_path / "tpv"
    bad.write_text("#!/bin/sh\necho 'lint failed: users.yml' >&2\nexit 1\n")
    bad.chmod(0o755)
    assert main(args(world, "--validate-tpv", "--tpv-cmd", str(bad))) == 1
    assert "lint failed" in capsys.readouterr().err and branches(world) == set()


def test_validate_tpv_with_the_real_linter(world):
    tpv = Path(__file__).parents[1] / ".venv/bin/tpv"
    if not tpv.exists():
        pytest.skip("tpv not installed")
    assert main(args(world, "--dry-run", "--validate-tpv", "--tpv-cmd", str(tpv))) == 0


def test_report_json_in_dry_run(world):
    report = world.tmp / "r.json"
    assert main(args(world, "--dry-run", "--report-json", str(report))) == 0
    data = json.loads(report.read_text())
    assert data["dry_run"] is True and data["published"] == []
    assert data["changes"]["group_vars/mq.yml"] == [
        {"object_id": "galaxy_alice01_byoc", "action": "create", "fields": []}
    ]
    assert data["vault"] == ["create rabbitmq_password_galaxy_alice01_byoc: ********"]


def test_help_mentions_every_environment_variable(capsys):
    with pytest.raises(SystemExit):
        main(["--help"])
    out = capsys.readouterr().out
    for name in ("GALAXY_API_KEY", "GITHUB_TOKEN", "BYOC_SYNC_VAULT_PASSWORD", "BYOC_SYNC_FINGERPRINT_KEY"):
        assert name in out


# -- option combinations -------------------------------------------------------------------------------------


@pytest.mark.parametrize("dry_run,allow_delete,single_pr,no_fallback", list(itertools.product([False, True], repeat=4)))
def test_create_then_remove_under_every_option_combination(world, dry_run, allow_delete, single_pr, no_fallback):
    flags = [
        flag
        for on, flag in (
            (dry_run, "--dry-run"),
            (allow_delete, "--allow-delete"),
            (single_pr, "--single-pr"),
            (no_fallback, "--no-fallback"),
        )
        if on
    ]
    branch = "byoc_ci_batch" if single_pr else "alice01_byoc_ci"
    assert main(args(world, *flags)) == 0
    if dry_run:
        assert branches(world) == set()
        return
    assert branches(world) == {branch}
    users_yml = git(world.origin, "show", f"{branch}:files/galaxy/tpv/users.yml")
    (rule,) = Doc(users_yml).data["users"]["alice@example.org"]["rules"]
    assert ("require" in rule["scheduling"]) == no_fallback and ("rank" in rule) != no_fallback

    merge_branch(world.origin, world.tmp, branch)
    world.gh.prs[-1].state, world.gh.prs[-1].merged_at = "closed", "2026-11-10T10:00:00Z"
    world.galaxy.users[0].prefs = {}
    assert main(args(world, *flags)) == 0
    if allow_delete:
        assert branches(world) == {branch}
        assert "Remove" in world.gh.prs[-1].title if not single_pr else "1 change" in world.gh.prs[-1].title
        removed = git(world.origin, "show", f"{branch}:group_vars/mq.yml")
        assert "galaxy_alice01_byoc" not in removed
    else:
        assert branches(world) == set() and world.gh.prs[-1].state == "closed"


@pytest.mark.parametrize(
    "flag,value",
    [("--concurrency", "0"), ("--retries", "-1"), ("--max-error-rate", "1.5"), ("--timeout", "0")],
)
def test_out_of_range_options_are_rejected(world, flag, value, capsys):
    with pytest.raises(SystemExit):
        main(args(world, flag, value))
    assert flag in capsys.readouterr().err


def test_github_repo_is_validated_before_galaxy_is_scanned(world, capsys):
    argv = args(world)
    argv[argv.index("owner/playbook")] = "not a slug"
    assert main(argv) == 1
    assert world.galaxy.requests == []


def test_origin_must_be_the_github_repository_pull_requests_go_to(world, capsys):
    # The checkout's origin is another GitHub repository: refused before anything is fetched or scanned.
    git(world.clone, "remote", "set-url", "origin", "https://github.com/someone/else.git")
    assert main(args(world)) == 1
    assert "origin (someone/else) does not match --github-repo (owner/playbook)" in capsys.readouterr().err
    assert world.galaxy.requests == []
