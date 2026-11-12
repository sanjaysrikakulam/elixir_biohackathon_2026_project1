"""The role's validation step: it fails before any change, naming every missing value at once."""

from __future__ import annotations

import pytest

from .conftest import good_vars

MISSING = "byoc_sync: set these variables (they are missing, empty or 'changeme'): "


def test_good_values_pass(run_role, tmp_path):
    proc = run_role("validate", good_vars(tmp_path))
    assert proc.returncode == 0, proc.stdout


def test_defaults_fail_listing_every_missing_variable(run_role, tmp_path):
    proc = run_role("validate", {"byoc_sync_user": good_vars(tmp_path)["byoc_sync_user"]})
    assert proc.returncode != 0
    assert (
        MISSING + "byoc_sync_group, byoc_sync_repo_url, byoc_sync_venv, byoc_sync_reports_dir, byoc_sync_vault_password"
        in proc.stdout
    )


@pytest.mark.parametrize("value", ["", "  ", "changeme"])
def test_empty_and_whitespace_count_as_missing(run_role, tmp_path, value):
    proc = run_role("validate", good_vars(tmp_path, byoc_sync_venv=value))
    assert proc.returncode != 0
    assert MISSING + 'byoc_sync_venv"' in proc.stdout


def test_missing_user_fails(run_role, tmp_path):
    proc = run_role("validate", good_vars(tmp_path, byoc_sync_user="no-such-user-byoc"))
    assert proc.returncode != 0
    assert "system user 'no-such-user-byoc' does not exist on localhost; create it first" in proc.stdout


@pytest.mark.parametrize("value", ["", "  ", "changeme", "ChangeMe", None])
def test_empty_or_changeme_group_counts_as_missing(run_role, tmp_path, value):
    proc = run_role("validate", good_vars(tmp_path, byoc_sync_group=value))
    assert proc.returncode != 0
    assert MISSING + 'byoc_sync_group"' in proc.stdout


def test_group_must_be_text(run_role, tmp_path):
    proc = run_role("validate", good_vars(tmp_path, byoc_sync_group=123))
    assert proc.returncode != 0
    assert "byoc_sync_group must be text; quote it in YAML" in proc.stdout


def test_missing_group_fails(run_role, tmp_path):
    proc = run_role("validate", good_vars(tmp_path, byoc_sync_group="no-such-group-byoc"))
    assert proc.returncode != 0
    assert "system group 'no-such-group-byoc' does not exist on localhost; create it first" in proc.stdout


@pytest.mark.parametrize("var", ["byoc_sync_user", "byoc_sync_group"])
@pytest.mark.parametrize("value", ["1000", "0"])
def test_user_and_group_must_be_names_not_numbers(run_role, tmp_path, var, value):
    proc = run_role("validate", good_vars(tmp_path, **{var: value}))
    assert proc.returncode != 0
    assert f"{var} must be a name, not a number ({value})" in proc.stdout


def test_cron_on_needs_run_values(run_role, tmp_path):
    proc = run_role("validate", good_vars(tmp_path, byoc_sync_cron_enabled=True))
    assert proc.returncode != 0
    assert MISSING + "byoc_sync_playbook_repo_url, byoc_sync_galaxy_url, byoc_sync_galaxy_api_key" in proc.stdout
    assert "byoc_sync_github_token" not in proc.stdout


def test_cron_on_without_dry_run_needs_github(run_role, tmp_path):
    values = good_vars(
        tmp_path,
        byoc_sync_cron_enabled=True,
        byoc_sync_dry_run=False,
        byoc_sync_playbook_repo_url="https://github.com/me/playbook.git",
        byoc_sync_galaxy_url="https://galaxy.example.org",
        byoc_sync_galaxy_api_key="key-123",
    )
    proc = run_role("validate", values)
    assert proc.returncode != 0
    assert MISSING + "byoc_sync_github_repo, byoc_sync_github_token" in proc.stdout


def test_gxadmin_does_not_need_galaxy_api_values(run_role, tmp_path):
    values = good_vars(
        tmp_path,
        byoc_sync_cron_enabled=True,
        byoc_sync_source="gxadmin",
        byoc_sync_playbook_repo_url="https://github.com/me/playbook.git",
    )
    proc = run_role("validate", values)
    assert proc.returncode == 0, proc.stdout


def test_unknown_source_fails(run_role, tmp_path):
    proc = run_role("validate", good_vars(tmp_path, byoc_sync_source="ldap"))
    assert proc.returncode != 0
    assert "byoc_sync_source must be api or gxadmin, got 'ldap'" in proc.stdout


