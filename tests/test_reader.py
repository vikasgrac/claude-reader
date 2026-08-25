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


# ---- search -------------------------------------------------------------------

def usr(text):                        # `a()` above is the assistant counterpart
    return rec(type="user", message={"content": text}) + "\n"


def test_search_transcript_matches_case_insensitively_by_default(tmp_path):
    from claude_reader import compile_query, search_transcript
    p = tmp_path / "s.jsonl"
    p.write_text(a("The Needle is here") + a("nothing") + usr("needle again"))
    hits = search_transcript(p, compile_query("needle"))
    assert [h.message.role for h in hits] == ["assistant", "user"]
    assert hits[0].span == (4, 10)
    assert search_transcript(p, compile_query("needle", case_sensitive=True))[0].message.role == "user"


def test_search_transcript_honours_roles_limit_and_regex(tmp_path):
    from claude_reader import compile_query, search_transcript
    p = tmp_path / "s.jsonl"
    p.write_text(a("x1") + usr("x2") + a("x3"))
    assert len(search_transcript(p, compile_query(r"x\d", regex=True))) == 3
    assert len(search_transcript(p, compile_query("x"), roles={"user"})) == 1
    assert len(search_transcript(p, compile_query("x"), limit=2)) == 2
    assert search_transcript(p, compile_query(r"x\d")) == []          # not a regex unless asked


def test_search_transcript_survives_junk_and_missing_files(tmp_path):
    from claude_reader import compile_query, search_transcript
    p = tmp_path / "s.jsonl"
    p.write_text("garbage\n\n" + '{"type":"assistant","message":null}\n' + a("needle"))
    assert len(search_transcript(p, compile_query("needle"))) == 1
    assert search_transcript(tmp_path / "gone.jsonl", compile_query("needle")) == []


@pytest.mark.parametrize("query,parsed_only", [
    ("plain text", False), ("it's", False), ("~/.claude", False),
    ('has "quote"', True), ("back\\slash", True), ("café", True), ("multi\nline", True),
])
def test_prefilter_only_used_for_queries_json_cannot_escape(query, parsed_only):
    from claude_reader import make_prefilter
    assert (make_prefilter(query) is None) is parsed_only
    assert make_prefilter(query, regex=True) is None


def test_prefilter_agrees_with_parsing_everything(tmp_path):
    """The fast path must never lose a match the slow path would find."""
    from claude_reader import compile_query, make_prefilter, search_transcript
    p = tmp_path / "s.jsonl"
    p.write_text(a("it's a hit") + a("IT'S shouting") + a("no match here") + a("tab\there"))
    for q in ["it's", "IT'S", "hit", "tab"]:
        pat = compile_query(q)
        assert search_transcript(p, pat, prefilter=make_prefilter(q)) == search_transcript(p, pat)


def test_excerpt_trims_and_marks_the_match():
    from claude_reader import excerpt
    lead, mid, tail = excerpt("hello " * 40 + "NEEDLE" + " world" * 40, (240, 246))
    assert mid == "NEEDLE"
    assert lead.startswith("…") and tail.endswith("…")
    assert len(lead) + len(mid) + len(tail) <= 100
    # short text: no ellipses, newlines collapsed
    assert excerpt("a\n\nNEEDLE\nb", (3, 9)) == ("a ", "NEEDLE", " b")


