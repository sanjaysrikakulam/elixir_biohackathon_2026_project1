"""Create, update and delete BYOC entries in copies of usegalaxy.eu's real files."""

import difflib

import pytest

from byoc_sync.naming import Names
from byoc_sync.render import Renderer, email_key
from byoc_sync.targets import Target
from byoc_sync.yamledit.doc import Doc

from .conftest import request_for

FILES = {
    "mq": "group_vars/mq.yml",
    "job_conf": "templates/galaxy/config/job_conf.yml",
    "destinations": "files/galaxy/tpv/destinations.yml.j2",
    "users": "files/galaxy/tpv/users.yml",
}


def desired(site, key, req, email="alice@example.org", strict=False):
    names = Names.for_(req.name, site.naming)
    return Renderer(site).desired(key, req, names, email, strict=strict)


def removal(site, key, req, email="alice@example.org"):
    return Renderer(site).removal(key, Names.for_(req.name, site.naming), email)


def plan(site, key, text, *objects):
    target = Target(site, key)
    return target.plan(text, dict(objects), file=FILES[key])


def edit_entry(text, entry, old, new):
    """Replace ``old`` with ``new`` inside one destination entry only (other EU entries look alike)."""
    span = Doc(text).span_of(["destinations", entry])
    lines = text.split("\n")
    block = "\n".join(lines[span.start : span.end]) + "\n"
    assert old in block, f"{old!r} not in {entry}"
    lines[span.start : span.end] = block.replace(old, new, 1).rstrip("\n").split("\n")
    return "\n".join(lines)


def changed_lines(before, after):
    return [
        line
        for line in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=0)
        if line[:1] in "+-" and not line.startswith(("+++", "---"))
    ]


@pytest.mark.parametrize("key", list(FILES))
def test_create_adds_only_the_rendered_block(site, eu_fixtures, key):
    text = (eu_fixtures / FILES[key]).read_text()
    obj = desired(site, key, request_for())
    result = plan(site, key, text, (obj.id, obj))
    added = [line[1:] for line in changed_lines(text, result.text)]
    assert all(line.startswith("+") for line in changed_lines(text, result.text))
    assert [a.strip() for a in added if a.strip()] == [line.strip() for line in obj.text.splitlines() if line.strip()]
    assert [c.action for c in result.changes] == ["create"]


def test_mq_user_goes_after_the_last_galaxy_user(site, eu_fixtures):
    text = (eu_fixtures / FILES["mq"]).read_text()
    obj = desired(site, "mq", request_for())
    lines = plan(site, "mq", text, (obj.id, obj)).text.splitlines()
    i = lines.index("  - user: galaxy_alice01_byoc")
    assert lines[i - 1] == "    vhost: /pulsar/galaxy_ufz_eve01"
    assert lines[i + 1] == "    password: '{{ rabbitmq_password_galaxy_alice01_byoc }}'"
    assert lines[i + 3] == "" and lines[i + 4] == "rabbitmq_plugins:"


def test_destination_goes_after_the_last_pulsar_destination_before_the_condor_section(site, eu_fixtures):
    text = (eu_fixtures / FILES["destinations"]).read_text()
    obj = desired(site, "destinations", request_for())
    lines = plan(site, "destinations", text, (obj.id, obj)).text.splitlines()
    i = lines.index("  pulsar_alice01_byoc_tpv:")
    assert lines[i - 1] == ""
    after = lines[i:]
    end = next(n for n, line in enumerate(after) if line == "")
    assert any("LOCAL CONDOR DESTINATIONS" in line for line in after[end : end + 4])


def test_second_byoc_entry_goes_after_the_first(site, eu_fixtures):
    text = (eu_fixtures / FILES["job_conf"]).read_text()
    a = desired(site, "job_conf", request_for("alice01"))
    text = plan(site, "job_conf", text, (a.id, a)).text
    b = desired(site, "job_conf", request_for("bob01"), email="bob@example.org")
    lines = plan(site, "job_conf", text, (b.id, b)).text.splitlines()
    assert lines.index("    - id: pulsar_eu_bob01_byoc") > lines.index("    - id: pulsar_eu_alice01_byoc")
    ids = [line.strip() for line in lines if line.strip().startswith("- id: pulsar_eu_")]
    assert ids[-2:] == ["- id: pulsar_eu_alice01_byoc", "- id: pulsar_eu_bob01_byoc"]


