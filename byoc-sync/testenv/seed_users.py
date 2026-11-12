"""Create test users in the throw-away Galaxy and fill in their BYOC form, as users would.

    python seed_users.py phase1   # initial requests
    python seed_users.py phase2   # changes after the phase-1 pull requests were merged

The form is saved through PUT /api/users/{id}/information/inputs, the endpoint Galaxy 26.1's
"Manage information" page uses. Expected byoc-sync actions are listed next to each user.
"""

from __future__ import annotations

import os
import sys

import requests

URL = os.environ.get("GALAXY_URL", "http://127.0.0.1:8089").rstrip("/")
KEY = os.environ.get("ADMIN_KEY", "byoc-sync-testenv-admin-key")
S = requests.Session()
S.headers["x-api-key"] = KEY


def byoc(name, password="MyPulS4rP4ssw0rd", cores="8", mem="31", min_gpus="0", max_gpus="0", nfs=""):
    return {
        "byoc_pulsar|username": name,
        "byoc_pulsar|password": password,
        "byoc_pulsar|max_accepted_cores": cores,
        "byoc_pulsar|max_accepted_mem": mem,
        "byoc_pulsar|min_accepted_gpus": min_gpus,
        "byoc_pulsar|max_accepted_gpus": max_gpus,
        "byoc_pulsar|nfs_path": nfs,
    }


CLEARED = {k: "" for k in byoc("x")}

PHASE1 = {
    "alice@example.org": byoc("alice01", nfs="/mnt/alice"),  # create
    "bob@example.org": byoc("bob01", cores="4", mem="15"),  # create
    "carol@example.org": byoc("carol01", password="short"),  # invalid: password
    "dave@example.org": {**CLEARED, "byoc_pulsar|username": "dave01"},  # invalid: incomplete form
    "frank@example.org": byoc("x{{7*7}}"),  # invalid: name
    "grace@example.org": byoc("grace01"),  # create
    "bjoern.gruening@gmail.com": byoc("bjoern01"),  # create, next to the hand-written entry
    "heidi@example.org": None,  # no request: nothing
}

PHASE2 = {
    "alice@example.org": byoc("alice01", password="NewPassw0rd123", nfs="/mnt/alice"),  # update: vault only
    "bob@example.org": byoc("bob01", cores="16", mem="60", max_gpus="1"),  # update: destination
    "eve@example.org": byoc("alice01"),  # conflict: owned name
    "bjoern.gruening@gmail.com": CLEARED,  # pending-delete / delete
}
DELETE_IN_PHASE2 = ["grace@example.org"]  # pending-delete / delete


def user_id(email: str) -> str:
    for deleted in ("false", "true"):
        for user in S.get(f"{URL}/api/users", params={"f_email": email, "deleted": deleted}).json():
            if user["email"] == email:
                return user["id"]
    response = S.post(
        f"{URL}/api/users",
        json={"email": email, "username": email.split("@")[0].replace(".", "-"), "password": "testpass123"},
    )
    response.raise_for_status()
    return response.json()["id"]


def save_form(email: str, values: dict | None) -> None:
    uid = user_id(email)
    if values is None:
        return
    response = S.put(f"{URL}/api/users/{uid}/information/inputs", json=values)
    if response.status_code != 200:
        sys.exit(f"{email}: saving the form failed: {response.status_code} {response.text}")
    print(f"{email}: form saved")


def admin_key() -> str:
    """A real admin account with its own API key: byoc-sync refuses keys that are not an admin user's."""
    uid = user_id("admin@example.org")
    response = S.post(f"{URL}/api/users/{uid}/api_key")
    response.raise_for_status()
    return response.json()


def main(phase: str) -> None:
    if phase == "admin-key":
        print(admin_key())
        return
    for email, values in {"phase1": PHASE1, "phase2": PHASE2}[phase].items():
        save_form(email, values)
    if phase == "phase2":
        for email in DELETE_IN_PHASE2:
            S.delete(f"{URL}/api/users/{user_id(email)}").raise_for_status()
            print(f"{email}: account deleted")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "phase1")
