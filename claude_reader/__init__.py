#!/usr/bin/env python3
"""claude-reader: live TUI viewer for the prose Claude Code writes to you.

Reads the session transcript (<config-root>/projects/<munged-cwd>/<session>.jsonl,
where the config roots scanned are $CLAUDE_CONFIG_DIR, ~/.claude, and any
per-account profiles in ~/.claude-profiles/), keeps only assistant text blocks
and your own prompts, and shows them in a sidebar + reading pane. Tails the
file so new messages appear live.

Usage:
    claude-reader                     # newest session for the current directory
    claude-reader <transcript.jsonl>  # a specific transcript file
    claude-reader <session-id>        # a session by id (or any unique prefix)
    claude-reader -s "text"           # search this project's sessions and exit
    claude-reader -s "text" -a        # ... every project's sessions
"""

import argparse
import json
import os
import re
import subprocess
import sys
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Footer, Header, Input, Label, ListItem, ListView, Markdown

def config_roots() -> list[Path]:
    """Claude Code config directories that may hold transcripts, in preference order:
    $CLAUDE_CONFIG_DIR (if set), the default ~/.claude, and any per-account profile
    directories under ~/.claude-profiles/ (each is a full CLAUDE_CONFIG_DIR)."""
    roots = []
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        roots.append(Path(env).expanduser())
    roots.append(Path.home() / ".claude")
    profiles = Path.home() / ".claude-profiles"
    if profiles.is_dir():
        try:
            roots.extend(sorted(p for p in profiles.iterdir() if p.is_dir()))
        except OSError:
            pass
    seen: set[Path] = set()
    unique = []
    for r in roots:
        key = r.resolve() if r.exists() else r
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique


def profile_label(transcript: Path) -> str:
    """Short name of the config root a transcript lives under: 'default' for
    ~/.claude, the profile directory's name for ~/.claude-profiles/<name>,
    '' when it is somewhere else entirely (an explicit path)."""
    root = transcript.parent.parent.parent  # <root>/projects/<munged-cwd>/<session>.jsonl
    if root == Path.home() / ".claude":
        return "default"
    if root.parent == Path.home() / ".claude-profiles":
        return root.name
    return ""
SYSTEM_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
COMMAND_TAG_RE = re.compile(r"<[a-z-]+-(?:command|stdout|stderr)[^>]*>.*?</[a-z-]+[^>]*>", re.DOTALL)


@dataclass
class Message:
    role: str  # "assistant" | "user"
    text: str
    timestamp: str  # "HH:MM:SS" local, best effort
    uuid: str


def munge_path(path: Path) -> str:
    return re.sub(r"[^A-Za-z0-9-]", "-", str(path))


@dataclass
class SessionInfo:
    path: Path
    title: str
    mtime: float

    @property
    def short_id(self) -> str:
        return self.path.stem[:8]

    @property
    def last_active(self) -> str:
        return datetime.fromtimestamp(self.mtime).strftime("%H:%M:%S")

    @property
    def age_seconds(self) -> float:
        return datetime.now().timestamp() - self.mtime


_TITLE_CACHE: dict[Path, tuple[float, int, str]] = {}   # path -> (mtime, size, title)


