"""scripts/bootstrap checks its playbook argument before it sets anything up.

A stub ``uv`` first on PATH stops the script at its first real step, so these tests never install packages or
start Galaxy: reaching the stub means the argument was accepted.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests.gitrepo import git

BOOTSTRAP = Path(__file__).parent.parent / "scripts" / "bootstrap"
STUB_EXIT = 97


@pytest.fixture
def run(tmp_path):
    stub_dir = tmp_path / "stub-bin"
    stub_dir.mkdir()
    (stub_dir / "uv").write_text(f"#!/bin/sh\necho uv-stub-reached\nexit {STUB_EXIT}\n")
    (stub_dir / "uv").chmod(0o755)
    env = {**os.environ, "PATH": f"{stub_dir}{os.pathsep}{os.environ['PATH']}"}

    def _run(*args: str, cwd: Path = tmp_path) -> subprocess.CompletedProcess:
        return subprocess.run([str(BOOTSTRAP), *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=60)

    return _run


@pytest.fixture
def playbook(tmp_path):
    repo = tmp_path / "playbook"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "master")
    git(repo, "commit", "-q", "--allow-empty", "-m", "initial")
    (repo / "sub").mkdir()
    return repo


def _outcome(proc: subprocess.CompletedProcess) -> tuple[int, str, bool]:
    """Exit status, error output, and whether any setup step started."""
    started = "uv-stub-reached" in proc.stdout or "==" in proc.stdout
    return proc.returncode, proc.stderr, started


def _refusal(shown: str) -> tuple[int, str, bool]:
    message = f"{shown} is not a git repository (give a checkout or clone URL of the infrastructure playbook)\n"
    return 2, message, False


def test_plain_folder_is_refused_before_anything_starts(run, tmp_path):
    (tmp_path / "plain").mkdir()
    assert _outcome(run(str(tmp_path / "plain"))) == _refusal(str(tmp_path / "plain"))


def test_missing_path_is_refused(run, tmp_path):
    assert _outcome(run(str(tmp_path / "missing"))) == _refusal(str(tmp_path / "missing"))


def test_subfolder_of_a_repository_is_refused(run, playbook):
    assert _outcome(run(str(playbook / "sub"))) == _refusal(str(playbook / "sub"))


def test_unreachable_url_is_refused(run):
    url = "file:///nonexistent/playbook.git"
    assert _outcome(run(url)) == _refusal(url)


def test_no_argument_prints_the_usage_before_anything_starts(run):
    proc = run()
    assert proc.returncode == 2 and "scripts/bootstrap PLAYBOOK" in proc.stderr
    assert "uv-stub-reached" not in proc.stdout and "==" not in proc.stdout


def test_repository_path_is_accepted(run, playbook):
    proc = run(str(playbook))
    assert proc.returncode == STUB_EXIT and "uv-stub-reached" in proc.stdout


def test_relative_path_is_taken_from_the_callers_folder(run, playbook):
    proc = run("playbook", cwd=playbook.parent)
    assert proc.returncode == STUB_EXIT and f"== Playbook: {playbook}\n" in proc.stdout


def test_tests_only_needs_no_playbook(run):
    proc = run("--tests-only")
    assert proc.returncode == STUB_EXIT and "uv-stub-reached" in proc.stdout


SETUP = BOOTSTRAP.parent.parent / "testenv" / "setup.sh"


@pytest.mark.parametrize("port,url", [(None, "http://127.0.0.1:8089"), ("9999", "http://127.0.0.1:9999")])
def test_testenv_prints_the_galaxy_url_for_its_port(port, url):
    env = {k: v for k, v in os.environ.items() if k != "GALAXY_PORT"}
    if port:
        env["GALAXY_PORT"] = port
    proc = subprocess.run([str(SETUP), "url"], env=env, capture_output=True, text=True, timeout=30)
    assert (proc.returncode, proc.stdout) == (0, url + "\n")


def test_bootstrap_takes_the_galaxy_url_from_the_testenv():
    # A port written into bootstrap would ignore GALAXY_PORT (testenv/setup.sh owns it).
    text = BOOTSTRAP.read_text()
    assert "8089" not in text and "$(testenv/setup.sh url)" in text
