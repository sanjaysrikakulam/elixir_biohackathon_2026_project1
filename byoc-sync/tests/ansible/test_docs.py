"""The role README, the example playbook, license and metadata stay in step with the role."""

from __future__ import annotations

import os
import re
import subprocess

import yaml

from .conftest import ANSIBLE_DIR, PLAYBOOK_BIN, ROLE_DIR

README = ROLE_DIR / "README.md"
EXAMPLE = ANSIBLE_DIR / "example-playbook.yml"
DEFAULTS = yaml.safe_load((ROLE_DIR / "defaults" / "main.yml").read_text())
CHANGEME = sorted(name for name, value in DEFAULTS.items() if value == "changeme")


def _must_set_table() -> set[str]:
    text = README.read_text()
    section = text.split("## Variables you must set", 1)[1].split("\n## ", 1)[0]
    return set(re.findall(r"^\| `(byoc_sync_[a-z_]+)`", section, re.M))


def test_every_default_is_documented():
    text = README.read_text()
    assert [name for name in DEFAULTS if f"`{name}`" not in text] == []


def test_every_changeme_default_is_in_the_must_set_tables():
    assert sorted(_must_set_table()) == CHANGEME


def test_readme_points_to_the_defaults_file():
    assert "defaults/main.yml" in README.read_text()


def test_example_playbook_sets_every_always_required_value():
    play = yaml.safe_load(EXAMPLE.read_text())[0]
    always = yaml.safe_load((ROLE_DIR / "vars" / "main.yml").read_text())["_byoc_sync_install_required"]
    assert len(always) == 6 and [name for name in always if name not in play["vars"]] == []
    assert play["become"] is True and play["roles"] == ["byoc_sync"]


def test_example_playbook_syntax(tmp_path):
    env = {**os.environ, "ANSIBLE_CONFIG": str(ANSIBLE_DIR / "ansible.cfg"), "ANSIBLE_HOME": str(tmp_path)}
    proc = subprocess.run(
        [str(PLAYBOOK_BIN), "-i", "localhost,", "--syntax-check", str(EXAMPLE)],
        capture_output=True,
        text=True,
        env=env,
        cwd=ANSIBLE_DIR,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_license_and_author():
    license_text = (ROLE_DIR / "LICENSE").read_text()
    meta = yaml.safe_load((ROLE_DIR / "meta" / "main.yml").read_text())["galaxy_info"]
    assert license_text.startswith("MIT License\n\nCopyright (c) 2026 Sanjay Srikakulam\n")
    assert (meta["author"], meta["license"]) == ("Sanjay Srikakulam", "MIT")
