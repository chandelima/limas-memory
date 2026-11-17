"""CLI to try the Memory Curator on a sample conversation.

Usage:
    python src/curate.py evals/scenarios/02_new_fact.txt
    python src/curate.py conversation.txt --context "Chat about habits"
    type conversation.txt | python src/curate.py -
    python src/curate.py ex.txt --memory-dir D:/tmp/test-memory   # does not touch the real memory
    python src/curate.py ex.txt --verbose    # shows each tool call and its result

Conversation format: one message per line, prefixed with `user:` or `assistant:`.
Lines without a prefix continue the previous message (or form a user message).
"""

import argparse
import re
import sys
from pathlib import Path

import config
from curator.agent import ChatMessage, build_curator, curate
from curator.tools import build_tools, make_store_and_git

_ROLE = re.compile(r"^\s*(user|assistant)\s*[:>]\s*(.*)$", re.I)
MAX_DIFF_LINES = 200
MAX_VALUE_CHARS = 80
MAX_RESULT_CHARS = 300


def describe_args(args: dict) -> str:
    parts = []
    for key, value in args.items():
        text = str(value)
        if len(text) > MAX_VALUE_CHARS:
            text = f"{text[:MAX_VALUE_CHARS]}...({len(text)} chars)"
        parts.append(f"{key}={text!r}")
    return ", ".join(parts)


def describe_usage(u) -> str:
    text = (
        f"model calls: {u.llm_calls} | input: {u.input_tokens} | output: {u.output_tokens} | "
        f"cache (read): {u.cache_read} | cache (creation): {u.cache_creation} | "
        f"reasoning: {u.reasoning} | total: {u.total_tokens}"
    )
    text += f" | cost: US$ {u.cost:.6f}" if u.cost_reported else " | cost: n/a"
    if u.unreported_calls:
        text += f" | WARNING: {u.unreported_calls} call(s) without usage data"
    return text


def parse_conversation(text: str) -> list[ChatMessage]:
    messages: list[ChatMessage] = []
    current: ChatMessage | None = None
    for line in text.splitlines():
        match = _ROLE.match(line)
        if match:
            current = ChatMessage(match.group(1).lower(), match.group(2).strip())
            messages.append(current)
        elif line.strip():
            if current is None:
                current = ChatMessage("user", line.strip())
                messages.append(current)
            else:
                current.text += "\n" + line.strip()
    return [m for m in messages if m.text]


def main(argv: list[str] | None = None, agent=None) -> int:
    ap = argparse.ArgumentParser(description="Run the Memory Curator on a conversation.")
    ap.add_argument("conversation", help=".txt file with the conversation, or '-' for stdin")
    ap.add_argument("--context", default="", help="additional context for the conversation")
    ap.add_argument("--channel", default="test (CLI)", help="channel reported to the Curator")
    ap.add_argument("--verbose", action="store_true", help="show each tool and its result")
    ap.add_argument("--memory-dir", type=Path, default=None,
                    help="memory folder (default: MEMORY_DIR / ./memory)")
    args = ap.parse_args(argv)

    text = sys.stdin.read() if args.conversation == "-" else Path(args.conversation).read_text(encoding="utf-8")
    messages = parse_conversation(text)
    if not messages:
        print("No messages found in the conversation.", file=sys.stderr)
        return 2

    memory_dir = args.memory_dir or config.MEMORY_DIR
    store, git = make_store_and_git(memory_dir)
    git.ensure_repo()
    before = git.head()

    if agent is None:
        try:
            agent = build_curator(tools=build_tools(store, git))
        except RuntimeError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

    print(f"Memory: {memory_dir}")
    print(f"Messages: {len(messages)}\n")
    result = curate(messages, args.context, agent=agent, channel=args.channel)

    print("== Tool calls ==")
    print(" -> ".join(result.tool_calls) or "(none)")
    if args.verbose and result.trace:
        print("\n== Tool details ==")
        for i, event in enumerate(result.trace, 1):
            print(f"{i}. {event.name}({describe_args(event.args)})")
            shown = event.result.strip().replace("\n", " | ")
            if len(shown) > MAX_RESULT_CHARS:
                shown = shown[:MAX_RESULT_CHARS] + "..."
            print(f"   -> {shown}")
    print("\n== Curator report ==")
    print(result.report or "(no report)")
    print("\n== Token usage ==")
    print(describe_usage(result.usage))

    after = git.head()
    print("\n== Memory changes ==")
    if after == before:
        print("(no new commit)")
    else:
        lines = git.diff(before).splitlines()
        print("\n".join(lines[:MAX_DIFF_LINES]))
        if len(lines) > MAX_DIFF_LINES:
            print(f"... ({len(lines) - MAX_DIFF_LINES} lines omitted)")
        print("\n== Commits ==")
        print("\n".join(git.log(5)))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):  # avoids accent errors on the Windows console
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
