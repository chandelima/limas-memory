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


def test_the_message_starts_as_italic_thinking_and_the_steps_are_edited_in_italics():
    gw = FakeGateway()

    async def scenario():
        p = Progress(gw, 42, edit_interval=0.02, thinking_cps=0)  # 0: no fake typing, lines appear at once
        await p.start()
        p.push("list_memory", {})
        p.push("read_memory", {"path": "a.md"})
        await asyncio.sleep(0.15)
        await p.finish("The answer.")

    run(scenario())
    assert gw.progress_edits[0] == "<i>💭 Thinking…</i>"
    assert gw.progress_edits[-1] == "<i>🗂️ Looking at what I remember…</i>\n<i>📖 Reading a.md…</i>"
    assert gw.sent() == ["The answer."]
    # the final message keeps the thinking (italics) on top and the answer below, in normal text
    assert gw.last_answer_html == "<i>🗂️ Looking at what I remember…</i>\n<i>📖 Reading a.md…</i>\n\nThe answer."


def test_the_models_text_is_streamed_as_it_arrives_below_the_thinking():
    gw = FakeGateway()

    async def scenario():
        p = Progress(gw, 42, edit_interval=0.02, thinking_cps=0)
        await p.start()
        p.push("list_tasks", {})
        await asyncio.sleep(0.1)
        for piece in ("You have ", "two tasks ", "today."):
            p.push_text(piece)  # tokens arriving
            await asyncio.sleep(0.06)
        await p.finish("You have two tasks today.")

    run(scenario())
    partials = [e for e in gw.progress_edits if "You have" in e]
    assert partials[0].endswith("▍")  # a cursor while the model is still writing
    assert any("You have two tasks" in e and e.endswith("▍") for e in gw.progress_edits)  # it grows as tokens arrive
    assert all(e.startswith("<i>📋 Checking your tasks…</i>") for e in partials)  # the thinking stays on top
    assert gw.last_answer_html == "<i>📋 Checking your tasks…</i>\n\nYou have two tasks today."  # no cursor in the end


def test_text_written_before_a_tool_call_becomes_an_italic_thought():
    gw = FakeGateway()

    async def scenario():
        p = Progress(gw, 42, edit_interval=0.02, thinking_cps=0)
        await p.start()
        p.push_text("Let me check your memory first.")  # the model "thinks aloud" ...
        await asyncio.sleep(0.08)
        p.push("list_memory", {})  # ... and then asks for a tool
        await asyncio.sleep(0.1)
        p.push_text("Found it.")
        await asyncio.sleep(0.08)
        await p.finish("Found it.")

    run(scenario())
    assert any(e == "Let me check your memory first. ▍" for e in gw.progress_edits)  # shown as text while streaming
    assert "<i>Let me check your memory first.</i>\n<i>🗂️ Looking at what I remember…</i>" in gw.progress_edits[-2] or \
        any("<i>Let me check your memory first.</i>\n<i>🗂️" in e for e in gw.progress_edits)
    assert gw.last_answer_html.endswith("</i>\n\nFound it.")


def test_the_thinking_has_a_fake_typing_effect_in_blocks_of_words():
    gw = FakeGateway()
    thought = "I should look at your trips first and then compare them with the tasks you already have."

    async def scenario():
        p = Progress(gw, 42, edit_interval=0.03, thinking_cps=400)  # ~12 chars per tick
        await p.start()
        p.push_text(thought)
        p.push("list_memory", {})
        await asyncio.sleep(0.6)
        p.push_text("Done.")  # held back until the thinking has finished typing
        await asyncio.sleep(0.2)
        await p.finish("Done.")

    run(scenario())
    typed = [e for e in gw.progress_edits if e.startswith("<i>I should")]
    assert len(typed) >= 3  # it grew over several edits ...
    lengths = [len(e) for e in typed]
    assert lengths == sorted(lengths)  # ... never shrinking
    assert typed[0].endswith(" ▍</i>") and not typed[0].startswith("<i>" + thought)  # partial, with the cursor
    assert any(thought in e and "Looking at what I remember" in e for e in gw.progress_edits)  # fully typed
    assert gw.last_answer_html.endswith("\n\nDone.")


