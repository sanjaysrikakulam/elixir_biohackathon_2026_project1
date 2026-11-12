"""Git plumbing for byoc-sync, on top of GitPython.

byoc-sync works directly in the checkout it is given (``--repo-path``, typically the one the CI job made), or in
its own clone (``--repo-url``). The checkout must have no uncommitted changes; byoc-sync checks out the base
branch and its own commits in it and switches back to the original branch at the end. Ignored files (such as a
local ``.vault_password``) are left alone.

The GitHub token reaches git through ``GIT_CONFIG_*`` environment variables (an HTTP auth header for
https://github.com/), so it never appears in a URL, on a command line or in ``.git/config``. That needs git 2.31.
"""

from __future__ import annotations

import base64
import shutil
from pathlib import Path

from git import Actor, GitCommandError, InvalidGitRepositoryError, NoSuchPathError, Repo


class GitError(RuntimeError):
    pass


def git_env(token: str | None) -> dict[str, str]:
    env = {"GIT_TERMINAL_PROMPT": "0"}
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env.update(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
            }
        )
    return env


class Workspace:
    def __init__(self, repo: Repo, base: str, bot: tuple[str, str], token: str | None, remote: str = "origin"):
        self.repo = repo
        self.path = Path(repo.working_dir)
        self.base = base
        self.bot = bot
        self.remote = remote
        self._token = token
        self._original = repo.head.ref.name if not repo.head.is_detached else repo.head.commit.hexsha

    def _scrub(self, text: str) -> str:
        if self._token:
            for secret in (self._token, git_env(self._token)["GIT_CONFIG_VALUE_0"].split()[-1]):
                text = text.replace(secret, "***")
        return text

    def _git(self, what: str, *args: str) -> str:
        try:
            return self.repo.git.execute(["git", *args])
        except GitCommandError as exc:
            stderr = exc.stderr if isinstance(exc.stderr, str) else str(exc.stderr or "")
            raise GitError(f"git {what} failed: {self._scrub(stderr.strip())}") from None

    # -- setup ------------------------------------------------------------------------------------------------

    @classmethod
    def open(
        cls,
        *,
        workdir: str | Path,
        base: str,
        bot: tuple[str, str],
        repo_path: str | Path | None = None,
        repo_url: str | None = None,
        token: str | None = None,
    ) -> Workspace:
        if bool(repo_path) == bool(repo_url):
            raise GitError("give exactly one of repo_path and repo_url")
        env = git_env(token)
        if repo_url:
            clone = Path(workdir).absolute() / "clone"
            repo = Repo(clone) if (clone / ".git").exists() else None
            if repo is not None and repo.remotes and repo.remotes.origin.url != repo_url:
                repo = None
            if repo is None:
                shutil.rmtree(clone, ignore_errors=True)
                clone.parent.mkdir(parents=True, exist_ok=True)
                try:
                    repo = Repo.clone_from(repo_url, clone, env=env)
                except GitCommandError as exc:
                    message = str(exc.stderr or "").strip()
                    raise GitError(
                        f"cannot clone {repo_url}: {message.replace(token, '***') if token else message}"
                    ) from None
        else:
            try:
                repo = Repo(Path(repo_path).absolute())
            except (InvalidGitRepositoryError, NoSuchPathError):
                raise GitError(f"{repo_path} is not a git repository") from None
            if repo.is_dirty(untracked_files=True):
                files = [line[3:] for line in repo.git.status("--porcelain").splitlines()][:10]
                raise GitError(
                    f"{repo_path} has uncommitted changes ({', '.join(files)}); byoc-sync needs a clean checkout"
                )
        if token and repo.git.version_info < (2, 31):
            raise GitError("git 2.31 or newer is needed to pass the GitHub token through the environment")
        repo.git.update_environment(**env)
        ws = cls(repo, base, bot, token)
        ws._git("fetch", "fetch", "-q", "--prune", ws.remote, f"+refs/heads/{base}:refs/remotes/{ws.remote}/{base}")
        return ws

    def close(self) -> None:
        """Switch the checkout back to where it was."""
        try:
            self._git("checkout", "checkout", "-q", "--force", self._original)
        except GitError:
            pass

    def __enter__(self) -> Workspace:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- operations -------------------------------------------------------------------------------------------

    def reset(self) -> None:
        """Make the checkout an exact copy of the base branch (ignored files are kept)."""
        ref = f"{self.remote}/{self.base}"
        self._git("checkout", "checkout", "-q", "--force", "--detach", ref)
        self._git("clean", "clean", "-q", "-fd")

    def base_sha(self) -> str:
        return self.repo.commit(f"{self.remote}/{self.base}").hexsha

    def remote_branch_sha(self, branch: str) -> str | None:
        out = self._git("ls-remote", "ls-remote", self.remote, f"refs/heads/{branch}")
        return out.split()[0] if out.strip() else None

    def fetch_branch(self, branch: str) -> str:
        """Fetch a remote branch's head without creating a ref for it."""
        self._git("fetch", "fetch", "-q", self.remote, f"refs/heads/{branch}")
        return self.repo.commit("FETCH_HEAD").hexsha

    def parent(self, sha: str) -> str | None:
        parents = self.repo.commit(sha).parents
        return parents[0].hexsha if parents else None

    def changed_paths(self, base: str, sha: str) -> set[str]:
        paths: set[str] = set()
        for change in self.repo.commit(base).diff(sha):
            paths.update(p for p in (change.a_path, change.b_path) if p)
        return paths

    def show(self, sha: str, path: str) -> bytes | None:
        try:
            return (self.repo.commit(sha).tree / path).data_stream.read()
        except KeyError:
            return None

    def commit(self, message: str, paths: list[str]) -> str:
        self.repo.index.add(paths)
        if not self.repo.index.diff("HEAD"):
            raise GitError("nothing to commit")
        actor = Actor(*self.bot)
        # The checkout's own hooks (pre-commit and the like) are not meant for generated commits.
        return self.repo.index.commit(message, author=actor, committer=actor, skip_hooks=True).hexsha

    def push(self, branch: str, expected: str | None) -> None:
        """Push HEAD to ``branch``, only if the remote branch is still at ``expected`` (None: absent)."""
        if branch == self.base:
            raise GitError(f"refusing to push to the base branch {branch!r}; it changes only through pull requests")
        lease = f"--force-with-lease=refs/heads/{branch}:{expected or ''}"
        self._git("push", "push", "-q", lease, self.remote, f"HEAD:refs/heads/{branch}")
