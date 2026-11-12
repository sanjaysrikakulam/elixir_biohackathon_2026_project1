# byoc_sync — Ansible role

Installs and configures [byoc-sync](../../../README.md), which turns Galaxy users' "Bring your own compute"
(BYOC) Pulsar preferences into pull requests against an infrastructure-playbook repository, and can run it from
cron.

The role:

1. checks the configuration first and stops, before changing anything, if a required value is missing, empty or
   still `changeme`, or if the system user or group does not exist;
2. installs git, a Python of 3.10 or newer with `venv`, `acl` and `util-linux` (`flock`);
3. clones byoc-sync and installs it into a virtualenv, owned by your system user and group;
4. writes the secrets into files only that user can read;
5. installs a wrapper command, `byoc-sync-run`, that runs byoc-sync with your options;
6. optionally adds a cron job (off by default; daily at midnight when on).

Read [`defaults/main.yml`](defaults/main.yml): every option is there, with a comment.

## Requirements

- ansible-core 2.16 or newer; only `ansible.builtin` modules are used.
- Target hosts: Rocky Linux or AlmaLinux 9 and 10, Ubuntu 22.04 and 24.04.
- Privilege escalation (`become: true`) for packages, `/etc` and `/usr/local/bin`.
- **The system user and group must already exist.** The role creates neither; it stops with
  `system user '<name>' does not exist on <host>; create it first` (or `system group '<name>' ...`).
- Some tasks run as that unprivileged user. Ansible needs either `pipelining = True` (see the example
  `ansible.cfg` next to the example playbook) or the `acl` package on the host, which the role installs.

## Quick start

See [`../../example-playbook.yml`](../../example-playbook.yml):

```bash
cd ansible
ansible-playbook -i inventory example-playbook.yml --ask-vault-pass
```

A fresh install is safe: `byoc_sync_dry_run` is `true` (reports only, nothing is pushed or published),
`byoc_sync_allow_delete` is `false` and cron is off. Run it by hand, look at the report, then switch things on.

## Variables you must set

These default to `changeme`. The role stops and lists every one that is still missing, empty or `changeme`
(in any case, with or without quotes). They must be text: quote numbers in YAML (`'0123'`, not `0123`).
Paths must be absolute, the virtualenv and the source folder must not be inside each other, and repository URLs
must not contain a password (they appear in the wrapper and in `ps`; use SSH keys or the GitHub token). The
switches (`byoc_sync_dry_run`, `byoc_sync_allow_delete`, `byoc_sync_cron_enabled`, `byoc_sync_manage_packages`,
`byoc_sync_install_tpv`) must be `true`/`false` (or `yes`/`no`); anything else, an empty value included, stops
the role instead of being read as `false`.

**Always:**

| Variable | What to set |
|---|---|
| `byoc_sync_user` | existing system user that owns and runs byoc-sync (never root; a name, not a UID) |
| `byoc_sync_group` | existing group of the folders and files the role creates, usually the user's own group; a name, not a GID (see [What the role writes](#what-the-role-writes)) |
| `byoc_sync_repo_url` | git URL of byoc-sync |
| `byoc_sync_venv` | virtualenv path, e.g. `/opt/byoc-sync/venv` |
| `byoc_sync_reports_dir` | folder for reports, the cron log and the run lock, e.g. `/var/lib/byoc-sync` |
| `byoc_sync_vault_password` | the infrastructure playbook's Ansible Vault password (secret; no leading or trailing whitespace) |

**To run byoc-sync** (checked by the role when cron is on, and by `byoc-sync-run` on every run):

| Variable | What to set |
|---|---|
| `byoc_sync_playbook_repo_url` | clone URL of the infrastructure playbook byoc-sync edits |
| `byoc_sync_galaxy_url` | Galaxy server URL (only for `byoc_sync_source: api`) |
| `byoc_sync_galaxy_api_key` | Galaxy **admin** API key (secret; only for `byoc_sync_source: api`) |

**To publish pull requests** (only when `byoc_sync_dry_run` is `false`):

| Variable | What to set |
|---|---|
| `byoc_sync_github_repo` | `OWNER/REPO` the pull requests are opened on; the same repository as the playbook clone URL |
| `byoc_sync_github_token` | GitHub token that may push branches and open pull requests there (secret) |

## Other variables

All optional; the defaults work.

