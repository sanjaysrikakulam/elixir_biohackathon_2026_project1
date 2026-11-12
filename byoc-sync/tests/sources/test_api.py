import json

import pytest

from byoc_sync.sources.api import ApiSource, GalaxyApiError
from tests.fake_galaxy import API_KEY, FakeGalaxy, FakeGalaxyServer, FakeUser

PW = "MyPulS4rP4ssw0rd"
BYOC = {"byoc_pulsar|username": "alice01", "byoc_pulsar|password": PW, "byoc_pulsar|max_accepted_cores": "8"}


@pytest.fixture
def galaxy():
    g = FakeGalaxy()
    g.add(
        FakeUser("a1", "alice@example.org", "alice", {**BYOC, "omero_account|password": "omero-secret"}),
        FakeUser("b2", "bob@example.org", "bob", {"localization|locale": "de"}),
        FakeUser("c3", "carol@example.org", "carol", None),
    )
    return g


def source(server, **kw):
    kw.setdefault("retry_backoff", 0)
    return ApiSource(server.url, API_KEY, **kw)


def test_scan_returns_only_users_with_the_section(galaxy):
    with FakeGalaxyServer(galaxy) as server:
        result = source(server).scan()
    assert [r.email for r in result.records] == ["alice@example.org"]
    assert result.complete and not result.errors
    assert result.galaxy_version == "26.1"


def test_scan_keeps_only_section_keys(galaxy):
    with FakeGalaxyServer(galaxy) as server:
        rec = source(server).scan().records[0]
    assert rec.prefs == BYOC and rec.galaxy_id == "a1" and rec.source == "api"


def test_scan_covers_every_account(galaxy):
    galaxy.add(
        *(
            FakeUser(f"x{i}", f"x{i:02d}@example.org", f"x{i}", {**BYOC, "byoc_pulsar|username": f"xuser{i:02d}"})
            for i in range(7)
        )
    )
    with FakeGalaxyServer(galaxy) as server:
        result = source(server).scan()
    assert len(result.records) == 8
    details = [
        r
        for r in galaxy.requests
        if r.startswith("/api/users/") and r not in ("/api/users/current", "/api/users/deleted")
    ]
    assert len(details) == 10  # every account, with or without BYOC data


def test_deleted_and_purged_users_are_scanned(galaxy):
    galaxy.add(FakeUser("d4", "dave@example.org", "dave", BYOC, deleted=True, purged=True))
    with FakeGalaxyServer(galaxy) as server:
        recs = {r.email: r for r in source(server).scan().records}
    assert recs["dave@example.org"].deleted and recs["dave@example.org"].purged


def test_accounts_listed_twice_are_fetched_once(galaxy):
    # An account can show up in both the active and the deleted list if it is deleted mid-scan.
    galaxy.add(FakeUser("a1", "alice@example.org", "alice", BYOC, deleted=True))
    with FakeGalaxyServer(galaxy) as server:
        result = source(server).scan()
    assert [r.email for r in result.records] == ["alice@example.org"]


def test_transient_errors_are_retried(galaxy):
    galaxy.flaky["a1"] = [429, 503, 502]
    with FakeGalaxyServer(galaxy) as server:
        result = source(server, retries=3).scan()
    assert result.complete and [r.email for r in result.records] == ["alice@example.org"]


def test_persistent_failures_count_against_the_error_budget(galaxy):
    galaxy.always_fail.add("b2")
    with FakeGalaxyServer(galaxy) as server:
        result = source(server, retries=1, max_error_rate=0.0).scan()
    assert not result.complete
    assert any("b2" in e for e in result.errors)


def test_failures_within_the_budget_keep_the_scan_complete(galaxy):
    galaxy.always_fail.add("b2")
    with FakeGalaxyServer(galaxy) as server:
        result = source(server, retries=0, max_error_rate=0.5).scan()
    assert result.complete and result.errors


def test_html_page_with_status_200_is_an_error(galaxy):
    galaxy.html_for.add("a1")
    with FakeGalaxyServer(galaxy) as server:
        result = source(server, retries=0, max_error_rate=0.0).scan()
    assert not result.complete and any("a1" in e for e in result.errors)


def test_malformed_preferences_json_is_reported_not_dropped(galaxy):
    galaxy.add(FakeUser("m5", "mal@example.org", "mal", "{not json byoc_pulsar|"))
    with FakeGalaxyServer(galaxy) as server:
        result = source(server).scan()
    assert any("mal@example.org" in e for e in result.errors)
    assert "mal@example.org" in result.unreadable


def test_non_object_preferences_json_is_reported(galaxy):
    galaxy.add(FakeUser("m6", "list@example.org", "lst", json.dumps(["byoc_pulsar|username"])))
    with FakeGalaxyServer(galaxy) as server:
        result = source(server).scan()
    assert "list@example.org" in result.unreadable


def test_galaxy_26_2_marks_passwords_unreadable(galaxy):
    galaxy.version = "26.2"
    with FakeGalaxyServer(galaxy) as server:
        rec = source(server).scan().records[0]
    assert rec.password_unreadable


@pytest.mark.parametrize("value", [{"is_set": True}, "__SECRET_PLACEHOLDER__"])
def test_redacted_password_values_mark_the_record_unreadable(galaxy, value):
    galaxy.users[0].prefs = {**BYOC, "byoc_pulsar|password": value}
    with FakeGalaxyServer(galaxy) as server:
        rec = source(server).scan().records[0]
    assert rec.password_unreadable


