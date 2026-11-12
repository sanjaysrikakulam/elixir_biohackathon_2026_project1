"""The same engine against a deployment with plain YAML files and its own naming and templates."""

from pathlib import Path

import pytest

from byoc_sync.config import SiteConfig
from byoc_sync.naming import Names
from byoc_sync.render import Renderer
from byoc_sync.targets import Target
from byoc_sync.yamledit.doc import Doc

from .conftest import request_for

ROOT = Path(__file__).parents[1] / "fixtures" / "othersite"


@pytest.fixture
def other():
    return SiteConfig.load(str(ROOT / "site.yml"))


@pytest.mark.parametrize("key", ["mq", "job_conf", "destinations", "users"])
def test_create_and_delete_round_trip(other, key):
    spec = other.targets[key]
    text = (ROOT / "repo" / spec.file).read_text()
    req = request_for("carol01")
    names = Names.for_(req.name, other.naming)
    renderer = Renderer(other)
    obj = renderer.desired(key, req, names, "carol@example.org")
    created = Target(other, key).plan(text, {obj.id: obj}).text
    assert created != text
    gone = renderer.removal(key, names, "carol@example.org")
    assert Target(other, key).plan(created, {gone.id: gone}).text == text


def test_custom_naming_and_anchor(other):
    text = (ROOT / "repo/tpv/destinations.yml").read_text()
    req = request_for("carol01")
    obj = Renderer(other).desired("destinations", req, Names.for_(req.name, other.naming), "carol@example.org")
    lines = Target(other, "destinations").plan(text, {obj.id: obj}).text.splitlines()
    assert lines[lines.index("  byoc_carol01:") - 2] == "    runner: pulsar"
    assert lines[-1] == "# end of destinations"
    assert Doc("\n".join(lines)).data["destinations"]["byoc_carol01"]["scheduling"]["require"] == ["byoc-carol01"]
