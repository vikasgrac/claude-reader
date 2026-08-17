"""Textual pilot tests: the app end to end, headless."""
import json
import os

import pytest
from textual.widgets import ListView, Markdown

from claude_reader import ReaderApp, list_sessions


def a(text):
    return json.dumps({"type": "assistant", "uuid": text, "message": {"content": [{"type": "text", "text": text}]}}) + "\n"


def u(text):
    return json.dumps({"type": "user", "message": {"content": text}}) + "\n"


@pytest.mark.asyncio
async def test_app_load_trim_reset_toggle(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text("".join(a(f"m{i}") for i in range(12)))
    app = ReaderApp(p, list_sessions(tmp_path), max_messages=5)
    app.no_pick = True
    async with app.run_test() as pilot:
        await pilot.pause()
        sb = app.query_one("#sidebar", ListView)
        assert len(sb) == 5 and [i.msg.text for i in sb.children] == ["m7", "m8", "m9", "m10", "m11"]
        assert sb.index == 4                                   # following the latest
        with open(p, "a") as f:
            f.write(u("question") + a("m12"))
        app.poll(); await pilot.pause()
        assert len(sb) == 5 and [i.msg.text for i in sb.children][-2:] == ["question", "m12"]
        # hide user prompts, then show again
        await pilot.press("u"); await pilot.pause()
        assert all(i.msg.role == "assistant" for i in sb.children)
        await pilot.press("u"); await pilot.pause()
        assert any(i.msg.role == "user" for i in sb.children)
        # transcript rewritten smaller: reset, no stale rows
        p.write_text(a("fresh"))
        app.poll(); await pilot.pause()
        assert [i.msg.text for i in sb.children] == ["fresh"]
        # transcript vanishes: a warning, not a crash; then comes back
        os.remove(p)
        app.poll(); await pilot.pause()
        assert app.tail.error
        p.write_text(a("back"))
        app.poll(); await pilot.pause()
        assert [i.msg.text for i in sb.children] == ["back"] and app.tail.error is None
        # malformed lines mid-stream do not kill polling
        with open(p, "a") as f:
            f.write('{"type":"assistant","message":null}\n' + "garbage\n" + a("after"))
        app.poll(); await pilot.pause()
        assert [i.msg.text for i in sb.children] == ["back", "after"]