def session_title(path: Path, mtime: float | None = None, size: int | None = None) -> str:
    """Claude Code writes an `ai-title` line once it has named the session.

    Cached by (mtime, size) so the picker does not re-scan every transcript each time it opens.
    """
    if mtime is None or size is None:
        try:
            st = path.stat()
            mtime, size = st.st_mtime, st.st_size
        except OSError:
            return "(untitled)"
    hit = _TITLE_CACHE.get(path)
    if hit and hit[0] == mtime and hit[1] == size:
        return hit[2]
    title = "(untitled)"
    try:
        with open(path, "rb") as f:
            for raw in f:
                if b'"ai-title"' not in raw:
                    continue
                try:
                    d = json.loads(raw.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
                if isinstance(d, dict) and isinstance(d.get("aiTitle"), str) and d["aiTitle"].strip():
                    title = d["aiTitle"].strip()
                    break
    except OSError:
        pass
    _TITLE_CACHE[path] = (mtime, size, title)
    return title


class StartupError(Exception):
    """Something we can not open at startup; main() prints it and exits 1."""


def project_dirs_for_cwd() -> list[Path]:
    """Every config root's project directory for the current cwd (profiles included)."""
    munged = munge_path(Path.cwd())
    candidates = [root / "projects" / munged for root in config_roots()]
    dirs = [d for d in candidates if d.is_dir()]
    if not dirs:
        looked = "\n".join(f"  {c}" for c in candidates)
        raise StartupError(f"no Claude Code transcripts for {Path.cwd()}\n(looked in:\n{looked})")
    return dirs


def list_sessions(project_dirs) -> list[SessionInfo]:
    """All main-conversation transcripts across the given project dir(s), newest first.
    Never raises."""
    if isinstance(project_dirs, Path):
        project_dirs = [project_dirs]
    sessions = []
    stats = []
    seen: set[Path] = set()
    for project_dir in project_dirs:
        try:
            paths = list(project_dir.glob("*.jsonl"))
        except OSError:
            continue
        for p in paths:
            if p.name.startswith("agent-") or p in seen:
                continue
            seen.add(p)
            try:
                st = p.stat()
            except OSError:      # deleted between glob and stat
                continue
            stats.append((p, st.st_mtime, st.st_size))
    stats.sort(key=lambda t: t[1], reverse=True)
    for p, mtime, size in stats:
        sessions.append(SessionInfo(p, session_title(p, mtime, size), mtime))
    return sessions


RECENT_SECONDS = 3600  # sessions active within the last hour count as "live"
DEFAULT_JUMP = 5       # messages ctrl+d / ctrl+u move by (--jump)

# a bare session id (or a prefix of one) rather than a path: hex and dashes only
SESSION_TOKEN_RE = re.compile(r"^[0-9a-fA-F][0-9a-fA-F-]{3,}$")


def find_transcript(explicit: str | None) -> tuple[Path, list[SessionInfo], list[Path]]:
    """Return (transcript to open, all sessions, project dirs scanned). An explicit
    transcript path — or a session id, as printed by `--search` — wins over the cwd."""
    if explicit:
        p = Path(explicit).expanduser()
        if not p.is_file():
            if SESSION_TOKEN_RE.match(explicit):
                p = resolve_session_id(explicit)      # raises if unknown or ambiguous
            else:
                raise StartupError(f"transcript not found (or not a file): {p}")
        try:
            with open(p, "rb"):
                pass
        except OSError as e:
            raise StartupError(f"cannot read {p}: {e.strerror or e}")
        return p, list_sessions(p.parent), [p.parent]
    dirs = project_dirs_for_cwd()
    sessions = list_sessions(dirs)
    if not sessions:
        raise StartupError("no session files found")
    return sessions[0].path, sessions, dirs


def local_time(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().strftime("%H:%M:%S")
    except (ValueError, AttributeError):
        return ""


def clean_user_text(text: str) -> str:
    text = SYSTEM_REMINDER_RE.sub("", text)
    text = COMMAND_TAG_RE.sub("", text)
    return text.strip()


def _text_blocks(content) -> list[str]:
    """The text parts of a message `content` field, whatever shape it has. Unknown shapes -> []."""
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    out = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            out.append(block["text"])
    return out


def parse_line(line: str) -> list[Message]:
    """Extract zero or more display messages from one transcript JSONL line.

    Anything that is not the shape we expect (nulls, arrays, missing keys, other record
    types) is skipped, never raised: a half-written or newer-format line must not kill the UI.
    """
    try:
        d = json.loads(line)
    except (json.JSONDecodeError, RecursionError, ValueError):
        return []
    if not isinstance(d, dict) or d.get("isSidechain") or d.get("isMeta"):
        return []

    kind = d.get("type")
    ts_raw = d.get("timestamp")
    ts = local_time(ts_raw) if isinstance(ts_raw, str) else ""
    uuid = d.get("uuid") if isinstance(d.get("uuid"), str) else ""
    message = d.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    msgs: list[Message] = []

    if kind == "assistant":
        # one assistant record = one turn: join its text blocks instead of one entry per block
        text = "\n\n".join(t.strip() for t in _text_blocks(content) if t.strip())
        if text:
            msgs.append(Message("assistant", text, ts, uuid))

    elif kind == "user" and "toolUseResult" not in d:
        text = clean_user_text("\n".join(_text_blocks(content)))
        # Skip interruption notices and command-invocation husks.
        if text and not text.startswith("[Request interrupted"):
            msgs.append(Message("user", text, ts, uuid))

    return msgs


PARTIAL_MAX = 32 * 1024 * 1024   # an unterminated line longer than this is corrupt: drop it


class TranscriptTail:
    """Incremental reader: each poll() returns (reset, messages).

    * bytes in, lines out: the file is read in binary, split on b"\n", and each complete line is
      decoded on its own — a multibyte character straddling the current end of file, or a stray
      invalid byte, can not raise.
    * (device, inode) and size are tracked: truncation or replacement (a smaller file, or a new
      inode) starts over and reports reset=True so the caller can drop what it showed.
    * a missing/unreadable file is "no news", reported once via .error (None when fine).
    """

    def __init__(self, path: Path):
        self.path = path
        self._pos = 0
        self._buf = b""
        self._ident: tuple[int, int] | None = None
        self.error: str | None = None
        self._reported = False

    def _reset(self):
        self._pos = 0
        self._buf = b""

    def poll(self) -> tuple[bool, list[Message]]:
        reset = False
        try:
            f = open(self.path, "rb")
        except OSError as e:
            if not self._reported:
                self.error = f"{self.path.name}: {e.strerror or e}"
                self._reported = True
            return False, []
        with f:
            try:
                st = os.fstat(f.fileno())
            except OSError as e:
                self.error = f"{self.path.name}: {e.strerror or e}"
                return False, []
            self.error = None
            self._reported = False
            ident = (st.st_dev, st.st_ino)
            if self._ident is not None and (ident != self._ident or st.st_size < self._pos):
                self._reset()          # replaced or truncated: start over
                reset = True
            self._ident = ident
            if st.st_size == self._pos:
                return reset, []
            try:
                f.seek(self._pos)
                chunk = f.read()
            except OSError as e:
                self.error = f"{self.path.name}: {e.strerror or e}"
                return reset, []
            self._pos += len(chunk)
        self._buf += chunk
        lines = self._buf.split(b"\n")
        self._buf = lines.pop()  # keep any partial trailing line for next poll
        if len(self._buf) > PARTIAL_MAX:
            self._buf = b""
        msgs = []
        for raw in lines:
            if raw.strip():
                msgs.extend(parse_line(raw.decode("utf-8", errors="replace")))
        return reset, msgs


def sidebar_title(msg: Message) -> str:
    first = next((ln.strip() for ln in msg.text.splitlines() if ln.strip()), "")
    first = re.sub(r"[#*`>]", "", first).strip()
    if len(first) > 46:
        first = first[:45] + "…"
    return first or "(empty)"


# ---- search ------------------------------------------------------------------

@dataclass
class Hit:
    """One matching message inside one transcript."""
    transcript: Path
    message: Message
    span: tuple[int, int]   # match offsets inside message.text


# A query whose bytes must appear verbatim in an encoded JSONL line, so we can use it
# to skip lines without parsing them: printable ASCII only, minus the two characters
# JSON always escapes. Anything else (control chars, quotes, backslashes, non-ASCII —
# which a writer may emit as \uXXXX) could be escaped in the file and is not safe.
PLAIN_QUERY_RE = re.compile(r'^[\x20-\x21\x23-\x5b\x5d-\x7e]+$')


def compile_query(query: str, regex: bool = False, case_sensitive: bool = False) -> re.Pattern:
    flags = 0 if case_sensitive else re.IGNORECASE
    return re.compile(query if regex else re.escape(query), flags)


def make_prefilter(query: str, regex: bool = False, case_sensitive: bool = False):
    """A cheap `is this raw line worth parsing?` test, or None when every line must be parsed.

    Searching every project means reading a few hundred megabytes of JSONL; skipping
    json.loads for the ~99.9% of lines that can not match keeps that under a second.
    """
    if regex or not PLAIN_QUERY_RE.match(query):
        return None
    if case_sensitive:
        needle = query.encode("utf-8")
        return lambda raw: needle in raw
    needle = query.lower().encode("utf-8")
    return lambda raw: needle in raw.lower()


def excerpt(text: str, span: tuple[int, int], width: int = 96) -> tuple[str, str, str]:
    """A one-line window around a match, as (before, match, after).

    Whitespace is collapsed in each part separately so the pieces can be styled
    (bold in a terminal, reverse in the TUI) without recomputing offsets.
    """
    start, end = span
    lead_room = max(12, width // 3)
    win_start = max(0, start - lead_room)
    win_end = min(len(text), end + width + lead_room)
    lead = re.sub(r"\s+", " ", text[win_start:start]).lstrip()
    hit = re.sub(r"\s+", " ", text[start:end])[:width]
    full_tail = re.sub(r"\s+", " ", text[end:win_end]).rstrip()
    tail = full_tail[:max(0, width - len(lead) - len(hit))]
    if win_start > 0:
        lead = "…" + lead
    if win_end < len(text) or len(tail) < len(full_tail):
        tail += "…"
    return lead, hit, tail


def search_transcript(path: Path, pattern: re.Pattern, roles: set[str] | None = None,
                      limit: int = 0, prefilter=None) -> list[Hit]:
    """Matching messages in one transcript, in file order. Never raises: an unreadable
    or half-written file just contributes nothing."""
    hits: list[Hit] = []
    try:
        with open(path, "rb") as f:
            for raw in f:
                if prefilter is not None and not prefilter(raw):
                    continue
                if not raw.strip():
                    continue
                for msg in parse_line(raw.decode("utf-8", errors="replace")):
                    if roles and msg.role not in roles:
                        continue
                    m = pattern.search(msg.text)
                    if m:
                        hits.append(Hit(path, msg, m.span()))
                        if limit and len(hits) >= limit:
                            return hits
    except OSError:
        pass
    return hits


def all_project_dirs() -> list[Path]:
    """Every <config-root>/projects/<munged-cwd> directory, across roots and profiles."""
    dirs: list[Path] = []
    seen: set[Path] = set()
    for root in config_roots():
        try:
            children = sorted((root / "projects").iterdir())
        except OSError:
            continue
        for d in children:
            key = d.resolve() if d.exists() else d
            if d.is_dir() and key not in seen:
                seen.add(key)
                dirs.append(d)
    return dirs


_PROJECT_CWD_RE = re.compile(rb'"cwd"\s*:\s*"((?:[^"\\]|\\.)*)"')
_PROJECT_CWD_CACHE: dict[Path, str] = {}


def project_cwd(project_dir: Path) -> str:
    """The directory a project's sessions were run from.

    The directory name is a lossy munge of that path (every separator became '-'),
    so read the real thing out of a transcript's `cwd` field; fall back to the name.
    """
    hit = _PROJECT_CWD_CACHE.get(project_dir)
    if hit is not None:
        return hit
    answer = project_dir.name
    try:
        transcripts = sorted(project_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        transcripts = []
    for p in transcripts[:3]:
        try:
            with open(p, "rb") as f:
                for _ in range(200):
                    raw = f.readline()
                    if not raw:
                        break
                    m = _PROJECT_CWD_RE.search(raw)
                    if m:
                        try:
                            answer = json.loads(b'"' + m.group(1) + b'"')
                        except ValueError:
                            answer = m.group(1).decode("utf-8", errors="replace")
                        break
        except OSError:
            continue
        if answer != project_dir.name:
            break
    _PROJECT_CWD_CACHE[project_dir] = answer
    return answer


def resolve_session_id(token: str) -> Path:
    """A session id (full, or any unique prefix) -> its transcript, searched across
    every config root. Raises StartupError when nothing or more than one thing matches."""
    matches: list[Path] = []
    for d in all_project_dirs():
        try:
            paths = list(d.glob(f"{token}*.jsonl"))
        except OSError:
            continue
        matches.extend(p for p in paths if not p.name.startswith("agent-"))
    if not matches:
        raise StartupError(f"no session id starting with {token!r} (and no such file)")
    if len(set(matches)) > 1:
        listed = "\n".join(f"  {p.stem}  in {project_cwd(p.parent)}" for p in sorted(matches))
        raise StartupError(f"session id {token!r} is ambiguous:\n{listed}")
    return matches[0]


class SessionPicker(ModalScreen[Path | None]):
    """Modal list of the project's sessions; dismisses with the chosen path."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    SessionPicker { align: center middle; }
    #picker-box {
        width: 80%;
        max-width: 100;
        height: auto;
        max-height: 80%;
        border: round $primary;
        background: $surface;
        padding: 1 2;
    }
    #picker-title { text-style: bold; margin-bottom: 1; }
    #picker-list { height: auto; max-height: 24; }
    #picker-list > ListItem { padding: 0 1; }
    .sess-title { text-style: bold; }
    .sess-meta { color: $text-muted; }
    .sess-live .sess-meta { color: $success; }
    """

    def __init__(self, sessions: list[SessionInfo], current: Path):
        super().__init__()
        self.sessions = sessions
        self.current = current

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-box"):
            yield Label("Sessions in this project  (Enter to open · Esc to cancel)", id="picker-title")
            lv = ListView(id="picker-list")
            yield lv

    def on_mount(self) -> None:
        lv = self.query_one("#picker-list", ListView)
        for i, s in enumerate(self.sessions):
            live = s.age_seconds < RECENT_SECONDS
            marker = "● " if s.path == self.current else "  "
            prof = profile_label(s.path)
            meta = f"{s.short_id} · last active {s.last_active}" \
                + (f" · {prof}" if prof else "") + ("  (live)" if live else "")
            item = ListItem(
                Label(marker + s.title, classes="sess-title"),
                Label("  " + meta, classes="sess-meta"),
            )
            if live:
                item.add_class("sess-live")
            item.session = s  # type: ignore[attr-defined]
            lv.append(item)
            if s.path == self.current:
                lv.index = i
        lv.focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.dismiss(event.item.session.path)  # type: ignore[attr-defined]

    def action_cancel(self) -> None:
        self.dismiss(None)


class MessageSearch(ModalScreen[tuple[int | None, str]]):
    """Incremental search over the messages currently in the sidebar.

    Dismisses with (sidebar index to jump to, query). The index is None when the
    search was cancelled, but the query comes back either way so n/N can reuse it.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("down", "cursor_down", show=False),
        Binding("up", "cursor_up", show=False),
    ]

    MAX_RESULTS = 200

    DEFAULT_CSS = """
    MessageSearch { align: center middle; }
    #search-box {
        width: 88%;
        max-width: 110;
        height: auto;
        max-height: 80%;
        border: round $primary;
        background: $surface;
        padding: 1 2;
    }
    #search-title { text-style: bold; margin-bottom: 1; }
    #search-results { height: auto; max-height: 20; margin-top: 1; }
    #search-results > ListItem { padding: 0 1; }
    #search-count { color: $text-muted; margin-top: 1; }
    .find-meta { color: $text-muted; }
    """

    def __init__(self, entries: list[tuple[int, Message]], initial: str = ""):
        super().__init__()
        self.entries = entries
        self.initial = initial
        self.results: list[int] = []       # sidebar indices, parallel to the results list

    def compose(self) -> ComposeResult:
        with Vertical(id="search-box"):
            yield Label("Search this session  (Enter to jump · ↑↓ to pick · Esc to cancel)", id="search-title")
            yield Input(placeholder="text to find…", value=self.initial, id="search-input")
            yield ListView(id="search-results")
            yield Label("", id="search-count")

    def on_mount(self) -> None:
        inp = self.query_one("#search-input", Input)
        inp.focus()
        inp.cursor_position = len(inp.value)
        self._show(self.initial)

    def _show(self, text: str) -> None:
        lv = self.query_one("#search-results", ListView)
        lv.clear()
        self.results = []
        count = self.query_one("#search-count", Label)
        if not text:
            count.update("")
            return
        pattern = compile_query(text)
        total = 0
        for index, msg in self.entries:
            m = pattern.search(msg.text)
            if not m:
                continue
            total += 1
            if len(self.results) >= self.MAX_RESULTS:
                continue
            self.results.append(index)
            who = "you" if msg.role == "user" else "claude"
            lead, mid, tail = excerpt(msg.text, m.span(), width=80)
            line = Text(f"{who:>6} {msg.timestamp}  ", style="dim")
            line.append(lead)
            line.append(mid, style="reverse")
            line.append(tail)
            lv.append(ListItem(Label(line)))
        shown = f" (first {self.MAX_RESULTS} shown)" if total > len(self.results) else ""
        count.update(f"{total} match{'es' if total != 1 else ''}{shown}" if total else "no matches")
        if self.results:
            lv.index = 0

    def on_input_changed(self, event: Input.Changed) -> None:
        self._show(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._jump(self.query_one("#search-results", ListView).index)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self._jump(event.list_view.index)

    def _jump(self, position: int | None) -> None:
        text = self.query_one("#search-input", Input).value
        if position is None or not self.results:
            self.dismiss((None, text))
            return
        self.dismiss((self.results[min(position, len(self.results) - 1)], text))

    def action_cursor_down(self) -> None:
        self.query_one("#search-results", ListView).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#search-results", ListView).action_cursor_up()

    def action_cancel(self) -> None:
        self.dismiss((None, self.query_one("#search-input", Input).value))


class MessageList(ListView):
    """The sidebar list. ListView inherits home/end from ScrollView, which scrolls the
    viewport and leaves the highlight (and so the reading pane) behind; here they move
    the selection, like g/G."""

    BINDINGS = [
        Binding("home", "first_message", "First message", show=False),
        Binding("end", "last_message", "Last message", show=False),
    ]

    def action_first_message(self) -> None:
        self.app.action_first_msg()

    def action_last_message(self) -> None:
        self.app.action_last_msg()


class ReaderApp(App):
    TITLE = "claude-reader"

    CSS = """
    #sidebar {
        width: 34%;
        min-width: 28;
        border-right: solid $primary-darken-2;
    }
    #sidebar > ListItem {
        padding: 0 1;
    }
    #sidebar > ListItem.user-msg Label {
        color: $text-muted;
        text-style: italic;
    }
    .msg-title { text-style: bold; }
    .msg-meta { color: $text-muted; }
    #reader {
        padding: 0 2;
    }
    /* Textual's fenced-code block scrolls horizontally (with the scrollbar
       hidden), so long lines inside ``` blocks were silently clipped at the
       pane edge. Claude Code soft-wraps them; do the same. */
    MarkdownFence {
        overflow: hidden hidden;
    }
    MarkdownFence > Label {
        width: 1fr;
        text-wrap: wrap;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("f", "follow", "Follow latest"),
        Binding("u", "toggle_user", "Show/hide my prompts"),
        Binding("s", "toggle_sidebar", "Show/hide sidebar"),
        Binding("c", "copy_message", "Copy message"),
        Binding("p", "pick_session", "Sessions"),
        Binding("slash", "search", "Search", key_display="/"),
        Binding("j", "next_msg", "Next", show=False),
        Binding("k", "prev_msg", "Prev", show=False),
        Binding("g", "first_msg", "First message", show=False),
        Binding("G", "last_msg", "Last message", show=False),
        Binding("ctrl+d", "jump_down", "Jump down", show=False),
        Binding("ctrl+u", "jump_up", "Jump up", show=False),
        Binding("n", "next_match", "Next match", show=False),
        Binding("N", "prev_match", "Previous match", show=False),
    ]

    def __init__(self, transcript: Path, sessions: list[SessionInfo], max_messages: int = 500,
                 session_dirs: list[Path] | None = None, jump: int = DEFAULT_JUMP):
        super().__init__()
        self.transcript = transcript
        self.sessions = sessions
        self.session_dirs = session_dirs or [transcript.parent]
        self.tail = TranscriptTail(transcript)
        self.max_messages = max(1, max_messages)
        # bounded: only the most recent max_messages are kept (and mounted as widgets)
        self.messages: deque[Message] = deque(maxlen=self.max_messages)
        self.jump = max(1, jump)
        self.follow = True
        self.show_user = True
        self.no_pick = False
        self.search_text = ""
        self._last_error: str | None = None
        self._set_subtitle()

    def _set_subtitle(self) -> None:
        info = next((s for s in self.sessions if s.path == self.transcript), None)
        title = info.title if info else self.transcript.stem[:8]
        self.sub_title = f"{title}  [{self.transcript.stem[:8]}]"

    def compose(self) -> ComposeResult:
        # No clock: it would force a redraw every second, and periodic output
        # from this pane breaks drag-selection in the neighbouring Claude pane.
        yield Header()
        with Horizontal():
            yield MessageList(id="sidebar")
            with VerticalScroll(id="reader"):
                yield Markdown("*Waiting for messages…*", id="body")
        yield Footer()

    def on_mount(self) -> None:
        self.poll()
        self.set_interval(0.5, self.poll)
        # More than one session touched within the last hour: ask which one.
        live = [s for s in self.sessions if s.age_seconds < RECENT_SECONDS]
        if len(live) > 1 and not self.no_pick:
            self.action_pick_session()

    def switch_session(self, path: Path | None) -> None:
        if path is None or path == self.transcript:
            return
        self.transcript = path
        self.tail = TranscriptTail(path)
        self.messages = deque(maxlen=self.max_messages)
        self.follow = True
        if path.parent not in self.session_dirs:
            self.session_dirs.append(path.parent)
        self.sessions = list_sessions(self.session_dirs)
        self._set_subtitle()
        self.query_one("#sidebar", ListView).clear()
        self.query_one("#body", Markdown).update("*Waiting for messages…*")
        self.poll()

    # ---- data flow ----------------------------------------------------

    def poll(self) -> None:
        reset, new = self.tail.poll()
        if self.tail.error != self._last_error:
            self._last_error = self.tail.error
            if self.tail.error:
                self.notify(self.tail.error, title="transcript unavailable", severity="warning", timeout=8)
        if reset:
            # the file was truncated or replaced: what we show is stale, start from what it now says
            self.messages.clear()
            self.query_one("#sidebar", ListView).clear()
            self.query_one("#body", Markdown).update("*Transcript was rewritten — reloading…*")
            self.follow = True
        if not new:
            return
        if len(new) > self.max_messages:
            new = new[-self.max_messages:]     # initial load of a huge file: only the tail is shown
        self.messages.extend(new)
        sidebar = self.query_one("#sidebar", ListView)
        for msg in new:
            if msg.role == "user" and not self.show_user:
                continue
            sidebar.append(self._make_item(msg))
        self._trim_sidebar(sidebar)
        if self.follow and len(sidebar) > 0:
            sidebar.index = len(sidebar) - 1

    def _trim_sidebar(self, sidebar: ListView) -> None:
        """Drop the oldest widgets so the sidebar never grows past max_messages."""
        excess = len(sidebar) - self.max_messages
        if excess > 0:
            for item in list(sidebar.children)[:excess]:
                item.remove()

    def _make_item(self, msg: Message) -> ListItem:
        who = "you" if msg.role == "user" else "claude"
        item = ListItem(
            Label(sidebar_title(msg), classes="msg-title"),
            Label(f"{who} · {msg.timestamp}", classes="msg-meta"),
        )
        if msg.role == "user":
            item.add_class("user-msg")
        item.msg = msg  # type: ignore[attr-defined]
        return item

    def _rebuild_sidebar(self) -> None:
        sidebar = self.query_one("#sidebar", ListView)
        sidebar.clear()
        for msg in self.messages:
            if msg.role == "user" and not self.show_user:
                continue
            sidebar.append(self._make_item(msg))
        if len(sidebar) > 0:
            sidebar.index = len(sidebar) - 1

    # ---- events ---------------------------------------------------------

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.item is None or event.list_view.id != "sidebar":
            return
        msg: Message = event.item.msg  # type: ignore[attr-defined]
        sidebar = self.query_one("#sidebar", ListView)
        self.follow = sidebar.index == len(sidebar) - 1
        who = "You" if msg.role == "user" else "Claude"
        heading = f"*{who} · {msg.timestamp}*\n\n---\n\n"
        self.query_one("#body", Markdown).update(heading + msg.text)
        self.query_one("#reader", VerticalScroll).scroll_home(animate=False)

    # ---- actions ----------------------------------------------------------

    def action_follow(self) -> None:
        self.follow = True
        sidebar = self.query_one("#sidebar", ListView)
        if len(sidebar) > 0:
            sidebar.index = len(sidebar) - 1

    def action_pick_session(self) -> None:
        self.sessions = list_sessions(self.session_dirs)
        self.push_screen(SessionPicker(self.sessions, self.transcript), self.switch_session)

    def action_copy_message(self) -> None:
        sidebar = self.query_one("#sidebar", ListView)
        item = sidebar.highlighted_child
        if item is None:
            return
        msg: Message = item.msg  # type: ignore[attr-defined]
        self.copy_to_clipboard(msg.text)
        self.notify(f"Copied message to clipboard ({len(msg.text)} chars)")

    def copy_to_clipboard(self, text: str) -> None:
        # Used by both `c` (whole message) and ctrl+c (mouse-selected snippet).
        # Under tmux with `set-clipboard external`, OSC 52 from apps in panes
        # is ignored, so hand the text to tmux directly; -w forwards it on to
        # the outer terminal's clipboard. The text goes into a dedicated named
        # buffer that is deleted right after, so it does not linger in tmux's
        # paste history.
        if not os.environ.get("TMUX"):
            super().copy_to_clipboard(text)
            return
        try:
            r = subprocess.run(["tmux", "load-buffer", "-w", "-b", "claude-reader", "-"],
                               input=text.encode(), capture_output=True, timeout=5)
            subprocess.run(["tmux", "delete-buffer", "-b", "claude-reader"], capture_output=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired) as e:
            self.notify(f"copy failed: {e}", severity="error")
            return
        if r.returncode != 0:
            self.notify(f"copy failed: {r.stderr.decode(errors='replace').strip() or 'tmux error'}", severity="error")

    def action_toggle_sidebar(self) -> None:
        sidebar = self.query_one("#sidebar", ListView)
        sidebar.display = not sidebar.display
        if not sidebar.display:
            self.query_one("#reader", VerticalScroll).focus()

    def action_toggle_user(self) -> None:
        self.show_user = not self.show_user
        self._rebuild_sidebar()

    def action_next_msg(self) -> None:
        self.query_one("#sidebar", ListView).action_cursor_down()

    def action_prev_msg(self) -> None:
        self.query_one("#sidebar", ListView).action_cursor_up()

    # ---- jumping around ---------------------------------------------------

    def _sidebar(self) -> ListView:
        return self.query_one("#sidebar", ListView)

    def _goto(self, index: int) -> None:
        """Highlight a message by position, clamped to the list. Highlighting it
        updates `follow` (see on_list_view_highlighted), so jumping back in history
        stops the pane from snapping to the latest message."""
        sidebar = self._sidebar()
        if len(sidebar) == 0:
            return
        sidebar.index = max(0, min(index, len(sidebar) - 1))

    def action_first_msg(self) -> None:
        self._goto(0)

    def action_last_msg(self) -> None:
        self.action_follow()

    def action_jump_down(self) -> None:
        self._goto((self._sidebar().index or 0) + self.jump)

    def action_jump_up(self) -> None:
        self._goto((self._sidebar().index or 0) - self.jump)

    # ---- search -----------------------------------------------------------

    def action_search(self) -> None:
        entries = [(i, item.msg) for i, item in enumerate(self._sidebar().children)]  # type: ignore[attr-defined]
        if not entries:
            self.notify("no messages to search yet")
            return
        self.push_screen(MessageSearch(entries, self.search_text), self._search_done)

    def _search_done(self, result: tuple[int | None, str] | None) -> None:
        if result is None:
            return
        index, text = result
        self.search_text = text
        if index is not None:
            self._goto(index)
            self._report_match(index)

    def _matches(self) -> list[int]:
        """Positions in the sidebar that match the current query. Recomputed on each
        n/N because the list grows (and is trimmed) while the session is live."""
        if not self.search_text:
            return []
        pattern = compile_query(self.search_text)
        return [i for i, item in enumerate(self._sidebar().children)
                if pattern.search(item.msg.text)]      # type: ignore[attr-defined]

    def _report_match(self, index: int) -> None:
        hits = self._matches()
        if index in hits:
            self.notify(f"match {hits.index(index) + 1} of {len(hits)} for {self.search_text!r}", timeout=3)

    def action_next_match(self) -> None:
        self._step_match(1)

    def action_prev_match(self) -> None:
        self._step_match(-1)

    def _step_match(self, direction: int) -> None:
        if not self.search_text:
            self.notify("no search yet — press / to search")
            return
        hits = self._matches()
        if not hits:
            self.notify(f"nothing matches {self.search_text!r}", severity="warning")
            return
        current = self._sidebar().index or 0
        if direction > 0:
            target = next((i for i in hits if i > current), hits[0])          # wraps to the top
        else:
            target = next((i for i in reversed(hits) if i < current), hits[-1])
        self._goto(target)
        self._report_match(target)


