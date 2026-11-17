import json
import tempfile

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

import compare
import config
from curator.agent import aggregate_usage, build_curator
from curator.tools import make_store_and_git

USAGE = {
    "input_tokens": 100, "output_tokens": 10, "total_tokens": 110,
    "input_token_details": {"cache_read": 20},
}

BEBIDAS = '---\ndescription: "Café sem açúcar"\n---\n# Bebidas\nToma café sem açúcar.\n'
PROJETO = '---\ndescription: "Projeto limas-memory"\n---\n# limas-memory\nSistema de memória.\n'


class ScriptedModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def msg(content="", calls=None):
    return AIMessage(
        content=content,
        tool_calls=[{"name": n, "args": a, "id": f"id{i}"} for i, (n, a) in enumerate(calls or [])],
        usage_metadata=USAGE,
    )


def script_for(scenario):
    if scenario.startswith("02"):
        return [
            msg(calls=[("list_memory", {})]),
            msg(calls=[("write_memory", {"path": "preferencias/bebidas.md", "content": BEBIDAS})]),
            msg(calls=[("write_memory", {"path": "projetos/limas-memory.md", "content": PROJETO})]),
            msg(calls=[("update_index", {})]),
            msg(calls=[("commit_memory", {"message": "docs: registra café e projeto"})]),
            msg("Criados 2 documentos."),
        ]
    return [msg("Nada com valor futuro.")]


def good_factory(model, tools, scenario):
    return build_curator(llm=ScriptedModel(responses=script_for(scenario)), tools=tools)


@pytest.fixture
def scenarios_dir(tmp_path):
    d = tmp_path / "scenarios_dir"
    d.mkdir()
    (d / "01_irrelevante.txt").write_text("usuario: bom dia\n", encoding="utf-8")
    (d / "02_fato_novo.txt").write_text("usuario: tomo café sem açúcar\n", encoding="utf-8")
    return d


@pytest.fixture(autouse=True)
def tmp_base(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))


def run_main(scenarios_dir, tmp_path, models, factory, runs=2):
    out = tmp_path / "cmp"
    code = compare.main(
        ["--models", models, "--runs", str(runs), "--scenarios", str(scenarios_dir), "--out", str(out)],
        factory=factory,
    )
    results = json.loads((tmp_path / "cmp-results.json").read_text(encoding="utf-8"))["results"]
    return code, results, (tmp_path / "cmp-report.md").read_text(encoding="utf-8")


def test_aggregate_usage_sums_tokens_cache_cost_and_flags_missing():
    full = AIMessage(
        content="a", usage_metadata={**USAGE, "output_token_details": {"reasoning": 4}},
        response_metadata={"token_usage": {"cost": 0.002}},
    )
    missing = AIMessage(content="b")
    u = aggregate_usage([full, full, missing])
    assert (u.input_tokens, u.output_tokens, u.total_tokens) == (200, 20, 220)
    assert (u.cache_read, u.reasoning, u.llm_calls, u.unreported_calls) == (40, 8, 3, 1)
    assert u.cost == pytest.approx(0.004) and u.cost_reported


def test_aggregate_usage_without_cost():
    u = aggregate_usage([AIMessage(content="a", usage_metadata=USAGE)])
    assert not u.cost_reported and u.cost == 0


