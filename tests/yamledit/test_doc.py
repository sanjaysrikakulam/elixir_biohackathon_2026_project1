import pytest

from byoc_sync.yamledit.doc import Doc, LocateError

DEST = "files/galaxy/tpv/destinations.yml.j2"
JOBCONF = "templates/galaxy/config/job_conf.yml"
MQ = "group_vars/mq.yml"


def lines_of(text, span):
    return text.splitlines()[span.start : span.end]


def test_map_child_span_of_an_eu_destination(eu_fixtures):
    text = (eu_fixtures / DEST).read_text()
    span = Doc(text).map_child(["destinations"], "pulsar_egi01_tpv")
    block = lines_of(text, span)
    assert block[0] == "  pulsar_egi01_tpv:"
    assert block[-1] == "        - egi-pulsar"
    assert span.indent == 2


def test_span_excludes_trailing_blank_lines_and_section_banner(eu_fixtures):
    text = (eu_fixtures / DEST).read_text()
    doc = Doc(text)
    last_pulsar = [k for k, _ in doc.children(["destinations"]) if k.startswith("pulsar_")][-1]
    span = doc.map_child(["destinations"], last_pulsar)
    following = text.splitlines()[span.end :]
    assert following[0] == ""  # blank line kept outside
    assert any("LOCAL CONDOR DESTINATIONS" in line for line in following[:4])


def test_comment_inside_a_block_stays_in_the_block():
    text = "d:\n  a:\n    x: 1\n# disabled: true\n    y: 2\n  b:\n    z: 3\n"
    span = Doc(text).map_child(["d"], "a")
    assert lines_of(text, span) == ["  a:", "    x: 1", "# disabled: true", "    y: 2"]


def test_block_scalar_ending_in_a_hash_line_is_kept_whole():
    text = "u:\n  r:\n    rank: |\n      x = 1\n      # last line of code\n  s: 1\n"
    span = Doc(text).map_child(["u"], "r")
    assert lines_of(text, span)[-1] == "      # last line of code"


def test_seq_item_span_in_mq_users(eu_fixtures):
    text = (eu_fixtures / MQ).read_text()
    span = Doc(text).seq_item(["rabbitmq_users"], "user", "galaxy_eosc01")
    assert [line.strip() for line in lines_of(text, span)] == [
        "- user: galaxy_eosc01",
        "password: '{{ rabbitmq_password_galaxy_eosc01 }}'",
        "vhost: /pulsar/galaxy_eosc01",
    ]


def test_seq_item_span_in_job_conf_plugins(eu_fixtures):
    text = (eu_fixtures / JOBCONF).read_text()
    span = Doc(text).seq_item(["galaxy_jobconf", "plugins"], "id", "pulsar_eu_eosc01")
    block = lines_of(text, span)
    assert block[0].strip() == "- id: pulsar_eu_eosc01" and block[-1].strip() == "amqp_publish_retry_max_retries: 60"


def test_missing_entries_are_none(eu_fixtures):
    doc = Doc((eu_fixtures / MQ).read_text())
    assert doc.seq_item(["rabbitmq_users"], "user", "galaxy_nobody") is None
    assert doc.map_child([], "nope") is None


def test_missing_collection_raises_with_path(eu_fixtures):
    doc = Doc((eu_fixtures / MQ).read_text(), name="group_vars/mq.yml")
    with pytest.raises(LocateError, match=r"group_vars/mq.yml.*rabbitmq_userz"):
        doc.seq_item(["rabbitmq_userz"], "user", "x")


def test_wrong_collection_type_raises(eu_fixtures):
    doc = Doc((eu_fixtures / MQ).read_text())
    with pytest.raises(LocateError, match="not a list"):
        doc.seq_item(["rabbitmq_config"], "user", "x")


def test_children_lists_keys_with_spans(eu_fixtures):
    doc = Doc((eu_fixtures / DEST).read_text())
    keys = [k for k, _ in doc.children(["destinations"])]
    assert keys[0] == "basic_docker_destination" and "condor_internal" in keys


def test_item_indent_of_lists(eu_fixtures):
    doc = Doc((eu_fixtures / JOBCONF).read_text())
    _, span = doc.children(["galaxy_jobconf", "plugins"])[0]
    assert span.indent == 4


def test_data_is_the_masked_plain_structure(eu_fixtures):
    doc = Doc((eu_fixtures / MQ).read_text())
    users = {u["user"] for u in doc.data["rabbitmq_users"]}
    assert "galaxy_eosc01" in users


def test_invalid_yaml_raises_locate_error():
    with pytest.raises(LocateError, match="cannot parse"):
        Doc("a: [1, 2\n", name="broken.yml")


@pytest.mark.parametrize("path", [DEST, JOBCONF, MQ, "files/galaxy/tpv/users.yml"])
def test_every_eu_target_file_parses(eu_fixtures, path):
    assert Doc((eu_fixtures / path).read_text(), name=path).data


def test_null_collection_is_reported():
    with pytest.raises(LocateError, match="not a mapping or list"):
        Doc("users:\n").children(["users"])