ROLE_WORD = {"user": "you", "assistant": "claude"}


class Ink:
    """ANSI styling, switched off for pipes, dumb terminals and NO_COLOR."""

    def __init__(self, enabled: bool):
        self.on = enabled

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.on else text

    def bold(self, t): return self._wrap("1", t)
    def dim(self, t): return self._wrap("2", t)
    def head(self, t): return self._wrap("1;36", t)
    def mark(self, t): return self._wrap("1;33", t)


def color_enabled(stream) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    try:
        return stream.isatty()
    except (AttributeError, ValueError):
        return False


def search_dirs(all_projects: bool) -> list[Path]:
    """Which project directories a command-line search covers."""
    if all_projects:
        dirs = all_project_dirs()
        if not dirs:
            raise StartupError("no Claude Code project directories found")
        return dirs
    return project_dirs_for_cwd()


def run_search(query: str, all_projects: bool, regex: bool = False, case_sensitive: bool = False,
               role: str = "any", per_session: int = 3, max_sessions: int = 0,
               out=None, err=None) -> int:
    """Print every session whose transcript matches, newest first. Returns 2 on a bad
    pattern, 1 when nothing matched, 0 otherwise (so `if claude-reader -s x` works)."""
    out = out or sys.stdout
    err = err or sys.stderr
    try:
        pattern = compile_query(query, regex, case_sensitive)
    except re.error as e:
        print(f"claude-reader: bad search pattern: {e}", file=err)
        return 2
    roles = None if role == "any" else {"user" if role == "you" else "assistant"}
    prefilter = make_prefilter(query, regex, case_sensitive)

    dirs = search_dirs(all_projects)
    transcripts: list[tuple[Path, Path, float]] = []   # (project dir, transcript, mtime)
    seen: set[Path] = set()
    for d in dirs:
        try:
            paths = list(d.glob("*.jsonl"))
        except OSError:
            continue
        for p in paths:
            if p.name.startswith("agent-") or p in seen:
                continue
            seen.add(p)
            try:
                transcripts.append((d, p, p.stat().st_mtime))
            except OSError:
                continue
    transcripts.sort(key=lambda t: t[2], reverse=True)
    if len(transcripts) > 50 and color_enabled(err):
        print(f"searching {len(transcripts)} transcripts…", file=err, flush=True)

    ink = Ink(color_enabled(out))
    total_hits = total_sessions = 0
    printed_projects: set[Path] = set()
    truncated = False
    for project_dir, path, mtime in transcripts:
        hits = search_transcript(path, pattern, roles, prefilter=prefilter)
        if not hits:
            continue
        if max_sessions and total_sessions >= max_sessions:
            truncated = True
            break
        total_sessions += 1
        total_hits += len(hits)
        if project_dir not in printed_projects:
            printed_projects.add(project_dir)
            print(("\n" if len(printed_projects) > 1 else "") + ink.head(project_cwd(project_dir)), file=out)
        prof = profile_label(path)
        meta = f"{len(hits)} match{'es' if len(hits) != 1 else ''}"
        if prof and prof != "default":
            meta += f" · {prof}"
        when = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
        print(f"  {ink.bold(path.stem)}  {session_title(path)}", file=out)
        print(f"    {ink.dim(f'{meta} · last active {when}')}", file=out)
        for hit in hits[:per_session] if per_session else hits:
            lead, mid, tail = excerpt(hit.message.text, hit.span)
            who = ROLE_WORD.get(hit.message.role, hit.message.role)
            stamp = f"{who:>6} {hit.message.timestamp or '--:--:--'}"
            print(f"      {ink.dim(stamp)}  {lead}{ink.mark(mid)}{tail}", file=out)
        if per_session and len(hits) > per_session:
            print(f"      {ink.dim(f'… {len(hits) - per_session} more in this session')}", file=out)
        print(f"      {ink.dim('open:')} claude-reader {path.stem}", file=out)

    if not total_hits:
        scope = "any project" if all_projects else "this project"
        print(f"no messages matching {query!r} in {scope}", file=out)
        return 1
    where = f"{len(printed_projects)} project{'s' if len(printed_projects) != 1 else ''}"
    tail = " (stopped early: --max-sessions)" if truncated else ""
    print(f"\n{total_hits} match{'es' if total_hits != 1 else ''} in "
          f"{total_sessions} session{'s' if total_sessions != 1 else ''} across {where}{tail}", file=out)
    return 0