def test_explicit_memory_dir_never_pushes(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_GIT_REMOTE", "https://github.com/x/y.git")
    monkeypatch.setattr(config, "MEMORY_GIT_TOKEN", "ghp_segredo")
    _, test_git = make_store_and_git(tmp_path / "teste")
    assert test_git.remote == "" and test_git.token == ""
    _, real_git = make_store_and_git(None)
    assert real_git.remote == "https://github.com/x/y.git" and real_git.token == "ghp_segredo"
    _, forced = make_store_and_git(tmp_path / "x", push=True)
    assert forced.remote == "https://github.com/x/y.git"


def test_end_to_end_runs_each_model_twice_with_isolated_memories(scenarios_dir, tmp_path):
    code, results, report = run_main(scenarios_dir, tmp_path, "fake/a,fake/b", good_factory)
    assert code == 0
    assert len(results) == 2 * 2 * 2  # modelos x rodadas x cenários
    assert all(not r["error"] and all(r["checks"].values()) for r in results)
    a = [r for r in results if r["model"] == "fake/a"]
    assert sum(r["usage"]["input_tokens"] for r in a) == 2 * (1 + 6) * 100
    assert sum(r["usage"]["cache_read"] for r in a) == 2 * (1 + 6) * 20
    assert "fake/a" in report and "fake/b" in report and "Nenhuma checagem falhou" in report
    assert "Custo n/d" in report
    assert len(list((tmp_path).glob("compare-*/fake_a-run*"))) == 2  # uma memória por rodada


def test_failing_check_is_reported(scenarios_dir, tmp_path):
    def sloppy(model, tools, scenario):
        # no cenário 01 grava algo indevido
        if scenario.startswith("01"):
            script = [
                msg(calls=[("write_memory", {"path": "lixo.md", "content": BEBIDAS})]),
                msg("Gravei."),
            ]
            return build_curator(llm=ScriptedModel(responses=script), tools=tools)
        return good_factory(model, tools, scenario)

    _, results, report = run_main(scenarios_dir, tmp_path, "fake/a", sloppy, runs=1)
    r01 = next(r for r in results if r["scenario"].startswith("01"))
    assert r01["checks"]["nao_gravou"] is False and r01["checks"]["sem_commit"] is True
    assert "- fake/a #1 01_irrelevante: nao_gravou" in report


def test_tool_errors_are_counted(scenarios_dir, tmp_path):
    def with_error(model, tools, scenario):
        if scenario.startswith("01"):
            script = [msg(calls=[("write_memory", {"path": "a.md", "content": "sem frontmatter"})]),
                      msg("Falhou.")]
            return build_curator(llm=ScriptedModel(responses=script), tools=tools)
        return good_factory(model, tools, scenario)

    _, results, _ = run_main(scenarios_dir, tmp_path, "fake/a", with_error, runs=1)
    r01 = next(r for r in results if r["scenario"].startswith("01"))
    assert r01["tool_calls"] == 1 and r01["tool_errors"] == 1
    assert r01["checks"]["sem_erro_de_tool"] is False


def test_model_error_is_recorded_and_run_continues(scenarios_dir, tmp_path):
    def flaky(model, tools, scenario):
        if model == "fake/bad":
            raise RuntimeError("400: temperature não suportado")
        return good_factory(model, tools, scenario)

    code, results, report = run_main(scenarios_dir, tmp_path, "fake/bad,fake/a", flaky, runs=1)
    assert code == 0
    bad = [r for r in results if r["model"] == "fake/bad"]
    assert len(bad) == 2 and all("temperature" in r["error"] and r["checks"] == {} for r in bad)
    assert all(all(r["checks"].values()) for r in results if r["model"] == "fake/a")
    assert "Execuções com erro" in report and "ERRO" in report


def test_scenario_04_and_05_checks():
    # a lógica de texto das checagens, sem agente: tolera acentos e maiúsculas
    assert compare.norm("CAFÉ Açúcar HIPÓTESE") == "cafe acucar hipotese"
    assert compare._has("Toma Café sem Açúcar", "cafe", "acucar")


def test_requires_api_key_for_real_runs(scenarios_dir, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "")
    code = compare.main(["--models", "x/y", "--scenarios", str(scenarios_dir), "--out", str(tmp_path / "c")])
    assert code == 1 and "OPENROUTER_API_KEY" in capsys.readouterr().err


def test_rejects_empty_inputs(tmp_path, capsys):
    empty = tmp_path / "vazio"
    empty.mkdir()
    assert compare.main(["--scenarios", str(empty)], factory=good_factory) == 2