def test_push_from_a_worker_thread_is_safe_and_identical_steps_are_one_line():
    gw = FakeGateway()

    async def scenario():
        p = Progress(gw, 42, edit_interval=0.05, thinking_cps=0)
        await p.start()

        def worker():
            for _ in range(5):  # five identical steps in a row
                p.push("list_tasks", {})
            p.push_text("ok")

        await asyncio.to_thread(worker)
        await asyncio.sleep(0.2)
        await p.finish("ok")

    run(scenario())
    assert gw.last_answer_html == "<i>📋 Checking your tasks…</i>\n\nok"
    assert max(e.count("Checking your tasks") for e in gw.progress_edits) == 1


def test_html_in_the_thinking_and_in_the_answer_is_escaped():
    gw = FakeGateway()

    async def scenario():
        p = Progress(gw, 42, edit_interval=0.02, thinking_cps=0)
        await p.start()
        p.push("search_memory", {"query": "<b>&"})
        await asyncio.sleep(0.1)
        await p.finish("a < b & c > d")

    run(scenario())
    assert gw.last_answer_html == "<i>🔎 Searching the memory for “&lt;b&gt;&amp;”…</i>\n\na &lt; b &amp; c &gt; d"


def test_without_thinking_the_message_just_becomes_the_answer():
    gw = FakeGateway()

    async def scenario():
        p = Progress(gw, 42, edit_interval=0.02)
        await p.start()
        await p.finish("quick answer")

    run(scenario())
    assert gw.progress_edits == ["<i>💭 Thinking…</i>"] and gw.sent() == ["quick answer"]
    assert gw.last_answer_html == "quick answer"  # no italic header when there was nothing to show


def test_if_the_progress_message_cannot_be_sent_the_answer_goes_the_normal_way():
    class NoPlaceholder(FakeGateway):
        async def send_placeholder(self, chat_id, text, *, html=False):
            raise RuntimeError("telegram is down")

    gw = NoPlaceholder()

    async def scenario():
        p = Progress(gw, 42)
        await p.start()
        p.push("list_memory", {})  # ignored
        p.push_text("hi")  # ignored
        await p.finish("answer")

    run(scenario())
    assert gw.sent() == ["answer"] and gw.progress_edits == []


def test_a_failing_edit_does_not_stop_the_progress_or_the_answer():
    class FlakyEdits(FakeGateway):
        async def edit_text(self, chat_id, message_id, text, *, html=False):
            raise RuntimeError("429 Too Many Requests")

    gw = FlakyEdits()

    async def scenario():
        p = Progress(gw, 42, edit_interval=0.02, thinking_cps=0)
        await p.start()
        p.push("list_memory", {})
        await asyncio.sleep(0.08)
        p.push_text("hello")
        await asyncio.sleep(0.08)
        await p.finish("still here")

    run(scenario())
    assert gw.sent() == ["still here"]


def test_the_final_text_is_never_overwritten_by_a_late_event():
    gw = FakeGateway()

    async def scenario():
        p = Progress(gw, 42, edit_interval=0.0, thinking_cps=0)
        await p.start()
        await p.finish("final")
        p.push("list_memory", {})  # events arriving after the end
        p.push_text("late")
        await asyncio.sleep(0.05)

    run(scenario())
    assert gw.events[-1] == ("text", "final") and gw.sent() == ["final"]


def test_a_very_long_answer_continues_in_new_messages():
    gw = FakeGateway()
    long = "word " * 1500  # 7500 characters

    async def scenario():
        p = Progress(gw, 42, edit_interval=0.02)
        await p.start()
        await p.finish(long)

    run(scenario())
    assert len(gw.sent()) >= 3 and all(len(x) <= 4096 for x in gw.sent())
    assert " ".join(" ".join(gw.sent()).split()) == " ".join(long.split())
