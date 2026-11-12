"""Command line interface: ``byoc-sync --help``.

A run: prepare the playbook checkout (or clone), open the vault (fails early on a wrong password), read the
BYOC form data from Galaxy, plan every user, print the summary and the diff, then (without ``--dry-run``)
publish branches and pull requests. Secrets are only read from the environment or from files, never from the
command line.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import git

from . import __version__
from .config import ConfigError, SiteConfig
from .github import GitHub, GitHubError
from .gitops import GitError, Workspace
from .planner import PlanError, PlanOptions, Repo, materialize, plan
from .publish import publish
from .report import diff, json_report, publish_summary, summary
from .sources.api import ApiSource, GalaxyApiError
from .sources.gxadmin import GxadminError, GxadminSource
from .vault import PASSWORD_ENV, VaultError, VaultFile, read_password
from .yamledit.doc import LocateError
from .yamledit.verify import VerificationError

FINGERPRINT_ENV = "BYOC_SYNC_FINGERPRINT_KEY"
BOT = ("byoc-sync", "byoc-sync@users.noreply.github.com")
EXPECTED_ERRORS = (
    ConfigError,
    GitError,
    GitHubError,
    PlanError,
    GalaxyApiError,
    GxadminError,
    VaultError,
    LocateError,
    VerificationError,
    ValueError,
    OSError,
)

EPILOG = f"""\
environment variables:
  GALAXY_API_KEY             Galaxy admin API key (name configurable with --api-key-env)
  GITHUB_TOKEN               token allowed to push branches and open pull requests (--github-token-env)
  {PASSWORD_ENV}   vault password, unless --vault-password-file is given
  {FINGERPRINT_ENV}  optional secret used to fingerprint requests in pull request bodies

