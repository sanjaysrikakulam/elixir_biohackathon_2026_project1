import json
import stat
import textwrap

import pytest

from byoc_sync.sources.gxadmin import BYOC_QUERY_SQL, GxadminError, GxadminSource, lookup_sql

PW = "MyPulS4rP4ssw0rd"


def fake_gxadmin(tmp_path, rows, exit_code=0, stderr=""):
    """An executable that records its arguments and prints ``rows`` as gxadmin rawquery would."""
    payload = tmp_path / "payload.json"
    payload.write_text(json.dumps(rows))
    args_file = tmp_path / "args.json"
    script = tmp_path / "gxadmin"
    script.write_text(
        textwrap.dedent(f"""\
        #!/usr/bin/env python3
        import json, sys
        json.dump(sys.argv[1:], open({str(args_file)!r}, "w"))
        sys.stderr.write({stderr!r})
        if {exit_code}:
            sys.exit({exit_code})
        sys.stdout.write(open({str(payload)!r}).read())
    """)
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return [str(script)], args_file


def row(id_, email, prefs, **kw):
    return {
        "id": id_,
        "email": email,
        "username": email.split("@")[0],
        "deleted": False,
        "purged": False,
        "active": True,
        "extra_user_preferences": prefs if isinstance(prefs, str) else json.dumps(prefs),
        **kw,
    }


BYOC = {"byoc_pulsar|username": "alice01", "byoc_pulsar|password": PW, "omero_account|password": "x"}


def test_scan_runs_one_rawquery_and_builds_records(tmp_path):
    cmd, args = fake_gxadmin(tmp_path, [row(1, "alice@example.org", BYOC)])
    result = GxadminSource(cmd).scan()
    assert json.loads(args.read_text()) == ["rawquery", "q", BYOC_QUERY_SQL]
    (rec,) = result.records
    assert rec.galaxy_id == "1" and rec.email == "alice@example.org" and rec.source == "gxadmin"
    assert rec.prefs == {"byoc_pulsar|username": "alice01", "byoc_pulsar|password": PW}
    assert result.complete


def test_sql_takes_the_newest_preference_row_per_user_before_filtering():
    sql = " ".join(BYOC_QUERY_SQL.split())
    assert "DISTINCT ON (up.user_id)" in sql and "ORDER BY up.user_id, up.id DESC" in sql
    assert sql.index("DISTINCT ON") < sql.index("LIKE '%byoc_pulsar|%'")


def test_empty_result(tmp_path):
    cmd, _ = fake_gxadmin(tmp_path, [])
    assert GxadminSource(cmd).scan().records == []


def test_null_output_is_an_error(tmp_path):
    # The query always returns a JSON array (COALESCE(..., '[]')); null means something went wrong.
    cmd, _ = fake_gxadmin(tmp_path, None)
    with pytest.raises(GxadminError):
        GxadminSource(cmd).scan()


def test_malformed_preferences_are_reported_per_user(tmp_path):
    cmd, _ = fake_gxadmin(
        tmp_path, [row(1, "alice@example.org", BYOC), row(2, "mal@example.org", "{oops byoc_pulsar|")]
    )
    result = GxadminSource(cmd).scan()
    assert [r.email for r in result.records] == ["alice@example.org"]
    assert "mal@example.org" in result.unreadable


def test_non_zero_exit_is_fatal_with_stderr(tmp_path):
    cmd, _ = fake_gxadmin(tmp_path, [], exit_code=2, stderr="psql: connection refused")
    with pytest.raises(GxadminError, match="connection refused"):
        GxadminSource(cmd).scan()


def test_garbage_output_is_fatal(tmp_path):
    cmd = [str(tmp_path / "gx")]
    (tmp_path / "gx").write_text("#!/bin/sh\necho 'ERROR: relation does not exist'\n")
    (tmp_path / "gx").chmod(0o755)
    with pytest.raises(GxadminError):
        GxadminSource(cmd).scan()


def test_command_prefix_is_kept(tmp_path):
    cmd, args = fake_gxadmin(tmp_path, [])
    GxadminSource(["env", "PGDATABASE=galaxy", *cmd]).scan()
    assert json.loads(args.read_text())[:2] == ["rawquery", "q"]


def test_lookup_uses_exact_email_and_returns_record(tmp_path):
    cmd, args = fake_gxadmin(tmp_path, [row(3, "o'neil@example.org", {"other|x": 1}, deleted=True)])
    rec = GxadminSource(cmd).lookup("o'neil@example.org")
    sent = json.loads(args.read_text())[2]
    assert sent == lookup_sql("o'neil@example.org")
    assert "'o''neil@example.org'" in sent  # quote doubled, no injection
    assert rec.deleted and rec.prefs == {}


def test_lookup_unknown_email_is_none(tmp_path):
    cmd, _ = fake_gxadmin(tmp_path, [])
    assert GxadminSource(cmd).lookup("nobody@example.org") is None


def test_lookup_rejects_control_characters(tmp_path):
    cmd, _ = fake_gxadmin(tmp_path, [])
    with pytest.raises(GxadminError):
        GxadminSource(cmd).lookup("a@b.c\n; DROP TABLE x")


def test_missing_gxadmin_binary_is_fatal(tmp_path):
    with pytest.raises(GxadminError, match="could not run gxadmin"):
        GxadminSource([str(tmp_path / "does-not-exist")]).scan()


def test_json_that_is_not_a_list_is_fatal(tmp_path):
    cmd, _ = fake_gxadmin(tmp_path, {"unexpected": True})
    with pytest.raises(GxadminError, match="not a list"):
        GxadminSource(cmd).scan()


def test_empty_output_is_an_error_not_an_empty_result(tmp_path):
    # psql reading from stdin exits 0 on SQL errors and prints nothing to stdout.
    script = tmp_path / "gx"
    script.write_text("#!/bin/sh\necho 'ERROR:  column \"purged\" does not exist' >&2\n")
    script.chmod(0o755)
    with pytest.raises(GxadminError, match='column "purged" does not exist'):
        GxadminSource([str(script)]).scan()
    with pytest.raises(GxadminError):
        GxadminSource([str(script)]).lookup("alice@example.org")


def test_error_messages_never_include_query_output(tmp_path):
    script = tmp_path / "gx"
    script.write_text(f'#!/bin/sh\necho \'{{"email": "a@b.c", "pw": "{PW}"\'\n')
    script.chmod(0o755)
    with pytest.raises(GxadminError) as info:
        GxadminSource([str(script)]).scan()
    assert PW not in str(info.value) and "a@b.c" not in str(info.value)


def test_lookup_of_malformed_preferences_raises(tmp_path):
    cmd, _ = fake_gxadmin(tmp_path, [row(1, "alice@example.org", "{oops")])
    with pytest.raises(GxadminError, match="not valid JSON"):
        GxadminSource(cmd).lookup("alice@example.org")


def test_lookup_rejects_backslashes(tmp_path):
    cmd, _ = fake_gxadmin(tmp_path, [])
    with pytest.raises(GxadminError):
        GxadminSource(cmd).lookup("a\\b@example.org")


def test_section_drives_the_query(tmp_path):
    cmd, args = fake_gxadmin(tmp_path, [])
    GxadminSource(cmd, section="my_pulsar").scan()
    assert "LIKE '%my_pulsar|%'" in json.loads(args.read_text())[2]
