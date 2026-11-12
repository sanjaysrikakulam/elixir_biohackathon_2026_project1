from ansible.parsing.vault import VaultLib, VaultSecret

import byoc_sync


def test_package_version():
    assert byoc_sync.__version__ == "0.1.0"


def test_eu_fixture_vault_decrypts_with_test_password(eu_fixtures):
    secret = (eu_fixtures.parent / "vault-pass.txt").read_text().strip().encode()
    blob = (eu_fixtures / "secret_group_vars/pulsar.yml").read_bytes()
    plain = VaultLib([("default", VaultSecret(secret))]).decrypt(blob).decode()
    assert "rabbitmq_password_galaxy_egi01:" in plain
