"""Parsing / tailing / CLI regression tests (no TUI). Run: python -m pytest -q"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from claude_reader import TranscriptTail, build_parser, find_transcript, list_sessions, parse_line, session_title, StartupError


def rec(**kw):
    return json.dumps(kw)


# ---- parse_line ---------------------------------------------------------------

@pytest.mark.parametrize("line", [
    "", "   ", "not json", "[1,2]", "null", '"str"', "42",
    rec(type="assistant", message=None),
    rec(type="assistant", message={"content": None}),
    rec(type="assistant", message={"content": [{"type": "text", "text": None}]}),
    rec(type="assistant", message={"content": [None, 1, "x", {"type": "text"}]}),
    rec(type="assistant", message="string"),
    rec(type="assistant", message={"content": {"type": "text", "text": "dict not list"}}),
    rec(type="user", message=None),
    rec(type="user", message={"content": [{"type": "tool_result"}]}),
    rec(type="user", message={"content": "x"}, toolUseResult={}),
    rec(type="user", message={"content": "[Request interrupted by user]"}),
    rec(type="user", message={"content": "<system-reminder>only this</system-reminder>"}),
    rec(type="progress", message={"content": "x"}),
    rec(type="assistant", isSidechain=True, message={"content": [{"type": "text", "text": "hi"}]}),
    rec(type="assistant", isMeta=True, message={"content": [{"type": "text", "text": "hi"}]}),
    rec(type="assistant", timestamp=5, uuid=7, message={"content": [{"type": "text", "text": "  "}]}),
])
def test_parse_line_skips_bad_shapes(line):
    assert parse_line(line) == []


def test_parse_assistant_joins_blocks():
    m = parse_line(rec(type="assistant", uuid="u1", timestamp="2026-08-17T06:00:00Z",
                       message={"content": [{"type": "text", "text": " a "}, {"type": "tool_use"}, {"type": "text", "text": "b"}]}))
    assert len(m) == 1 and m[0].role == "assistant" and m[0].text == "a\n\nb" and m[0].uuid == "u1" and m[0].timestamp


def test_parse_user_str_and_blocks_and_odd_types():
    assert parse_line(rec(type="user", message={"content": "hello"}))[0].text == "hello"
    m = parse_line(rec(type="user", timestamp=None, uuid=None, message={"content": [{"type": "text", "text": "x"}, {"type": "image"}]}))
    assert m[0].text == "x" and m[0].timestamp == "" and m[0].uuid == ""


def test_parse_user_strips_reminders():
    m = parse_line(rec(type="user", message={"content": "<system-reminder>zzz</system-reminder>real\n<local-command-stdout>o</local-command-stdout>"}))
    assert m[0].text == "real"


# ---- TranscriptTail -------------------------------------------------------------

def a(text):
    return rec(type="assistant", message={"content": [{"type": "text", "text": text}]}) + "\n"


def test_tail_incremental_partial_lines_and_utf8(tmp_path):
    p = tmp_path / "t.jsonl"
    t = TranscriptTail(p)
    assert t.poll() == (False, []) and t.error            # missing file: no news, error set once
    p.write_bytes(a("one").encode())
    reset, m = t.poll()
    assert not reset and [x.text for x in m] == ["one"] and t.error is None
    full = a("héllo wörld ✓").encode()
    with open(p, "ab") as f:
        f.write(full[:-7])                                # cut inside a multibyte char + no newline
    assert t.poll() == (False, [])
    with open(p, "ab") as f:
        f.write(full[-7:])
    assert [x.text for x in t.poll()[1]] == ["héllo wörld ✓"]
    with open(p, "ab") as f:
        f.write(b'{"type":"assistant","message":{"content":[{"type":"text","text":"bad \xff byte"}]}}\n')
    assert t.poll()[1][0].text.startswith("bad ")          # invalid byte replaced, no exception


def test_tail_truncate_and_replace_report_reset(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(a("one") + a("two"))
    t = TranscriptTail(p)
    assert len(t.poll()[1]) == 2
    p.write_text(a("new"))                                # truncated + rewritten, smaller
    reset, m = t.poll()
    assert reset and [x.text for x in m] == ["new"]
    # replaced by a *larger* file with a new inode (e.g. atomic rewrite)
    q = tmp_path / "t.new"
    q.write_text(a("r1") + a("r2") + a("r3"))
    os.replace(q, p)
    reset, m = t.poll()
    assert reset and [x.text for x in m] == ["r1", "r2", "r3"]
    assert t.poll() == (False, [])


def test_tail_directory_is_error_not_crash(tmp_path):
    t = TranscriptTail(tmp_path)
    assert t.poll() == (False, []) and t.error


# ---- discovery / CLI --------------------------------------------------------------

def test_sessions_and_titles(tmp_path):
    good = tmp_path / "s1.jsonl"; good.write_text(a("x") + rec(type="ai-title", aiTitle=" Good ") + "\n")
    bad = tmp_path / "s2.jsonl"; bad.write_text(rec(type="ai-title", aiTitle=None) + "\n" + '["ai-title"]\n')
    (tmp_path / "agent-1.jsonl").write_text(a("sub"))
    (tmp_path / "s3.jsonl").write_bytes(b'{"ai-title": \xff}\n')
    ss = list_sessions(tmp_path)
    assert {s.path.name for s in ss} == {"s1.jsonl", "s2.jsonl", "s3.jsonl"}
    assert {s.path.name: s.title for s in ss}["s1.jsonl"] == "Good"
    assert {s.path.name: s.title for s in ss}["s2.jsonl"] == "(untitled)"
    assert session_title(good) == "Good" and session_title(tmp_path / "nope.jsonl") == "(untitled)"
    assert list_sessions(tmp_path / "missing") == []


def test_find_transcript_errors(tmp_path):
    with pytest.raises(StartupError):
        find_transcript(str(tmp_path))                     # a directory
    with pytest.raises(StartupError):
        find_transcript(str(tmp_path / "nope.jsonl"))
    f = tmp_path / "s.jsonl"; f.write_text(a("x"))
    assert find_transcript(str(f))[0] == f


def test_cli_rejects_unknown_options_and_extra_paths():
    p = build_parser()
    with pytest.raises(SystemExit) as e:
        p.parse_args(["--bogus"])
    assert e.value.code == 2
    with pytest.raises(SystemExit) as e:
        p.parse_args(["a.jsonl", "b.jsonl"])
    assert e.value.code == 2
    ns = p.parse_args(["x.jsonl", "--no-pick", "--mouse", "--max-messages", "10"])
    assert ns.transcript == "x.jsonl" and ns.no_pick and ns.mouse and ns.max_messages == 10


def test_main_exit_codes(tmp_path):
    env = dict(os.environ, HOME=str(tmp_path))
    r = subprocess.run([sys.executable, "-m", "claude_reader", "--bogus"], capture_output=True, env=env, cwd=str(Path(__file__).resolve().parents[1]))
    assert r.returncode == 2 and b"usage" in r.stderr
    r = subprocess.run([sys.executable, "-m", "claude_reader", str(tmp_path / "missing.jsonl")], capture_output=True, env=env, cwd=str(Path(__file__).resolve().parents[1]))
    assert r.returncode == 1 and b"transcript not found" in r.stderr
