"""Platform selection: packages, Python and cron per supported distribution (facts are faked, nothing installs)."""

from __future__ import annotations

import json

import pytest


def _platform(run_role, tmp_path, facts: dict):
    out = tmp_path / "platform.json"
    dump = {
        "ansible.builtin.copy": {
            "dest": str(out),
            "mode": "0600",
            "content": "{{ {'python': _byoc_sync_python, 'packages': _byoc_sync_packages, "
            "'cron_package': _byoc_sync_cron_package, 'cron_service': _byoc_sync_cron_service} | to_json }}",
        }
    }
    proc = run_role("platform", {"ansible_facts": facts}, after=[dump])
    return proc, (json.loads(out.read_text()) if out.exists() else None)


def _facts(distribution: str, major: str, version: str) -> dict:
    return {"distribution": distribution, "distribution_major_version": major, "distribution_version": version}


EL = {"cron_package": "cronie", "cron_service": "crond"}
UBUNTU = {"cron_package": "cron", "cron_service": "cron"}


@pytest.mark.parametrize(
    "facts,expected",
    [
        (
            {"distribution": "Rocky", "distribution_major_version": "9", "distribution_version": "9.6"},
            {"python": "python3.12", "packages": ["acl", "git", "python3.12", "util-linux"], **EL},
        ),
        (
            {"distribution": "AlmaLinux", "distribution_major_version": "9", "distribution_version": "9.5"},
            {"python": "python3.12", "packages": ["acl", "git", "python3.12", "util-linux"], **EL},
        ),
        (
            {"distribution": "Rocky", "distribution_major_version": "10", "distribution_version": "10.0"},
            {"python": "python3", "packages": ["acl", "git", "python3", "util-linux"], **EL},
        ),
        (
            {"distribution": "AlmaLinux", "distribution_major_version": "10", "distribution_version": "10.1"},
            {"python": "python3", "packages": ["acl", "git", "python3", "util-linux"], **EL},
        ),
        (
            {"distribution": "Ubuntu", "distribution_major_version": "22", "distribution_version": "22.04"},
            {"python": "python3", "packages": ["acl", "git", "python3", "python3-venv", "util-linux"], **UBUNTU},
        ),
        (
            {"distribution": "Ubuntu", "distribution_major_version": "24", "distribution_version": "24.04"},
            {"python": "python3", "packages": ["acl", "git", "python3", "python3-venv", "util-linux"], **UBUNTU},
        ),
    ],
)
def test_platform_packages(run_role, tmp_path, facts, expected):
    proc, chosen = _platform(run_role, tmp_path, facts)
    assert proc.returncode == 0, proc.stdout
    assert chosen == expected


@pytest.mark.parametrize(
    "facts,shown",
    [
        (_facts("Debian", "12", "12.7"), "Debian 12.7"),
        (_facts("Rocky", "8", "8.10"), "Rocky 8.10"),
        (_facts("Ubuntu", "20", "20.04"), "Ubuntu 20.04"),
    ],
)
def test_unsupported_platform_fails(run_role, tmp_path, facts, shown):
    proc, chosen = _platform(run_role, tmp_path, facts)
    assert proc.returncode != 0 and chosen is None
    assert f"byoc_sync supports Rocky/AlmaLinux 9 and 10 and Ubuntu 22.04 and 24.04, not {shown}" in proc.stdout


def test_python_can_be_overridden(run_role, tmp_path):
    facts = {"distribution": "Rocky", "distribution_major_version": "9", "distribution_version": "9.6"}
    out = tmp_path / "python.txt"
    dump = {"ansible.builtin.copy": {"dest": str(out), "mode": "0600", "content": "{{ _byoc_sync_python }}"}}
    proc = run_role("platform", {"ansible_facts": facts, "byoc_sync_python": "python3.13"}, after=[dump])
    assert proc.returncode == 0, proc.stdout
    assert out.read_text() == "python3.13"
