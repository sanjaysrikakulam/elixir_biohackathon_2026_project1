import dataclasses

import pytest
import yaml

from byoc_sync.config import ConfigError, SiteConfig
from byoc_sync.render import Renderer, email_key

from .conftest import request_for


def test_eu_site_config_loads(site):
    assert site.base_branch == "master"
    assert site.targets["mq"].path == ["rabbitmq_users"] and site.targets["mq"].field == "user"
    assert site.naming.runner_id == "pulsar_eu_{x}_byoc"
    assert site.vault_file == "secret_group_vars/pulsar.yml"


def test_site_config_from_a_file_path(tmp_path, site):
    text = (SiteConfig.builtin_path("eu")).read_text().replace("mq.galaxyproject.eu", "mq.example.org")
    path = tmp_path / "mine.yml"
    path.write_text(text)
    assert SiteConfig.load(str(path)).site["mq_host"] == "mq.example.org"


def test_missing_target_keys_are_reported(tmp_path):
    path = tmp_path / "bad.yml"
    path.write_text("section: byoc_pulsar\ntargets:\n  mq:\n    file: x.yml\n")
    with pytest.raises(ConfigError, match="mq.*path"):
        SiteConfig.load(str(path))


def test_unknown_site_is_reported():
    with pytest.raises(ConfigError, match="nope"):
        SiteConfig.load("nope")


def test_email_key_is_the_plain_address():
    assert email_key("alice.smith@uni-x.example.org") == "alice.smith@uni-x.example.org"


# TPV reads users keys as regular expressions: an address with regex operators would not match itself.
@pytest.mark.parametrize(
    "email",
    [
        "a\n@b.c",
        "",
        "x" * 300 + "@b.c",
        "no-at-sign",
        "alice+lab@x.org",
        "a?b@x.org",
        "a|b@x.org",
        "a^b@x.org",
        "a$b@x.org",
    ],
)
def test_unusable_emails_are_rejected(email):
    with pytest.raises(ValueError):
        email_key(email)


def test_render_never_receives_the_password(site, alice):
    req, names, email = alice
    out = Renderer(site).render("destinations", req, names, email)
    assert req.password not in out


def test_rendered_runner_matches_the_shape_of_existing_eu_runners(site, alice):
    req, names, email = alice
    assert Renderer(site).render("job_conf", req, names, email).splitlines() == [
        "- id: pulsar_eu_alice01_byoc",
        "  load: galaxy.jobs.runners.pulsar:PulsarMQJobRunner",
        "  params:",
        '    amqp_url: "pyamqp://galaxy_alice01_byoc:{{ rabbitmq_password_galaxy_alice01_byoc }}'
        '@mq.galaxyproject.eu:5671//pulsar/galaxy_alice01_byoc?ssl=1"',
        '    galaxy_url: "https://usegalaxy.eu"',
        "    manager: production",
        '    amqp_acknowledge: "true"',
        "    amqp_ack_republish_time: 300",
        "    amqp_consumer_timeout: 2.0",
        '    amqp_publish_retry: "true"',
        "    amqp_publish_retry_max_retries: 60",
    ]


def test_rendered_destination_quotes_the_owner_email_for_python_and_yaml(site):
    from byoc_sync.naming import Names
    from byoc_sync.yamledit.doc import Doc

    req = request_for()
    tricky = "o'neil@example.org"
    out = Renderer(site).render("destinations", req, Names.for_(req.name, site.naming), tricky)
    guard = Doc(out).data["pulsar_alice01_byoc_tpv"]["rules"][0]["if"]
    assert guard == 'user is None or user.email != "o\'neil@example.org"'
    assert eval(guard, {"user": type("U", (), {"email": tricky})()}) is False  # noqa: S307 - the owner passes


def test_universe_covers_every_optional_part(site, alice):
    req, names, email = alice
    destination = Renderer(site).universe("destinations", names, email)
    assert "SINGULARITY_CACHEDIR" in destination["env"] and "jobs_directory" in destination["params"]
    (rule,) = Renderer(site).universe("users", names, email)["rules"]
    assert "rank" in rule and {"prefer", "require"} <= set(rule["scheduling"])


