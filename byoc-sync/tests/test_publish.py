"""Publishing plans as branches and pull requests (local bare origin + fake GitHub)."""

from __future__ import annotations

import dataclasses
import shutil
from pathlib import Path

import pytest

from byoc_sync import publish as publish_module
from byoc_sync.config import ConfigError, SiteConfig
from byoc_sync.github import GitHub
from byoc_sync.gitops import Workspace
from byoc_sync.planner import PlanOptions, Repo, plan
from byoc_sync.publish import fingerprint, publish
from byoc_sync.sources import ScanResult
from byoc_sync.vault import VaultFile
from tests.fake_github import TOKEN, FakeGitHub, FakeGitHubServer, FakePR
from tests.gitrepo import git, make_origin, merge_branch
from tests.test_planner import FakeSource, prefs, user

FIXTURES = Path(__file__).parent / "fixtures"
VAULT_PASSWORD = (FIXTURES / "vault-pass.txt").read_text().strip().encode()
BOT = ("byoc-sync", "byoc-sync@example.org")


@pytest.fixture
def site():
    return SiteConfig.load("eu")


@pytest.fixture
def env(tmp_path, eu_fixtures, site):
    content = tmp_path / "content"
    shutil.copytree(eu_fixtures, content)
    form = content / site.user_preferences_file
    form.parent.mkdir(parents=True, exist_ok=True)
    form.write_text((FIXTURES / "user_preferences_extra_conf.yml").read_text())
    origin, clone = make_origin(tmp_path, content)
    gh = FakeGitHub()
    with FakeGitHubServer(gh) as server:
        yield type(
            "Env",
            (),
            {
                "origin": origin,
                "clone": clone,
                "gh": gh,
                "api": GitHub("owner/playbook", TOKEN, api_url=server.url, pace_requests=False),
                "tmp": tmp_path,
                "site": site,
            },
        )


def sync(env, records, *, lookups=None, mode="per-user", reopen_rejected=False, complete=True, **opts):
    """One byoc-sync run: fresh workspace, plan against it, publish."""
    with Workspace.open(repo_path=env.clone, workdir=env.tmp / "work", base="master", bot=BOT) as ws:
        ws.reset()
        repo = Repo(ws.path, env.site)
        vault = VaultFile(ws.path / env.site.vault_file, VAULT_PASSWORD)
        accounts = {r.email: r for r in records}
        accounts.update(lookups or {})
        scan = ScanResult(records=list(records), complete=complete)
        run_plan = plan(scan, repo, FakeSource(accounts), vault, PlanOptions(**opts))
        return publish(
            run_plan,
            ws,
            env.api,
            env.site,
            VAULT_PASSWORD,
            mode=mode,
            reopen_rejected=reopen_rejected,
            fingerprint_key=b"k",
        )


def outcomes(results):
    return {r.name: r.outcome for r in results}


def test_new_user_gets_a_branch_and_a_pull_request(env):
    results = sync(env, [user()])
    assert outcomes(results) == {"alice01": "opened"}
    (pr,) = env.gh.prs
    assert pr.head == "alice01_byoc_ci" and pr.base == "master" and "alice01" in pr.title
    branch = git(env.origin, "rev-parse", "alice01_byoc_ci")
    changed = set(git(env.origin, "diff", "--name-only", "master", branch).split())
    assert changed == {
        "group_vars/mq.yml",
        "templates/galaxy/config/job_conf.yml",
        "files/galaxy/tpv/destinations.yml.j2",
        "files/galaxy/tpv/users.yml",
        "secret_group_vars/pulsar.yml",
    }
    assert git(env.origin, "log", "-1", "--format=%ae", branch) == BOT[1]


def test_rerun_without_changes_pushes_nothing(env):
    sync(env, [user()])
    before = git(env.origin, "rev-parse", "alice01_byoc_ci")
    assert outcomes(sync(env, [user()])) == {"alice01": "unchanged"}
    assert git(env.origin, "rev-parse", "alice01_byoc_ci") == before
    assert len(env.gh.prs) == 1


