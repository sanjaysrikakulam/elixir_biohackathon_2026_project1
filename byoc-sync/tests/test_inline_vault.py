"""vault.format: inline: a plain YAML file with one ``!vault`` value per password (docs/inline-vault.md)."""

from __future__ import annotations

import pytest
import yaml
from ansible.parsing.vault import VaultLib, VaultSecret

from byoc_sync.config import ConfigError, SiteConfig
from byoc_sync.vault import InlineVaultFile, VaultError, VaultFile, open_vault

from .test_vault import PASSWORD

OLD = "rabbitmq_password_galaxy_byoc_0e0fad4d103ae8ed"
NEW = "rabbitmq_password_galaxy_alice01_byoc"


def encrypted(value: str, vault_id: str | None = "byoc", password: bytes = PASSWORD) -> list[str]:
    lib = VaultLib([(vault_id or "default", VaultSecret(password))])
    return ["  " + line for line in lib.encrypt(value.encode(), VaultSecret(password), vault_id=vault_id).decode().splitlines()]


def inline_file(values: dict[str, str], vault_id: str | None = "byoc") -> str:
    lines = ["---", "# Bring your own compute (BYOC).", "tpv_byoc_enabled: true", ""]
    for key, value in values.items():
        lines += [f"# BEGIN BYOC {key}", f"{key}: !vault |", *encrypted(value, vault_id), f"# END BYOC {key}"]
    lines += ["# BYOC: generated blocks go above this line", ""]
    return "\n".join(lines)


@pytest.fixture
def path(tmp_path):
    path = tmp_path / "byoc.yml"
    path.write_text(inline_file({OLD: "0ldPassw0rd"}))
    return path


def decrypt_all(text: str) -> dict:
    """What Ansible would see: every !vault value decrypted."""

    class Loader(yaml.SafeLoader):
        pass

    lib = VaultLib([("byoc", VaultSecret(PASSWORD))])
    Loader.add_constructor("!vault", lambda loader, node: lib.decrypt(loader.construct_scalar(node)).decode())
    return yaml.load(text, Loader=Loader)  # noqa: S506


def test_reads_inline_values(path):
    vault = InlineVaultFile(path, PASSWORD)
    assert vault.get(OLD) == "0ldPassw0rd"
    assert vault.get("tpv_byoc_enabled") == "True"
    assert OLD in vault.keys()


def test_new_value_is_added_and_existing_ciphertext_is_kept(path):
    before = path.read_text()
    plan = InlineVaultFile(path, PASSWORD).plan({NEW: "NewPassw0rd"})
    assert [(c.key, c.action) for c in plan.changes] == [(NEW, "create")]
    after = plan.ciphertext().decode()
    # The original lines are all still there, in order: the old value is not re-encrypted.
    lines = after.splitlines()
    start = lines.index(f"{NEW}: !vault |")
    block = lines[start + 1 : start + 1 + sum(1 for line in lines[start + 1 :] if line.startswith("  "))]
    assert lines[start - 1] == ""  # the editor's separator: entries in this file are separated by blank lines
    assert lines[: start - 1] + lines[start + 1 + len(block) :] == before.splitlines()
    assert block[0] == "  $ANSIBLE_VAULT;1.2;AES256;byoc"
    assert "NewPassw0rd" not in after
    assert decrypt_all(after) == {"tpv_byoc_enabled": True, OLD: "0ldPassw0rd", NEW: "NewPassw0rd"}


def test_anchor_keeps_new_values_out_of_other_generated_blocks(path):
    vault = InlineVaultFile(path, PASSWORD, anchor=r"^(tpv_byoc_enabled|rabbitmq_password_galaxy_.+_byoc)$")
    path.write_bytes(vault.plan({NEW: "NewPassw0rd"}).ciphertext())
    vault = InlineVaultFile(path, PASSWORD, anchor=r"^(tpv_byoc_enabled|rabbitmq_password_galaxy_.+_byoc)$")
    lines = vault.plan({"rabbitmq_password_galaxy_bob01_byoc": "B0bPassw0rd"}).ciphertext().decode().splitlines()
    order = [line.split(":")[0] for line in lines if line.startswith(("tpv_", "rabbitmq_", "# BEGIN"))]
    assert order == ["tpv_byoc_enabled", NEW, "rabbitmq_password_galaxy_bob01_byoc", f"# BEGIN BYOC {OLD}", OLD]


