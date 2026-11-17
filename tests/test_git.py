import subprocess

import pytest

from storage.git import GitError, MemoryGit


def run(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", check=True
    ).stdout.strip()


@pytest.fixture
def remote(tmp_path):
    path = tmp_path / "remote.git"
    run(tmp_path, "init", "--bare", "-b", "main", str(path))
    return path


@pytest.fixture
def mem(tmp_path):
    root = tmp_path / "memory"
    return root


def test_ensure_repo_creates_and_is_idempotent(mem):
    g = MemoryGit(mem)
    g.ensure_repo()
    g.ensure_repo()
    assert g.is_repo()
    assert run(mem, "config", "core.autocrlf") == "false"
    assert run(mem, "config", "user.name")  # identidade garantida


def test_commit_returns_sha_and_none_when_clean(mem):
    g = MemoryGit(mem)
    g.ensure_repo()
    (mem / "a.md").write_text("---\ndescription: x\n---\noi\n", encoding="utf-8")
    sha = g.commit("docs: adiciona a")
    assert sha and g.log(1) == [f"{sha} docs: adiciona a"]
    assert g.commit("nada mudou") is None
    assert len(g.log()) == 1


def test_commit_accepts_multiline_utf8_message(mem):
    g = MemoryGit(mem)
    g.ensure_repo()
    (mem / "a.md").write_text("x\n", encoding="utf-8")
    g.commit("docs: açaí\n\nCorpo da mensagem com acentuação")
    assert run(mem, "log", "-1", "--format=%B").startswith("docs: açaí")


def test_commit_rejects_empty_message(mem):
    g = MemoryGit(mem)
    g.ensure_repo()
    with pytest.raises(GitError):
        g.commit("   ")


def test_commit_and_push_to_remote(mem, remote):
    g = MemoryGit(mem, remote=str(remote))
    (mem.parent / "memory").mkdir(exist_ok=True)
    (mem / "a.md").write_text("oi\n", encoding="utf-8")
    result = g.commit_and_push("docs: primeiro")
    assert result.sha and result.pushed and result.push_error == ""
    assert run(remote, "log", "-1", "--format=%s", "main") == "docs: primeiro"


def test_push_failure_does_not_raise(mem, tmp_path):
    g = MemoryGit(mem, remote=str(tmp_path / "nao-existe.git"))
    mem.mkdir()
    (mem / "a.md").write_text("oi\n", encoding="utf-8")
    result = g.commit_and_push("docs: a")
    assert result.sha and not result.pushed and result.push_error
    assert g.log(1)  # commit local preservado


def test_no_remote_reports_error_without_raising(mem):
    g = MemoryGit(mem)
    mem.mkdir()
    (mem / "a.md").write_text("oi\n", encoding="utf-8")
    result = g.commit_and_push("docs: a")
    assert result.sha and not result.pushed and "MEMORY_GIT_REMOTE" in result.push_error


def test_commit_and_push_without_changes(mem, remote):
    g = MemoryGit(mem, remote=str(remote))
    mem.mkdir()
    (mem / "a.md").write_text("oi\n", encoding="utf-8")
    g.commit_and_push("docs: a")
    result = g.commit_and_push("docs: de novo")
    assert result.sha is None and not result.pushed


def test_remote_url_is_updated(mem, remote, tmp_path):
    MemoryGit(mem, remote=str(remote)).ensure_repo()
    other = tmp_path / "outro.git"
    MemoryGit(mem, remote=str(other)).ensure_repo()
    assert run(mem, "remote", "get-url", "origin") == str(other)


def test_configured_identity_overrides_existing(mem):
    g = MemoryGit(mem, user_name="Curador", user_email="curador@exemplo.com")
    g.ensure_repo()
    assert run(mem, "config", "user.name") == "Curador"
    assert run(mem, "config", "user.email") == "curador@exemplo.com"
    (mem / "a.md").write_text("oi\n", encoding="utf-8")
    g.commit("docs: a")
    assert run(mem, "log", "-1", "--format=%an <%ae>") == "Curador <curador@exemplo.com>"


def test_token_goes_to_env_only_and_not_to_config(mem, remote):
    g = MemoryGit(mem, remote=str(remote), token="ghp_segredo123")
    env = g._env()
    assert env["GIT_CONFIG_KEY_0"] == "http.extraHeader"
    assert "ghp_segredo123" not in env["GIT_CONFIG_VALUE_0"]  # vai em base64
    g.ensure_repo()
    assert "ghp_segredo123" not in (mem / ".git" / "config").read_text(encoding="utf-8")


def test_without_token_no_auth_header(mem):
    assert "GIT_CONFIG_KEY_0" not in MemoryGit(mem)._env() or "extraHeader" not in MemoryGit(mem)._env().get("GIT_CONFIG_KEY_0", "")


def test_token_is_redacted_from_errors(mem):
    g = MemoryGit(mem, token="ghp_segredo123")
    assert g._redact("erro com ghp_segredo123 aqui") == "erro com *** aqui"


def test_push_with_token_still_works_on_local_remote(mem, remote):
    g = MemoryGit(mem, remote=str(remote), token="ghp_segredo123")
    mem.mkdir()
    (mem / "a.md").write_text("oi\n", encoding="utf-8")
    assert g.commit_and_push("docs: a").pushed