def test_changed_request_updates_the_open_pull_request(env):
    sync(env, [user()])
    before = git(env.origin, "rev-parse", "alice01_byoc_ci")
    assert outcomes(sync(env, [user(p=prefs(cores="16"))])) == {"alice01": "updated"}
    assert git(env.origin, "rev-parse", "alice01_byoc_ci") != before
    assert len(env.gh.prs) == 1 and "16" in env.gh.prs[0].body


def test_merged_request_with_deleted_branch_is_left_alone(env):
    sync(env, [user()])
    merge_branch(env.origin, env.tmp, "alice01_byoc_ci")
    env.gh.prs[0].state, env.gh.prs[0].merged_at = "closed", "2026-11-10T10:00:00Z"
    assert sync(env, [user()]) == []  # noop for the planner: nothing to publish
    assert len(env.gh.prs) == 1


def test_change_after_merge_reuses_the_branch_name_for_a_new_pull_request(env):
    sync(env, [user()])
    merge_branch(env.origin, env.tmp, "alice01_byoc_ci")
    env.gh.prs[0].state, env.gh.prs[0].merged_at = "closed", "2026-11-10T10:00:00Z"
    assert outcomes(sync(env, [user(p=prefs(cores="12"))])) == {"alice01": "opened"}
    assert [p.state for p in env.gh.prs] == ["closed", "open"]


def test_rejected_request_is_not_reopened(env):
    sync(env, [user()])
    env.gh.prs[0].state = "closed"
    git(env.origin, "branch", "-D", "alice01_byoc_ci")
    assert outcomes(sync(env, [user()])) == {"alice01": "rejected"}
    assert len(env.gh.prs) == 1


def test_rejected_request_can_be_reopened_on_demand_or_by_new_data(env):
    sync(env, [user()])
    env.gh.prs[0].state = "closed"
    assert outcomes(sync(env, [user()], reopen_rejected=True)) == {"alice01": "opened"}
    env.gh.prs[1].state = "closed"
    assert outcomes(sync(env, [user(p=prefs(cores="4"))])) == {"alice01": "opened"}


def test_password_change_alone_does_not_reopen_a_rejected_request(env):
    sync(env, [user()])
    env.gh.prs[0].state = "closed"
    assert outcomes(sync(env, [user(p=prefs(password="AnotherPassw0rd"))])) == {"alice01": "rejected"}


def test_branch_is_rebuilt_when_the_base_moves(env):
    sync(env, [user(), user("bob@example.org", "2", prefs("bob01"))])
    merge_branch(env.origin, env.tmp, "bob01_byoc_ci")
    env.gh.prs[1].state, env.gh.prs[1].merged_at = "closed", "2026-11-10T10:00:00Z"
    results = sync(env, [user(), user("bob@example.org", "2", prefs("bob01"))])
    assert outcomes(results) == {"alice01": "updated"}
    branch = git(env.origin, "rev-parse", "alice01_byoc_ci")
    assert git(env.origin, "rev-parse", f"{branch}^") == git(env.origin, "rev-parse", "master")
    # alice's branch now applies cleanly on top of bob's merged vault change
    assert "galaxy_bob01_byoc" in git(env.origin, "show", f"{branch}:group_vars/mq.yml")


def test_single_pull_request_mode(env):
    results = sync(env, [user(), user("bob@example.org", "2", prefs("bob01"))], mode="single")
    assert {r.outcome for r in results} == {"opened"} and len(env.gh.prs) == 1
    assert env.gh.prs[0].head == "byoc_ci_batch"
    assert "alice01" in env.gh.prs[0].body and "bob01" in env.gh.prs[0].body