def test_update_of_limits_changes_only_those_lines(site, eu_fixtures):
    text = (eu_fixtures / FILES["destinations"]).read_text()
    obj = desired(site, "destinations", request_for())
    created = plan(site, "destinations", text, (obj.id, obj)).text
    new = desired(site, "destinations", request_for(max_cores=16, max_gpus=1))
    result = plan(site, "destinations", created, (new.id, new))
    assert sorted(changed_lines(created, result.text)) == sorted(
        [
            "-    max_accepted_cores: 8",
            "+    max_accepted_cores: 16",
            "-    max_accepted_gpus: 0",
            "+    max_accepted_gpus: 1",
        ]
    )
    assert [c.action for c in result.changes] == ["update"]
    assert set(result.changes[0].fields) == {"max_accepted_cores", "max_accepted_gpus"}


def test_unchanged_entry_is_a_noop(site, eu_fixtures):
    text = (eu_fixtures / FILES["users"]).read_text()
    obj = desired(site, "users", request_for())
    created = plan(site, "users", text, (obj.id, obj)).text
    result = plan(site, "users", created, (obj.id, obj))
    assert result.text == created and result.changes == []


def test_nfs_path_added_changed_and_removed(site, eu_fixtures):
    text = (eu_fixtures / FILES["destinations"]).read_text()
    base = desired(site, "destinations", request_for())
    created = plan(site, "destinations", text, (base.id, base)).text

    with_nfs = desired(site, "destinations", request_for(nfs_path="/mnt/share"))
    added = plan(site, "destinations", created, (with_nfs.id, with_nfs)).text
    data = Doc(added).data["destinations"]["pulsar_alice01_byoc_tpv"]
    assert data["params"] == {
        "jobs_directory": "/mnt/share/staging",
        "persistence_directory": "/mnt/share/persisted_data",
    }
    assert data["env"] == {"SINGULARITY_CACHEDIR": "/mnt/share/var/database/container_cache"}

    moved = desired(site, "destinations", request_for(nfs_path="/nfs/other"))
    changed = plan(site, "destinations", added, (moved.id, moved)).text
    assert (
        Doc(changed).data["destinations"]["pulsar_alice01_byoc_tpv"]["params"]["jobs_directory"] == "/nfs/other/staging"
    )
    assert len(added.splitlines()) == len(changed.splitlines())
    assert sum(a != b for a, b in zip(added.splitlines(), changed.splitlines(), strict=True)) == 3

    removed = plan(site, "destinations", changed, (base.id, base)).text
    assert removed == created


def test_hand_added_keys_survive_updates(site, eu_fixtures):
    text = (eu_fixtures / FILES["destinations"]).read_text()
    obj = desired(site, "destinations", request_for())
    created = plan(site, "destinations", text, (obj.id, obj)).text
    edited = created.replace(
        "    runner: pulsar_eu_alice01_byoc\n",
        "    runner: pulsar_eu_alice01_byoc\n    # tuned by an admin\n    params:\n      submit_request_cpus: 4\n",
    )
    new = desired(site, "destinations", request_for(max_cores=12))
    result = plan(site, "destinations", edited, (new.id, new))
    entry = Doc(result.text).data["destinations"]["pulsar_alice01_byoc_tpv"]
    assert entry["params"] == {"submit_request_cpus": 4} and entry["max_accepted_cores"] == 12
    assert "    # tuned by an admin" in result.text


