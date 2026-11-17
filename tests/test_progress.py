import asyncio

from fakes import FakeGateway
from progress import Progress, step_label


def run(coro):
    return asyncio.run(coro)


def test_step_labels_name_the_tool_and_trim_long_arguments():
    assert step_label("list_memory", {}) == "🗂️ Looking at what I remember…"
    assert step_label("read_memory", {"path": "trips/petrolina-2027.md"}) == "📖 Reading trips/petrolina-2027.md…"
    assert step_label("list_tasks", {}) == "📋 Checking your tasks…"
    assert step_label("complete_task", {}) == "✏️ Updating your tasks…" == step_label("tag_task", {})
    assert step_label("some_new_tool", {}) == "⚙️ Working on it…"
    long = step_label("search_memory", {"query": "x" * 200})
    assert long.startswith("🔎 Searching the memory for “xxx") and long.endswith("…”…") and len(long) < 80


def test_steps_edit_the_message_and_the_answer_replaces_it():
    gw = FakeGateway()

    async def scenario():
        p = Progress(gw, 42, edit_interval=0.01)
        await p.start()
        p.push("list_memory", {})
        p.push("read_memory", {"path": "a.md"})
        await asyncio.sleep(0.1)
        await p.finish("The answer.")

    run(scenario())
    assert gw.progress_edits[0] == "💭 Thinking…"
    assert gw.progress_edits[-1] == "🗂️ Looking at what I remember…\n📖 Reading a.md…"
    assert gw.sent() == ["The answer."]  # the final text goes through replace_text, on the same message


def test_push_from_a_worker_thread_is_safe_and_edits_are_throttled_and_deduplicated():
    gw = FakeGateway()

    async def scenario():
        p = Progress(gw, 42, edit_interval=0.2)
        await p.start()

        def worker():
            for _ in range(5):  # five identical steps in a row
                p.push("list_tasks", {})

        await asyncio.to_thread(worker)
        await asyncio.sleep(0.1)
        await p.finish("done")

    run(scenario())
    assert gw.progress_edits.count("📋 Checking your tasks…") == 1  # one line, one edit
    assert len(gw.progress_edits) == 2  # "thinking" + one edit


def test_without_steps_the_thinking_message_just_becomes_the_answer():
    gw = FakeGateway()

    async def scenario():
        p = Progress(gw, 42, edit_interval=0.01)
        await p.start()
        await p.finish("quick answer")

    run(scenario())
    assert gw.progress_edits == ["💭 Thinking…"] and gw.sent() == ["quick answer"]


def test_if_the_progress_message_cannot_be_sent_the_answer_goes_the_normal_way():
    class NoPlaceholder(FakeGateway):
        async def send_placeholder(self, chat_id, text):
            raise RuntimeError("telegram is down")

    gw = NoPlaceholder()

    async def scenario():
        p = Progress(gw, 42)
        await p.start()
        p.push("list_memory", {})  # ignored
        await p.finish("answer")

    run(scenario())
    assert gw.sent() == ["answer"] and gw.progress_edits == []


def test_a_failing_edit_does_not_stop_the_progress_or_the_answer():
    class FlakyEdits(FakeGateway):
        async def edit_text(self, chat_id, message_id, text):
            raise RuntimeError("429 Too Many Requests")

    gw = FlakyEdits()

    async def scenario():
        p = Progress(gw, 42, edit_interval=0.01)
        await p.start()
        p.push("list_memory", {})
        await asyncio.sleep(0.05)
        p.push("list_tasks", {})
        await asyncio.sleep(0.05)
        await p.finish("still here")

    run(scenario())
    assert gw.sent() == ["still here"]


def test_the_final_edit_is_never_overwritten_by_a_late_step():
    gw = FakeGateway()

    async def scenario():
        p = Progress(gw, 42, edit_interval=0.0)
        await p.start()
        await p.finish("final")
        p.push("list_memory", {})  # a step arriving after the end
        await asyncio.sleep(0.05)

    run(scenario())
    assert gw.events[-1] == ("text", "final") and gw.sent() == ["final"]
