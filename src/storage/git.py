"""Versionamento da memória: a pasta memory/ é o seu próprio repositório git.

Usa o binário `git` via subprocess. Falha de push NUNCA levanta exceção: o
commit local já preserva o histórico e o push pode ser refeito depois.
"""

import base64
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

PUSH_TIMEOUT = 60  # segundos
_FALLBACK_NAME = "Memory Curator"
_FALLBACK_EMAIL = "memory-curator@localhost"
_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


class GitError(RuntimeError):
    """Falha em operação local do git (init, add, commit)."""


@dataclass
class CommitResult:
    sha: str | None  # None quando não havia mudanças
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

    # ---- execução ---------------------------------------------------------

    def _env(self) -> dict:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}  # nunca travar pedindo senha
        if self.token:
            # Autenticação HTTPS só em memória (via variáveis de ambiente do processo):
            # o token não vai para argv, para o remote nem para o .git/config.
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
            raise GitError(f"git {args[0]} falhou: {detail}")
        return proc.stdout.strip()

    # ---- repositório ------------------------------------------------------

    def is_repo(self) -> bool:
        return (self.root / ".git").exists()

    def ensure_repo(self) -> None:
        """Cria o repo (branch main) se não existir e configura identidade e remote."""
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.is_repo():
            self._git_ok("init", "-b", "main")
        self._git_ok("config", "core.autocrlf", "false")  # memória é sempre LF
        # Identidade: a configurada (env) vence; senão a já existente; senão um padrão.
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

    # ---- commit e push ----------------------------------------------------

    def commit(self, message: str) -> str | None:
        """Commita tudo que mudou. Devolve o sha curto, ou None se nada mudou."""
        message = message.strip()
        if not message:
            raise GitError("Mensagem de commit vazia")
        self._git_ok("add", "-A")
        if self._git("diff", "--cached", "--quiet").returncode == 0:
            return None
        self._git_ok("commit", "-F", "-", input=message + "\n")
        return self._git_ok("rev-parse", "--short", "HEAD")

    def push(self) -> tuple[bool, str]:
        """(ok, erro). Nunca levanta exceção."""
        if self._git("remote", "get-url", "origin").returncode != 0:
            return False, "Sem remote 'origin' configurado (defina MEMORY_GIT_REMOTE)"
        try:
            proc = self._git("push", "-u", "origin", "HEAD", timeout=PUSH_TIMEOUT)
        except subprocess.TimeoutExpired:
            return False, f"Push excedeu {PUSH_TIMEOUT}s"
        except OSError as exc:
            return False, self._redact(str(exc))
        if proc.returncode != 0:
            return False, self._redact((proc.stderr or proc.stdout).strip())
        return True, ""

    def commit_and_push(self, message: str) -> CommitResult:
        """Commit com a mensagem dada e tentativa de push. Sem mudanças = sem commit."""
        self.ensure_repo()
        sha = self.commit(message)
        if sha is None:
            return CommitResult(sha=None, pushed=False)
        pushed, error = self.push()
        return CommitResult(sha=sha, pushed=pushed, push_error=error)

    def log(self, limit: int = 10) -> list[str]:
        """Últimos commits ('sha assunto'), mais recentes primeiro."""
        proc = self._git("log", f"-{limit}", "--format=%h %s")
        return proc.stdout.strip().splitlines() if proc.returncode == 0 else []

    def head(self) -> str | None:
        """Sha curto do último commit, ou None se ainda não há commits."""
        proc = self._git("rev-parse", "--short", "HEAD")
        return proc.stdout.strip() if proc.returncode == 0 else None

    def diff(self, since: str | None) -> str:
        """Mudanças (resumo + patch) de `since` até HEAD; None = desde o início."""
        proc = self._git("diff", "--stat", "-p", since or _EMPTY_TREE, "HEAD")
        return proc.stdout.strip() if proc.returncode == 0 else ""
