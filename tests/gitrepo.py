"""Helpers that build throw-away git repositories for tests."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.org",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.org",
    "HOME": "/nonexistent",
}


def git(cwd: Path, *args: str, env: dict | None = None) -> str:
    import os

    full_env = {**os.environ, **ENV, **(env or {})}
    return subprocess.run(
        ["git", *args], cwd=cwd, env=full_env, check=True, capture_output=True, text=True
    ).stdout.strip()


def make_origin(tmp_path: Path, content: Path, branch: str = "master") -> tuple[Path, Path]:
    """A bare 'origin' seeded with ``content`` on ``branch``, and a developer clone of it."""
    seed = tmp_path / "seed"
    shutil.copytree(content, seed)
    git(seed, "init", "-q", "-b", branch)
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "initial")
    origin = tmp_path / "origin.git"
    git(tmp_path, "clone", "-q", "--bare", str(seed), str(origin))
    clone = tmp_path / "checkout"
    git(tmp_path, "clone", "-q", str(origin), str(clone))
    return origin, clone


def merge_branch(origin: Path, tmp_path: Path, branch: str, base: str = "master") -> None:
    """Merge ``branch`` into ``base`` on the bare origin (as GitHub's merge button would) and delete it."""
    work = tmp_path / f"merge-{branch}"
    git(tmp_path, "clone", "-q", str(origin), str(work))
    git(work, "checkout", "-q", base)
    git(work, "merge", "-q", "--no-ff", "-m", f"Merge {branch}", f"origin/{branch}")
    git(work, "push", "-q", "origin", base, f":{branch}")
    shutil.rmtree(work)