def test_failure_for_one_user_does_not_stop_the_others_and_a_rerun_converges(env, monkeypatch):
    original = env.api.create_pr

    def flaky(branch, *args):
        if branch == "alice01_byoc_ci":
            raise RuntimeError("GitHub hiccup")
        return original(branch, *args)

    monkeypatch.setattr(env.api, "create_pr", flaky)
    records = [user(), user("bob@example.org", "2", prefs("bob01"))]
    assert outcomes(sync(env, records)) == {"alice01": "failed", "bob01": "opened"}
    monkeypatch.setattr(env.api, "create_pr", original)
    assert outcomes(sync(env, records)) == {"alice01": "opened", "bob01": "unchanged"}


def test_delete_with_allow_delete_opens_a_removal_request(env):
    sync(env, [user()])
    merge_branch(env.origin, env.tmp, "alice01_byoc_ci")
    env.gh.prs[0].state, env.gh.prs[0].merged_at = "closed", "2026-11-10T10:00:00Z"
    results = sync(env, [], lookups={"alice@example.org": user(p={})}, allow_delete=True)
    assert outcomes(results) == {"alice01": "opened"}
    assert "Remove" in env.gh.prs[-1].title


def test_pull_request_text_never_contains_secrets_or_the_email(env):
    sync(env, [user()])
    body = env.gh.prs[0].body + env.gh.prs[0].title
    assert "MyPulS4rP4ssw0rd" not in body and "alice@example.org" not in body


def test_fingerprint_ignores_the_password_and_depends_on_the_key():
    from byoc_sync.models import ByocRequest
    from byoc_sync.planner import UserPlan

    req = ByocRequest("alice01", "Passw0rd1234", 8, 31, 0, 0, None)
    a = UserPlan("a@b.c", "create", "1", "alice01", request=req)
    b = UserPlan("a@b.c", "create", "1", "alice01", request=ByocRequest("alice01", "Other0000000", 8, 31, 0, 0, None))
    assert fingerprint(a, b"k") == fingerprint(b, b"k") != fingerprint(a, b"other")
    assert "Passw0rd1234" not in fingerprint(a, None)


def test_pull_request_body_lists_request_and_files(env):
    sync(env, [user(p=prefs(nfs="/mnt/byoc"))])
    body = env.gh.prs[0].body
    assert "8 cores, 31 GB memory" in body and "`/mnt/byoc`" in body
    assert "`files/galaxy/tpv/destinations.yml.j2`: create `pulsar_alice01_byoc_tpv`" in body
    assert "`secret_group_vars/pulsar.yml`: create `rabbitmq_password_galaxy_alice01_byoc` (encrypted)" in body
    assert body.count("### Files") == 1


def test_password_change_while_the_request_is_open_updates_only_the_vault(env):
    sync(env, [user()])
    assert outcomes(sync(env, [user(p=prefs(password="AnotherPassw0rd"))])) == {"alice01": "updated"}
    branch = git(env.origin, "rev-parse", "alice01_byoc_ci")
    assert "secret_group_vars/pulsar.yml" in git(env.origin, "diff", "--name-only", "master", branch)


def test_single_mode_failure_is_reported_for_every_user(env, monkeypatch):
    monkeypatch.setattr(env.api, "create_pr", lambda *a: (_ for _ in ()).throw(RuntimeError("down")))
    results = sync(env, [user(), user("bob@example.org", "2", prefs("bob01"))], mode="single")
    assert {r.outcome for r in results} == {"failed"} and len(results) == 2


def test_crlf_files_stay_crlf_in_the_published_branch(env):
    work = env.tmp / "crlf"
    git(env.tmp, "clone", "-q", str(env.origin), str(work))
    mq = work / "group_vars/mq.yml"
    mq.write_bytes(mq.read_bytes().replace(b"\n", b"\r\n"))
    git(work, "commit", "-qam", "CRLF mq.yml")
    git(work, "push", "-q", "origin", "master")
    git(env.clone, "pull", "-q")
    sync(env, [user()])
    stat = git(env.origin, "diff", "--numstat", "master", "alice01_byoc_ci", "--", "group_vars/mq.yml")
    assert stat.split()[:2] == ["3", "0"]


