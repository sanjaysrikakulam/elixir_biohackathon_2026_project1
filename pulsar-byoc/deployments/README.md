# BioHackathon 2026 test deployments

This folder records what we deployed on SURF Research Cloud and de.NBI SimpleVM during the ELIXIR BioHackathon Europe 2026. Both ran the playbook at commit `b33f972` on a single Ubuntu 22.04 VM that is both the Pulsar host and the worker, with SLURM as the scheduler. Addresses, usernames and secrets are placeholders.

Both VMs used placeholder message queue addresses (`relay.invalid`, `mq.invalid`), so every install step ran but Pulsar did not connect to a Galaxy. The connection to test.usegalaxy.be over RabbitMQ is the next step.

## SURF Research Cloud

A plain Ubuntu 22.04 workspace with 4 cores and 32 GB. SLURM, with Pulsar in relay mode and a placeholder relay.

```bash
ansible-playbook -i deployments/surf-research-cloud/inventory.ini site.yml -e @secrets.yml \
  -e pulsar_scheduler=slurm -e pulsar_connection=relay -e pulsar_relay_placement=own \
  -e pulsar_nfs_network=192.168.0.0/16
```

`secrets.yml` set `pulsar_relay_url: https://relay.invalid`, a placeholder `pulsar_relay_username` and random passwords. The run ended with 85 tasks ok and none failed. A test job submitted with `sbatch` as the `pulsar` user ran and wrote to `/data/share/staging`. The summary reported 4 cores and 28 GB.

## de.NBI SimpleVM

A "de.NBI default" VM with 2 cores and 4 GB. SLURM, with Pulsar in RabbitMQ mode and a placeholder queue.

```bash
ansible-playbook -i deployments/denbi-simplevm/inventory.ini site.yml -e @secrets.yml \
  -e pulsar_scheduler=slurm -e pulsar_connection=mq -e pulsar_nfs_network=192.168.1.0/24
```

`secrets.yml` set `pulsar_message_queue_url: amqps://byoc_bh_denbi:<random>@mq.invalid:5671//pulsar/byoc_bh_denbi`. The run ended with 91 tasks ok and none failed. A test job ran as the `pulsar` user, and Pulsar's RabbitMQ consumers for job setup, status and kill started.

A second SimpleVM VM ("de.NBI small", 8 cores, 16 GB) was meant as a separate worker. The two VMs could only reach each other over SSH, so the multi-VM setup waits until SimpleVM opens the SLURM, HTCondor and NFS ports between them.
