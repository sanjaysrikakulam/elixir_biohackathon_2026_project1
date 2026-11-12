import pytest

from byoc_sync.github import GitHub, GitHubError
from tests.fake_github import TOKEN, FakeGitHub, FakeGitHubServer, FakePR


@pytest.fixture
def gh():
    return FakeGitHub()


def client(server, token=TOKEN):
    return GitHub("owner/playbook", token, api_url=server.url, pace_requests=False)


def test_create_find_update_comment_and_close(gh):
    with FakeGitHubServer(gh) as server:
        api = client(server)
        pr = api.create_pr("alice01_byoc_ci", "master", "title", "body")
        assert pr.number == 1 and pr.is_open
        assert [p.number for p in api.prs_for_branch("alice01_byoc_ci")] == [1]
        api.update_pr(1, title="new title", body="new body")
        api.comment(1, "closing")
        api.close_pr(1)
    assert gh.prs[0].title == "new title" and gh.prs[0].comments == ["closing"] and gh.prs[0].state == "closed"


def test_newest_pr_comes_first(gh):
    gh.prs = [FakePR(1, "b", "master", "t", "x", state="closed"), FakePR(2, "b", "master", "t", "y")]
    with FakeGitHubServer(gh) as server:
        assert [p.number for p in client(server).prs_for_branch("b")] == [2, 1]


def test_pull_request_history_is_paged(gh):
    gh.prs = [FakePR(i, "alice01_byoc_ci", "master", "t", "", state="closed") for i in range(1, 131)]
    with FakeGitHubServer(gh) as server:
        assert len(client(server).prs_for_branch("alice01_byoc_ci")) == 130


def test_merged_and_rejected_states(gh):
    gh.prs = [
        FakePR(1, "a", "master", "t", "", state="closed", merged_at="2026-11-01T00:00:00Z"),
        FakePR(2, "b", "master", "t", "", state="closed"),
    ]
    with FakeGitHubServer(gh) as server:
        api = client(server)
        (merged,) = api.prs_for_branch("a")
        (rejected,) = api.prs_for_branch("b")
    assert merged.merged and not merged.rejected and rejected.rejected


def test_bad_token_raises_without_leaking_it(gh):
    with FakeGitHubServer(gh) as server:
        with pytest.raises(GitHubError) as info:
            client(server, token="ghp_wrong_secret").prs_for_branch("x")
    assert "401" in str(info.value) and "ghp_wrong_secret" not in str(info.value)


def test_token_is_sent_in_the_authorization_header(gh):
    with FakeGitHubServer(gh) as server:
        client(server).prs_for_branch("x")
    assert len(gh.auth) == 1 and TOKEN in next(iter(gh.auth))
    assert all(TOKEN not in path for _, path in gh.calls)


def test_repository_slug_is_validated():
    with pytest.raises(ValueError, match=r"must look like OWNER/REPO"):
        GitHub("not a slug", TOKEN)
