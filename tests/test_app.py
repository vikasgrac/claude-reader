"""Textual pilot tests: the app end to end, headless."""
import json
import os

import pytest
from textual.widgets import ListView, Markdown

from claude_reader import MessageSearch, ReaderApp, list_sessions


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


@pytest.mark.asyncio
async def test_long_fenced_lines_wrap_instead_of_clipping(tmp_path):
    """Claude often puts paste-ready prose inside ``` fences; Textual's fence block
    scrolls horizontally with a hidden scrollbar, so long lines were cut off."""
    from textual.widgets._markdown import MarkdownFence

    long_line = "word " * 80
    p = tmp_path / "s.jsonl"
    p.write_text(a(f"Intro:\n\n```\n{long_line.strip()}\n```\n"))
    app = ReaderApp(p, list_sessions(tmp_path))
    app.no_pick = True
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        fence = app.query_one(MarkdownFence)
        assert fence.virtual_size.width <= fence.size.width     # nothing hidden off-screen
        assert fence.size.height > 3                            # the line wrapped onto several rows


@pytest.mark.asyncio
async def test_jump_navigation(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text("".join(a(f"m{i}") for i in range(20)))
    app = ReaderApp(p, list_sessions(tmp_path), jump=5)
    app.no_pick = True
    async with app.run_test() as pilot:
        await pilot.pause()
        sb = app.query_one("#sidebar", ListView)
        assert sb.index == 19 and app.follow
        await pilot.press("g"); await pilot.pause()
        assert sb.index == 0 and not app.follow                # jumping back stops following
        await pilot.press("ctrl+d"); await pilot.pause()
        assert sb.index == 5
        await pilot.press("ctrl+d", "ctrl+d"); await pilot.pause()
        assert sb.index == 15
        await pilot.press("ctrl+u"); await pilot.pause()
        assert sb.index == 10
        await pilot.press("ctrl+u", "ctrl+u", "ctrl+u"); await pilot.pause()
        assert sb.index == 0                                   # clamped, no wrap, no crash
        await pilot.press("G"); await pilot.pause()
        assert sb.index == 19 and app.follow
        await pilot.press("ctrl+d"); await pilot.pause()
        assert sb.index == 19                                  # clamped at the end too
        # home/end move the selection, not just the sidebar's scroll offset
        await pilot.press("home"); await pilot.pause()
        assert sb.index == 0
        await pilot.press("end"); await pilot.pause()
        assert sb.index == 19


@pytest.mark.asyncio
async def test_jump_size_is_configurable(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text("".join(a(f"m{i}") for i in range(20)))
    app = ReaderApp(p, list_sessions(tmp_path), jump=3)
    app.no_pick = True
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g", "ctrl+d"); await pilot.pause()
        assert app.query_one("#sidebar", ListView).index == 3


@pytest.mark.asyncio
async def test_jump_on_empty_session_does_not_crash(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text("")
    app = ReaderApp(p, list_sessions(tmp_path))
    app.no_pick = True
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g", "G", "ctrl+d", "ctrl+u", "n", "slash"); await pilot.pause()
        assert len(app.query_one("#sidebar", ListView)) == 0


@pytest.mark.asyncio
async def test_search_modal_jumps_to_a_match(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text("".join(a(f"message {i}") for i in range(10)) + a("the needle here"))
    app = ReaderApp(p, list_sessions(tmp_path))
    app.no_pick = True
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g"); await pilot.pause()            # away from the match
        await pilot.press("slash"); await pilot.pause()
        assert isinstance(app.screen, MessageSearch)
        await pilot.press("N", "E", "E", "D", "L", "E"); await pilot.pause()   # case-insensitive
        assert app.screen.results == [10]
        await pilot.press("enter"); await pilot.pause()
        assert app.query_one("#sidebar", ListView).index == 10
        assert app.search_text == "NEEDLE"


@pytest.mark.asyncio
async def test_next_and_prev_match_wrap(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text("".join(a("hit" if i in (2, 5, 8) else f"m{i}") for i in range(10)))
    app = ReaderApp(p, list_sessions(tmp_path))
    app.no_pick = True
    async with app.run_test() as pilot:
        await pilot.pause()
        app.search_text = "hit"
        await pilot.press("g"); await pilot.pause()
        sb = app.query_one("#sidebar", ListView)
        for expected in (2, 5, 8, 2):                          # wraps past the last match
            await pilot.press("n"); await pilot.pause()
            assert sb.index == expected
        for expected in (8, 5, 2, 8):
            await pilot.press("N"); await pilot.pause()
            assert sb.index == expected


@pytest.mark.asyncio
async def test_search_survives_hidden_prompts_and_new_messages(tmp_path):
    """The sidebar is what n/N walks, so hiding prompts or tailing new lines must
    not leave stale positions behind."""
    p = tmp_path / "s.jsonl"
    p.write_text(u("needle from me") + "".join(a(f"m{i}") for i in range(5)))
    app = ReaderApp(p, list_sessions(tmp_path))
    app.no_pick = True
    async with app.run_test() as pilot:
        await pilot.pause()
        app.search_text = "needle"
        assert app._matches() == [0]
        await pilot.press("u"); await pilot.pause()            # hide my prompts
        assert app._matches() == []
        await pilot.press("u"); await pilot.pause()
        with open(p, "a") as f:
            f.write(a("another needle"))
        app.poll(); await pilot.pause()
        assert app._matches() == [0, 6]


@pytest.mark.asyncio
async def test_search_escape_keeps_the_query_but_does_not_move(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text("".join(a(f"m{i}") for i in range(5)) + a("needle"))
    app = ReaderApp(p, list_sessions(tmp_path))
    app.no_pick = True
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g"); await pilot.pause()
        await pilot.press("slash"); await pilot.pause()
        await pilot.press("n", "e", "e", "d"); await pilot.pause()
        await pilot.press("escape"); await pilot.pause()
        assert app.query_one("#sidebar", ListView).index == 0   # stayed put
        assert app.search_text == "need"                        # but n/N can pick it up
        await pilot.press("n"); await pilot.pause()
        assert app.query_one("#sidebar", ListView).index == 5
