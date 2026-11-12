# Pulsar endpoint for Galaxy with SLURM or HTCondor

This playbook sets up a Pulsar endpoint that a Galaxy server can send jobs to. Pulsar runs on one host. The jobs run on one or more worker hosts, through SLURM or HTCondor. Pulsar receives jobs from Galaxy over RabbitMQ or over [pulsar-relay](https://github.com/galaxyproject/pulsar-relay). With the relay, Galaxy and Pulsar both connect out to the relay over HTTPS, so the Pulsar site needs no open ports for messages.

It was started at the ELIXIR BioHackathon Europe 2026, project 1 "Bridging ELIXIR nodes for collaborative and scalable BYOC in Galaxy and TREs". The aim is that de.NBI SimpleVM, SURF Research Cloud and other cloud providers can call it with their own inventory to build a "bring your own compute" (BYOC) endpoint.

The playbook adds no new roles. It combines existing community roles and sets their variables from the inventory:

| Part | Role |
| --- | --- |
| Pulsar | [galaxyproject.pulsar](https://github.com/galaxyproject/ansible-pulsar) |
| SLURM | [galaxyproject.slurm](https://github.com/galaxyproject/ansible-slurm), with `slurm-drmaa` from [galaxyproject.repos](https://github.com/galaxyproject/ansible-repos) |
| HTCondor | [grycap.htcondor](https://github.com/usegalaxy-eu/ansible-htcondor-grycap) (usegalaxy-eu fork) |
| Shared directory | [geerlingguy.nfs](https://github.com/geerlingguy/ansible-role-nfs) |
| Reference data and containers | [galaxyproject.cvmfs](https://github.com/galaxyproject/ansible-cvmfs), [usegalaxy_eu.apptainer](https://github.com/usegalaxy-eu/ansible-apptainer) |

`requirements.yml` takes the HTCondor role from [a fork](https://github.com/pauldg/ansible-role-htcondor/tree/eu-htcondor-repo-url) until [usegalaxy-eu/ansible-htcondor-grycap#7](https://github.com/usegalaxy-eu/ansible-htcondor-grycap/pull/7) is merged. The upstream role downloads from research.cs.wisc.edu, which apt on Ubuntu 22.04 rejects.

## Where this fits in BYOC

A user brings their own compute to a Galaxy server in three steps:

1. They deploy a Pulsar endpoint with this playbook, with a RabbitMQ username and password they choose.
2. They enter those values in the BYOC form of their Galaxy (*User > Preferences > Manage information > byoc_pulsar*). The connection summary below lists what to enter.
3. A sync script reads the form and opens a PR on the Galaxy's infrastructure playbook with the user's RabbitMQ account, runner and TPV destination. An admin reviews and merges it, and Galaxy then sends that user's jobs to their Pulsar.

The sync script is `scripts/byoc/byoc_sync.py` on Mirela Minkova's `byoc-variant2-relay` branch of the usegalaxy.eu playbook. It works for usegalaxy.eu and, with [mirelaminkova/infrastructure-playbook#2](https://github.com/mirelaminkova/infrastructure-playbook/pull/2), for other Galaxy servers such as test.usegalaxy.be.

## How it works

The Pulsar host exports a shared directory over NFS. The workers mount it. Pulsar stages each job's files there, so the workers can read the inputs and write the outputs. The Pulsar host also runs the scheduler's controller: `slurmctld` for SLURM, or the HTCondor central manager and submit role. The workers run `slurmd` or the HTCondor execute role, with CVMFS and Apptainer for tools in containers.

Two variables choose the setup:

- `pulsar_scheduler`: `slurm` or `htcondor`
- `pulsar_connection`: `mq` or `relay`

With RabbitMQ, `pulsar_message_queue_url` is the Galaxy server's RabbitMQ, for example `pyamqp://USER:PASSWORD@test.usegalaxy.be:59871//pulsar/USER?ssl=1`.

With the relay, `pulsar_relay_placement` chooses which relay to use:

- `central` (default) uses usegalaxy.eu's relay at `stargate.galaxyproject.eu`. The account is `byoc_<id>`, where `<id>` is the user's encoded Galaxy user id (`pulsar_galaxy_user_id`, shown by `/api/users/current`). The user chooses the password. usegalaxy.eu creates the account after it merges the user's BYOC PR.
- `own` uses a relay the user runs, for example the [SURF Research Cloud relay component](https://github.com/mirelaminkova/pulsar-relay-src). Set `pulsar_relay_url`, `pulsar_relay_username` and `pulsar_relay_password`.

The Pulsar job manager is called `production` for RabbitMQ and `_default_` for the relay. These are the names usegalaxy.eu's runners expect. Override `pulsar_manager_name` for another Galaxy.

A host can be in both the `pulsar` and `workers` groups. That gives a single-VM endpoint.

## Connection summary

At the end of the run the playbook writes `/opt/pulsar/byoc-connection.yml` on the Pulsar host and prints it. It lists the values the user enters in the BYOC form: the transport, the relay or RabbitMQ account, the staging directory and the CPU and memory limits of the largest worker. It holds no passwords. A provider such as SimpleVM can show this file to the user.

With the relay, the playbook also checks that Pulsar has connected. It signs in to the relay and waits for Pulsar's capability topic (`pulsar_capabilities`). On the central relay this only works after usegalaxy.eu has created the account, so a first run reports "relay login failed" in the summary instead of stopping. Run the playbook again after the PR is merged to confirm the connection.

## Usage

The playbook runs from any machine that can reach the hosts over SSH, with a user that can `sudo` without a password. Install the roles, add the hosts and secrets, and run it:

```bash
ansible-galaxy install -r requirements.yml
cp inventory.example.ini inventory.ini        # add your hosts
cp secrets.example.yml secrets.yml            # add the queue URL or relay password
ansible-playbook site.yml -e @secrets.yml -e pulsar_scheduler=slurm -e pulsar_connection=mq
```

Set `pulsar_nfs_network` to the subnet of your hosts. The other defaults are in `group_vars/all/`, one file for each topic.

## Tested platforms

Both platforms below ran the full playbook with SLURM on a single VM (Ubuntu 22.04). A test job ran as the `pulsar` user and wrote to the shared staging directory. The connection to a real Galaxy has not been tested yet.

| Platform | VM | Access | Result |
| --- | --- | --- | --- |
| SURF Research Cloud | plain Ubuntu 22.04 workspace, 4 cores, 32 GB | `ssh <SRAM username>@<workspace IP>`; members of the CO admin group can `sudo` without a password | SLURM, CVMFS, Apptainer and Pulsar (relay mode) running |
| de.NBI SimpleVM | "de.NBI default", 2 cores, 4 GB | `ssh -p <port> ubuntu@<gateway>`; set `ansible_port` in the inventory | SLURM, CVMFS, Apptainer and Pulsar (RabbitMQ mode) running |

[`deployments/`](deployments/) has the inventories, variables and commands we used.

A cluster with separate workers needs the hosts to reach each other on the scheduler and NFS ports: 6817 and 6818 (SLURM) or 9618 (HTCondor), and 2049 and 111 (NFS). On both platforms, VMs only reach each other over SSH by default, so the provider has to open these ports first.

## Galaxy side

Galaxy sends jobs to this endpoint with the Pulsar message-queue runner. For RabbitMQ:

```yaml
runners:
  pulsar_byoc:
    load: galaxy.jobs.runners.pulsar:PulsarMQJobRunner
    amqp_url: pyamqp://USER:PASSWORD@MQ_HOST:5671//pulsar/USER?ssl=1
    galaxy_url: https://GALAXY_HOST
    manager: production
```

For the relay (Galaxy 26.0 or newer), use `relay_url`, `relay_username`, `relay_password` and `relay_topic_prefix` instead of `amqp_url`, and `manager: _default_`.

The environment points at Pulsar's staging directory:

```yaml
execution:
  environments:
    byoc_pulsar:
      runner: pulsar_byoc
      jobs_directory: /data/share/staging
      dependency_resolution: remote
      default_file_action: remote_transfer
```

Pulsar must be able to reach `galaxy_url` over HTTPS in both cases, because files go directly between Pulsar and Galaxy. With BYOC, the sync script writes the runner and the TPV destination for each user.

## Open points

- HTCondor installs with the fixed role but has not run a job yet.
- The playbook sets `net.ipv4.tcp_mtu_probing=1`. On SURF Research Cloud (MTU 8950), uploads to test.usegalaxy.be stalled without it, because ICMP "fragmentation needed" messages did not come back on that path.
- The connection to a real Galaxy, over RabbitMQ or the relay, has not been tested yet.
- A relay on the same VM as Pulsar (as on SURF Research Cloud, where there is no central relay) is not a placement option yet.
- `grycap.htcondor` lists Ubuntu 20.04 and 22.04 and EL. Ubuntu 24.04 is untested.
- SLURM on EL takes `slurm-drmaa` from the Galaxy depot. Only Ubuntu has been tested.
- HTCondor uses a shared pool password. Tokens would be better for a multi-site setup.
- The connection check covers the relay only. There is no check for RabbitMQ yet.
- The summary field names follow the BYOC form on usegalaxy.eu. They change if that form changes.
