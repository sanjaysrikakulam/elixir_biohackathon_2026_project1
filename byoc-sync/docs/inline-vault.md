# Passwords as inline vault values

byoc-sync stores each endpoint's RabbitMQ password as an Ansible vault variable. By default the whole file is one vault, as on usegalaxy.eu (`secret_group_vars/pulsar.yml`). Some playbooks keep their secrets in a plain YAML file instead, with one encrypted value per variable:

```yaml
tpv_byoc_enabled: true

rabbitmq_password_galaxy_alice01_byoc: !vault |
  $ANSIBLE_VAULT;1.2;AES256;byoc
  6238...
```

The site setting `vault.format: inline` selects this layout:

```yaml
vault:
  file: inventories/host_vars/test.usegalaxy.be/byoc.yml
  format: inline
  vault_id: byoc
  anchor: "^(tpv_byoc_enabled|rabbitmq_password_galaxy_.+_byoc)$"
```

## How byoc-sync edits the file

byoc-sync decrypts every `!vault` value in memory and edits a *decrypted view* of the file, the same way it edits a whole-file vault. When it writes the file back, every value that did not change keeps its original ciphertext, byte for byte. Only new and changed passwords are encrypted again. A pull request therefore shows one new block per new endpoint, and nothing else in the file moves. Before handing the file out, byoc-sync reads it back and checks that it decrypts to the planned content.

Plain variables and comments in the file are left alone. byoc-sync only manages one-line text values.

## Settings

- `format`: `file` (default) or `inline`.
- `vault_id`: the vault id for new values. Without it, byoc-sync uses the id of the values already in the file, and stops if they use more than one.
- `anchor`: new variables go after the last variable whose name matches this regular expression, else after the last variable. Default `^rabbitmq_password_`. It also applies to whole-file vaults.

All values in the file must decrypt with the one vault password byoc-sync is given.

## The test.usegalaxy.be site

[`../src/byoc_sync/sites/usegalaxy-be-test.yml`](../src/byoc_sync/sites/usegalaxy-be-test.yml) uses an inline vault and records owners by user id ([routing-by-user-id.md](routing-by-user-id.md)). Its anchors name the entry just before an older script's generated blocks, so new entries never land between that script's `BEGIN`/`END` markers. Its destination template follows pulsar-byoc's layout: with `nfs_path` set, it sets the staging directory to `<nfs_path>/staging` and the container cache to `<nfs_path>/container_cache`. Apptainer's temporary files always go to the job's own temporary directory.
