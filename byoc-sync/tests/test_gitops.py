from pathlib import Path

import pytest
from git import Actor

from byoc_sync.gitops import GitError, Workspace, git_env
from tests.gitrepo import git, make_origin

BOT = ("byoc-sync", "byoc-sync@example.org")
TOKEN = "ghp_never_in_argv_or_config"


@pytest.fixture
def origin_and_clone(tmp_path, eu_fixtures):
    return make_origin(tmp_path, eu_fixtures)


def open_ws(clone, tmp_path, **kw):
    return Workspace.open(repo_path=clone, workdir=tmp_path / "work", base="master", bot=BOT, **kw)


def test_checkout_with_uncommitted_changes_is_refused(origin_and_clone, tmp_path):
    _, clone = origin_and_clone
    (clone / "group_vars/mq.yml").write_text("local edit\n")
    with pytest.raises(GitError, match="uncommitted changes"):
        open_ws(clone, tmp_path)


def test_checkout_returns_to_its_branch_and_keeps_ignored_files(origin_and_clone, tmp_path):
    _, clone = origin_and_clone
    (clone / ".gitignore").write_text(".vault_password\n")
    git(clone, "add", ".gitignore")
    git(clone, "commit", "-q", "-m", "ignore the vault password")
    git(clone, "push", "-q", "origin", "master")
    (clone / ".vault_password").write_text("local secret\n")
    head = git(clone, "rev-parse", "HEAD")
    with open_ws(clone, tmp_path) as ws:
        ws.reset()
        (ws.path / "group_vars/mq.yml").write_text("changed: true\n")
        ws.commit("test commit", ["group_vars/mq.yml"])
    assert git(clone, "branch", "--show-current") == "master"
    assert git(clone, "rev-parse", "HEAD") == head
    assert (clone / ".vault_password").read_text() == "local secret\n"


def test_push_creates_and_updates_branches_with_lease(origin_and_clone, tmp_path):
    _, clone = origin_and_clone
    with open_ws(clone, tmp_path) as ws:
        ws.reset()
        assert ws.remote_branch_sha("alice01_byoc_ci") is None
        (ws.path / "a.txt").write_text("1")
        first = ws.commit("one", ["a.txt"])
        ws.push("alice01_byoc_ci", expected=None)
        assert ws.remote_branch_sha("alice01_byoc_ci") == first
        ws.reset()
        (ws.path / "a.txt").write_text("2")
        second = ws.commit("two", ["a.txt"])
        with pytest.raises(GitError, match="stale|rejected"):
            ws.push("alice01_byoc_ci", expected="0" * 40)  # someone else moved the branch
        ws.push("alice01_byoc_ci", expected=first)
        assert ws.remote_branch_sha("alice01_byoc_ci") == second


def test_push_to_the_base_branch_is_refused(origin_and_clone, tmp_path):
    origin, clone = origin_and_clone
    before = git(origin, "rev-parse", "master")
    with open_ws(clone, tmp_path) as ws:
        ws.reset()
        (ws.path / "a.txt").write_text("1")
        ws.commit("one", ["a.txt"])
        with pytest.raises(GitError, match="base branch"):
            ws.push("master", expected=before)
    assert git(origin, "rev-parse", "master") == before


def test_commits_use_the_bot_identity_and_one_parent_on_base(origin_and_clone, tmp_path):
    _, clone = origin_and_clone
    with open_ws(clone, tmp_path) as ws:
        ws.reset()
        (ws.path / "a.txt").write_text("1")
        sha = ws.commit("one", ["a.txt"])
        commit = ws.repo.commit(sha)
        assert commit.author == Actor(*BOT) and ws.parent(sha) == ws.base_sha()
        assert ws.show(sha, "a.txt") == b"1"
        assert ws.show(sha, "missing.txt") is None
        assert ws.changed_paths(ws.base_sha(), sha) == {"a.txt"}


def test_fetched_branch_leaves_no_refs_behind(origin_and_clone, tmp_path):
    _, clone = origin_and_clone
    with open_ws(clone, tmp_path) as ws:
        ws.reset()
        (ws.path / "a.txt").write_text("1")
        sha = ws.commit("one", ["a.txt"])
        ws.push("x_byoc_ci", expected=None)
        assert ws.fetch_branch("x_byoc_ci") == sha
    assert "x_byoc_ci" not in git(clone, "for-each-ref", "refs/heads", "refs/byoc-sync")


def test_clone_mode_from_url(origin_and_clone, tmp_path):
    origin, _ = origin_and_clone
    with Workspace.open(repo_url=str(origin), workdir=tmp_path / "w", base="master", bot=BOT) as ws:
        ws.reset()
        assert (ws.path / "group_vars/mq.yml").is_file()
    with Workspace.open(repo_url=str(origin), workdir=tmp_path / "w", base="master", bot=BOT) as ws:
        ws.reset()  # reuses the existing clone


def test_not_a_git_repository_is_refused(tmp_path):
    (tmp_path / "plain").mkdir()
    with pytest.raises(GitError, match="not a git repository"):
        Workspace.open(repo_path=tmp_path / "plain", workdir=tmp_path / "w", base="master", bot=BOT)


def test_unknown_base_branch_is_refused(origin_and_clone, tmp_path):
    _, clone = origin_and_clone
    with pytest.raises(GitError, match="main"):
        Workspace.open(repo_path=clone, workdir=tmp_path / "w", base="main", bot=BOT)


def test_token_only_travels_in_the_environment(origin_and_clone, tmp_path):
    _, clone = origin_and_clone
    env = git_env(TOKEN)
    assert env["GIT_CONFIG_COUNT"] == "1" and env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert TOKEN not in env["GIT_CONFIG_VALUE_0"]  # base64 encoded basic auth
    with open_ws(clone, tmp_path, token=TOKEN) as ws:
        ws.reset()
        (ws.path / "a.txt").write_text("1")
        ws.commit("one", ["a.txt"])
        ws.push("x_byoc_ci", expected=None)
    for config in [clone / ".git/config", *Path(tmp_path).rglob("config")]:
        if config.is_file():
            assert TOKEN not in config.read_text(errors="ignore")


def test_git_errors_never_show_the_token(origin_and_clone, tmp_path):
    _, clone = origin_and_clone
    with open_ws(clone, tmp_path, token=TOKEN) as ws:
        ws.reset()
        with pytest.raises(GitError) as info:
            ws.push("x_byoc_ci", expected="1" * 40)
    assert TOKEN not in str(info.value)


def test_dirty_checkout_error_names_the_files(origin_and_clone, tmp_path):
    _, clone = origin_and_clone
    (clone / "byoc-report.json").write_text("{}")
    with pytest.raises(GitError, match="byoc-report.json"):
        open_ws(clone, tmp_path)


def test_repository_hooks_do_not_run_for_byoc_sync_commits(origin_and_clone, tmp_path):
    _, clone = origin_and_clone
    hook = clone / ".git/hooks/pre-commit"
    marker = tmp_path / "hook-ran"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    hook.chmod(0o755)
    with open_ws(clone, tmp_path) as ws:
        ws.reset()
        (ws.path / "a.txt").write_text("1")
        sha = ws.commit("one", ["a.txt"])
        assert ws.show(sha, "a.txt") == b"1"
    assert not marker.exists()