def test_missing_site_config_file_fails(run_role, tmp_path):
    proc = run_role("validate", good_vars(tmp_path, byoc_sync_site_config="/nonexistent/site.yml"))
    assert proc.returncode != 0
    assert "byoc_sync_site_config: /nonexistent/site.yml does not exist on localhost" in proc.stdout


def test_bundled_site_name_is_not_checked_before_install(run_role, tmp_path):
    proc = run_role("validate", good_vars(tmp_path, byoc_sync_site_config="othersite"))
    assert proc.returncode == 0, proc.stdout


def test_secrets_never_printed(run_role, tmp_path):
    values = good_vars(
        tmp_path,
        byoc_sync_user="no-such-user-byoc",
        byoc_sync_galaxy_api_key="api-SECRET-1",
        byoc_sync_github_token="ghp-SECRET-2",
        byoc_sync_fingerprint_key="fp-SECRET-3",
    )
    proc = run_role("validate", values)
    assert proc.returncode != 0
    out = proc.stdout + proc.stderr
    for secret in ("vault-secret-123", "api-SECRET-1", "ghp-SECRET-2", "fp-SECRET-3"):
        assert secret not in out


@pytest.mark.parametrize(
    "key",
    [
        "BAD KEY",
        "X=1; rm -rf ~; #",
        "$(id)",
        "1ABC",
        "PATH",
        "IFS",
        "HOME",
        "ENV",
        "BASH_ENV",
        "LD_PRELOAD",
        "PYTHONPATH",
        "GALAXY_API_KEY",
        "GITHUB_TOKEN",
        "BYOC_SYNC_FINGERPRINT_KEY",
        "BYOC_SYNC_VAULT_PASSWORD",
    ],
)
def test_unsafe_extra_env_names_are_rejected(run_role, tmp_path, key):
    proc = run_role("validate", good_vars(tmp_path, byoc_sync_extra_env={key: "x"}))
    assert proc.returncode != 0
    assert f"byoc_sync_extra_env: {key!r} cannot be used as an environment variable name" in proc.stdout


def test_plain_extra_env_names_are_accepted(run_role, tmp_path):
    proc = run_role("validate", good_vars(tmp_path, byoc_sync_extra_env={"PGHOST": "db", "_X1": "y", "PGPORT": 5432}))
    assert proc.returncode == 0, proc.stdout


@pytest.mark.parametrize("password", [" lead", "trail ", "trail\n"])
def test_vault_password_with_outer_whitespace_is_rejected(run_role, tmp_path, password):
    proc = run_role("validate", good_vars(tmp_path, byoc_sync_vault_password=password))
    assert proc.returncode != 0
    assert "byoc_sync_vault_password must not start or end with whitespace (byoc-sync strips it)" in proc.stdout
    assert password.strip() not in proc.stdout


@pytest.mark.parametrize("var", ["byoc_sync_reports_dir", "byoc_sync_wrapper_path"])
def test_percent_in_cron_paths_is_rejected(run_role, tmp_path, var):
    values = good_vars(
        tmp_path,
        byoc_sync_cron_enabled=True,
        byoc_sync_source="gxadmin",
        byoc_sync_playbook_repo_url="https://github.com/me/playbook.git",
        **{var: str(tmp_path / "a%b")},
    )
    proc = run_role("validate", values)
    assert proc.returncode != 0
    assert f"{var} must not contain '%' when cron is on (cron treats it as a newline)" in proc.stdout


@pytest.mark.parametrize("value", ["ChangeMe", "'changeme'", '"changeme"', " CHANGEME ", None])
def test_changeme_variants_and_null_count_as_missing(run_role, tmp_path, value):
    proc = run_role("validate", good_vars(tmp_path, byoc_sync_repo_url=value))
    assert proc.returncode != 0
    assert MISSING + 'byoc_sync_repo_url"' in proc.stdout


@pytest.mark.parametrize("value", [123, True, 1.5, ["a"]])
def test_required_values_must_be_text(run_role, tmp_path, value):
    proc = run_role("validate", good_vars(tmp_path, byoc_sync_vault_password=value))
    assert proc.returncode != 0
    assert "byoc_sync_vault_password must be text; quote it in YAML" in proc.stdout


