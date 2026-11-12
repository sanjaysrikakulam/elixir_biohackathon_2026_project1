"""`ansible-playbook --check` runs the whole role on a fresh host without changing it."""

from __future__ import annotations

from tests.gitrepo import git

from .conftest import ME, good_vars


def test_check_mode_on_a_fresh_host_changes_nothing(run_role, tmp_path):
    # The git module asks the repository for its head even in check mode, so give it a real (local) one.
    repo = tmp_path.parent / f"{tmp_path.name}-byoc-sync-repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "commit", "-q", "--allow-empty", "-m", "initial")
    values = good_vars(
        tmp_path,
        byoc_sync_repo_url=f"file://{repo}",
        byoc_sync_manage_packages=False,
        _byoc_sync_become=False,
        _byoc_sync_manage_cron=False,
        byoc_sync_config_dir=str(tmp_path / "etc"),
        byoc_sync_wrapper_path=str(tmp_path / "bin" / "byoc-sync-run"),
        _byoc_sync_wrapper_owner=ME,
    )
    proc = run_role("main", values, gather_facts=True, flags=("--check",))
    assert proc.returncode == 0, proc.stdout[-3000:]
    assert [
        p.name for p in tmp_path.iterdir() if p.name not in {"play.yml", "vars.json", "ansible-tmp", "ansible-home"}
    ] == []
