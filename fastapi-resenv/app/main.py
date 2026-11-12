import json
import logging
import os
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader
from datetime import datetime

from app.slurm_info import get_slurm_summary

# Configure a minimal, structured logger.  The app is unauthenticated and
# relies on the reverse proxy for access control, so we keep logging lean but
# useful for operations (uvicorn already logs each request line).
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("pulsar.resenv")

# Resolve paths relative to the repository root so the app behaves the same
# regardless of the current working directory (e.g. when run behind a
# reverse proxy such as OpenResty).
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Setup Jinja2 for templating
env = Environment(loader=FileSystemLoader(os.path.join(BASE_DIR, "templates")))

app = FastAPI(title="PULSAR BYOC Information Service")

# Serve static assets (CSS/JS) from /static/...
app.mount(
    "/static",
    StaticFiles(directory=os.path.join(BASE_DIR, "static")),
    name="static",
)

STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")

# Required keys that must be present inside "pulsar_config" for the
# configuration to be considered complete and rendered on the dashboard.
REQUIRED_CONFIG_KEYS = ("username", "password", "max_cpus", "max_ram_gib", "max_gpus")

# Service states.  UNAVAILABLE is reported when no valid state.json is present
# yet (i.e. the configuration has not been provisioned), in contrast to
# INCOMPLETE which is an explicit state stored in state.json.
STATE_UNAVAILABLE = "UNAVAILABLE"

# Bind address / port.  The app performs no authentication of its own; access
# control is provided by the FORC/OpenResty reverse proxy, which proxies the
# backend to this host:port.  The defaults match a local/standalone setup and
# are normally overridden per environment (e.g. by the systemd unit or the
# resenv image build):
#   PULSAR_RESENV_HOST - interface to bind (default 127.0.0.1)
#   PULSAR_RESENV_PORT - port to listen on (default 8080, matches the resenv)
BIND_HOST = os.environ.get("PULSAR_RESENV_HOST", "127.0.0.1")
BIND_PORT = int(os.environ.get("PULSAR_RESENV_PORT", "8080"))


def load_state():
    """Load the service state from state.json.

    Returns a tuple of (pulsar_config, service_state).

    - When state.json is missing or cannot be read, (None, "UNAVAILABLE") is
      returned so the template shows the "configuration not available yet"
      notice.
    - When the file is present but its "pulsar_config" is absent or incomplete,
      the stored (or default) service_state is returned, but pulsar_config is
      set to None so the configuration cards are not rendered.
    """
    if not os.path.exists(STATE_FILE):
        logger.warning("state file %s not found; serving UNAVAILABLE view", STATE_FILE)
        return None, STATE_UNAVAILABLE

    try:
        with open(STATE_FILE, "r") as f:
            state = json.load(f)
    except (json.JSONDecodeError, IOError) as exc:
        # A state file that exists but is broken is treated the same as a
        # missing one: no configuration data can be shown.
        logger.warning("could not read state file %s: %s", STATE_FILE, exc)
        return None, STATE_UNAVAILABLE

    service_state = state.get("service_state", "OPERATIONAL")
    pulsar_config = state.get("pulsar_config")

    # Even when the configuration is present it must carry all rendered
    # values; otherwise the cards are not rendered.
    if not isinstance(pulsar_config, dict) or any(
        key not in pulsar_config for key in REQUIRED_CONFIG_KEYS
    ):
        return None, service_state

    return pulsar_config, service_state


@app.get("/", response_class=HTMLResponse)
async def root():
    """Display the details needed to register the cluster with Galaxy (PULSAR)."""
    template = env.get_template("index.html")

    pulsar_config, service_state = load_state()
    slurm_summary = get_slurm_summary()
    current_year = datetime.now().year

    content = template.render(
        title="PULSAR BYOC - Connect your cluster to Galaxy",
        welcome_message="PULSAR BYOC",
        description="Information to register your Slurm cluster with Galaxy (bring your own compute)",
        current_year=current_year,
        pulsar_config=pulsar_config,
        service_state=service_state,
        slurm_summary=slurm_summary,
    )
    return HTMLResponse(content=content)


@app.get("/health")
async def health_check():
    """Health check endpoint for monitoring."""
    return {"status": "healthy", "service": "pulsar-BYOC-info"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=BIND_HOST,
        port=BIND_PORT,
        # reload=False is required in production; --reload spawns a watcher
        # process that is unsuitable for a supervised (systemd) service.
        reload=False,
    )