def test_run_search_reports_sessions_and_exit_codes(tmp_path, monkeypatch, capsys):
    import claude_reader
    proj = tmp_path / "projects" / "-home-me-thing"
    proj.mkdir(parents=True)
    (proj / "aaaaaaaa-1111-2222-3333-444444444444.jsonl").write_text(
        json.dumps({"type": "assistant", "cwd": "/home/me/thing",
                    "message": {"content": [{"type": "text", "text": "the needle"}]}}) + "\n")
    (proj / "bbbbbbbb-1111-2222-3333-444444444444.jsonl").write_text(a("unrelated"))
    (proj / "agent-cccccccc.jsonl").write_text(a("needle in a subagent"))   # not a session
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(claude_reader, "config_roots", lambda: [tmp_path])

    assert claude_reader.run_search("needle", all_projects=True) == 0
    out = capsys.readouterr().out
    assert "/home/me/thing" in out                              # real cwd, not the munged dir name
    assert "aaaaaaaa-1111-2222-3333-444444444444" in out
    assert "bbbbbbbb" not in out and "agent-" not in out
    assert "1 match in 1 session across 1 project" in out

    assert claude_reader.run_search("absent", all_projects=True) == 1
    assert "no messages matching" in capsys.readouterr().out
    assert claude_reader.run_search("(unclosed", all_projects=True, regex=True) == 2


def test_resolve_session_id(tmp_path, monkeypatch):
    import claude_reader
    from claude_reader import StartupError, resolve_session_id
    for name in ["aaaaaaaa-1111.jsonl", "aaaaaaaa-2222.jsonl", "bbbbbbbb-1111.jsonl"]:
        d = tmp_path / "projects" / "-p"
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(a("hi"))
    monkeypatch.setattr(claude_reader, "config_roots", lambda: [tmp_path])
    assert resolve_session_id("bbbbbbbb-1111").name == "bbbbbbbb-1111.jsonl"
    assert resolve_session_id("bbbb").name == "bbbbbbbb-1111.jsonl"          # unique prefix
    with pytest.raises(StartupError, match="ambiguous"):
        resolve_session_id("aaaa")
    with pytest.raises(StartupError, match="no session id"):
        resolve_session_id("dddd")


def test_find_transcript_accepts_a_session_id(tmp_path, monkeypatch):
    import claude_reader
    from claude_reader import find_transcript
    d = tmp_path / "projects" / "-p"
    d.mkdir(parents=True)
    (d / "eeeeeeee-1111.jsonl").write_text(a("hi"))
    monkeypatch.setattr(claude_reader, "config_roots", lambda: [tmp_path])
    assert find_transcript("eeeeeeee-1111")[0] == d / "eeeeeeee-1111.jsonl"
    with pytest.raises(claude_reader.StartupError, match="not found"):
        find_transcript("./not/a/session")      # looks like a path: reported as a path


def test_run_search_groups_a_project_split_across_config_roots(tmp_path, monkeypatch, capsys):
    """The same project has its own transcript dir under every config root (the default
    account and each profile). That is one project, not one per root."""
    import claude_reader
    munged = "-home-me-thing"
    for root, session, text in [("default", "aaaaaaaa-1111", "needle one"),
                                ("profile", "bbbbbbbb-2222", "needle two"),
                                ("default", "cccccccc-3333", "needle three")]:
        d = tmp_path / root / "projects" / munged
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{session}.jsonl").write_text(
            json.dumps({"type": "assistant", "cwd": "/home/me/thing",
                        "message": {"content": [{"type": "text", "text": text}]}}) + "\n")
    other = tmp_path / "default" / "projects" / "-home-me-other"
    other.mkdir(parents=True)
    (other / "dddddddd-4444.jsonl").write_text(
        json.dumps({"type": "assistant", "cwd": "/home/me/other",
                    "message": {"content": [{"type": "text", "text": "needle four"}]}}) + "\n")
    monkeypatch.setattr(claude_reader, "config_roots",
                        lambda: [tmp_path / "default", tmp_path / "profile"])

    assert claude_reader.run_search("needle", all_projects=True) == 0
    out = capsys.readouterr().out
    assert out.count("/home/me/thing") == 1          # one header, not one per config root
    assert out.count("/home/me/other") == 1
    for session in ["aaaaaaaa-1111", "bbbbbbbb-2222", "cccccccc-3333", "dddddddd-4444"]:
        assert session in out
    assert "4 matches in 4 sessions across 2 projects" in out