| Variable | Default | Meaning |
|---|---|---|
| `byoc_sync_version` | `main` | branch, tag or commit of byoc-sync. Every play run updates the clone to it, so **pin a tag or commit in production** |
| `byoc_sync_src_dir` | `<venv>-src` | where byoc-sync is cloned |
| `byoc_sync_install_tpv` | `false` | install the `tpv` extra (for `--validate-tpv`); setting it back to `false` does not uninstall it |
| `byoc_sync_python` | empty | Python used for the virtualenv; empty picks `python3.12` on Rocky/AlmaLinux 9 and `python3` elsewhere |
| `byoc_sync_manage_packages` | `true` | install the system packages (and cron's package); `false` if you provide them |
| `byoc_sync_config_dir` | `/etc/byoc-sync` | folder for the secret files |
| `byoc_sync_wrapper_path` | `/usr/local/bin/byoc-sync-run` | the wrapper command |
| `byoc_sync_work_dir` | `<reports>/work` | byoc-sync's `--workdir`, where it clones the playbook |
| `byoc_sync_source` | `api` | `api` (Galaxy admin API) or `gxadmin` (one SQL query; gxadmin and database access on this host are up to you) |
| `byoc_sync_site_config` | `eu` | bundled site name or absolute path of a site YAML on the host. As in byoc-sync, a value with a file suffix (`.yml`) is a file, anything else a bundled name, checked against the sites in the cloned byoc-sync. **`eu` is usegalaxy.eu's layout**; other sites set their own |
| `byoc_sync_dry_run` | `true` | report only; `false` pushes branches and opens pull requests |
| `byoc_sync_allow_delete` | `false` | remove endpoints of users who cleared the form or were deleted, and apply renames |
| `byoc_sync_gxadmin_cmd` | `gxadmin` | `--gxadmin-cmd` for `byoc_sync_source: gxadmin` |
| `byoc_sync_extra_args` | `[]` | further byoc-sync options, one list item per argument, e.g. `["--allow-list", "/etc/byoc-sync/allow.txt"]`. They come after the role's options. No secrets here: the wrapper is world-readable |
| `byoc_sync_extra_env` | `{}` | extra environment variables, e.g. `PGHOST`/`PGUSER` for gxadmin. Names must be plain (`[A-Za-z_][A-Za-z0-9_]*`) and not `PATH`, `IFS`, `HOME`, `ENV`, `BASH_ENV`, `SHELL`, `LD_*`, `PYTHON*` or a name the role sets |
| `byoc_sync_fingerprint_key` | empty | `BYOC_SYNC_FINGERPRINT_KEY` (secret). Recommended: without it the fingerprint in pull request bodies is a plain hash that includes the owner's e-mail. Keep it the same between runs |
| `byoc_sync_cron_enabled` | `false` | run byoc-sync from cron as `byoc_sync_user` |
| `byoc_sync_cron_minute` | `0` | cron schedule (daily at midnight by default) |
| `byoc_sync_cron_hour` | `0` | |
| `byoc_sync_cron_day` | `*` | |
| `byoc_sync_cron_month` | `*` | |
| `byoc_sync_cron_weekday` | `*` | |

## Secrets

Put secrets in Ansible Vault (`ansible-vault encrypt_string`), not in plain text. They are written to
`<config_dir>/env` (API key, GitHub token, fingerprint key, `byoc_sync_extra_env`) and
`<config_dir>/vault-password`, both owned by the user and group with mode 0600 in a 2700 folder, and never show up in
Ansible's output, on a command line or in the crontab. The GitHub token is only written when dry-run is off.
A secret that contains `{{` must be marked `!unsafe` (or come from Vault), or Ansible tries to template it.

## Running by hand

```bash
sudo -u <user> byoc-sync-run                 # with the configured options
sudo -u <user> byoc-sync-run --allow-delete  # extra byoc-sync options are appended
```

`byoc-sync-run` refuses to run as any other user, refuses to start while a value it needs is missing, and
exits with an error if `flock` is missing or fails. It
writes `report-<UTC time>.json` into the reports folder and holds a lock there, so a second run while one is
active exits without doing anything. Reports are never cleaned up.

## Cron

With `byoc_sync_cron_enabled: true` the role installs and starts cron (`cronie`/`crond` on Rocky and AlmaLinux,
`cron` on Ubuntu) and adds an entry named `byoc-sync` for the user that runs `byoc-sync-run` and appends its
output to `<reports>/byoc-sync.log`. Setting it back to `false` removes that entry and nothing else. The
reports folder and wrapper path must not contain `%` (cron reads it as a newline). The log is never rotated:
add a logrotate rule if it matters. If you later change `byoc_sync_user`, first run the role with cron off for
the old user, or remove the old user's `byoc-sync` entry by hand: the role only manages the current user's.

## What the role writes

| Path | Owner, mode |
|---|---|
| `byoc_sync_config_dir` with `env` and `vault-password` | the user and group, 2700 / 0600 |
| `byoc_sync_wrapper_path` | root:root, 0755 (no secrets in it) |
| `byoc_sync_src_dir`, `byoc_sync_venv` | the user and group, 2755 |
| `byoc_sync_reports_dir`, `byoc_sync_work_dir` | the user and group, 2750 |

"The group" is `byoc_sync_group`. The folders have the setgid bit (the leading 2), so the files and folders that
git, pip and byoc-sync create inside them get that group too, also when it is not the user's primary group. The
group gets no access to the secrets, and the reports are mode 0600, so a group can list the reports folder but
not read the reports. If you change `byoc_sync_group` later, the role changes the group of the paths above only;
run `chgrp -R` on the folders yourself for what is already inside them.

byoc-sync is reinstalled whenever the checked-out revision, the extras or the interpreter differs from what is
recorded in `<venv>/.byoc-sync-installed`; a new interpreter rebuilds the virtualenv first. The record is written
only after a successful install, so a failed update is retried on the next run.

## Limitations

- **Private repositories:** byoc-sync gives the GitHub token to git only when it publishes (dry-run off). To
  clone a private byoc-sync or playbook repository otherwise, give the user its own git credentials, e.g. an SSH
  deploy key, the host in its `known_hosts` and an SSH clone URL. Setting that up is up to you.
- A commit that is not on a branch or tag may need a full clone; use a tag or a branch commit for
  `byoc_sync_version`.
- Starting cron needs a service manager (systemd on the supported platforms).
- The byoc-sync clone is updated by fast-forward; if its branch was force-pushed, delete `byoc_sync_src_dir`
  and run the role again.
- Tasks that run as the user use `become_user`; with a system user whose shell is `nologin`, use `sudo` (the
  default `become_method`), not `su`.
- `ansible-playbook --check` works, including on a fresh host; it reads the byoc-sync repository's head, so the
  URL must be reachable.

## License

MIT, see [LICENSE](LICENSE). Author: Sanjay Srikakulam.