def test_hand_added_list_items_survive_updates(site, eu_fixtures):
    text = (eu_fixtures / FILES["destinations"]).read_text()
    obj = desired(site, "destinations", request_for())
    created = plan(site, "destinations", text, (obj.id, obj)).text
    edited = created.replace("        - alice01-byoc-pulsar\n", "        - alice01-byoc-pulsar\n        - offline\n")
    result = plan(site, "destinations", edited, (obj.id, obj))
    assert result.changes == [] and result.text == edited


def test_managed_value_turned_into_a_mapping_is_a_conflict(site, eu_fixtures):
    text = (eu_fixtures / FILES["destinations"]).read_text()
    obj = desired(site, "destinations", request_for())
    created = plan(site, "destinations", text, (obj.id, obj)).text
    edited = edit_entry(
        created, "pulsar_alice01_byoc_tpv", "    max_accepted_cores: 8\n", "    max_accepted_cores:\n      weird: 1\n"
    )
    result = plan(site, "destinations", edited, (obj.id, obj))
    assert result.text == edited and result.conflicts and "max_accepted_cores" in result.conflicts[0].reason


def test_delete_restores_the_original_file(site, eu_fixtures):
    for key, path in FILES.items():
        text = (eu_fixtures / path).read_text()
        obj = desired(site, key, request_for())
        created = plan(site, key, text, (obj.id, obj)).text
        result = plan(site, key, created, (obj.id, removal(site, key, request_for())))
        assert result.text == text, key
        assert [c.action for c in result.changes] == ["delete"]


def test_delete_of_hand_edited_entry_is_a_conflict(site, eu_fixtures):
    text = (eu_fixtures / FILES["mq"]).read_text()
    obj = desired(site, "mq", request_for())
    created = plan(site, "mq", text, (obj.id, obj)).text
    edited = created.replace(
        "    vhost: /pulsar/galaxy_alice01_byoc\n", "    vhost: /pulsar/galaxy_alice01_byoc\n    tags: monitoring\n"
    )
    result = plan(site, "mq", edited, (obj.id, removal(site, "mq", request_for())))
    assert result.text == edited and result.conflicts


def test_delete_of_missing_entry_is_a_noop(site, eu_fixtures):
    text = (eu_fixtures / FILES["mq"]).read_text()
    ghost = removal(site, "mq", request_for("ghost01"))
    result = plan(site, "mq", text, (ghost.id, ghost))
    assert result.text == text and not result.changes


def test_users_rule_joins_a_handwritten_entry_and_leaves_it_on_delete(site, eu_fixtures):
    text = (eu_fixtures / FILES["users"]).read_text()
    obj = desired(site, "users", request_for(), email="bjoern.gruening@gmail.com")
    created = plan(site, "users", text, (obj.id, obj)).text
    rules = Doc(created).data["users"]["bjoern.gruening@gmail.com"]["rules"]
    assert [r["id"] for r in rules] == ["user-specific-notebook", "byoc-pulsar-alice01"]
    assert all(line.startswith("+") for line in changed_lines(text, created))
    gone = removal(site, "users", request_for(), email="bjoern.gruening@gmail.com")
    result = plan(site, "users", created, (gone.id, gone))
    assert result.text == text and [c.action for c in result.changes] == ["delete"]


def test_new_users_entry_uses_the_plain_address_as_key(site, eu_fixtures):
    text = (eu_fixtures / FILES["users"]).read_text()
    obj = desired(site, "users", request_for(), email="o'neil@example.org")
    result = plan(site, "users", text, (obj.id, obj)).text
    assert changed_lines(text, result)[0] == "+  'o''neil@example.org':"
    assert "o'neil@example.org" in Doc(result).data["users"]


def test_switching_to_strict_mode_replaces_the_routing_keys(site, eu_fixtures):
    text = (eu_fixtures / FILES["users"]).read_text()
    obj = desired(site, "users", request_for())
    created = plan(site, "users", text, (obj.id, obj)).text
    strict = desired(site, "users", request_for(), strict=True)
    rule = Doc(plan(site, "users", created, (strict.id, strict)).text).data["users"][email_key("alice@example.org")][
        "rules"
    ][0]
    assert set(rule) == {"id", "if", "scheduling"} and set(rule["scheduling"]) == {"require"}
    back = plan(site, "users", plan(site, "users", created, (strict.id, strict)).text, (obj.id, obj)).text
    assert back == created