exit status: 0 success, 1 error (nothing was published), 3 incomplete scan or a failed pull request
"""


class UsageError(Exception):
    pass


def _ranged(kind, low, high=None):
    """argparse type: a number within [low, high]."""

    def parse(text: str):
        try:
            value = kind(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
        if value < low or (high is not None and value > high):
            limit = f"between {low} and {high}" if high is not None else f"at least {low}"
            raise argparse.ArgumentTypeError(f"must be {limit}")
        return value

    return parse


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="byoc-sync",
        description="Create, update and remove BYOC Pulsar endpoints in an infrastructure playbook from the "
        "users' Galaxy preferences, as pull requests.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--version", action="version", version=f"byoc-sync {__version__}")

    g = p.add_argument_group("Galaxy")
    g.add_argument(
        "--source",
        choices=("api", "gxadmin"),
        default="api",
        help="read form data through the admin API (default) or with one gxadmin SQL query",
    )
    g.add_argument("--galaxy-url", help="Galaxy server URL (required for --source api)")
    g.add_argument(
        "--api-key-env",
        default="GALAXY_API_KEY",
        metavar="NAME",
        help="environment variable holding the admin API key (default: %(default)s)",
    )
    g.add_argument(
        "--gxadmin-cmd",
        default="gxadmin",
        metavar="CMD",
        help="gxadmin executable (default: %(default)s); needs database access from this host",
    )
    g.add_argument(
        "--concurrency", type=_ranged(int, 1), default=16, help="parallel API requests (default: %(default)s)"
    )
    g.add_argument(
        "--max-error-rate",
        type=_ranged(float, 0.0, 1.0),
        default=0.01,
        help="share of users that may fail to load before the run publishes nothing (default: %(default)s)",
    )
    g.add_argument(
        "--timeout", type=_ranged(float, 0.1), default=30, help="seconds per API request (default: %(default)s)"
    )
    g.add_argument("--retries", type=_ranged(int, 0), default=3, help="retries per API request (default: %(default)s)")

    r = p.add_argument_group("playbook repository")
    where = r.add_mutually_exclusive_group(required=True)
    where.add_argument(
        "--repo-path", type=Path, help="clean checkout to work in; byoc-sync switches back to its branch at the end"
    )
    where.add_argument("--repo-url", help="clone this URL into --workdir")
    r.add_argument("--workdir", type=Path, help="where --repo-url is cloned (default: a new temporary directory)")
    r.add_argument(
        "--site-config", default="eu", help="bundled site name or path to a site YAML (default: %(default)s)"
    )
    r.add_argument("--base-branch", help="branch pull requests target (default: from the site config)")
    r.add_argument("--github-repo", metavar="OWNER/REPO", help="repository to open pull requests in")
    r.add_argument(
        "--github-token-env",
        default="GITHUB_TOKEN",
        metavar="NAME",
        help="environment variable holding the GitHub token (default: %(default)s)",
    )
    r.add_argument("--github-api-url", default="https://api.github.com", help=argparse.SUPPRESS)

    v = p.add_argument_group("vault")
    v.add_argument(
        "--vault-file",
        help="vault holding the RabbitMQ passwords, relative to the repository (default: from the site config)",
    )
    v.add_argument("--vault-password-file", help=f"file with the vault password (default: ${PASSWORD_ENV})")

    b = p.add_argument_group("behaviour")
    b.add_argument("--dry-run", action="store_true", help="show what would change; push and publish nothing")
    b.add_argument(
        "--allow-delete",
        action="store_true",
        help="remove entries of users who cleared the form or were deleted, and apply renames",
    )
    b.add_argument(
        "--no-fallback",
        action="store_true",
        help="strict routing: jobs that do not fit a user's endpoint fail instead of running on the server",
    )
    b.add_argument("--single-pr", action="store_true", help="one pull request for all changes instead of one per user")
    b.add_argument(
        "--reopen-rejected",
        action="store_true",
        help="open pull requests again even if an identical one was closed without merging",
    )
    b.add_argument(
        "--allow-list", type=Path, metavar="FILE", help="only these e-mails (one per line) may get new endpoints"
    )
    b.add_argument("--validate-tpv", action="store_true", help="run 'tpv lint' on the edited TPV users file")
    b.add_argument("--tpv-cmd", default="tpv", help="TPV command for --validate-tpv (default: %(default)s)")

    o = p.add_argument_group("output")
    o.add_argument("--report-json", type=Path, metavar="FILE", help="write a machine readable report")
    o.add_argument("-q", "--quiet", action="store_true", help="do not print the diff")
    return p


def _env(name: str, what: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise UsageError(f"{what}: set the {name} environment variable")
    return value


def _allow_list(path: Path | None) -> set[str] | None:
    if path is None:
        return None
    lines = (line.split("#", 1)[0].strip() for line in path.read_text().splitlines())
    return {line for line in lines if line}


def _validate_tpv(cmd: str, users_text: str) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as handle:
        handle.write(users_text)
    try:
        proc = subprocess.run([*shlex.split(cmd), "lint", handle.name], capture_output=True, text=True)
    finally:
        os.unlink(handle.name)
    if proc.returncode != 0:
        raise PlanError(f"tpv lint failed on the edited users file:\n{(proc.stderr or proc.stdout).strip()[-2000:]}")


def run(ns: argparse.Namespace) -> int:
    site = SiteConfig.load(ns.site_config)
    if ns.base_branch:
        site = dataclasses.replace(site, base_branch=ns.base_branch)
    if ns.vault_file:
        site = dataclasses.replace(site, vault_file=ns.vault_file)
    if not ns.dry_run and not ns.github_repo:
        raise UsageError("--github-repo is required unless --dry-run is given")
    token = None if ns.dry_run else _env(ns.github_token_env, "GitHub token")
    gh = None if ns.dry_run else GitHub(ns.github_repo, token, api_url=ns.github_api_url)
    vault_password = read_password(ns.vault_password_file)
    if ns.source == "api":
        if not ns.galaxy_url:
            raise UsageError("--galaxy-url is required with --source api")
        source = ApiSource(
            ns.galaxy_url,
            _env(ns.api_key_env, "Galaxy admin API key"),
            section=site.section,
            concurrency=ns.concurrency,
            timeout=ns.timeout,
            retries=ns.retries,
            max_error_rate=ns.max_error_rate,
        )
    else:
        source = GxadminSource(shlex.split(ns.gxadmin_cmd), section=site.section)

    if gh is not None:
        _check_origin(ns)
    workdir = ns.workdir or Path(tempfile.mkdtemp(prefix="byoc-sync-"))
    try:
        return _run_in_workspace(ns, site, source, workdir, token, gh, vault_password)
    finally:
        if ns.workdir is None:
            shutil.rmtree(workdir, ignore_errors=True)


def _check_origin(ns: argparse.Namespace) -> None:
    """Branches are pushed to 'origin' (or --repo-url) and pull requests opened on --github-repo: same repository."""
    github_repo = ns.github_repo
    if ns.repo_url:
        url = ns.repo_url
    else:
        try:
            url = git.Repo(ns.repo_path).remotes.origin.url
        except (git.InvalidGitRepositoryError, git.NoSuchPathError, AttributeError, ValueError):
            return  # Workspace.open reports a missing repository or remote
    match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", url)
    if match and match.group(1).lower() != github_repo.lower():
        raise UsageError(f"origin ({match.group(1)}) does not match --github-repo ({github_repo})")


def _run_in_workspace(ns, site, source, workdir, token, gh, vault_password) -> int:
    with Workspace.open(
        repo_path=ns.repo_path, repo_url=ns.repo_url, workdir=workdir, base=site.base_branch, bot=BOT, token=token
    ) as ws:
        ws.reset()
        repo = Repo(ws.path, site)
        vault = VaultFile(ws.path / site.vault_file, vault_password)  # wrong password: stop before Galaxy
        scan = source.scan()
        options = PlanOptions(
            allow_delete=ns.allow_delete, strict=ns.no_fallback, allow_list=_allow_list(ns.allow_list)
        )
        run_plan = plan(scan, repo, source, vault, options)
        combined = materialize(run_plan.publishable(), repo, vault)

        print(summary(run_plan))
        if scan.errors:
            print(f"\n{len(scan.errors)} user(s) could not be read:", file=sys.stderr)
            for error in scan.errors[:50]:
                print(f"  {error}", file=sys.stderr)
        if not ns.quiet and combined.changed:
            print()
            print(diff(combined, site.vault_file))
        if ns.validate_tpv:
            users_file = site.targets["users"].file
            plan_users = combined.files.get(users_file)
            _validate_tpv(ns.tpv_cmd, plan_users.text if plan_users else repo.read(users_file))

        published = []
        status = 0
        if not scan.complete:
            print("\nscan incomplete: too many users could not be read; nothing is published", file=sys.stderr)
            status = 3
        elif not ns.dry_run:
            fingerprint_key = os.environ.get(FINGERPRINT_ENV, "").encode() or None
            if fingerprint_key is None:
                print(
                    f"warning: {FINGERPRINT_ENV} is not set, so the fingerprint in each pull request body is a plain "
                    "hash that includes the owner's e-mail; anyone who can read the pull request can check a guessed "
                    "address against it",
                    file=sys.stderr,
                )
            published = publish(
                run_plan,
                ws,
                gh,
                site,
                vault_password,
                mode="single" if ns.single_pr else "per-user",
                reopen_rejected=ns.reopen_rejected,
                fingerprint_key=fingerprint_key,
            )
            print()
            print(publish_summary(published))
            if any(r.outcome == "failed" for r in published):
                status = 3
        if ns.report_json:
            ns.report_json.write_text(
                json_report(scan=scan, run_plan=run_plan, result=combined, published=published, dry_run=ns.dry_run)
            )
        return status


def main(argv: list[str] | None = None) -> int:
    ns = build_parser().parse_args(argv)
    try:
        return run(ns)
    except UsageError as exc:
        print(f"byoc-sync: error: {exc}", file=sys.stderr)
        return 1
    except EXPECTED_ERRORS as exc:
        print(f"byoc-sync: error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