@pytest.mark.parametrize(
    "mutate,message",
    [
        (lambda t: t + "\n  : [", "invalid YAML"),
        (lambda t: t.replace("vault:\n  file: secret_group_vars/pulsar.yml", "vault: {}"), "vault.file"),
        (lambda t: t.replace("templates: eu", "templates: no-such-dir"), "templates directory"),
        (lambda t: t.replace("naming:\n", "naming:\n  bogus: x\n"), "naming"),
        (lambda t: t.replace("  users:\n    file:", "  userz:\n    file:"), "targets.users"),
    ],
)
def test_broken_site_configs_are_reported(tmp_path, mutate, message):
    path = tmp_path / "site.yml"
    path.write_text(mutate(SiteConfig.builtin_path("eu").read_text()))
    with pytest.raises(ConfigError, match=message):
        SiteConfig.load(str(path))


def test_email_from_key_accepts_only_usable_addresses():
    from byoc_sync.render import email_from_key

    assert email_from_key("alice@example.org") == "alice@example.org"
    assert email_from_key(r"^alice@example\.org$") is None
    assert email_from_key("default") is None


def _eu_with_branch(tmp_path, branch):
    text = SiteConfig.builtin_path("eu").read_text().replace('branch: "{x}_byoc_ci"', f'branch: "{branch}"')
    path = tmp_path / "site.yml"
    path.write_text(text)
    return path


@pytest.mark.parametrize(
    "branch",
    [
        "byoc_ci",
        "master",
        "{{x}}_ci",
        "{x!r}_ci",
        "{x:>8}_ci",
        "{x:s}_ci",  # renders like {x}, but name_from could not reverse it
        "{x}_{y}",
        "{x",
        "{x}",  # no fixed part: a user name alone, such as "master", would be the branch
        "{x_dash}",
        "{x}{x_dash}",
        "byoc/{x}/{x_dash}",  # one field only
        "{x}_{x}",
        # fixed text git would refuse or that is easy to confuse
        "{x}.lock",
        "{x}..y",
        "{x} ",
        "{x}/",
        "/{x}",
        "{x}//ci",
        "{x}~",
        "x[{x}]",
        "-{x}",
        "{x}_CI",
        "{{a}}{x}_ci",
    ],
)
def test_branch_pattern_without_the_name_is_rejected(tmp_path, branch):
    with pytest.raises(ConfigError, match=r"naming\.branch.*\{x\}"):
        SiteConfig.load(str(_eu_with_branch(tmp_path, branch)))


@pytest.mark.parametrize("branch", ["byoc-{x_dash}", "byoc/{x}", "{x}_byoc_ci", "{x}-"])
def test_branch_pattern_naming_the_user_is_accepted(tmp_path, branch):
    assert SiteConfig.load(str(_eu_with_branch(tmp_path, branch))).naming.branch == branch


def test_site_config_without_naming_uses_the_default_branch_pattern(tmp_path):
    path = tmp_path / "site.yml"
    text = SiteConfig.builtin_path("eu").read_text()
    path.write_text(text[: text.index("naming:\n")] + text[text.index("targets:\n") :])
    assert SiteConfig.load(str(path)).naming.branch == "{x}_byoc_ci"


@pytest.mark.parametrize("base", ["alice01_byoc_ci", "byoc_ci_batch", "byoc_ci_batch/old"])
def test_base_branch_that_byoc_sync_could_write_is_rejected(tmp_path, base):
    text = SiteConfig.builtin_path("eu").read_text().replace("base_branch: master", f"base_branch: {base}")
    path = tmp_path / "site.yml"
    path.write_text(text)
    with pytest.raises(ConfigError, match=rf"base_branch {base!r}"):
        SiteConfig.load(str(path))


def test_base_branch_override_is_checked_too(site):
    with pytest.raises(ConfigError, match=r"base_branch 'bob01_byoc_ci'"):
        dataclasses.replace(site, base_branch="bob01_byoc_ci")


def test_base_branch_no_user_name_can_produce_is_accepted(site):
    # "ab" is too short to be a BYOC name, so no user can ever get the branch "ab_byoc_ci".
    assert dataclasses.replace(site, base_branch="ab_byoc_ci").base_branch == "ab_byoc_ci"


