"""Install byoc-sync with the role into tmp_path, as the current user, without packages or become.

Opt-in (needs PyPI for byoc-sync's dependencies): BYOC_ANSIBLE_INSTALL_TEST=1.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from tests.gitrepo import git

from .conftest import ROOT, good_vars

pytestmark = pytest.mark.skipif(
    os.environ.get("BYOC_ANSIBLE_INSTALL_TEST") != "1", reason="set BYOC_ANSIBLE_INSTALL_TEST=1 (needs PyPI)"
)


def _recap_changed(stdout: str) -> int:
    match = re.search(r"localhost\s+:\s+ok=\d+\s+changed=(\d+)", stdout)
    assert match, stdout
    return int(match.group(1))


@pytest.fixture
def source(tmp_path):
    """A clone of this repository at HEAD that the test can add commits to."""
    src = tmp_path / "byoc-sync-origin"
    git(tmp_path, "clone", "-q", "--no-hardlinks", str(ROOT), str(src))
    return src


UV = Path.home() / ".local" / "bin" / "uv"


def _python_shim(tmp_path) -> str:
    """This host has no python3-venv (the role installs it on Ubuntu, but this test cannot use sudo): make
    `python -m venv DIR` create the venv with `uv venv --seed`, and run anything else on the system Python."""
    uv = str(UV) if UV.exists() else shutil.which("uv")
    if not uv:
        pytest.skip("needs uv to stand in for python3-venv")
    shim = tmp_path / "python-shim"
    shim.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = -m ] && [ "$2" = venv ]; then\n'
        '  shift 2; clear=; if [ "$1" = --clear ]; then clear=--clear; shift; fi\n'
        "  exec " + uv + ' venv -q --seed $clear -p /usr/bin/python3 "$1"\n'
        "fi\n"
        'exec /usr/bin/python3 "$@"\n'
    )
    shim.chmod(0o755)
    return str(shim)


def _values(tmp_path, source):
    return good_vars(
        tmp_path,
        byoc_sync_repo_url=f"file://{source}",
        byoc_sync_version="HEAD",
        byoc_sync_manage_packages=False,
        byoc_sync_python=_python_shim(tmp_path),
        _byoc_sync_become=False,
        _byoc_sync_manage_cron=False,
        byoc_sync_config_dir=str(tmp_path / "etc"),
        byoc_sync_wrapper_path=str(tmp_path / "bin" / "byoc-sync-run"),
        _byoc_sync_wrapper_owner=os.environ.get("USER") or subprocess.check_output(["id", "-un"], text=True).strip(),
        _byoc_sync_wrapper_group=subprocess.check_output(["id", "-gn"], text=True).strip(),
    )


def _version(tmp_path) -> str:
    return subprocess.run(
        [str(tmp_path / "venv" / "bin" / "byoc-sync"), "--version"], capture_output=True, text=True
    ).stdout.strip()


def _marker(tmp_path) -> dict[str, str]:
    """The install record: revision, extras and interpreter of the last successful install."""
    text = (tmp_path / "venv" / ".byoc-sync-installed").read_text()
    return dict(line.split("=", 1) for line in text.splitlines())


def test_install_is_idempotent_and_reinstalls_after_a_failed_update(run_role, tmp_path, source):
    (tmp_path / "bin").mkdir()
    values = _values(tmp_path, source)

    first = run_role("main", values, gather_facts=True)
    assert first.returncode == 0, first.stdout[-3000:]
    assert _version(tmp_path) == "byoc-sync 0.1.0"
    rev_a = git(source, "rev-parse", "HEAD")
    assert _marker(tmp_path)["rev"] == rev_a and _marker(tmp_path)["extras"] == ""

    again = run_role("main", values, gather_facts=True)
    assert again.returncode == 0 and _recap_changed(again.stdout) == 0, again.stdout[-3000:]

    (source / "NEW_FILE").write_text("x\n")
    git(source, "add", "NEW_FILE")
    git(source, "commit", "-q", "-m", "a new commit")
    rev_b = git(source, "rev-parse", "HEAD")

    os.environ["PIP_CONSTRAINT"] = str(tmp_path / "no-such-constraints.txt")  # makes pip fail
    try:
        broken = run_role("main", values, gather_facts=True)
    finally:
        del os.environ["PIP_CONSTRAINT"]
    assert broken.returncode != 0
    assert _marker(tmp_path)["rev"] == rev_a  # not recorded as installed

    retried = run_role("main", values, gather_facts=True)
    assert retried.returncode == 0, retried.stdout[-3000:]
    assert _marker(tmp_path)["rev"] == rev_b
    assert (tmp_path / "venv-src" / "NEW_FILE").exists()

    # Another interpreter: the virtualenv is rebuilt and byoc-sync reinstalled into it.
    other = tmp_path / "other-python"
    other.write_bytes(Path(values["byoc_sync_python"]).read_bytes())
    other.chmod(0o755)
    values["byoc_sync_python"] = str(other)
    switched = run_role("main", values, gather_facts=True)
    assert switched.returncode == 0 and _recap_changed(switched.stdout) > 0, switched.stdout[-3000:]
    assert re.fullmatch(re.escape(str(other)) + r" \d+\.\d+", _marker(tmp_path)["python"])  # major.minor only
    assert _version(tmp_path) == "byoc-sync 0.1.0"


def test_install_folders_get_the_configured_group_and_pass_it_on(run_role, tmp_path, source, other_group):
    (tmp_path / "bin").mkdir()
    values = {**_values(tmp_path, source), "byoc_sync_group": other_group.gr_name}
    proc = run_role("main", values, gather_facts=True)
    assert proc.returncode == 0, proc.stdout[-3000:]
    src, venv = tmp_path / "venv-src", tmp_path / "venv"
    assert [(p.stat().st_gid, stat.S_IMODE(p.stat().st_mode)) for p in (src, venv)] == [
        (other_group.gr_gid, 0o2755),
        (other_group.gr_gid, 0o2755),
    ]
    # Made by git, uv/venv, pip and the role's install record; none of them sets a group itself.
    inside = [
        src / "pyproject.toml",
        venv / "pyvenv.cfg",
        venv / ".byoc-sync-installed",
        *venv.glob("lib/python*/site-packages/byoc_sync/__init__.py"),
    ]
    assert len(inside) == 4
    assert {str(p.relative_to(tmp_path)): p.stat().st_gid for p in inside} == {
        str(p.relative_to(tmp_path)): other_group.gr_gid for p in inside
    }
