"""The byoc-sync-run wrapper and the secret files, rendered into tmp_path and run against a stub byoc-sync."""

from __future__ import annotations

import fcntl
import getpass
import grp
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from .conftest import good_vars

ME = getpass.getuser()
MY_GROUP = grp.getgrgid(os.getgid()).gr_name
OTHER_USER = "nobody" if ME == "root" else "root"
SHELLCHECK = Path(sys.executable).parent / "shellcheck"  # shellcheck-py installs it next to Python

STUB = """#!/bin/sh
calls="$(dirname "$0")/../calls"
mkdir -p "$calls"
printf '%s\\0' "$@" > "$calls/argv"
env -0 > "$calls/env"
"""


class Setup:
    def __init__(self, tmp_path: Path, **overrides):
        self.tmp = tmp_path
        self.venv = Path(overrides.pop("venv", tmp_path / "venv"))
        self.reports = Path(overrides.pop("reports", tmp_path / "reports"))
        self.config = tmp_path / "etc"
        self.wrapper = tmp_path / "bin" / "byoc-sync-run"
        self.values = good_vars(
            tmp_path,
            byoc_sync_venv=str(self.venv),
            byoc_sync_reports_dir=str(self.reports),
            byoc_sync_config_dir=str(self.config),
            byoc_sync_wrapper_path=str(self.wrapper),
            byoc_sync_playbook_repo_url="https://github.com/me/playbook.git",
            byoc_sync_galaxy_url="https://galaxy.example.org",
            byoc_sync_galaxy_api_key="api-SECRET-1",
            _byoc_sync_wrapper_owner=ME,
            _byoc_sync_wrapper_group=MY_GROUP,
        )
        self.values.update(overrides)

    def render(self, run_role, vars_yaml: str | None = None, tasks_from: str = "render"):
        (self.venv / "bin").mkdir(parents=True, exist_ok=True)
        (self.wrapper.parent).mkdir(parents=True, exist_ok=True)
        self.reports.mkdir(parents=True, exist_ok=True)
        stub = self.venv / "bin" / "byoc-sync"
        stub.write_text(STUB)
        stub.chmod(0o755)
        proc = run_role(tasks_from, self.values, vars_yaml=vars_yaml)
        assert proc.returncode == 0, proc.stdout
        self.proc = proc
        return self

    def run(self, *args: str) -> subprocess.CompletedProcess:
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.tmp), "LANG": "C"}
        return subprocess.run([str(self.wrapper), *args], capture_output=True, text=True, env=env, timeout=60)

    @property
    def calls(self) -> Path:
        return self.venv / "calls"

    def argv(self) -> list[str]:
        raw = (self.calls / "argv").read_bytes().decode()
        return raw.split("\0")[:-1]

    def env(self) -> dict[str, str]:
        items = (self.calls / "env").read_bytes().decode().split("\0")[:-1]
        return dict(item.split("=", 1) for item in items)


def _without_report(argv: list[str]) -> list[str]:
    i = argv.index("--report-json")
    assert re.fullmatch(r".*/report-\d{8}T\d{6}Z\.json", argv[i + 1])
    return argv[:i] + argv[i + 2 :]


def test_wrapper_passes_the_configured_options(run_role, tmp_path):
    s = Setup(tmp_path).render(run_role)
    proc = s.run()
    assert proc.returncode == 0, proc.stderr
    argv = s.argv()
    assert argv[argv.index("--report-json") + 1].startswith(str(s.reports) + "/report-")
    assert _without_report(argv) == [
        "--site-config", "eu",
        "--source", "api",
        "--repo-url", "https://github.com/me/playbook.git",
        "--workdir", str(s.reports / "work"),
        "--vault-password-file", str(s.config / "vault-password"),
        "--galaxy-url", "https://galaxy.example.org",
        "--dry-run",
    ]  # fmt: skip
    assert s.env()["GALAXY_API_KEY"] == "api-SECRET-1"


def test_gxadmin_options(run_role, tmp_path):
    s = Setup(tmp_path, byoc_sync_source="gxadmin", byoc_sync_gxadmin_cmd="sudo -u galaxy gxadmin").render(run_role)
    assert s.run().returncode == 0
    argv = _without_report(s.argv())
    assert argv[argv.index("--gxadmin-cmd") + 1] == "sudo -u galaxy gxadmin"
    assert "--galaxy-url" not in argv and "GALAXY_API_KEY" not in s.env()


def test_publishing_options(run_role, tmp_path):
    s = Setup(
        tmp_path,
        byoc_sync_dry_run=False,
        byoc_sync_allow_delete=True,
        byoc_sync_github_repo="me/playbook",
        byoc_sync_github_token="ghp-SECRET-2",
    ).render(run_role)
    assert s.run().returncode == 0, s.run().stderr
    argv = _without_report(s.argv())
    assert "--dry-run" not in argv and "--allow-delete" in argv
    assert argv[argv.index("--github-repo") + 1] == "me/playbook"
    assert s.env()["GITHUB_TOKEN"] == "ghp-SECRET-2"