def test_owners_reads_only_generated_user_keys(site, eu_fixtures):
    text = (eu_fixtures / FILES["users"]).read_text()
    obj = desired(site, "users", request_for())
    created = plan(site, "users", text, (obj.id, obj)).text
    created += "  mallory@example.org:\n    rules:\n      - id: something-else\n        if: true\n"
    assert Target(site, "users").owners(created) == {"alice01": "alice@example.org"}


def test_owned_names_in_other_files(site, eu_fixtures):
    text = (eu_fixtures / FILES["job_conf"]).read_text()
    obj = desired(site, "job_conf", request_for())
    created = plan(site, "job_conf", text, (obj.id, obj)).text
    assert Target(site, "job_conf").names(created) == {"alice01"}
    assert Target(site, "job_conf").names(text) == set()


def test_untouched_lines_are_byte_identical_after_every_operation(site, eu_fixtures):
    text = (eu_fixtures / FILES["destinations"]).read_text()
    obj = desired(site, "destinations", request_for(nfs_path="/mnt/x"))
    created = plan(site, "destinations", text, (obj.id, obj)).text
    assert all(line.startswith("+") for line in changed_lines(text, created))


def test_missing_collection_in_file_raises(site):
    from byoc_sync.yamledit.doc import LocateError

    obj = desired(site, "mq", request_for())
    with pytest.raises(LocateError, match="rabbitmq_users"):
        plan(site, "mq", "other: 1\n", (obj.id, obj))


def test_object_without_desired_state_or_removal_is_a_programming_error(site, eu_fixtures):
    with pytest.raises(TypeError):
        plan(site, "mq", (eu_fixtures / FILES["mq"]).read_text(), ("galaxy_x_byoc", None))


def test_several_new_entries_in_one_plan_keep_their_order_and_spacing(site, eu_fixtures):
    text = (eu_fixtures / FILES["destinations"]).read_text()
    a = desired(site, "destinations", request_for("alice01"))
    b = desired(site, "destinations", request_for("bob01"), email="bob@example.org")
    lines = plan(site, "destinations", text, (a.id, a), (b.id, b)).text.splitlines()
    i, j = lines.index("  pulsar_alice01_byoc_tpv:"), lines.index("  pulsar_bob01_byoc_tpv:")
    assert i < j and lines[j - 1] == ""


def test_crlf_files_stay_crlf(site, eu_fixtures):
    text = (eu_fixtures / FILES["mq"]).read_text().replace("\n", "\r\n")
    obj = desired(site, "mq", request_for())
    out = plan(site, "mq", text, (obj.id, obj)).text
    assert out.count("\r\n") == out.count("\n") and "galaxy_alice01_byoc" in out


def test_comments_between_list_items_are_kept(site, eu_fixtures):
    text = (eu_fixtures / FILES["job_conf"]).read_text()
    obj = desired(site, "job_conf", request_for())
    out = plan(site, "job_conf", text, (obj.id, obj)).text
    assert "    #- id: drmaa" in out and out.count("#") == text.count("#")


def test_removed_owner_guard_rule_is_restored_on_update(site, eu_fixtures):
    text = (eu_fixtures / FILES["destinations"]).read_text()
    obj = desired(site, "destinations", request_for())
    created = plan(site, "destinations", text, (obj.id, obj)).text
    # An admin replaced our guard rule with a rule of their own.
    span = Doc(created).span_of(["destinations", "pulsar_alice01_byoc_tpv", "rules", 0])
    lines = created.split("\n")
    lines[span.start : span.end] = ["      - id: admin-note", "        if: false"]
    result = plan(site, "destinations", "\n".join(lines), (obj.id, obj))
    rules = Doc(result.text).data["destinations"]["pulsar_alice01_byoc_tpv"]["rules"]
    assert [r["id"] for r in rules] == ["admin-note", "byoc-owner-guard"]
    assert result.changes[0].fields == ["rules[byoc-owner-guard]"]


