"""Retrieval of live cluster resource information from Slurm.

The information is gathered by invoking the ``sinfo`` command line tool and
parsing its output.  This is intentionally dependency free: it works on any
node that has Slurm installed (i.e. ``sinfo`` on the ``PATH``) and degrades
gracefully to ``None`` everywhere else, so the web app can be run on a plain
development machine without Slurm at all.

The result is a small, JSON friendly dictionary that the template renders in
the "Retrieved from Slurm" section.
"""

import logging
import re
import shutil
import subprocess

logger = logging.getLogger(__name__)

# Fields requested from sinfo, in order:
#   %n  node name(s)   (collapsed with + for identical nodes)
#   %c  total CPUs per node
#   %m  total memory per node in MB
#   %G  GRES specification per node, e.g. "gpu:a100:2" or "none"
SINFO_FORMAT = "%n %c %m %G"
SINFO_TIMEOUT_SECONDS = 10


def _parse_gres(gres_field):
    """Extract the total number of GPUs from a sinfo %G field.

    A GRES field looks like ``gpu:a100:2`` (2 A100 GPUs), ``gpu:2`` or
    ``none`` / an empty string for nodes without GRES.  Multiple comma
    separated entries (e.g. ``gpu:a100:2,nvme:4``) are supported and only the
    GPU entries are summed.
    """
    if not gres_field:
        return 0

    total = 0
    for entry in gres_field.split(","):
        entry = entry.strip()
        if not entry or entry.lower() == "none":
            continue

        parts = entry.split(":")
        kind = parts[0].strip().lower()
        if kind != "gpu":
            continue

        # The quantity is either the last part ("gpu:a100:2" -> 2) or the
        # second part ("gpu:2" -> 2).  A missing quantity means a single one.
        if len(parts) >= 3:
            count = parts[-1]
        elif len(parts) == 2:
            count = parts[1]
        else:
            count = "1"

        count = count.strip()
        if count.isdigit():
            total += int(count)
        else:
            # e.g. "gpu:2" where a non numeric suffix slipped in; count as one
            total += 1

    return total


def _to_int(value, default=0):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _parse_nodes(output):
    """Parse the output of ``sinfo -o "<SINFO_FORMAT>"`` into a list of nodes.

    Returns a list of dicts with keys ``name``, ``cpus``, ``ram_mb`` and
    ``gpus``.  Collapsed node lines such as ``node[01-08]`` are expanded into
    one entry per node.  Returns an empty list when nothing could be parsed.
    """
    nodes = []

    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.lower().startswith("partition"):
            continue

        # sinfo prints one node line per row: "<names> <cpus> <ram_mb> <gres>"
        parts = line.split()
        if len(parts) < 4:
            continue

        name_field, cpus_field, ram_field, gres_field = parts[0], parts[1], parts[2], " ".join(parts[3:])
        cpus = _to_int(cpus_field)
        ram_mb = _to_int(ram_field)
        gpus = _parse_gres(gres_field)

        # Expand a collapsed range: "node[01-04]" -> node01..node04
        m = re.match(r"^(?P<prefix>.+?)\[(?P<start>\d+)-(?P<end>\d+)\]$", name_field)
        if m:
            prefix = m.group("prefix")
            start = int(m.group("start"))
            end = int(m.group("end"))
            width = len(m.group("start"))
            for i in range(start, end + 1):
                nodes.append({
                    "name": f"{prefix}{i:0{width}d}",
                    "cpus": cpus,
                    "ram_mb": ram_mb,
                    "gpus": gpus,
                })
        else:
            nodes.append({
                "name": name_field,
                "cpus": cpus,
                "ram_mb": ram_mb,
                "gpus": gpus,
            })

    return nodes


def get_slurm_summary():
    """Query Slurm and summarise the available cluster resources.

    Returns ``None`` if Slurm is not available (``sinfo`` missing, the command
    fails, or the output cannot be parsed), so callers can simply hide the
    section.  On success it returns::

        {
            "node_count": int,          # number of nodes considered
            "max_cpus": int,            # largest CPU count of any single node
            "max_ram_gib": float,       # largest memory of any single node
            "max_gpus": int,            # largest GPU count of any single node
            "total_cpus": int,          # sum of CPUs across all nodes
            "total_ram_gib": float,     # sum of memory across all nodes
            "total_gpus": int,          # sum of GPUs across all nodes
        }

    Every node reported by sinfo is considered (per the intended semantics of
    "single node maximum" and "sum of all").
    """
    if shutil.which("sinfo") is None:
        logger.debug("sinfo not found on PATH; Slurm section hidden")
        return None

    try:
        result = subprocess.run(
            ["sinfo", "-o", SINFO_FORMAT],
            capture_output=True,
            text=True,
            timeout=SINFO_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Failed to run sinfo: %s", exc)
        return None

    if result.returncode != 0:
        logger.warning(
            "sinfo exited with %s: %s", result.returncode, result.stderr.strip()
        )
        return None

    nodes = _parse_nodes(result.stdout)
    if not nodes:
        logger.warning("sinfo returned no parsable node lines")
        return None

    max_cpus = max(n["cpus"] for n in nodes)
    max_ram_gib = round(max(n["ram_mb"] for n in nodes) / 1024, 1)
    max_gpus = max(n["gpus"] for n in nodes)

    total_cpus = sum(n["cpus"] for n in nodes)
    total_ram_gib = round(sum(n["ram_mb"] for n in nodes) / 1024, 1)
    total_gpus = sum(n["gpus"] for n in nodes)

    return {
        "node_count": len(nodes),
        "max_cpus": max_cpus,
        "max_ram_gib": max_ram_gib,
        "max_gpus": max_gpus,
        "total_cpus": total_cpus,
        "total_ram_gib": total_ram_gib,
        "total_gpus": total_gpus,
    }