def test_dry_run_does_not_get_the_github_token(run_role, tmp_path):
    s = Setup(tmp_path, byoc_sync_github_token="ghp-SECRET-2").render(run_role)
    assert s.run().returncode == 0
    assert "GITHUB_TOKEN" not in s.env()
    assert "ghp-SECRET-2" not in (s.config / "env").read_text()


def test_extra_args_are_passed_verbatim(run_role, tmp_path):
    s = Setup(tmp_path, byoc_sync_extra_args=["--allow-list", "/a b/c's.txt", "--concurrency", 8]).render(run_role)
    assert s.run("--quiet").returncode == 0
    assert s.argv()[-5:] == ["--allow-list", "/a b/c's.txt", "--concurrency", "8", "--quiet"]


def test_env_file_round_trips_special_characters(run_role, tmp_path):
    secret = "a'b\"c$d`e f\\g\nh -i=j"
    s = Setup(tmp_path, byoc_sync_fingerprint_key=secret, byoc_sync_extra_env={"PGPASSWORD": secret, "PGPORT": 5432})
    s.render(run_role)
    assert s.run().returncode == 0
    env = s.env()
    assert env["BYOC_SYNC_FINGERPRINT_KEY"] == secret and env["PGPASSWORD"] == secret and env["PGPORT"] == "5432"


def test_secrets_with_template_braces_pass_when_marked_unsafe(run_role, tmp_path):
    s = Setup(tmp_path)
    s.render(run_role, vars_yaml="byoc_sync_fingerprint_key: !unsafe 'x{{ y }}z'\n")
    assert s.run().returncode == 0
    assert s.env()["BYOC_SYNC_FINGERPRINT_KEY"] == "x{{ y }}z"


def test_no_secret_in_wrapper_or_argv(run_role, tmp_path):
    s = Setup(
        tmp_path, byoc_sync_dry_run=False, byoc_sync_github_repo="me/p", byoc_sync_github_token="ghp-SECRET-2"
    ).render(run_role)
    assert s.run().returncode == 0
    argv = s.argv()
    assert len(argv) > 10
    wrapper = s.wrapper.read_text()
    for secret in ("api-SECRET-1", "ghp-SECRET-2", "vault-secret-123"):
        assert secret not in wrapper and all(secret not in arg for arg in argv)
    assert (s.config / "vault-password").read_text() == "vault-secret-123\n"


def test_wrapper_refuses_other_users(run_role, tmp_path):
    # The secret files still belong to the test user (chown to another user needs root).
    s = Setup(tmp_path, byoc_sync_user=OTHER_USER, _byoc_sync_file_owner=ME).render(run_role)
    proc = s.run()
    assert proc.returncode == 1
    assert f"byoc-sync-run: run as {OTHER_USER}, e.g. sudo -u {OTHER_USER} {s.wrapper}" in proc.stderr
    assert not s.calls.exists()


def test_wrapper_refuses_while_run_values_are_missing(run_role, tmp_path):
    s = Setup(tmp_path, byoc_sync_playbook_repo_url="changeme", byoc_sync_galaxy_api_key="")
    s.render(run_role)
    proc = s.run()
    assert proc.returncode == 1
    assert "byoc-sync-run: set these variables first: byoc_sync_playbook_repo_url, byoc_sync_galaxy_api_key" in (
        proc.stderr
    )
    assert not s.calls.exists()
    assert "GALAXY_API_KEY" not in (s.config / "env").read_text()


