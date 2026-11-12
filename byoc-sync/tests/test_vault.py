from pathlib import Path

import pytest
from ansible.parsing.vault import VaultLib, VaultSecret

from byoc_sync.vault import VaultError, VaultFile, read_password

FIXTURES = Path(__file__).parent / "fixtures"
PASSWORD = (FIXTURES / "vault-pass.txt").read_text().strip().encode()
NEW_PW = "MyPulS4rP4ssw0rd"


@pytest.fixture
def vault_path(tmp_path, eu_fixtures):
    path = tmp_path / "pulsar.yml"
    path.write_bytes((eu_fixtures / "secret_group_vars/pulsar.yml").read_bytes())
    return path


def decrypt(data: bytes, password: bytes = PASSWORD) -> str:
    return VaultLib([("default", VaultSecret(password))]).decrypt(data).decode()


def test_reads_existing_values(vault_path):
    vault = VaultFile(vault_path, PASSWORD)
    assert vault.get("rabbitmq_password_galaxy_egi01").startswith("dummy-")
    assert vault.get("missing") is None
    assert "rabbitmq_password_galaxy_egi01" in vault.keys()


def test_add_update_and_remove_keys(vault_path):
    vault = VaultFile(vault_path, PASSWORD)
    plan = vault.plan({"rabbitmq_password_galaxy_alice01_byoc": NEW_PW})
    assert plan.changed and [c.action for c in plan.changes] == ["create"]
    data = VaultFile.parse(plan.plaintext)
    assert data["rabbitmq_password_galaxy_alice01_byoc"] == NEW_PW

    vault_path.write_bytes(plan.ciphertext())
    vault = VaultFile(vault_path, PASSWORD)
    plan = vault.plan({"rabbitmq_password_galaxy_alice01_byoc": "AnotherPassw0rd"})
    assert [c.action for c in plan.changes] == ["update"]

    vault_path.write_bytes(plan.ciphertext())
    plan = VaultFile(vault_path, PASSWORD).plan({"rabbitmq_password_galaxy_alice01_byoc": None})
    assert [c.action for c in plan.changes] == ["delete"]
    assert plan.plaintext == (FIXTURES / "eu-vault-plain.yml").read_text()


def test_unchanged_value_is_not_a_change(vault_path):
    vault = VaultFile(vault_path, PASSWORD)
    value = vault.get("rabbitmq_password_galaxy_egi01")
    plan = vault.plan({"rabbitmq_password_galaxy_egi01": value, "rabbitmq_password_galaxy_nobody_byoc": None})
    assert not plan.changed and plan.changes == []


def test_other_plaintext_lines_are_untouched(vault_path):
    before = (FIXTURES / "eu-vault-plain.yml").read_text().splitlines()
    plan = VaultFile(vault_path, PASSWORD).plan({"rabbitmq_password_galaxy_alice01_byoc": NEW_PW})
    after = plan.plaintext.splitlines()
    assert [line for line in after if "alice01" not in line] == before


def test_new_entry_goes_after_the_last_rabbitmq_password(vault_path):
    plan = VaultFile(vault_path, PASSWORD).plan({"rabbitmq_password_galaxy_alice01_byoc": NEW_PW})
    lines = plan.plaintext.splitlines()
    i = next(n for n, line in enumerate(lines) if "alice01" in line)
    assert lines[i - 1].startswith("rabbitmq_password_")


def test_digit_only_password_stays_a_string(vault_path):
    plan = VaultFile(vault_path, PASSWORD).plan({"rabbitmq_password_galaxy_alice01_byoc": "0123456701"})
    assert VaultFile.parse(plan.plaintext)["rabbitmq_password_galaxy_alice01_byoc"] == "0123456701"


def test_ciphertext_decrypts_to_the_plan_and_keeps_the_vault_id(tmp_path):
    lib = VaultLib([("prod", VaultSecret(PASSWORD))])
    path = tmp_path / "labelled.yml"
    path.write_bytes(lib.encrypt(b"a_key: 'x'\n", VaultSecret(PASSWORD), vault_id="prod"))
    plan = VaultFile(path, PASSWORD).plan({"rabbitmq_password_galaxy_alice01_byoc": NEW_PW})
    blob = plan.ciphertext()
    assert blob.startswith(b"$ANSIBLE_VAULT;1.2;AES256;prod\n")
    assert decrypt(blob) == plan.plaintext