def test_new_value_goes_after_the_last_password(path):
    after = InlineVaultFile(path, PASSWORD).plan({NEW: "NewPassw0rd"}).ciphertext().decode().splitlines()
    assert after.index(f"{NEW}: !vault |") > after.index(f"{OLD}: !vault |")
    assert after.index(f"{NEW}: !vault |") < after.index("# BYOC: generated blocks go above this line")


def test_update_and_delete(path):
    plan = InlineVaultFile(path, PASSWORD).plan({NEW: "NewPassw0rd"})
    path.write_bytes(plan.ciphertext())
    plan = InlineVaultFile(path, PASSWORD).plan({NEW: "Changed0ne"})
    assert [c.action for c in plan.changes] == ["update"]
    path.write_bytes(plan.ciphertext())
    assert decrypt_all(path.read_text())[NEW] == "Changed0ne"
    plan = InlineVaultFile(path, PASSWORD).plan({NEW: None})
    assert [c.action for c in plan.changes] == ["delete"]
    path.write_bytes(plan.ciphertext())
    assert NEW not in decrypt_all(path.read_text())
    assert decrypt_all(path.read_text())[OLD] == "0ldPassw0rd"


def test_same_value_is_not_a_change_and_writes_the_same_bytes(path):
    vault = InlineVaultFile(path, PASSWORD)
    plan = vault.plan({OLD: "0ldPassw0rd"})
    assert not plan.changed
    assert plan.ciphertext() == path.read_bytes()


def test_view_hides_ciphertext_and_shows_values(path):
    view = InlineVaultFile(path, PASSWORD).plaintext
    assert f"{OLD}: '0ldPassw0rd'" in view
    assert "$ANSIBLE_VAULT" not in view
    assert "# END BYOC" in view


def test_vault_id_is_taken_from_the_file_or_the_site(path, tmp_path):
    assert InlineVaultFile(path, PASSWORD)._vault_id == "byoc"
    empty = tmp_path / "empty.yml"
    empty.write_text("---\ntpv_byoc_enabled: true\n")
    plan = InlineVaultFile(empty, PASSWORD, vault_id="byoc").plan({NEW: "NewPassw0rd"})
    assert "$ANSIBLE_VAULT;1.2;AES256;byoc" in plan.ciphertext().decode()


def test_wrong_password_is_refused(path):
    with pytest.raises(VaultError, match="wrong vault password"):
        InlineVaultFile(path, b"not-the-password")


def test_whole_file_vault_is_refused_in_inline_mode(tmp_path):
    path = tmp_path / "vault.yml"
    lib = VaultLib([("default", VaultSecret(PASSWORD))])
    path.write_bytes(lib.encrypt(b"a: 1\n", VaultSecret(PASSWORD)))
    with pytest.raises(VaultError, match="vault.format: file"):
        InlineVaultFile(path, PASSWORD)


def test_open_vault_follows_the_site(path, eu_fixtures):
    site = SiteConfig.load("eu")
    assert type(open_vault(eu_fixtures / "secret_group_vars/pulsar.yml", PASSWORD, site)) is VaultFile
    inline = SiteConfig(**{**site.__dict__, "vault_format": "inline", "vault_id": "byoc"})
    assert type(open_vault(path, PASSWORD, inline)) is InlineVaultFile


def test_unknown_vault_format_is_refused(tmp_path):
    raw = yaml.safe_load(open(SiteConfig.load("eu").source).read())
    raw["vault"]["format"] = "sops"
    with pytest.raises(ConfigError, match="vault.format"):
        SiteConfig.from_dict(raw, base=tmp_path, source="bad")