EPILOG = """\
keys: up/down or j/k move · ctrl+d / ctrl+u jump 5 (--jump N) · g / G first and
      last message · / search this session · n / N next and previous match
      f follow latest · s toggle sidebar · u toggle your prompts
      c copy message · p session picker · q quit

search: claude-reader -s "auth token"        this project's sessions
        claude-reader -s "auth token" -a     every project
        claude-reader <session-id>           open one of the sessions it found
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="claude-reader",
        description="Live reading pane for Claude Code sessions. Run it in a second terminal pane, "
                    "from the same directory as your Claude Code session.",
        epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("transcript", nargs="?", metavar="transcript.jsonl|session-id",
                   help="open a specific transcript, or a session id (any unique prefix), "
                        "instead of auto-detecting")
    p.add_argument("--mouse", action="store_true",
                   help="enable in-app mouse (off by default: Textual's mouse tracking can break "
                        "drag-selection in neighbouring panes)")
    p.add_argument("--no-pick", action="store_true", help="never show the session picker at startup")
    p.add_argument("--max-messages", type=int, default=500, metavar="N",
                   help="keep/show only the most recent N messages (default 500)")
    p.add_argument("--jump", type=int, default=DEFAULT_JUMP, metavar="N",
                   help=f"how many messages ctrl+d / ctrl+u move by (default {DEFAULT_JUMP})")

    s = p.add_argument_group("search (prints matching sessions and exits)")
    s.add_argument("-s", "--search", metavar="TEXT",
                   help="find messages containing TEXT and print the sessions holding them")
    s.add_argument("-a", "--all-projects", action="store_true",
                   help="search every project, not just this directory's")
    s.add_argument("-E", "--regex", action="store_true", help="treat TEXT as a regular expression")
    s.add_argument("-c", "--case-sensitive", action="store_true", help="match case (default: ignore it)")
    s.add_argument("--role", choices=["any", "you", "claude"], default="any",
                   help="only search your prompts, or only Claude's replies (default any)")
    s.add_argument("--per-session", type=int, default=3, metavar="N",
                   help="how many matching messages to preview per session, 0 for all (default 3)")
    s.add_argument("--max-sessions", type=int, default=0, metavar="N",
                   help="stop after N matching sessions (default: no limit)")
    return p


SEARCH_ONLY = ["all_projects", "regex", "case_sensitive"]


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)     # unknown options / extra positionals: usage error, exit 2
    if args.max_messages < 1:
        parser.error("--max-messages must be >= 1")
    if args.jump < 1:
        parser.error("--jump must be >= 1")
    if args.per_session < 0:
        parser.error("--per-session must be >= 0")

    if args.search is not None:
        if args.transcript:
            parser.error("--search searches whole projects: drop the transcript argument")
        try:
            return run_search(args.search, args.all_projects, regex=args.regex,
                              case_sensitive=args.case_sensitive, role=args.role,
                              per_session=args.per_session, max_sessions=args.max_sessions)
        except StartupError as e:
            print(f"claude-reader: {e}", file=sys.stderr)
            return 1
    stray = [f"--{n.replace('_', '-')}" for n in SEARCH_ONLY if getattr(args, n)]
    if stray:
        parser.error(f"{', '.join(stray)} only applies with --search")

    try:
        transcript, sessions, session_dirs = find_transcript(args.transcript)
    except StartupError as e:
        print(f"claude-reader: {e}", file=sys.stderr)
        return 1
    app = ReaderApp(transcript, sessions, max_messages=args.max_messages,
                    session_dirs=session_dirs, jump=args.jump)
    app.no_pick = args.no_pick
    app.run(mouse=args.mouse)
    return 0


if __name__ == "__main__":
    sys.exit(main())