def test_concurrent_run_is_skipped(run_role, tmp_path):
    s = Setup(tmp_path).render(run_role)
    with open(s.reports / ".byoc-sync.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        busy = s.run()
        fcntl.flock(lock, fcntl.LOCK_UN)
    assert busy.returncode == 0
    assert "byoc-sync-run: another byoc-sync run is in progress" in busy.stderr
    assert not s.calls.exists()
    free = s.run()
    assert free.returncode == 0 and (s.calls / "argv").exists()


def test_files_have_the_documented_modes(run_role, tmp_path):
    s = Setup(tmp_path).render(run_role)
    mode = lambda p: stat.S_IMODE(p.stat().st_mode)  # noqa: E731
    assert (mode(s.config), mode(s.config / "env"), mode(s.config / "vault-password"), mode(s.wrapper)) == (
        0o2700,
        0o600,
        0o600,
        0o755,
    )


def _gid_and_mode(path: Path) -> tuple[int, int]:
    st = path.stat()
    return st.st_gid, stat.S_IMODE(st.st_mode)


def test_secret_files_get_the_configured_group(run_role, tmp_path, other_group):
    s = Setup(tmp_path, byoc_sync_group=other_group.gr_name).render(run_role)
    assert [_gid_and_mode(p) for p in (s.config, s.config / "env", s.config / "vault-password")] == [
        (other_group.gr_gid, 0o2700),
        (other_group.gr_gid, 0o600),
        (other_group.gr_gid, 0o600),
    ]
    assert s.wrapper.stat().st_gid == grp.getgrnam(MY_GROUP).gr_gid


def test_folders_get_the_configured_group_and_pass_it_on(run_role, tmp_path, other_group):
    s = Setup(tmp_path, byoc_sync_group=other_group.gr_name).render(run_role, tasks_from="configure")
    assert [_gid_and_mode(p) for p in (s.reports, s.reports / "work")] == [
        (other_group.gr_gid, 0o2750),
        (other_group.gr_gid, 0o2750),
    ]
    # The test user's primary group is not other_group: only the setgid bit gives new entries that group.
    (s.reports / "new-file").touch()
    (s.reports / "new-folder").mkdir()
    assert [(s.reports / name).stat().st_gid for name in ("new-file", "new-folder")] == [other_group.gr_gid] * 2


def test_second_run_with_a_group_changes_nothing(run_role, tmp_path, other_group):
    s = Setup(tmp_path, byoc_sync_group=other_group.gr_name).render(run_role, tasks_from="configure")
    again = s.render(run_role, tasks_from="configure").proc.stdout
    assert re.search(r"localhost\s+:\s+ok=\d+\s+changed=0\s", again), again


def test_reports_are_private(run_role, tmp_path):
    s = Setup(tmp_path).render(run_role)
    stub = s.venv / "bin" / "byoc-sync"
    stub.write_text(STUB + 'touch "$(dirname "$0")/../calls/made-by-run"\n')
    assert s.run().returncode == 0
    assert stat.S_IMODE((s.calls / "made-by-run").stat().st_mode) == 0o600


def test_paths_with_spaces_and_dollar_signs(run_role, tmp_path):
    s = Setup(tmp_path, venv=tmp_path / "my venv", reports=tmp_path / "rep $(touch INJECTED)")
    s.render(run_role)
    assert s.run().returncode == 0, s.run().stderr
    argv = s.argv()
    assert argv[argv.index("--workdir") + 1] == str(tmp_path / "rep $(touch INJECTED)" / "work")
    assert not list(tmp_path.rglob("INJECTED"))


@pytest.mark.skipif(not SHELLCHECK.exists() and not shutil.which("shellcheck"), reason="shellcheck not installed")
def test_rendered_wrapper_passes_shellcheck(run_role, tmp_path):
    s = Setup(tmp_path, byoc_sync_extra_args=["--quiet"]).render(run_role)
    tool = str(SHELLCHECK) if SHELLCHECK.exists() else shutil.which("shellcheck")
    proc = subprocess.run([tool, "-s", "sh", str(s.wrapper)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout


def test_rendering_never_shows_secrets_even_verbose_with_diff(run_role, tmp_path):
    s = Setup(
        tmp_path,
        byoc_sync_dry_run=False,
        byoc_sync_github_repo="me/p",
        byoc_sync_github_token="ghp-SECRET-2",
        byoc_sync_fingerprint_key="fp-SECRET-3",
        byoc_sync_extra_env={"PGPASSWORD": "pg-SECRET-4"},
    )
    (s.venv / "bin").mkdir(parents=True)
    (s.wrapper.parent).mkdir(parents=True)
    s.reports.mkdir(parents=True)
    for fingerprint in ("fp-SECRET-3", "fp-SECRET-5"):  # create the files, then change them (a diff would show it)
        s.values["byoc_sync_fingerprint_key"] = fingerprint
        proc = run_role("render", s.values, flags=("-vvv", "--diff"))
        assert proc.returncode == 0, proc.stdout[-2000:]
        out = proc.stdout + proc.stderr
        assert len(out) > 1000
        for secret in ("api-SECRET-1", "ghp-SECRET-2", "fp-SECRET-3", "pg-SECRET-4", "fp-SECRET-5", "vault-secret-123"):
            assert secret not in out


@pytest.mark.parametrize("code", [2, 127])
def test_a_failing_flock_is_an_error_not_a_busy_lock(run_role, tmp_path, code):
    s = Setup(tmp_path).render(run_role)
    fake = s.venv / "bin" / "flock"  # first on the wrapper's PATH
    fake.write_text(f"#!/bin/sh\nexit {code}\n")
    fake.chmod(0o755)
    proc = s.run()
    assert proc.returncode == 1
    assert f"byoc-sync-run: flock failed (exit {code}); is util-linux installed?" in proc.stderr
    assert not s.calls.exists()


@pytest.mark.parametrize("value", ["ChangeMe", "'changeme'", " changeme "])
def test_wrapper_treats_changeme_variants_as_missing(run_role, tmp_path, value):
    s = Setup(tmp_path, byoc_sync_galaxy_api_key=value).render(run_role)
    proc = s.run()
    assert proc.returncode == 1
    assert "byoc-sync-run: set these variables first: byoc_sync_galaxy_api_key" in proc.stderr
    assert "GALAXY_API_KEY" not in (s.config / "env").read_text()


def test_changing_the_group_later_moves_the_role_paths_to_it(run_role, tmp_path, other_group):
    s = Setup(tmp_path).render(run_role, tasks_from="configure")  # first with the primary group
    s.values["byoc_sync_group"] = other_group.gr_name
    s.render(run_role, tasks_from="configure")
    paths = (s.config, s.config / "env", s.config / "vault-password", s.reports, s.reports / "work")
    assert [p.stat().st_gid for p in paths] == [other_group.gr_gid] * 5
