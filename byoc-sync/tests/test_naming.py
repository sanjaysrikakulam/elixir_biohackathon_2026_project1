from byoc_sync.naming import Names, NamingConfig


def test_eu_naming_for_simple_name():
    n = Names.for_("alice01", NamingConfig())
    assert n.mq_user == "galaxy_alice01_byoc"
    assert n.vhost == "/pulsar/galaxy_alice01_byoc"
    assert n.vault_var == "rabbitmq_password_galaxy_alice01_byoc"
    assert n.runner_id == "pulsar_eu_alice01_byoc"
    assert n.destination_id == "pulsar_alice01_byoc_tpv"
    assert n.tag == "alice01-byoc-pulsar"
    assert n.rule_id == "byoc-pulsar-alice01"
    assert n.branch == "alice01_byoc_ci"


def test_underscores_become_dashes_in_tags_and_rule_ids_only():
    n = Names.for_("ufz_eve01", NamingConfig())
    assert n.tag == "ufz-eve01-byoc-pulsar"
    assert n.rule_id == "byoc-pulsar-ufz-eve01"
    assert n.destination_id == "pulsar_ufz_eve01_byoc_tpv"


def test_custom_patterns():
    cfg = NamingConfig(mq_user="org_{x}", tag="{x_dash}-mine")
    n = Names.for_("a_b", cfg)
    assert n.mq_user == "org_a_b" and n.tag == "a-b-mine"


def test_name_from_rule_id_round_trip():
    cfg = NamingConfig()
    assert cfg.name_from("rule_id", "byoc-pulsar-ufz-eve01") == "ufz_eve01"
    assert cfg.name_from("destination_id", "pulsar_ufz_eve01_byoc_tpv") == "ufz_eve01"
    assert cfg.name_from("runner_id", "pulsar_eu_egi01") is None
    assert cfg.name_from("mq_user", "galaxy_egi01") is None
    assert cfg.name_from("vault_var", "rabbitmq_password_galaxy_abc_byoc") == "abc"
    # a name we could never have generated is not ours
    assert cfg.name_from("vault_var", "rabbitmq_password_galaxy_a_byoc") is None


def test_name_from_unknown_kind_is_a_value_error():
    import pytest

    with pytest.raises(ValueError):
        NamingConfig().name_from("nonsense", "x")


def test_name_from_rejects_near_misses():
    cfg = NamingConfig()
    assert cfg.name_from("tag", "foo-byoc-byoc-pulsar") is None
    assert cfg.name_from("destination_id", "pulsar_alice01_byoc_tpv_extra") is None
    assert cfg.name_from("branch", "alice01_byoc_ci") == "alice01"
