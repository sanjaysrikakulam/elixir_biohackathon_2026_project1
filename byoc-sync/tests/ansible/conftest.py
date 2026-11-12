"""Run parts of the byoc_sync role against localhost without touching the host.

Every test play uses ``connection: local``, no ``become``, and writes only below pytest's ``tmp_path``.
"""

from __future__ import annotations

import getpass
import grp
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
ANSIBLE_DIR = ROOT / "ansible"
ROLE_DIR = ANSIBLE_DIR / "roles" / "byoc_sync"
PLAYBOOK_BIN = Path(sys.executable).parent / "ansible-playbook"
ME = getpass.getuser()
MY_GROUP = grp.getgrgid(os.getgid()).gr_name
# A group the test user belongs to besides its primary one: files can be given it without root.
OTHER_GROUPS = sorted(grp.getgrgid(gid).gr_name for gid in os.getgroups() if gid != os.getgid())


def good_vars(tmp_path: Path, **overrides) -> dict:
    """Values that pass validation: the current user and paths below tmp_path."""
    values = {
        "byoc_sync_user": ME,
        "byoc_sync_group": MY_GROUP,
        "byoc_sync_repo_url": "https://example.org/byoc-sync.git",
        "byoc_sync_venv": str(tmp_path / "venv"),
        "byoc_sync_reports_dir": str(tmp_path / "reports"),
        "byoc_sync_vault_password": "vault-secret-123",
    }
    values.update(overrides)
    return values


@pytest.fixture
def other_group() -> grp.struct_group:
    """A supplementary group of the test user, so a file's group visibly differs from the primary one."""
    if not OTHER_GROUPS:
        pytest.skip("the test user has no supplementary group")
    return grp.getgrnam(OTHER_GROUPS[0])


@pytest.fixture
def run_role(tmp_path):
    def _run(
        tasks_from: str,
        extra_vars: dict,
        *,
        gather_facts: bool = False,
        after: list | None = None,
        vars_yaml: str | None = None,
        flags: tuple[str, ...] = (),
    ):
        play = {
            "hosts": "localhost",
            "connection": "local",
            "gather_facts": gather_facts,
            "tasks": [{"ansible.builtin.include_role": {"name": "byoc_sync", "tasks_from": tasks_from}}]
            + (after or []),
        }
        playbook = tmp_path / "play.yml"
        playbook.write_text(yaml.safe_dump([play], sort_keys=False))
        vars_file = tmp_path / "vars.json"
        vars_file.write_text(json.dumps({"ansible_python_interpreter": sys.executable, **extra_vars}))
        env = {
            **os.environ,
            "ANSIBLE_ROLES_PATH": str(ANSIBLE_DIR / "roles"),
            "ANSIBLE_LOCAL_TEMP": str(tmp_path / "ansible-tmp"),
            "ANSIBLE_REMOTE_TEMP": str(tmp_path / "ansible-tmp"),
            "ANSIBLE_NOCOLOR": "1",
            "ANSIBLE_FORCE_COLOR": "0",
            "ANSIBLE_RETRY_FILES_ENABLED": "0",
            "ANSIBLE_HOME": str(tmp_path / "ansible-home"),
            "ANSIBLE_DEPRECATION_WARNINGS": "0",
        }
        extra = ["-e", f"@{vars_file}"]
        if vars_yaml is not None:  # for values that need YAML tags such as !unsafe
            yaml_file = tmp_path / "vars-extra.yml"
            yaml_file.write_text(vars_yaml)
            extra += ["-e", f"@{yaml_file}"]
        return subprocess.run(
            [str(PLAYBOOK_BIN), "-i", "localhost,", str(playbook), *extra, *flags],
            capture_output=True,
            text=True,
            env=env,
            timeout=300,
        )

    return _run