@pytest.mark.parametrize(
    "var",
    [
        "byoc_sync_dry_run",
        "byoc_sync_allow_delete",
        "byoc_sync_cron_enabled",
        "byoc_sync_manage_packages",
        "byoc_sync_install_tpv",
    ],
)
@pytest.mark.parametrize("value", [None, "ture", "true ", 2, "on"])
def test_switches_must_be_true_or_false(run_role, tmp_path, var, value):
    proc = run_role("validate", good_vars(tmp_path, **{var: value}))
    assert proc.returncode != 0
    assert f"{var} must be true or false" in proc.stdout


@pytest.mark.parametrize("value", [True, False, "true", "false", "yes", "no", "True"])
def test_switch_spellings_that_are_accepted(run_role, tmp_path, value):
    proc = run_role("validate", good_vars(tmp_path, byoc_sync_dry_run=value))
    assert proc.returncode == 0, proc.stdout


@pytest.mark.parametrize("value", [None, True, [1, 2], {"a": 1}])
def test_extra_env_values_must_be_text_or_numbers(run_role, tmp_path, value):
    proc = run_role("validate", good_vars(tmp_path, byoc_sync_extra_env={"PGHOST": value}))
    assert proc.returncode != 0
    assert "byoc_sync_extra_env: the value of 'PGHOST' must be text or a number" in proc.stdout


@pytest.mark.parametrize("var", ["byoc_sync_repo_url", "byoc_sync_playbook_repo_url"])
def test_urls_with_a_password_are_rejected(run_role, tmp_path, var):
    proc = run_role("validate", good_vars(tmp_path, **{var: "https://me:ghp-SECRET@github.com/me/x.git"}))
    assert proc.returncode != 0
    assert f"{var} must not contain a password (it would be visible in the wrapper and in ps)" in proc.stdout
    assert "ghp-SECRET" not in proc.stdout


@pytest.mark.parametrize("url", ["git@github.com:me/x.git", "ssh://git@github.com/me/x.git", "file:///srv/x"])
def test_urls_without_a_password_are_accepted(run_role, tmp_path, url):
    proc = run_role("validate", good_vars(tmp_path, byoc_sync_repo_url=url, byoc_sync_playbook_repo_url=url))
    assert proc.returncode == 0, proc.stdout


@pytest.mark.parametrize(
    "var",
    [
        "byoc_sync_venv",
        "byoc_sync_reports_dir",
        "byoc_sync_config_dir",
        "byoc_sync_wrapper_path",
        "byoc_sync_src_dir",
        "byoc_sync_work_dir",
    ],
)
def test_paths_must_be_absolute(run_role, tmp_path, var):
    proc = run_role("validate", good_vars(tmp_path, **{var: "relative/path"}))
    assert proc.returncode != 0
    assert f"{var} must be an absolute path, got 'relative/path'" in proc.stdout


@pytest.mark.parametrize("src", ["venv", "venv/src", "."])
def test_venv_and_source_must_be_separate(run_role, tmp_path, src):
    values = good_vars(tmp_path)
    values["byoc_sync_src_dir"] = (
        str((tmp_path / src).resolve()) if src != "venv/src" else values["byoc_sync_venv"] + "/src"
    )
    proc = run_role("validate", values)
    assert proc.returncode != 0
    assert "byoc_sync_venv and byoc_sync_src_dir must be separate folders, neither inside the other" in proc.stdout


def test_relative_site_config_file_is_rejected(run_role, tmp_path):
    proc = run_role("validate", good_vars(tmp_path, byoc_sync_site_config="site.yml"))
    assert proc.returncode != 0
    assert (
        "byoc_sync_site_config: site.yml must be an absolute path "
        "(a value with a file suffix is read as a file)" in proc.stdout
    )


@pytest.mark.parametrize(
    "var,value",
    [
        ("byoc_sync_fingerprint_key", 123),
        ("byoc_sync_galaxy_api_key", True),
        ("byoc_sync_github_token", 4.5),
        ("byoc_sync_gxadmin_cmd", 7),
    ],
)
def test_optional_text_values_must_be_text_too(run_role, tmp_path, var, value):
    proc = run_role("validate", good_vars(tmp_path, **{var: value}))
    assert proc.returncode != 0
    assert f"{var} must be text; quote it in YAML" in proc.stdout


def test_url_with_a_token_as_user_name_is_rejected(run_role, tmp_path):
    proc = run_role(
        "validate", good_vars(tmp_path, byoc_sync_playbook_repo_url="https://ghp_SECRET@github.com/a/b.git")
    )
    assert proc.returncode != 0
    assert "byoc_sync_playbook_repo_url must not contain a password" in proc.stdout
    assert "ghp_SECRET" not in proc.stdout