def test_clearing_nfs_keeps_hand_added_params(site, eu_fixtures):
    text = (eu_fixtures / FILES["destinations"]).read_text()
    obj = desired(site, "destinations", request_for(nfs_path="/mnt/share"))
    created = plan(site, "destinations", text, (obj.id, obj)).text
    edited = created.replace(
        '      persistence_directory: "/mnt/share/persisted_data"\n',
        '      persistence_directory: "/mnt/share/persisted_data"\n      submit_request_cpus: 2\n',
    )
    base = desired(site, "destinations", request_for())
    entry = Doc(plan(site, "destinations", edited, (base.id, base)).text).data["destinations"][
        "pulsar_alice01_byoc_tpv"
    ]
    assert entry["params"] == {"submit_request_cpus": 2} and "env" not in entry


@pytest.mark.parametrize(
    "edit",
    [
        ("    max_accepted_cores: 8\n", "    max_accepted_cores: 8\n    env: {X: y}\n"),
    ],
)
def test_flow_style_inside_a_managed_entry_is_a_conflict_not_a_crash(site, eu_fixtures, edit):
    text = (eu_fixtures / FILES["destinations"]).read_text()
    obj = desired(site, "destinations", request_for())
    created = plan(site, "destinations", text, (obj.id, obj)).text
    old, new = edit
    edited = edit_entry(created, "pulsar_alice01_byoc_tpv", old, new)
    changed = desired(site, "destinations", request_for(nfs_path="/mnt/x", max_cores=9))
    result = plan(site, "destinations", edited, (changed.id, changed))
    assert result.text == edited and result.conflicts


def test_tagged_values_elsewhere_in_the_file_do_not_break_verification(site, eu_fixtures):
    text = (eu_fixtures / FILES["mq"]).read_text()
    text = text.replace(
        "rabbitmq_users:\n",
        "rabbitmq_admin_password: !vault |\n  $ANSIBLE_VAULT;1.1;AES256\n  3132\nrabbitmq_users:\n",
        1,
    )
    obj = desired(site, "mq", request_for())
    result = plan(site, "mq", text, (obj.id, obj))
    assert "galaxy_alice01_byoc" in result.text and "!vault |" in result.text


def test_mixed_line_endings_are_refused(site, eu_fixtures):
    from byoc_sync.yamledit.edit import EditError

    text = (eu_fixtures / FILES["mq"]).read_text()
    text = text.replace("\n", "\r\n", 3)
    obj = desired(site, "mq", request_for())
    with pytest.raises(EditError, match="mixed line endings"):
        plan(site, "mq", text, (obj.id, obj))


def test_flow_style_part_that_needs_no_change_does_not_block_other_updates(site, eu_fixtures):
    text = (eu_fixtures / FILES["destinations"]).read_text()
    obj = desired(site, "destinations", request_for())
    created = plan(site, "destinations", text, (obj.id, obj)).text
    edited = edit_entry(
        created,
        "pulsar_alice01_byoc_tpv",
        "    scheduling:\n      require:\n        - alice01-byoc-pulsar\n",
        "    scheduling: {require: [alice01-byoc-pulsar]}\n",
    )
    changed = desired(site, "destinations", request_for(max_cores=9))
    result = plan(site, "destinations", edited, (changed.id, changed))
    assert "    scheduling: {require: [alice01-byoc-pulsar]}" in result.text and not result.conflicts
    assert Doc(result.text).data["destinations"]["pulsar_alice01_byoc_tpv"]["max_accepted_cores"] == 9


def test_users_file_without_final_newline_still_accepts_new_entries(site, eu_fixtures):
    text = (eu_fixtures / FILES["users"]).read_text().rstrip("\n")
    obj = desired(site, "users", request_for())
    result = plan(site, "users", text, (obj.id, obj))
    assert not result.conflicts and [c.action for c in result.changes] == ["create"]
