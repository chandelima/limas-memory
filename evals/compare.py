"""Compara modelos no Memory Curator: roda os cenários de evals/scenarios/ várias vezes por
modelo, mede tokens/tempo/chamadas e confere checagens objetivas sobre a memória gerada.

Uso:
    python evals/compare.py --models openai/gpt-6-luna,anthropic/claude-haiku-5.5 --runs 2

Cada (modelo, rodada) usa uma memória temporária NOVA e isolada, sem remote (nunca faz
push). Os cenários rodam em ordem dentro da mesma memória, pois o 03 e o 04 dependem do
que o 02 gravou. Saídas: compare-report.md (resumo) e compare-results.json (dados brutos).
As checagens pegam só erros objetivos; a qualidade da escrita continua sendo julgada
lendo as memórias, cujos caminhos são impressos ao final.
"""

import argparse
import json
import re
import sys
import tempfile
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

# Este script vive em evals/, fora de src/: garante que os módulos de src/ sejam importáveis.
_SRC = str(Path(__file__).resolve().parent.parent / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import config  # noqa: E402
from curate import parse_conversation  # noqa: E402
from curator.agent import Usage, build_curator, curate  # noqa: E402
from curator.tools import build_tools, make_store_and_git  # noqa: E402
from storage import index as idx  # noqa: E402
from storage.store import MemoryStore  # noqa: E402

CONTEST_MARKERS = ("contest", "em revis", "contradit", "contradic", "conflit", "nao confirm", "em aberto", "nao resolvid")


def norm(text: str) -> str:
    """Minúsculas e sem acentos, para checagens tolerantes."""
    decomposed = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn")


@dataclass
class ScenarioRun:
    model: str
    run: int
    scenario: str
    seconds: float = 0.0
    tool_calls: int = 0
    tool_errors: int = 0
    committed: bool = False
    usage: Usage = field(default_factory=Usage)
    checks: dict[str, bool] = field(default_factory=dict)
    error: str = ""  # exceção da execução (ex.: erro 400 da API); sem checagens


# ---- checagens ---------------------------------------------------------------------


@dataclass
class Snapshot:
    store: MemoryStore
    docs: dict[str, str]  # caminho -> texto (inclui index.md)

    @classmethod
    def take(cls, store: MemoryStore) -> "Snapshot":
        return cls(store, {p: store.read_doc(p) for p in store.list_paths()})

    def content_docs(self) -> dict[str, str]:
        return {p: t for p, t in self.docs.items() if p != idx.INDEX_PATH}


def _has(text: str, *needles: str) -> bool:
    n = norm(text)
    return all(norm(x) in n for x in needles)


def run_checks(key: str, before: Snapshot, after: Snapshot, run: ScenarioRun, now: datetime) -> dict:
    new = {p: t for p, t in after.content_docs().items() if p not in before.content_docs()}
    all_text = "\n".join(after.content_docs().values())
    committed = run.committed

    checks = {
        "sem_erro_de_tool": run.tool_errors == 0,
        "frontmatter_valido": all(d.description for d in after.store.list_docs()),
        "sem_link_quebrado": not idx.find_broken_links(after.store),
        "sem_telegram_inventado": "telegram" not in norm("\n".join(after.docs.values())),
    }

    if key == "01":
        checks["nao_gravou"] = after.docs == before.docs
        checks["sem_commit"] = not committed
    elif key == "02":
        index_text = after.docs.get(idx.INDEX_PATH, "")
        checks["criou_2_docs"] = len(new) >= 2
        checks["registrou_cafe"] = _has(all_text, "cafe", "acucar")
        checks["registrou_projeto"] = _has(all_text, "limas-memory")
        checks["indice_lista_todos"] = bool(new) and all(p in index_text for p in after.content_docs())
        checks["commitou"] = committed
    elif key == "03":
        checks["nao_criou_doc"] = not new
        checks["adicionou_detalhes"] = _has(all_text, "coado") and _has(all_text, "manha")
        checks["manteve_sem_acucar"] = _has(all_text, "acucar")
        checks["commitou"] = committed
    elif key == "04":
        checks["registrou_cha"] = bool(re.search(r"\bcha\b", norm(all_text)))
        checks["preservou_cafe_antigo"] = _has(all_text, "coado") or _has(all_text, "acucar")
        checks["marcou_contradicao"] = any(m in norm(all_text) for m in CONTEST_MARKERS)
        checks["commitou"] = committed
    elif key == "05":
        lisboa = [t for t in new.values() if _has(t, "lisboa")]
        text = "\n".join(lisboa)
        checks["criou_doc_lisboa"] = bool(lisboa)
        checks["converteu_ano"] = str(now.year + 1) in text
        checks["marcou_hipotese"] = any(m in norm(text) for m in ("hipotese", "palpite", "nao verificad"))
        checks["commitou"] = committed
    return checks


# ---- execução ----------------------------------------------------------------------


def _default_factory(model: str, tools: list, scenario: str):
    return build_curator(model=model, tools=tools)


def run_model(model, run_idx, scenarios, base_dir, channel, factory, now) -> list[ScenarioRun]:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", model)
    mem = base_dir / f"{safe}-run{run_idx}"
    store, git = make_store_and_git(mem, push=False)
    git.ensure_repo()
    results = []
    for path in scenarios:
        key = path.stem.split("_")[0]
        run = ScenarioRun(model=model, run=run_idx, scenario=path.stem)
        messages = parse_conversation(path.read_text(encoding="utf-8"))
        before, head_before = Snapshot.take(store), git.head()
        started = time.perf_counter()
        try:
            agent = factory(model, build_tools(store, git), path.stem)
            result = curate(messages, agent=agent, channel=channel, now=now)
            run.usage = result.usage
            run.tool_calls = len(result.trace)
            run.tool_errors = sum(1 for e in result.trace if e.result.startswith("ERRO"))
        except Exception as exc:  # noqa: BLE001 - qualquer falha vira dado do relatório
            run.error = f"{type(exc).__name__}: {str(exc)[:300]}"
        run.seconds = time.perf_counter() - started
        run.committed = git.head() != head_before
        if not run.error:
            run.checks = run_checks(key, before, Snapshot.take(store), run, now)
        results.append(run)
        print(_progress_line(run), flush=True)
    return results


def _progress_line(r: ScenarioRun) -> str:
    head = f"[{r.model} #{r.run} {r.scenario}]"
    if r.error:
        return f"{head} ERRO: {r.error}"
    ok = sum(r.checks.values())
    return (
        f"{head} {r.tool_calls} tools ({r.tool_errors} com erro), {r.seconds:.1f}s, "
        f"in={r.usage.input_tokens} out={r.usage.output_tokens}, checagens {ok}/{len(r.checks)}"
    )


# ---- relatório ---------------------------------------------------------------------


def _sum_usage(runs: list[ScenarioRun]) -> Usage:
    total = Usage()
    for r in runs:
        total.add(r.usage)
    return total


def _fmt_cost(u: Usage) -> str:
    return f"US$ {u.cost:.4f}" if u.cost_reported else "n/d"


def _table(headers: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def _metrics_row(label: list[str], runs: list[ScenarioRun]) -> list[str]:
    u = _sum_usage(runs)
    done = [r for r in runs if not r.error]
    passed = sum(sum(r.checks.values()) for r in done)
    total = sum(len(r.checks) for r in done)
    errors = sum(1 for r in runs if r.error)
    return [
        *label, f"{u.input_tokens:,}", f"{u.output_tokens:,}", f"{u.cache_read:,}",
        f"{u.cache_creation:,}", f"{u.reasoning:,}", f"{u.total_tokens:,}", _fmt_cost(u),
        str(u.llm_calls), str(sum(r.tool_calls for r in runs)), str(sum(r.tool_errors for r in runs)),
        f"{sum(r.seconds for r in runs):.0f}", f"{passed}/{total}", str(errors),
    ]


_METRIC_HEADERS = [
    "Tokens entrada", "Tokens saída", "Cache (leitura)", "Cache (criação)", "Raciocínio",
    "Total tokens", "Custo", "Chamadas ao modelo", "Chamadas de tool", "Tools com erro",
    "Tempo (s)", "Checagens", "Execuções com erro",
]


def render_report(results: list[ScenarioRun], meta: dict) -> str:
    models = list(dict.fromkeys(r.model for r in results))
    cols = list(dict.fromkeys((r.model, r.run) for r in results))
    out = [
        "# Comparação de modelos no Memory Curator",
        "",
        f"- Data: {meta['when']}",
        f"- Modelos: {', '.join(models)}",
        f"- Rodadas por modelo: {meta['runs']} | Cenários: {meta['scenarios']}",
        f"- Memórias geradas em: `{meta['base_dir']}`",
        "",
        "## Resumo por modelo (soma de todas as rodadas e cenários)",
        "",
        _table(["Modelo", *_METRIC_HEADERS],
               [_metrics_row([m], [r for r in results if r.model == m]) for m in models]),
        "",
        "## Por rodada",
        "",
        _table(["Modelo", "Rodada", *_METRIC_HEADERS],
               [_metrics_row([m, str(n)], [r for r in results if (r.model, r.run) == (m, n)])
                for m, n in cols]),
        "",
        "## Por cenário (tokens totais / chamadas de tool)",
        "",
    ]
    scenarios = list(dict.fromkeys(r.scenario for r in results))
    rows = []
    for s in scenarios:
        row = [s]
        for m, n in cols:
            r = next((x for x in results if (x.model, x.run, x.scenario) == (m, n, s)), None)
            row.append("—" if r is None else "ERRO" if r.error else f"{r.usage.total_tokens:,} / {r.tool_calls}")
        rows.append(row)
    out += [_table(["Cenário", *[f"{m} #{n}" for m, n in cols]], rows), "", "## Checagens", ""]

    check_rows = []
    for s in scenarios:
        names = list(dict.fromkeys(c for r in results if r.scenario == s for c in r.checks))
        for c in names:
            row = [f"{s} · {c}"]
            for m, n in cols:
                r = next((x for x in results if (x.model, x.run, x.scenario) == (m, n, s)), None)
                row.append("—" if r is None or c not in r.checks else "ok" if r.checks[c] else "FALHOU")
            check_rows.append(row)
    out += [_table(["Checagem", *[f"{m} #{n}" for m, n in cols]], check_rows), ""]

    failures = [f"- {r.model} #{r.run} {r.scenario}: {c}" for r in results for c, ok in r.checks.items() if not ok]
    errors = [f"- {r.model} #{r.run} {r.scenario}: {r.error}" for r in results if r.error]
    out += ["## Falhas", ""] + (failures or ["Nenhuma checagem falhou."])
    if errors:
        out += ["", "## Execuções com erro", ""] + errors
    unreported = sum(r.usage.unreported_calls for r in results)
    if unreported:
        out += ["", f"ATENÇÃO: {unreported} chamada(s) ao modelo vieram sem dados de uso; os totais ficam subestimados."]
    if not any(r.usage.cost_reported for r in results):
        out += ["", "Custo n/d: o provedor não devolveu o custo. Use os tokens com o preço de cada modelo, "
                "ou a aba Activity do OpenRouter."]
    return "\n".join(out) + "\n"


# ---- CLI ---------------------------------------------------------------------------


def main(argv: list[str] | None = None, factory=_default_factory) -> int:
    ap = argparse.ArgumentParser(description="Compara modelos no Memory Curator.")
    ap.add_argument("--models", default=config.OPENROUTER_MODEL,
                    help="slugs do OpenRouter separados por vírgula (padrão: OPENROUTER_MODEL)")
    ap.add_argument("--runs", type=int, default=2, help="rodadas por modelo (padrão: 2)")
    ap.add_argument("--scenarios", type=Path, default=config.ROOT / "evals" / "scenarios",
                    help="pasta com os cenários .txt")
    ap.add_argument("--channel", default="teste (CLI)")
    ap.add_argument("--out", default="compare", help="prefixo dos arquivos de saída")
    args = ap.parse_args(argv)

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    scenarios = sorted(args.scenarios.glob("*.txt"))
    if not models or not scenarios or args.runs < 1:
        print("Informe ao menos um modelo, um cenário e --runs >= 1.", file=sys.stderr)
        return 2
    if factory is _default_factory and not config.OPENROUTER_API_KEY:
        print("Erro: defina OPENROUTER_API_KEY no ambiente ou no .env", file=sys.stderr)
        return 1

    now = datetime.now().astimezone()
    base_dir = Path(tempfile.gettempdir()) / f"compare-{now:%Y%m%d-%H%M%S}"
    base_dir.mkdir(parents=True)
    total = len(models) * args.runs * len(scenarios)
    print(f"{total} execuções do Curator ({len(models)} modelo(s) x {args.runs} rodada(s) x "
          f"{len(scenarios)} cenário(s)). Memórias em {base_dir}\n", flush=True)

    results: list[ScenarioRun] = []
    for model in models:
        for run_idx in range(1, args.runs + 1):
            results += run_model(model, run_idx, scenarios, base_dir, args.channel, factory, now)

    meta = {"when": f"{now:%Y-%m-%d %H:%M}", "runs": args.runs, "scenarios": len(scenarios),
            "base_dir": str(base_dir)}
    report = render_report(results, meta)
    Path(f"{args.out}-report.md").write_text(report, encoding="utf-8", newline="\n")
    Path(f"{args.out}-results.json").write_text(
        json.dumps({"meta": meta, "results": [asdict(r) for r in results]}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print("\n" + report)
    print(f"Relatório: {args.out}-report.md | Dados: {args.out}-results.json")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