def test_branch_pattern_that_could_produce_the_single_pr_branch_is_rejected(tmp_path):
    # user name "byoc_ci" would render "{x}_batch" to "byoc_ci_batch", the --single-pr branch
    with pytest.raises(ConfigError, match="byoc_ci_batch"):
        SiteConfig.load(str(_eu_with_branch(tmp_path, "{x}_batch")))


def _eu_with_base(tmp_path, base, branch="{x}_byoc_ci"):
    path = _eu_with_branch(tmp_path, branch)
    path.write_text(path.read_text().replace("base_branch: master", f"base_branch: {base}"))
    return path


@pytest.mark.parametrize(
    "base,branch",
    [
        ("byoc", "byoc/{x}"),  # git cannot have refs/heads/byoc and refs/heads/byoc/alice01 at once
        ("alice01_byoc_ci/next", "{x}_byoc_ci"),
        ("byoc/alice01/x", "byoc/{x}"),
    ],
)
def test_base_branch_in_the_same_path_as_a_user_branch_is_rejected(tmp_path, base, branch):
    with pytest.raises(ConfigError, match=rf"base_branch {base!r}"):
        SiteConfig.load(str(_eu_with_base(tmp_path, base, branch)))


def test_single_pr_branch_in_the_same_path_as_a_user_branch_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="byoc_ci_batch"):
        SiteConfig.load(str(_eu_with_branch(tmp_path, "byoc_ci_batch/{x}")))


@pytest.mark.parametrize("base,branch", [("release/2026", "byoc/{x}"), ("main", "byoc/{x}"), ("byoc-old", "byoc/{x}")])
def test_base_branch_outside_the_user_branches_is_accepted(tmp_path, base, branch):
    site = SiteConfig.load(str(_eu_with_base(tmp_path, base, branch)))
    assert (site.base_branch, site.naming.branch) == (base, branch)


@pytest.mark.parametrize(
    "old,new,message",
    [
        ("base_branch: master", "base_branch: 2024", "base_branch"),
        ("base_branch: master", "base_branch:", "base_branch"),
        ("base_branch: master", 'base_branch: ""', "base_branch"),
        ('branch: "{x}_byoc_ci"', "branch: 5", "naming.branch"),
        ("mq_user: galaxy_{x}_byoc", "mq_user: [x]", "naming.mq_user"),
    ],
)
def test_names_that_are_not_text_are_reported(tmp_path, old, new, message):
    path = tmp_path / "site.yml"
    path.write_text(SiteConfig.builtin_path("eu").read_text().replace(old, new))
    with pytest.raises(ConfigError, match=message):
        SiteConfig.load(str(path))


def _eu_raw(**changes):
    raw = yaml.safe_load(SiteConfig.builtin_path("eu").read_text())
    naming = changes.pop("branch", None)
    if naming:
        raw["naming"]["branch"] = naming
    raw.update(changes)
    return raw


FOLDER = "the same name, or one is a folder of the other"


@pytest.mark.parametrize(
    "changes,source,message",
    [
        (
            {"base_branch": "byoc", "branch": "byoc/{x}"},
            "",
            f"base_branch 'byoc' clashes with the branch of every BYOC name (naming.branch 'byoc/{{x}}'): {FOLDER}",
        ),
        (
            {"branch": "byoc_ci_batch/{x}"},
            "",
            "the --single-pr branch 'byoc_ci_batch' clashes with the branch of every BYOC name "
            f"(naming.branch 'byoc_ci_batch/{{x}}'): {FOLDER}",
        ),
        (
            {"base_branch": "alice01_byoc_ci/next"},
            "",
            "base_branch 'alice01_byoc_ci/next' clashes with the branch of the BYOC name 'alice01' "
            f"(naming.branch '{{x}}_byoc_ci'): {FOLDER}",
        ),
        ({"base_branch": 2024}, "", "base_branch must be non-empty text, got 2024"),
        ({"base_branch": ""}, "site.yml", "site.yml: base_branch must be non-empty text, got ''"),
    ],
)
def test_config_errors_say_exactly_what_is_wrong(tmp_path, changes, source, message):
    with pytest.raises(ConfigError) as info:
        SiteConfig.from_dict(_eu_raw(**changes), base=SiteConfig.builtin_path("eu").parent, source=source)
    assert str(info.value) == message