def test_shallow_checkout_still_refreshes_branches_after_the_base_moves(env):
    sync(env, [user(), user("bob@example.org", "2", prefs("bob01"))])
    merge_branch(env.origin, env.tmp, "bob01_byoc_ci")
    env.gh.prs[1].state, env.gh.prs[1].merged_at = "closed", "2026-11-10T10:00:00Z"
    shallow = env.tmp / "shallow"
    git(env.tmp, "clone", "-q", "--depth", "1", f"file://{env.origin}", str(shallow))
    env.clone = shallow
    results = sync(env, [user(), user("bob@example.org", "2", prefs("bob01"))])
    assert outcomes(results) == {"alice01": "updated"}


def test_pull_requests_from_forks_are_never_touched(env):
    env.gh.prs.append(FakePR(1, "alice01_byoc_ci", "master", "someone else's", "body", head_repo="mallory/playbook"))
    results = sync(env, [user()])
    assert outcomes(results) == {"alice01": "opened"}
    assert env.gh.prs[0].title == "someone else's" and env.gh.prs[0].state == "open"
    assert env.gh.prs[1].head_repo == "owner/playbook"


def test_open_request_for_a_cleared_form_is_left_to_the_admins(env):
    sync(env, [user()])
    assert sync(env, [], lookups={"alice@example.org": user(p={})}) == []
    assert env.gh.prs[0].state == "open"


def test_unrelated_base_commits_do_not_rewrite_open_branches(env):
    sync(env, [user()])
    before = git(env.origin, "rev-parse", "alice01_byoc_ci")
    work = env.tmp / "unrelated"
    git(env.tmp, "clone", "-q", str(env.origin), str(work))
    (work / "README.extra").write_text("unrelated change\n")
    git(work, "add", "README.extra")
    git(work, "commit", "-q", "-m", "unrelated")
    git(work, "push", "-q", "origin", "master")
    git(env.clone, "pull", "-q")
    assert outcomes(sync(env, [user()])) == {"alice01": "unchanged"}
    assert git(env.origin, "rev-parse", "alice01_byoc_ci") == before


def test_single_pull_request_body_stays_within_githubs_limit(env, monkeypatch):
    import byoc_sync.publish as publish_module

    monkeypatch.setattr(publish_module, "MAX_BODY", 1500)
    records = [user(f"u{i}@example.org", str(i), prefs(f"user{i:02d}")) for i in range(1, 8)]
    sync(env, records, mode="single")
    body = env.gh.prs[0].body
    assert len(body) <= 1500 and "user07" in body and "fingerprint" in body


@pytest.mark.parametrize("mode", ["per-user", "single"])
def test_publishing_never_moves_the_base_branch(env, mode):
    before = git(env.origin, "rev-parse", "master")
    results = sync(env, [user(), user("bob@example.org", "2", prefs("bob01"))], mode=mode)
    assert {r.outcome for r in results} == {"opened"}
    assert git(env.origin, "rev-parse", "master") == before


def test_site_config_whose_branches_could_be_the_base_is_refused(env):
    with pytest.raises(ConfigError, match=r"naming\.branch"):
        dataclasses.replace(env.site, naming=dataclasses.replace(env.site.naming, branch="{x}"))


def test_push_to_the_base_branch_is_refused_and_reported_for_every_user(env, monkeypatch):
    # Config checks make this unreachable; the push guard is the last line of defence.
    monkeypatch.setattr(publish_module, "SINGLE_BRANCH", "master")
    before = git(env.origin, "rev-parse", "master")
    results = sync(env, [user(), user("bob@example.org", "2", prefs("bob01"))], mode="single")
    assert [r.outcome for r in results] == ["failed", "failed"]
    assert all("base branch" in r.detail for r in results)
    assert git(env.origin, "rev-parse", "master") == before and env.gh.prs == []