def test_lookup_finds_exact_email_only(galaxy):
    galaxy.add(FakeUser("e7", "alice@example.org.evil.example", "evil", BYOC))
    with FakeGalaxyServer(galaxy) as server:
        rec = source(server).lookup("alice@example.org")
    assert rec.galaxy_id == "a1" and rec.prefs == BYOC


def test_lookup_finds_deleted_users(galaxy):
    galaxy.add(FakeUser("d4", "dave@example.org", "dave", BYOC, deleted=True))
    with FakeGalaxyServer(galaxy) as server:
        rec = source(server).lookup("dave@example.org")
    assert rec.deleted


def test_lookup_of_unknown_email_is_none(galaxy):
    with FakeGalaxyServer(galaxy) as server:
        assert source(server).lookup("nobody@example.org") is None


def test_lookup_of_user_without_section_returns_record_with_empty_prefs(galaxy):
    with FakeGalaxyServer(galaxy) as server:
        rec = source(server).lookup("bob@example.org")
    assert rec.prefs == {}


def test_api_key_is_sent_as_header_never_in_the_url(galaxy):
    with FakeGalaxyServer(galaxy) as server:
        source(server).scan()
    assert all(API_KEY not in r for r in galaxy.requests)
    assert set(galaxy.seen_keys) == {API_KEY}


def test_wrong_api_key_is_fatal(galaxy):
    with FakeGalaxyServer(galaxy) as server:
        with pytest.raises(GalaxyApiError, match="403"):
            ApiSource(server.url, "wrong", retry_backoff=0).scan()


def test_errors_never_contain_the_api_key_or_passwords(galaxy):
    galaxy.always_fail.add("a1")
    with FakeGalaxyServer(galaxy) as server:
        result = source(server, retries=0).scan()
    text = " ".join(result.errors)
    assert API_KEY not in text and PW not in text


def test_unreachable_galaxy_is_fatal():
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]  # closed again before the request: connection refused
    with pytest.raises(GalaxyApiError, match="connection failed"):
        ApiSource(f"http://127.0.0.1:{port}", API_KEY, retries=1, retry_backoff=0).scan()


def test_unparseable_version_is_treated_as_not_redacting(galaxy):
    galaxy.version = "dev-build"
    with FakeGalaxyServer(galaxy) as server:
        rec = source(server).scan().records[0]
    assert not rec.password_unreadable


def test_lookup_of_user_with_malformed_preferences_raises(galaxy):
    galaxy.users[0].prefs = "{broken"
    with FakeGalaxyServer(galaxy) as server:
        with pytest.raises(GalaxyApiError, match="not valid JSON"):
            source(server).lookup("alice@example.org")


def test_non_admin_key_is_refused(galaxy):
    from tests.fake_galaxy import USER_KEY

    with FakeGalaxyServer(galaxy) as server:
        with pytest.raises(GalaxyApiError, match="admin"):
            ApiSource(server.url, USER_KEY, retry_backoff=0).scan()
        with pytest.raises(GalaxyApiError, match="admin"):
            ApiSource(server.url, USER_KEY, retry_backoff=0).lookup("alice@example.org")


def test_failed_users_are_named_and_never_look_like_removals(galaxy):
    galaxy.always_fail.add("a1")
    with FakeGalaxyServer(galaxy) as server:
        result = source(server, retries=0, max_error_rate=1.0).scan()
    assert "alice@example.org" in result.unreadable
    assert any("alice@example.org" in e for e in result.errors)


def test_redirects_are_not_followed(galaxy):
    galaxy.redirect_for.add("a1")
    with FakeGalaxyServer(galaxy) as server:
        result = source(server, retries=0, max_error_rate=1.0).scan()
    assert "alice@example.org" in result.unreadable
    assert any("302" in e for e in result.errors)
    assert not any("steal" in r for r in galaxy.requests)


def test_plain_http_is_refused_for_remote_hosts():
    with pytest.raises(ValueError, match="https"):
        ApiSource("http://galaxy.example.org", API_KEY)
    ApiSource("http://localhost:8080", API_KEY)
    ApiSource("https://galaxy.example.org", API_KEY)


def test_retries_are_bounded(galaxy):
    galaxy.flaky["a1"] = [503, 503, 503]
    with FakeGalaxyServer(galaxy) as server:
        result = source(server, retries=1, max_error_rate=1.0).scan()
    assert "alice@example.org" in result.unreadable
    assert sum(r.startswith("/api/users/a1") for r in galaxy.requests) == 2


def test_activation_state_comes_from_the_user_list(galaxy):
    # Galaxy's detailed user view has no "active" field; only the list view does.
    galaxy.users[0].active = False
    with FakeGalaxyServer(galaxy) as server:
        assert not source(server).scan().records[0].active
        assert not source(server).lookup("alice@example.org").active


def test_a_slow_user_counts_as_a_failure_instead_of_aborting_the_scan(galaxy):
    galaxy.slow["b2"] = 1.0
    with FakeGalaxyServer(galaxy) as server:
        result = source(server, timeout=0.2, retries=0, max_error_rate=1.0).scan()
    assert "bob@example.org" in result.unreadable
    assert [r.email for r in result.records] == ["alice@example.org"]
