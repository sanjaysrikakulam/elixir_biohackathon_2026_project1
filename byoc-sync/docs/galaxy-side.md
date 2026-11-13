# What the Galaxy server needs

byoc-sync proposes per-user changes to a Galaxy server's playbook repository. Those changes only work if the server already has a few things in place. A Galaxy admin sets them up once, before byoc-sync runs for the first time. The files in [`../galaxy-side/`](../galaxy-side/) are ready to copy.

## The form

Users register their endpoint in *User > Preferences > Manage information*. The `byoc_pulsar` section must exist in `user_preferences_extra_conf.yml`, with plain inputs (no `store: vault`), because byoc-sync reads them through the API or gxadmin. [`../galaxy-side/config/user_preferences_byoc.yml`](../galaxy-side/config/user_preferences_byoc.yml) has the section. Replace `RABBITMQ_HOST:PORT` in its description with the address users' Pulsars connect to.

Galaxy reads the form at start-up, so it needs a restart after the change.

## Routing jobs to a user's endpoint

TPV must send a user's jobs to their own endpoint, and only theirs. byoc-sync supports two ways.

- **By e-mail** (default, `owner_key: email`). byoc-sync writes one entry per user to TPV's `users.yml`, keyed by the user's address. Nothing to add on the Galaxy side, but every BYOC user's address ends up in the repository.
- **By user id** (`owner_key: galaxy_id`). A Galaxy admin adds one fixed rule to the default tool entity: [`../galaxy-side/tpv/byoc_rule.yml`](../galaxy-side/tpv/byoc_rule.yml), and creates the owners file from [`../galaxy-side/byoc_owners.yml`](../galaxy-side/byoc_owners.yml). byoc-sync then writes no address anywhere. [routing-by-user-id.md](routing-by-user-id.md) explains how it works and how to set up the site file.

With a TPV version that has `SchedulingTags` (3.x), the rule can be used as it is.

## A parent destination for remote Pulsars

Generated destinations inherit from one parent destination (`pulsar_default` on usegalaxy.eu, set in the site's destination template). It holds what every Pulsar endpoint has in common: the staging settings (`default_file_action: remote_transfer`, `jobs_directory`, `rewrite_parameters`) and the container settings.

Its container volumes must only name paths that exist on any Pulsar: the job's own directories and CVMFS reference data. Volumes that mount the Galaxy server's own shared storage make container creation fail on a remote Pulsar, with "mount source doesn't exist":

```yaml
singularity_volumes: "$job_directory:rw,$tool_directory:ro,$job_directory/outputs:rw,$working_directory:rw,/cvmfs/data.galaxyproject.org:ro"
```

If the tool defaults set volumes, the parent destination must override them.

## Reachable from the users' Pulsars

- **RabbitMQ.** Users' Pulsars connect to the server's RabbitMQ over TLS. Its port must be reachable from outside, and the generated runner's `amqp_url` names the address Galaxy itself uses. The two can differ, for example behind a port forward.
- **Galaxy over HTTPS.** Pulsar downloads job inputs from and uploads outputs to the Galaxy URL in the runner. That URL must be reachable from the users' networks. A path that drops large packets without the usual "fragmentation needed" reply makes uploads stall; Pulsar hosts then need `net.ipv4.tcp_mtu_probing=1`.

## For byoc-sync itself

- A Galaxy admin API key (API source) or gxadmin with database access (gxadmin source). Galaxy 26.1 returns password inputs to admins; 26.2 and later do not, so newer servers need the gxadmin source.
- The vault password of the file byoc-sync writes the RabbitMQ passwords to.
- A token that may push branches to the playbook repository and open pull requests there. Pull requests opened with a workflow's built-in token do not start other workflows, so a bot or GitHub App token is better.
- A site file (`sites/<site>.yml`) with the paths, anchors, naming and templates of the server's playbook repository. [`../src/byoc_sync/sites/eu.yml`](../src/byoc_sync/sites/eu.yml) is the example.