def test_wrong_password_fails_before_anything_else(vault_path):
    with pytest.raises(VaultError, match="decrypt"):
        VaultFile(vault_path, b"wrong-password")


def test_file_that_is_not_a_vault_is_refused(tmp_path):
    path = tmp_path / "plain.yml"
    path.write_text("a: 1\n")
    with pytest.raises(VaultError, match="not an Ansible vault"):
        VaultFile(path, PASSWORD)


def test_missing_vault_file_is_reported(tmp_path):
    with pytest.raises(VaultError, match="not found"):
        VaultFile(tmp_path / "nope.yml", PASSWORD)


def test_vault_that_is_not_a_mapping_is_refused(tmp_path):
    path = tmp_path / "list.yml"
    path.write_bytes(VaultLib([("default", VaultSecret(PASSWORD))]).encrypt(b"- a\n- b\n", VaultSecret(PASSWORD)))
    with pytest.raises(VaultError, match="mapping"):
        VaultFile(path, PASSWORD)


def test_plans_and_changes_never_show_passwords(vault_path):
    plan = VaultFile(vault_path, PASSWORD).plan({"rabbitmq_password_galaxy_alice01_byoc": NEW_PW})
    text = repr(plan) + str(plan) + repr(plan.changes) + "".join(c.describe() for c in plan.changes)
    assert NEW_PW not in text and "********" in text


def test_read_password_from_file_or_environment(tmp_path, monkeypatch):
    path = tmp_path / "pass"
    path.write_text("from-file\n")
    monkeypatch.delenv("BYOC_SYNC_VAULT_PASSWORD", raising=False)
    assert read_password(str(path)) == b"from-file"
    monkeypatch.setenv("BYOC_SYNC_VAULT_PASSWORD", "from-env")
    assert read_password(None) == b"from-env"
    assert read_password(str(path)) == b"from-file"  # an explicit file wins


def test_missing_password_is_reported(monkeypatch):
    monkeypatch.delenv("BYOC_SYNC_VAULT_PASSWORD", raising=False)
    with pytest.raises(VaultError, match="BYOC_SYNC_VAULT_PASSWORD"):
        read_password(None)


def test_empty_password_file_is_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("BYOC_SYNC_VAULT_PASSWORD", raising=False)
    path = tmp_path / "pass"
    path.write_text("\n")
    with pytest.raises(VaultError, match="empty"):
        read_password(str(path))


def _vault_with(tmp_path, plaintext: bytes):
    path = tmp_path / "v.yml"
    path.write_bytes(VaultLib([("default", VaultSecret(PASSWORD))]).encrypt(plaintext, VaultSecret(PASSWORD)))
    return path


@pytest.mark.parametrize(
    "plaintext",
    [
        b"rabbitmq_password_a: 'SecretOne111'\nrabbitmq_password_a: 'SecretTwo222'\n",
        b"rabbitmq_password_a: 'SecretOne111'\nrabbitmq_password_b: [SecretTwo222\n",
    ],
)
def test_broken_vault_content_is_reported_without_its_values(tmp_path, plaintext):
    with pytest.raises(VaultError) as info:
        VaultFile(_vault_with(tmp_path, plaintext), PASSWORD)
    assert "SecretOne111" not in str(info.value) and "SecretTwo222" not in str(info.value)
    assert "line" in str(info.value)


def test_inline_vault_values_inside_the_vault_do_not_break_planning(tmp_path):
    path = _vault_with(tmp_path, b"other: !vault |\n  $ANSIBLE_VAULT;1.1;AES256\n  3132\nrabbitmq_password_a: 'x'\n")
    plan = VaultFile(path, PASSWORD).plan({"rabbitmq_password_galaxy_alice01_byoc": NEW_PW})
    assert "rabbitmq_password_galaxy_alice01_byoc" in plan.plaintext


def test_decrypted_vault_text_is_not_cached(vault_path):
    from byoc_sync.yamledit import doc as doc_module

    doc_module._parsed.cache_clear()
    VaultFile(vault_path, PASSWORD).plan({"rabbitmq_password_galaxy_alice01_byoc": NEW_PW})
    assert doc_module._parsed.cache_info().currsize == 0
