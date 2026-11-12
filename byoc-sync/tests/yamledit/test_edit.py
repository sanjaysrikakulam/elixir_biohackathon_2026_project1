import pytest

from byoc_sync.yamledit.doc import Doc
from byoc_sync.yamledit.edit import Edit, EditError, apply, delete_edit, insert_after_edit

DEST = "files/galaxy/tpv/destinations.yml.j2"
MQ = "group_vars/mq.yml"


def test_insert_and_replace_and_delete_bottom_up():
    text = "a: 1\nb: 2\nc: 3\n"
    edits = [Edit("insert", 1, 1, ["x: 0"]), Edit("replace", 2, 3, ["c: 4"]), Edit("delete", 0, 1, [])]
    assert apply(text, edits) == "x: 0\nb: 2\nc: 4\n"


def test_overlapping_edits_are_rejected():
    with pytest.raises(EditError):
        apply("a\nb\nc\n", [Edit("replace", 0, 2, ["x"]), Edit("delete", 1, 3, [])])


def test_crlf_is_preserved():
    assert apply("a: 1\r\nb: 2\r\n", [Edit("insert", 1, 1, ["x: 0"])]) == "a: 1\r\nx: 0\r\nb: 2\r\n"


def test_missing_final_newline_is_preserved():
    assert apply("a: 1\nb: 2", [Edit("replace", 1, 2, ["b: 3"])]) == "a: 1\nb: 3"


def test_insert_after_last_pulsar_destination_lands_before_the_condor_banner(eu_fixtures):
    text = (eu_fixtures / DEST).read_text()
    doc = Doc(text)
    block = ["  pulsar_new_byoc_tpv:", "    inherits: pulsar_default"]
    edit = insert_after_edit(doc, ["destinations"], anchor=r"^pulsar_", lines=block)
    out = apply(text, [edit]).splitlines()
    i = out.index("  pulsar_new_byoc_tpv:")
    assert out[i - 1] == ""  # separated like its neighbours
    assert any("LOCAL CONDOR DESTINATIONS" in line for line in out[i + 2 : i + 6])
    assert Doc(apply(text, [edit])).data["destinations"]["pulsar_new_byoc_tpv"]["inherits"] == "pulsar_default"


def test_insert_into_a_list_without_blank_separators(eu_fixtures):
    text = (eu_fixtures / MQ).read_text()
    block = ["  - user: galaxy_new_byoc", "    vhost: /pulsar/galaxy_new_byoc"]
    out = apply(text, [insert_after_edit(Doc(text), ["rabbitmq_users"], anchor=r"^galaxy_", lines=block, field="user")])
    lines = out.splitlines()
    i = lines.index("  - user: galaxy_new_byoc")
    assert lines[i - 1].strip() == "vhost: /pulsar/galaxy_ufz_eve01"
    assert lines[i + 2] == ""


def test_insert_without_matching_anchor_appends_to_the_collection():
    text = "users:\n  a@b.c:\n    cores: 1\nother: 2\n"
    out = apply(text, [insert_after_edit(Doc(text), ["users"], anchor=r"^zzz", lines=["  new: {}"])])
    assert out == "users:\n  a@b.c:\n    cores: 1\n  new: {}\nother: 2\n"


def test_insert_into_empty_flow_mapping_is_refused():
    text = "users: {}\n"
    with pytest.raises(EditError, match="flow"):
        insert_after_edit(Doc(text), ["users"], anchor=r".", lines=["  a: 1"])


def test_delete_after_insert_restores_the_file_exactly(eu_fixtures):
    text = (eu_fixtures / DEST).read_text()
    block = ["  pulsar_new_byoc_tpv:", "    inherits: pulsar_default"]
    inserted = apply(text, [insert_after_edit(Doc(text), ["destinations"], anchor=r"^pulsar_", lines=block)])
    doc = Doc(inserted)
    restored = apply(inserted, [delete_edit(doc, doc.map_child(["destinations"], "pulsar_new_byoc_tpv"))])
    assert restored == text


def test_delete_list_item_restores_exactly(eu_fixtures):
    text = (eu_fixtures / MQ).read_text()
    block = ["  - user: galaxy_new_byoc", "    vhost: /pulsar/galaxy_new_byoc"]
    inserted = apply(
        text, [insert_after_edit(Doc(text), ["rabbitmq_users"], anchor=r"^galaxy_", lines=block, field="user")]
    )
    doc = Doc(inserted)
    restored = apply(inserted, [delete_edit(doc, doc.seq_item(["rabbitmq_users"], "user", "galaxy_new_byoc"))])
    assert restored == text


def test_lines_outside_the_edit_are_byte_identical(eu_fixtures):
    text = (eu_fixtures / DEST).read_text()
    doc = Doc(text)
    span = doc.map_child(["destinations"], "pulsar_egi01_tpv")
    out = apply(text, [Edit("replace", span.start, span.end, ["  pulsar_egi01_tpv: {}"])])
    a, b = text.splitlines(), out.splitlines()
    assert a[: span.start] == b[: span.start]
    assert a[span.end :] == b[span.start + 1 :]
