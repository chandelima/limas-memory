"""Compare models on the Memory Curator: run the scenarios in evals/scenarios/ several times per
model, measure tokens/time/calls and run objective checks on the generated memory.

Usage:
    python evals/compare.py --models openai/gpt-6-luna,anthropic/claude-haiku-5.5 --runs 2

Each (model, run) uses a NEW, isolated temporary memory, with no remote (it never pushes).
The scenarios run in order inside the same memory, since 03 and 04 depend on what 02 wrote.
Outputs: compare-report.md (summary) and compare-results.json (raw data).
The checks only catch objective errors; the quality of the writing is still judged by
reading the memories, whose paths are printed at the end.
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

# This script lives in evals/, outside src/: make sure the src/ modules are importable.
_SRC = str(Path(__file__).resolve().parent.parent / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import config  # noqa: E402
from curate import parse_conversation  # noqa: E402
from curator.agent import Usage, build_curator, curate  # noqa: E402
from curator.tools import build_tools, make_store_and_git  # noqa: E402
from storage import index as idx  # noqa: E402
from storage import markdown as md  # noqa: E402
from storage.store import MemoryStore  # noqa: E402

CONTEST_MARKERS = ("contest", "under review", "contradict", "conflict", "unconfirmed", "open question", "unresolved")


def norm(text: str) -> str:
    """Lowercase and accent-free, for tolerant checks."""
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
    tool_error_messages: list[str] = field(default_factory=list)  # "tool(args): ERROR..."
    committed: bool = False
    usage: Usage = field(default_factory=Usage)
    checks: dict[str, bool] = field(default_factory=dict)
    error: str = ""  # exception of the run (e.g. API 400 error); no checks


# ---- checks ------------------------------------------------------------------------


@dataclass
class Snapshot:
    store: MemoryStore
    docs: dict[str, str]  # path -> text (includes index.md)

    @classmethod
    def take(cls, store: MemoryStore) -> "Snapshot":
        return cls(store, {p: store.read_doc(p) for p in store.list_paths()})

    def content_docs(self) -> dict[str, str]:
        return {p: t for p, t in self.docs.items() if p != idx.INDEX_PATH}


def _body(text: str) -> str:
    try:
        return md.parse_frontmatter(text)[1]
    except md.MarkdownError:
        return text


def _heading_texts(*texts: str) -> list[str]:
    return [norm(h) for t in texts for _, h in md.headings(_body(t))]


def _has(text: str, *needles: str) -> bool:
    n = norm(text)
    return all(norm(x) in n for x in needles)


def run_checks(key: str, before: Snapshot, after: Snapshot, run: ScenarioRun, now: datetime) -> dict:
    new = {p: t for p, t in after.content_docs().items() if p not in before.content_docs()}
    all_text = "\n".join(after.content_docs().values())
    committed = run.committed

    checks = {
        "no_tool_errors": run.tool_errors == 0,
        "valid_frontmatter": all(d.description for d in after.store.list_docs()),
        "no_broken_links": not idx.find_broken_links(after.store),
        "no_invented_telegram": "telegram" not in norm("\n".join(after.docs.values())),
        "docs_have_title": all(md.first_heading(_body(t)) for t in after.content_docs().values()),
    }

    if key == "01":
        checks["wrote_nothing"] = after.docs == before.docs
        checks["no_commit"] = not committed
    elif key == "02":
        index_text = after.docs.get(idx.INDEX_PATH, "")
        checks["created_2_docs"] = len(new) >= 2
        checks["recorded_coffee"] = _has(all_text, "coffee", "sugar")
        checks["recorded_project"] = _has(all_text, "limas-memory")
        checks["index_lists_all"] = bool(new) and all(p in index_text for p in after.content_docs())
        checks["committed"] = committed
    elif key == "03":
        checks["created_no_doc"] = not new
        checks["added_details"] = _has(all_text, "brewed") and _has(all_text, "morning")
        checks["kept_no_sugar"] = _has(all_text, "sugar")
        checks["committed"] = committed
    elif key == "04":
        checks["recorded_tea"] = bool(re.search(r"\btea\b", norm(all_text)))
        checks["preserved_old_coffee"] = _has(all_text, "brewed") or _has(all_text, "sugar")
        checks["flagged_contradiction"] = any(m in norm(all_text) for m in CONTEST_MARKERS)
        checks["contested_section"] = any(
            "contest" in h for h in _heading_texts(*after.content_docs().values())
        )
        checks["committed"] = committed
    elif key == "05":
        lisbon = [t for t in new.values() if _has(t, "lisbon")]
        text = "\n".join(lisbon)
        checks["created_lisbon_doc"] = bool(lisbon)
        checks["converted_year"] = str(now.year + 1) in text
        checks["flagged_hypothesis"] = any(m in norm(text) for m in ("hypothesis", "guess", "unverified"))
        checks["hypotheses_section"] = any("hypothes" in h for h in _heading_texts(*lisbon))
        checks["committed"] = committed
    return checks


# ---- execution ---------------------------------------------------------------------


def _default_factory(model: str, tools: list, scenario: str):
    return build_curator(model=model, tools=tools)


def _short_args(args: dict, limit: int = 80) -> str:
    return ", ".join(f"{k}={str(v)[:limit]!r}" for k, v in args.items())


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
            failed = [e for e in result.trace if e.result.startswith("ERROR")]
            run.tool_errors = len(failed)
            run.tool_error_messages = [
                f"{e.name}({_short_args(e.args)}): {e.result.strip()[:300]}" for e in failed
            ]
        except Exception as exc:  # noqa: BLE001 - any failure becomes report data
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
        return f"{head} ERROR: {r.error}"
    ok = sum(r.checks.values())
    return (
        f"{head} {r.tool_calls} tools ({r.tool_errors} with errors), {r.seconds:.1f}s, "
        f"in={r.usage.input_tokens} out={r.usage.output_tokens}, checks {ok}/{len(r.checks)}"
    )


# ---- report ------------------------------------------------------------------------


def _sum_usage(runs: list[ScenarioRun]) -> Usage:
    total = Usage()
    for r in runs:
        total.add(r.usage)
    return total


def _fmt_cost(u: Usage) -> str:
    return f"US$ {u.cost:.4f}" if u.cost_reported else "n/a"


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
    "Input tokens", "Output tokens", "Cache (read)", "Cache (creation)", "Reasoning",
    "Total tokens", "Cost", "Model calls", "Tool calls", "Tools with errors",
    "Time (s)", "Checks", "Runs with errors",
]


def render_report(results: list[ScenarioRun], meta: dict) -> str:
    models = list(dict.fromkeys(r.model for r in results))
    cols = list(dict.fromkeys((r.model, r.run) for r in results))
    out = [
        "# Model comparison on the Memory Curator",
        "",
        f"- Date: {meta['when']}",
        f"- Models: {', '.join(models)}",
        f"- Runs per model: {meta['runs']} | Scenarios: {meta['scenarios']}",
        f"- Memories generated in: `{meta['base_dir']}`",
        "",
        "## Summary per model (sum of all runs and scenarios)",
        "",
        _table(["Model", *_METRIC_HEADERS],
               [_metrics_row([m], [r for r in results if r.model == m]) for m in models]),
        "",
        "## Per run",
        "",
        _table(["Model", "Run", *_METRIC_HEADERS],
               [_metrics_row([m, str(n)], [r for r in results if (r.model, r.run) == (m, n)])
                for m, n in cols]),
        "",
        "## Per scenario (total tokens / tool calls)",
        "",
    ]
    scenarios = list(dict.fromkeys(r.scenario for r in results))
    rows = []
    for s in scenarios:
        row = [s]
        for m, n in cols:
            r = next((x for x in results if (x.model, x.run, x.scenario) == (m, n, s)), None)
            row.append("—" if r is None else "ERROR" if r.error else f"{r.usage.total_tokens:,} / {r.tool_calls}")
        rows.append(row)
    out += [_table(["Scenario", *[f"{m} #{n}" for m, n in cols]], rows), "", "## Checks", ""]

    check_rows = []
    for s in scenarios:
        names = list(dict.fromkeys(c for r in results if r.scenario == s for c in r.checks))
        for c in names:
            row = [f"{s} · {c}"]
            for m, n in cols:
                r = next((x for x in results if (x.model, x.run, x.scenario) == (m, n, s)), None)
                row.append("—" if r is None or c not in r.checks else "ok" if r.checks[c] else "FAILED")
            check_rows.append(row)
    out += [_table(["Check", *[f"{m} #{n}" for m, n in cols]], check_rows), ""]

    failures = [f"- {r.model} #{r.run} {r.scenario}: {c}" for r in results for c, ok in r.checks.items() if not ok]
    errors = [f"- {r.model} #{r.run} {r.scenario}: {r.error}" for r in results if r.error]
    tool_errs = [
        f"- {r.model} #{r.run} {r.scenario}: {m}" for r in results for m in r.tool_error_messages
    ]
    out += ["## Failures", ""] + (failures or ["No check failed."])
    if tool_errs:
        out += ["", "## Tool errors (the agent received them and could fix itself)", ""] + tool_errs
    if errors:
        out += ["", "## Runs with errors", ""] + errors
    unreported = sum(r.usage.unreported_calls for r in results)
    if unreported:
        out += ["", f"WARNING: {unreported} model call(s) came without usage data; the totals are underestimated."]
    if not any(r.usage.cost_reported for r in results):
        out += ["", "Cost n/a: the provider did not return the cost. Use the tokens with each model's price, "
                "or the OpenRouter Activity tab."]
    return "\n".join(out) + "\n"


# ---- CLI ---------------------------------------------------------------------------


def main(argv: list[str] | None = None, factory=_default_factory) -> int:
    ap = argparse.ArgumentParser(description="Compare models on the Memory Curator.")
    ap.add_argument("--models", default=config.OPENROUTER_MODEL,
                    help="comma-separated OpenRouter slugs (default: OPENROUTER_MODEL)")
    ap.add_argument("--runs", type=int, default=2, help="runs per model (default: 2)")
    ap.add_argument("--scenarios", type=Path, default=config.ROOT / "evals" / "scenarios",
                    help="folder with the .txt scenarios")
    ap.add_argument("--channel", default="test (CLI)")
    ap.add_argument("--out", default="compare", help="prefix of the output files")
    args = ap.parse_args(argv)

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    scenarios = sorted(args.scenarios.glob("*.txt"))
    if not models or not scenarios or args.runs < 1:
        print("Provide at least one model, one scenario and --runs >= 1.", file=sys.stderr)
        return 2
    if factory is _default_factory and not config.OPENROUTER_API_KEY:
        print("Error: set OPENROUTER_API_KEY in the environment or in .env", file=sys.stderr)
        return 1

    now = datetime.now().astimezone()
    base_dir = Path(tempfile.gettempdir()) / f"compare-{now:%Y%m%d-%H%M%S}"
    base_dir.mkdir(parents=True)
    total = len(models) * args.runs * len(scenarios)
    print(f"{total} Curator runs ({len(models)} model(s) x {args.runs} run(s) x "
          f"{len(scenarios)} scenario(s)). Memories in {base_dir}\n", flush=True)

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
    print(f"Report: {args.out}-report.md | Data: {args.out}-results.json")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
