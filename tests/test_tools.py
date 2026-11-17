import subprocess

import pytest

from curator.tools import build_tools
from storage.git import MemoryGit
from storage.store import MemoryStore

DOC = "---\ndescription: Preferências de café\n---\n# Café\nGosta de café sem açúcar.\n"


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "memory"
    root.mkdir()
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True)
    tools = {t.name: t for t in build_tools(MemoryStore(root), MemoryGit(root, remote=str(bare)))}
    return tools, root


def call(tools, name, **kwargs):
    return tools[name].invoke(kwargs)


def test_exposes_the_six_tools_with_descriptions(env):
    tools, _ = env
    assert set(tools) == {
        "list_memory", "search_memory", "read_memory",
        "write_memory", "update_index", "commit_memory",
    }
    assert all(t.description for t in tools.values())


def test_full_flow_write_search_read_index_commit(env):
    tools, root = env
    assert call(tools, "list_memory") == "A memória está vazia."
    assert call(tools, "write_memory", path="preferencias/cafe.md", content=DOC).startswith("OK")
    assert "preferencias/cafe.md — Preferências de café" in call(tools, "list_memory")
    assert "preferencias/cafe.md:5:" in call(tools, "search_memory", query="sem açúcar")
    assert call(tools, "read_memory", path="preferencias/cafe.md") == DOC
    assert "Nenhum link quebrado" in call(tools, "update_index")
    assert (root / "index.md").exists()
    assert "enviado ao remote" in call(tools, "commit_memory", message="docs: registra café")
    assert call(tools, "commit_memory", message="docs: nada") == "Nada para commitar."


def test_write_says_updated_on_overwrite(env):
    tools, _ = env
    call(tools, "write_memory", path="a.md", content=DOC)
    assert "atualizado" in call(tools, "write_memory", path="a.md", content=DOC)


def test_errors_become_text_not_exceptions(env):
    tools, _ = env
    assert call(tools, "write_memory", path="../x.md", content=DOC).startswith("ERRO")
    assert call(tools, "write_memory", path="a.md", content="sem frontmatter").startswith("ERRO")
    assert call(tools, "read_memory", path="nao-existe.md").startswith("ERRO")
    assert call(tools, "search_memory", query=" ").startswith("ERRO")
    assert call(tools, "commit_memory", message=" ").startswith("ERRO")


def test_update_index_reports_broken_links(env):
    tools, _ = env
    call(tools, "write_memory", path="a.md", content="---\ndescription: A\n---\n[x](b.md)\n")
    assert "links quebrados" in call(tools, "update_index")


def test_push_failure_is_reported_but_commit_kept(tmp_path):
    root = tmp_path / "memory"
    root.mkdir()
    tools = {t.name: t for t in build_tools(MemoryStore(root), MemoryGit(root))}  # sem remote
    call(tools, "write_memory", path="a.md", content=DOC)
    out = call(tools, "commit_memory", message="docs: a")
    assert "salvo localmente" in out and "MEMORY_GIT_REMOTE" in out


def _fill(tools, n, folder=""):
    for i in range(n):
        prefix = f"{folder}/" if folder else ""
        call(tools, "write_memory", path=f"{prefix}d{i:03}.md",
             content=f"---\ndescription: doc {i}\n---\nassunto comum {i}\n")


def test_list_memory_is_paginated_with_folders_and_footer(env):
    tools, _ = env
    _fill(tools, 55)
    _fill(tools, 3, "pessoas")
    p1 = call(tools, "list_memory")
    assert "Pastas: (raiz) (55), pessoas (3)" in p1
    assert "Página 1/2 (58 documentos). Use page=2 para continuar." in p1
    assert p1.count("\n- ") + p1.startswith("- ") == 50
    p2 = call(tools, "list_memory", page=2)
    assert "Página 2/2" in p2 and "continuar" not in p2 and "Pastas:" in p2


def test_list_memory_folder_filter_and_errors(env):
    tools, _ = env
    _fill(tools, 3, "pessoas")
    out = call(tools, "list_memory", folder="pessoas")
    assert "Pastas:" not in out and "Página 1/1 (3 documentos)." in out
    assert "Nenhum documento na pasta 'x'" in call(tools, "list_memory", folder="x")
    assert call(tools, "list_memory", page=0).startswith("ERRO")
    assert call(tools, "list_memory", folder="pessoas", page=2).startswith("ERRO")


def test_search_memory_warns_when_truncated(env):
    tools, _ = env
    _fill(tools, 25)
    out = call(tools, "search_memory", query="assunto comum")
    assert out.count("\n") == 20 and "há mais, refine a busca" in out
    assert "há mais" not in call(tools, "search_memory", query="assunto comum 1")
