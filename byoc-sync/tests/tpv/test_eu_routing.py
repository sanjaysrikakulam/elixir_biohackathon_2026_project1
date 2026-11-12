"""Routing of BYOC users through usegalaxy.eu's real TPV configuration.

These tests load EU's rendered TPV files (tool defaults, destinations, tools, interactive tools, users,
roles, resubmission) and add one BYOC endpoint rendered by byoc-sync's own templates. They cover the
behaviour the generated entries promise:

* jobs that fit the endpoint always go there, everything else falls back to EU's own destinations;
* nobody but the owner can reach the endpoint, not even by selecting its tag as "remote resources";
* the users key cannot be matched by a look-alike e-mail address;
* a hand-written entry for the same e-mail keeps working next to the generated one;
* strict mode fails oversized jobs instead of falling back.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytest.importorskip("tpv.commands.test.mock_galaxy")
from galaxy.jobs.mapper import JobMappingException  # noqa: E402

from byoc_sync.config import SiteConfig  # noqa: E402
from byoc_sync.models import ByocRequest  # noqa: E402
from byoc_sync.naming import Names  # noqa: E402
from byoc_sync.render import Renderer  # noqa: E402

from .harness import EU_CONFIG_ORDER, Tool, User, eu_tpv_dir, route, write_job_conf  # noqa: E402

pytestmark = [
    pytest.mark.tpv,
    pytest.mark.skipif(eu_tpv_dir() is None, reason="set BYOC_EU_TPV_DIR to a rendered EU TPV directory"),
]

FIXTURES = Path(__file__).parent / "fixtures"
BYOC = "pulsar_alice01_byoc_tpv"
TAG = "alice01-byoc-pulsar"
ALICE = "alice@example.org"
DRAWS = 20


def generated(strict: bool = False) -> str:
    """alice's destination and users entry, rendered by byoc-sync's EU templates into one TPV file."""
    site = SiteConfig.load("eu")
    req = ByocRequest(name="alice01", password="unused", max_cores=8, max_mem=31, min_gpus=0, max_gpus=0, nfs_path=None)
    names = Names.for_(req.name, site.naming)
    renderer = Renderer(site)

    def indented(key):
        text = renderer.render(key, req, names, ALICE, strict=strict)
        return "\n".join("  " + line if line else line for line in text.split("\n"))

    return f"destinations:\n{indented('destinations')}\nusers:\n{indented('users')}\n"


@pytest.fixture
def tpv(tmp_path):
    """Returns ``route(tool, user, byoc_text=None, extra=())`` -> destination, bound to EU's configuration."""

    def _route(tool, user, byoc_text=None, extra=()):
        byoc = tmp_path / "byoc.yml"
        byoc.write_text(byoc_text if byoc_text is not None else generated())
        files = [eu_tpv_dir() / name for name in EU_CONFIG_ORDER]
        files += [FIXTURES / "test_tools.yml", byoc, *extra]
        return route(tool, user, files, write_job_conf(files, tmp_path / "job_conf.yml")).id

    return _route


def strict_variant(_text: str = "") -> str:
    return generated(strict=True)


def test_fitting_job_always_goes_to_the_owners_endpoint(tpv):
    assert {tpv(Tool("byoc_test_small"), User(ALICE)) for _ in range(DRAWS)} == {BYOC}


def test_tool_requiring_a_tag_the_endpoint_accepts_still_goes_there(tpv):
    # "singularity" is accepted through pulsar_default, which the endpoint inherits.
    assert tpv(Tool("byoc_test_singularity"), User(ALICE)) == BYOC


def test_oversized_job_falls_back_to_eu_destinations(tpv):
    picks = {tpv(Tool("byoc_test_big"), User(ALICE)) for _ in range(DRAWS)}
    assert BYOC not in picks and picks


def test_tool_needing_galaxy_runs_on_the_internal_destination(tpv):
    tool = Tool("__DATA_FETCH__", tool_type="data_source", requires_galaxy=True)
    assert tpv(tool, User(ALICE)) == "condor_internal"


