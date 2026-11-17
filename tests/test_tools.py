import subprocess

import pytest

from curator.tools import build_tools
from storage.git import MemoryGit
from storage.store import MemoryStore

DOC = "---\ndescription: Coffee preferences\n---\n# Coffee\nLikes coffee without sugar.\n"


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
    assert call(tools, "list_memory") == "The memory is empty."
    assert call(tools, "write_memory", path="preferences/coffee.md", content=DOC).startswith("OK")
    assert "preferences/coffee.md — Coffee preferences" in call(tools, "list_memory")
    assert "preferences/coffee.md:5:" in call(tools, "search_memory", query="without sugar")
    assert call(tools, "read_memory", path="preferences/coffee.md") == DOC
    assert "No broken links" in call(tools, "update_index")
    assert (root / "index.md").exists()
    assert "sent to the remote" in call(tools, "commit_memory", message="docs: record coffee")
    assert call(tools, "commit_memory", message="docs: nothing") == "Nothing to commit."


def test_write_says_updated_on_overwrite(env):
    tools, _ = env
    call(tools, "write_memory", path="a.md", content=DOC)
    assert "updated" in call(tools, "write_memory", path="a.md", content=DOC)


def test_errors_become_text_not_exceptions(env):
    tools, _ = env
    assert call(tools, "write_memory", path="../x.md", content=DOC).startswith("ERROR")
    assert call(tools, "write_memory", path="a.md", content="no frontmatter").startswith("ERROR")
    assert call(tools, "read_memory", path="missing.md").startswith("ERROR")
    assert call(tools, "search_memory", query=" ").startswith("ERROR")
    assert call(tools, "commit_memory", message=" ").startswith("ERROR")


def test_update_index_reports_broken_links(env):
    tools, _ = env
    call(tools, "write_memory", path="a.md", content="---\ndescription: A\n---\n[x](b.md)\n")
    assert "broken links" in call(tools, "update_index")


def test_push_failure_is_reported_but_commit_kept(tmp_path):
    root = tmp_path / "memory"
    root.mkdir()
    tools = {t.name: t for t in build_tools(MemoryStore(root), MemoryGit(root))}  # no remote
    call(tools, "write_memory", path="a.md", content=DOC)
    out = call(tools, "commit_memory", message="docs: a")
    assert "saved locally" in out and "MEMORY_GIT_REMOTE" in out


def _fill(tools, n, folder=""):
    for i in range(n):
        prefix = f"{folder}/" if folder else ""
        call(tools, "write_memory", path=f"{prefix}d{i:03}.md",
             content=f"---\ndescription: doc {i}\n---\ncommon subject {i}\n")


def test_list_memory_is_paginated_with_folders_and_footer(env):
    tools, _ = env
    _fill(tools, 55)
    _fill(tools, 3, "people")
    p1 = call(tools, "list_memory")
    assert "Folders: (root) (55), people (3)" in p1
    assert "Page 1/2 (58 documents). Use page=2 to continue." in p1
    assert p1.count("\n- ") + p1.startswith("- ") == 50
    p2 = call(tools, "list_memory", page=2)
    assert "Page 2/2" in p2 and "continue" not in p2 and "Folders:" in p2


def test_list_memory_folder_filter_and_errors(env):
    tools, _ = env
    _fill(tools, 3, "people")
    out = call(tools, "list_memory", folder="people")
    assert "Folders:" not in out and "Page 1/1 (3 documents)." in out
    assert "No documents in folder 'x'" in call(tools, "list_memory", folder="x")
    assert call(tools, "list_memory", page=0).startswith("ERROR")
    assert call(tools, "list_memory", folder="people", page=2).startswith("ERROR")


def test_search_memory_warns_when_truncated(env):
    tools, _ = env
    _fill(tools, 25)
    out = call(tools, "search_memory", query="common subject")
    assert out.count("\n") == 20 and "there are more, refine the search" in out
    assert "there are more" not in call(tools, "search_memory", query="common subject 1")
