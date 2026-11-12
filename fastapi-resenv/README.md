# PULSAR BYOC resenv information service

Webservice that shows the information needed to **connect a Slurm cluster in
SimpleVM to Galaxy** via [PULSAR](https://galaxyproject.org/cloudman/services/pulsar/)
(Galaxy's "bring your own compute" / remote job execution mechanism, instead
of a dedicated Galaxy cloud site).

PULSAR is a standalone light-weight server that handles remote Galaxy job
execution and exchanges the requisite provenance information. This resenv
displays, for the user's own cluster, the details required to register and
point Galaxy at it:

- The **Galaxy (PULSAR) credentials** to use.
- The **resource limits** (max CPUs, RAM, GPUs) the user is allowed to use.
- When running on a Slurm node, the **live cluster capacity** retrieved from
  Slurm (`sinfo`) - the single-node maximum and the total across all nodes.

It is deployed to **per-user machines** behind the FORC/OpenResty reverse
proxy that performs authentication/authorization.

> **Security model:** the FastAPI app performs **no authentication**. The
> FORC/OpenResty reverse proxy is the sole security gate and proxies to the
> app. The Galaxy password lives in `app/state.json`, which is **never
> committed** and is written per host (mode `0640`) by the resenv provisioning.

## Local development

```bash
python3 -m venv vendor
source vendor/bin/activate
pip install -r requirements.txt

# Create a local state file (git-ignored) - copy the example:
cp app/state.json.example app/state.json   # then edit the values

python -m app.main        # http://127.0.0.1:8080
```

The "Retrieved from Slurm" section only appears where `sinfo` is available.

## Endpoints

- `GET /` - Main HTML page
- `GET /health` - Health check
- `GET /docs` - Swagger UI (auto-generated)

## Production deployment (SimpleVM / FORC)

The app is deployed as a **resenv** in the [`resenvs`](https://github.com/SimpleVM/resenvs)
repository (`resenvs/pulsar/`), which builds a prebuilt image with the app
installed as a supervised `systemd` service on port **8080**.

**Access control is provided by the FORC/OpenResty reverse proxy**, not by the
app. The FORC backend template
[`pulsar%v01.conf`](https://github.com/deNBI/simpleVMWebGateway/blob/master/examples/templates/pulsar%v01.conf)
in the public [`deNBI/simpleVMWebGateway`](https://github.com/deNBI/simpleVMWebGateway)
repository performs openidc authentication + owner-only authorization and
proxies the backend (stripping the `/<key_url>/` prefix) to the app. The app
performs no authentication of its own.

```
FORC/OpenResty (openidc + owner auth)
   └─ rewrite ^/<key_url>/(.*)$ /$1
      └─ proxy_pass -> <backend>:8080  ->  uvicorn (systemd, pulsar-resenv)
```

### Per-user state

The real Galaxy credentials are per-user and are **not** baked into the image.
At provisioning time, write `app/state.json` (mode `0640`) with the user's
values, then set `service_state` to `OPERATIONAL`. Until then the service
shows the `INCOMPLETE`/`UNAVAILABLE` view.

## Project structure

This repository is a **pure application** - it contains no deployment or
provisioning code. All Ansible provisioning lives in the
[`resenvs`](https://github.com/SimpleVM/resenvs) repository
(`resenvs/pulsar/`).

```
├── app/
│   ├── main.py               # FastAPI app (env-configurable bind, state loading)
│   ├── slurm_info.py         # sinfo-based cluster capacity (optional)
│   ├── state.json.example    # committed template (real state.json is ignored)
│   └── state.json            # per-host secret (git-ignored, resenv-managed)
├── templates/
│   └── index.html            # Main HTML template
├── requirements.txt
└── README.md
```

> The FORC backend template lives in the public
> [`deNBI/simpleVMWebGateway`](https://github.com/deNBI/simpleVMWebGateway)
> repository: `examples/templates/pulsar%v01.conf`.