def test_interactive_tool_does_not_go_to_the_endpoint(tpv):
    tool = Tool("interactive_tool_jupyter_notebook", tool_type="interactive")
    assert tpv(tool, User(ALICE)) != BYOC


def test_other_users_never_reach_the_endpoint(tpv):
    assert BYOC not in {tpv(Tool("byoc_test_small"), User("bob@example.org")) for _ in range(DRAWS)}


def test_selecting_the_tag_as_remote_resource_does_not_hijack_the_endpoint(tpv):
    bob = User("bob@example.org", {"distributed_compute|remote_resources": TAG})
    with pytest.raises(JobMappingException):
        tpv(Tool("byoc_test_small"), bob)


def test_without_the_owner_guard_the_hijack_would_succeed(tpv):
    # Negative control for the test above: proves the guard is what stops it.
    text = generated()
    unguarded = re.sub(r"\n    rules:\n      - id: byoc-owner-guard.*?(?=\nusers:)", "", text, flags=re.S)
    assert "byoc-owner-guard" not in unguarded
    bob = User("bob@example.org", {"distributed_compute|remote_resources": TAG})
    assert tpv(Tool("byoc_test_small"), bob, byoc_text=unguarded) == BYOC


def test_lookalike_email_never_reaches_the_endpoint(tpv):
    # The users key is a prefix regex in TPV, so the look-alike inherits alice's rule; the owner guard stops it.
    lookalike = User("alice@example.org.evil.example")
    assert BYOC not in {tpv(Tool("byoc_test_small"), lookalike) for _ in range(DRAWS)}


def test_rule_added_to_a_handwritten_entry_keeps_both_working(tmp_path):
    # byoc-sync adds its rule to an existing entry for the same e-mail; both rules then apply to alice's jobs.
    from byoc_sync.targets import Target

    site = SiteConfig.load("eu")
    req = ByocRequest(name="alice01", password="unused", max_cores=8, max_mem=31, min_gpus=0, max_gpus=0, nfs_path=None)
    users_obj = Renderer(site).desired("users", req, Names.for_(req.name, site.naming), ALICE)
    users = tmp_path / "users.yml"
    users.write_text(
        Target(site, "users").plan((FIXTURES / "handwritten_alice.yml").read_text(), {users_obj.id: users_obj}).text
    )
    destinations = tmp_path / "byoc.yml"
    destinations.write_text(generated().split("\nusers:\n")[0] + "\n")
    files = [eu_tpv_dir() / name for name in EU_CONFIG_ORDER] + [FIXTURES / "test_tools.yml", destinations, users]
    destination = route(Tool("byoc_test_small"), User(ALICE), files, write_job_conf(files, tmp_path / "job_conf.yml"))
    assert destination.id == BYOC
    assert destination.params.get("handwritten_marker") == "kept"


def test_strict_mode_sends_fitting_jobs_to_the_endpoint(tpv):
    strict = strict_variant()
    assert {tpv(Tool("byoc_test_small"), User(ALICE), byoc_text=strict) for _ in range(DRAWS)} == {BYOC}
    assert tpv(Tool("byoc_test_singularity"), User(ALICE), byoc_text=strict) == BYOC


def test_strict_mode_fails_oversized_jobs(tpv):
    strict = strict_variant()
    with pytest.raises(JobMappingException):
        tpv(Tool("byoc_test_big"), User(ALICE), byoc_text=strict)


def test_strict_mode_keeps_internal_tools_internal(tpv):
    strict = strict_variant()
    tool = Tool("__DATA_FETCH__", tool_type="data_source", requires_galaxy=True)
    assert tpv(tool, User(ALICE), byoc_text=strict) == "condor_internal"


def test_gpu_job_falls_back_when_the_endpoint_offers_no_gpus(tpv):
    assert BYOC not in {tpv(Tool("byoc_test_gpu"), User(ALICE)) for _ in range(5)}
