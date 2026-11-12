"""Route jobs through TPV the way Galaxy does, using usegalaxy.eu's rendered TPV configuration.

The rendered tree comes from ``scripts/render-eu-tpv`` (EU CI's own templating playbook). Point
``BYOC_EU_TPV_DIR`` at its ``files/galaxy/tpv`` directory to enable these tests.
"""

from __future__ import annotations

import os
import types
from pathlib import Path

import yaml
from tpv.commands.test import mock_galaxy
from tpv.rules import gateway

# Same order as tpv_config_files in usegalaxy.eu's templates/galaxy/config/job_conf.yml, minus the
# shared database fetched over HTTPS.
EU_CONFIG_ORDER = (
    "tool_defaults.yml",
    "destinations.yml",
    "tools.yml",
    "interactive_tools.yml",
    "conda_in_container.yml",
    "users.yml",
    "roles.yml",
    "resubmission.yml",
)


def eu_tpv_dir() -> Path | None:
    value = os.environ.get("BYOC_EU_TPV_DIR")
    return Path(value) if value else None


class Tool(mock_galaxy.Tool):
    """TPV's mock tool plus the attributes usegalaxy.eu's rules read."""

    def __init__(self, tool_id: str, tool_type: str = "default", requires_galaxy: bool = False):
        super().__init__(tool_id, tool_type=tool_type)
        self.requires_galaxy_python_environment = requires_galaxy


class User(mock_galaxy.User):
    """TPV's mock user plus ``extra_preferences`` (read by EU's ``remote_resources`` rule)."""

    def __init__(self, email: str, extra_preferences: dict | None = None):
        super().__init__(email.split("@")[0], email)
        self.extra_preferences = dict(extra_preferences or {})


class Job(mock_galaxy.Job):
    """TPV's mock job with the fields of a job that has not run yet (read by EU's jwd parameter)."""

    def __init__(self):
        super().__init__()
        self.working_directory = None
        self.destination_params = {}


class _NoTrainingsCursor:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, *args):
        pass

    def fetchall(self):
        return []


class _NoTrainingsConnection(_NoTrainingsCursor):
    def cursor(self):
        return _NoTrainingsCursor()


def fake_psycopg2() -> types.ModuleType:
    """EU's TIaaS rule queries the TIaaS database on every job. This module answers "no ongoing trainings"."""
    module = types.ModuleType("psycopg2")
    module.connect = lambda **kwargs: _NoTrainingsConnection()
    return module


class JobWrapper:
    """Non-None job wrapper: EU's rules skip themselves when TPV runs without one (``tpv dry-run``)."""


def write_job_conf(tpv_files: list[Path], path: Path) -> Path:
    """Galaxy job_conf listing every runner the TPV destinations refer to."""
    runners = {"local"}
    for f in tpv_files:
        data = yaml.safe_load(f.read_text()) or {}
        for dest in (data.get("destinations") or {}).values():
            if isinstance(dest, dict) and dest.get("runner"):
                runners.add(dest["runner"])
    conf = {
        "runners": {r: {"load": "galaxy.jobs.runners.local:LocalJobRunner"} for r in sorted(runners)},
        "handling": {"assign": ["db-skip-locked"]},
        "execution": {
            "default": "tpv_dispatcher",
            "environments": {
                "tpv_dispatcher": {
                    "runner": "dynamic",
                    "type": "python",
                    "function": "map_tool_to_destination",
                    "rules_module": "tpv.rules",
                }
            },
        },
    }
    path.write_text(yaml.safe_dump(conf))
    return path


def route(tool: Tool, user: User, tpv_files: list[Path], job_conf: Path):
    """Return the Galaxy JobDestination TPV picks for this job (needs ``psycopg2`` faked, see conftest)."""
    gateway.ACTIVE_DESTINATION_MAPPERS = {}
    app = mock_galaxy.App(job_conf=str(job_conf))
    destination = gateway.map_tool_to_destination(
        app,
        Job(),
        tool,
        user,
        tpv_config_files=[str(f) for f in tpv_files],
        job_wrapper=JobWrapper(),
    )
    return destination
