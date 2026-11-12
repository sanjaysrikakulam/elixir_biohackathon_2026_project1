"""The cron job line (rendered alone) and the shape of the cron tasks (never run: they would edit a real crontab)."""

from __future__ import annotations

import yaml

from .conftest import ROLE_DIR, good_vars


def _job(run_role, tmp_path, **overrides) -> str:
    out = tmp_path / "job.txt"
    dump = {"ansible.builtin.copy": {"dest": str(out), "mode": "0600", "content": "{{ _byoc_sync_cron_job }}"}}
    proc = run_role("cron_vars", good_vars(tmp_path, **overrides), after=[dump])
    assert proc.returncode == 0, proc.stdout
    return out.read_text()


def test_cron_job_line(run_role, tmp_path):
    job = _job(run_role, tmp_path, byoc_sync_reports_dir="/srv/byoc/reports")
    assert job == "umask 077; /usr/local/bin/byoc-sync-run >> /srv/byoc/reports/byoc-sync.log 2>&1"


def test_cron_job_line_quotes_paths(run_role, tmp_path):
    job = _job(run_role, tmp_path, byoc_sync_reports_dir="/srv/my reports", byoc_sync_wrapper_path="/opt/b s/run")
    assert job == "umask 077; '/opt/b s/run' >> '/srv/my reports/byoc-sync.log' 2>&1"


def _tasks(name: str) -> list[dict]:
    def flatten(tasks):
        for task in tasks:
            yield task
            yield from flatten(task.get("block", []))

    return list(flatten(yaml.safe_load((ROLE_DIR / "tasks" / name).read_text())))


def test_cron_tasks_install_and_remove_the_named_entry():
    crons = [t for t in _tasks("cron.yml") if "ansible.builtin.cron" in t]
    present = [t for t in crons if t["ansible.builtin.cron"].get("state", "present") == "present"]
    absent = [t for t in crons if t["ansible.builtin.cron"].get("state") == "absent"]
    assert len(present) == 1 and len(absent) == 1
    entry = present[0]["ansible.builtin.cron"]
    assert entry == {
        "name": "byoc-sync",
        "user": "{{ byoc_sync_user }}",
        "minute": "{{ byoc_sync_cron_minute }}",
        "hour": "{{ byoc_sync_cron_hour }}",
        "day": "{{ byoc_sync_cron_day }}",
        "month": "{{ byoc_sync_cron_month }}",
        "weekday": "{{ byoc_sync_cron_weekday }}",
        "job": "{{ _byoc_sync_cron_job }}",
        "state": "present",
    }
    assert absent[0]["ansible.builtin.cron"] == {"name": "byoc-sync", "user": "{{ byoc_sync_user }}", "state": "absent"}


def test_cron_tasks_are_guarded():
    top = yaml.safe_load((ROLE_DIR / "tasks" / "cron.yml").read_text())
    tasks = _tasks("cron.yml")
    # Everything that touches cron sits under the _byoc_sync_manage_cron switch (tests switch it off).
    blocks = [t for t in top if "block" in t]
    assert len(blocks) == 2 and all("_byoc_sync_manage_cron | bool" in t["when"] for t in blocks)
    assert [t for t in top if "block" not in t] == [
        {"name": "Build the cron job line", "ansible.builtin.import_tasks": "cron_vars.yml"}
    ]
    service = [t for t in tasks if "ansible.builtin.service" in t]
    assert service and service[0]["ansible.builtin.service"] == {
        "name": "{{ _byoc_sync_cron_service }}",
        "state": "started",
        "enabled": True,
    }


def test_apt_cache_is_refreshed_before_packages_on_ubuntu():
    tasks = _tasks("install.yml")
    names = [next(k for k in t if k.startswith("ansible.builtin.")) for t in tasks if "name" in t and "block" not in t]
    apts = [t for t in tasks if "ansible.builtin.apt" in t]
    assert len(apts) == 1
    apt = apts[0]
    assert apt["ansible.builtin.apt"] == {"update_cache": True, "cache_valid_time": 3600}
    assert "ansible_facts['os_family'] == 'Debian'" in str(apt["when"]) and "byoc_sync_manage_packages" in str(
        apt["when"]
    )
    assert names.index("ansible.builtin.apt") < names.index("ansible.builtin.package")


def test_read_only_install_commands_also_run_in_check_mode():
    readers = [t for t in _tasks("install.yml") if t.get("name", "").startswith("Read ")]
    assert len(readers) >= 4 and all(t.get("check_mode") is False for t in readers)


def test_bundled_site_is_checked_right_after_the_clone():
    names = [t.get("name", "") for t in _tasks("install.yml")]
    assert "Check the bundled site name" in names
    clone = next(i for i, n in enumerate(names) if n.startswith("Clone byoc-sync"))
    site = names.index("Check the bundled site name")
    venv = names.index("Create or rebuild the virtualenv")
    assert clone < site < venv
    assert "site_check.yml" not in (ROLE_DIR / "tasks" / "configure.yml").read_text()
