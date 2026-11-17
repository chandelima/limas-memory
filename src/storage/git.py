"""Memory versioning: the memory/ folder is its own git repository.

Uses the `git` binary through subprocess. A failed push NEVER raises: the
local commit already preserves the history and the push can be redone later.
"""

import base64
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

PUSH_TIMEOUT = 60  # seconds
_FALLBACK_NAME = "Memory Curator"
_FALLBACK_EMAIL = "memory-curator@localhost"
_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


class GitError(RuntimeError):
    """Failure in a local git operation (init, add, commit)."""


@dataclass
class CommitResult:
    sha: str | None  # None when there was nothing to commit
    pushed: bool
    push_error: str = ""


class MemoryGit:
    def __init__(
        self,
        root: Path,
        remote: str = "",
        user_name: str = "",
        user_email: str = "",
        token: str = "",
    ):
        self.root = Path(root).resolve()
        self.remote = remote
        self.user_name = user_name
        self.user_email = user_email
        self.token = token

    # ---- execution ---------------------------------------------------------

    def _env(self) -> dict:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}  # never hang asking for a password
        if self.token:
            # HTTPS authentication in memory only (via process environment variables):
            # the token never goes to argv, to the remote or to .git/config.
            basic = base64.b64encode(f"x-access-token:{self.token}".encode()).decode()
            env.update(
                GIT_CONFIG_COUNT="1",
                GIT_CONFIG_KEY_0="http.extraHeader",
                GIT_CONFIG_VALUE_0=f"Authorization: Basic {basic}",
            )
        return env

    def _redact(self, text: str) -> str:
        return text.replace(self.token, "***") if self.token else text

    def _git(self, *args: str, input: str | None = None, timeout: int | None = None):
        env = self._env()
        return subprocess.run(
            ["git", *args],
            cwd=self.root,
            input=input,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            env=env,
        )

    def _git_ok(self, *args: str, input: str | None = None) -> str:
        proc = self._git(*args, input=input)
        if proc.returncode != 0:
            detail = self._redact((proc.stderr or proc.stdout).strip())
            raise GitError(f"git {args[0]} failed: {detail}")
        return proc.stdout.strip()

    # ---- repository ------------------------------------------------------

    def is_repo(self) -> bool:
        return (self.root / ".git").exists()

    def ensure_repo(self) -> None:
        """Create the repo (main branch) if missing and configure identity and remote."""
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.is_repo():
            self._git_ok("init", "-b", "main")
        self._git_ok("config", "core.autocrlf", "false")  # memory is always LF
        # Identity: the configured one (env) wins; otherwise the existing one; otherwise a default.
        for key, wanted, fallback in (
            ("user.name", self.user_name, _FALLBACK_NAME),
            ("user.email", self.user_email, _FALLBACK_EMAIL),
        ):
            if wanted:
                self._git_ok("config", key, wanted)
            elif not self._git("config", key).stdout.strip():
                self._git_ok("config", key, fallback)
        if self.remote:
            current = self._git("remote", "get-url", "origin")
            if current.returncode != 0:
                self._git_ok("remote", "add", "origin", self.remote)
            elif current.stdout.strip() != self.remote:
                self._git_ok("remote", "set-url", "origin", self.remote)

    # ---- commit and push ----------------------------------------------------

    def commit(self, message: str) -> str | None:
        """Commit everything that changed. Return the short sha, or None if nothing changed."""
        message = message.strip()
        if not message:
            raise GitError("Empty commit message")
        self._git_ok("add", "-A")
        if self._git("diff", "--cached", "--quiet").returncode == 0:
            return None
        self._git_ok("commit", "-F", "-", input=message + "\n")
        return self._git_ok("rev-parse", "--short", "HEAD")

    def push(self) -> tuple[bool, str]:
        """(ok, error). Never raises."""
        if self._git("remote", "get-url", "origin").returncode != 0:
            return False, "No 'origin' remote configured (set MEMORY_GIT_REMOTE)"
        try:
            proc = self._git("push", "-u", "origin", "HEAD", timeout=PUSH_TIMEOUT)
        except subprocess.TimeoutExpired:
            return False, f"Push exceeded {PUSH_TIMEOUT}s"
        except OSError as exc:
            return False, self._redact(str(exc))
        if proc.returncode != 0:
            return False, self._redact((proc.stderr or proc.stdout).strip())
        return True, ""

    def commit_and_push(self, message: str) -> CommitResult:
        """Commit with the given message and try to push. No changes = no commit."""
        self.ensure_repo()
        sha = self.commit(message)
        if sha is None:
            return CommitResult(sha=None, pushed=False)
        pushed, error = self.push()
        return CommitResult(sha=sha, pushed=pushed, push_error=error)

    def sync(self, message: str) -> CommitResult:
        """Like `commit_and_push`, but tries the push even without new changes: it sends
        local commits that have not been pushed yet (e.g. remote configured later).
        "Everything up-to-date" counts as success."""
        self.ensure_repo()
        sha = self.commit(message)
        if self.head() is None:
            return CommitResult(sha=None, pushed=False)  # repo with no commits at all
        pushed, error = self.push()
        return CommitResult(sha=sha, pushed=pushed, push_error=error)

    def log(self, limit: int = 10) -> list[str]:
        """Latest commits ('sha subject'), newest first."""
        proc = self._git("log", f"-{limit}", "--format=%h %s")
        return proc.stdout.strip().splitlines() if proc.returncode == 0 else []

    def head(self) -> str | None:
        """Short sha of the latest commit, or None if there are no commits yet."""
        proc = self._git("rev-parse", "--short", "HEAD")
        return proc.stdout.strip() if proc.returncode == 0 else None

    def diff(self, since: str | None) -> str:
        """Changes (summary + patch) from `since` to HEAD; None = since the beginning."""
        proc = self._git("diff", "--stat", "-p", since or _EMPTY_TREE, "HEAD")
        return proc.stdout.strip() if proc.returncode == 0 else ""
