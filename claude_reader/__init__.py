#!/usr/bin/env python3
"""claude-reader: live TUI viewer for the prose Claude Code writes to you.

Reads the session transcript (~/.claude/projects/<munged-cwd>/<session>.jsonl),
keeps only assistant text blocks and your own prompts, and shows them in a
sidebar + reading pane. Tails the file so new messages appear live.

Usage:
    claude-reader                     # newest session for the current directory
    claude-reader <transcript.jsonl>  # a specific transcript file
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

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Footer, Header, Label, ListItem, ListView, Markdown

PROJECTS_DIR = Path.home() / ".claude" / "projects"
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


def project_dir_for_cwd() -> Path:
    project_dir = PROJECTS_DIR / munge_path(Path.cwd())
    if not project_dir.is_dir():
        raise StartupError(f"no Claude Code transcripts for {Path.cwd()}\n(looked in {project_dir})")
    return project_dir


def list_sessions(project_dir: Path) -> list[SessionInfo]:
    """All main-conversation transcripts in a project, newest first. Never raises."""
    sessions = []
    try:
        paths = list(project_dir.glob("*.jsonl"))
    except OSError:
        return sessions
    stats = []
    for p in paths:
        if p.name.startswith("agent-"):
            continue
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


def find_transcript(explicit: str | None) -> tuple[Path, list[SessionInfo]]:
    """Return (transcript to open, all sessions). Explicit path wins."""
    if explicit:
        p = Path(explicit).expanduser()
        if not p.is_file():
            raise StartupError(f"transcript not found (or not a file): {p}")
        try:
            with open(p, "rb"):
                pass
        except OSError as e:
            raise StartupError(f"cannot read {p}: {e.strerror or e}")
        return p, list_sessions(p.parent)
    sessions = list_sessions(project_dir_for_cwd())
    if not sessions:
        raise StartupError("no session files found")
    return sessions[0].path, sessions


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
            meta = f"{s.short_id} · last active {s.last_active}" + ("  (live)" if live else "")
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
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("f", "follow", "Follow latest"),
        Binding("u", "toggle_user", "Show/hide my prompts"),
        Binding("s", "toggle_sidebar", "Show/hide sidebar"),
        Binding("c", "copy_message", "Copy message"),
        Binding("p", "pick_session", "Sessions"),
        Binding("j", "next_msg", "Next", show=False),
        Binding("k", "prev_msg", "Prev", show=False),
    ]

    def __init__(self, transcript: Path, sessions: list[SessionInfo], max_messages: int = 500):
        super().__init__()
        self.transcript = transcript
        self.sessions = sessions
        self.tail = TranscriptTail(transcript)
        self.max_messages = max(1, max_messages)
        # bounded: only the most recent max_messages are kept (and mounted as widgets)
        self.messages: deque[Message] = deque(maxlen=self.max_messages)
        self.follow = True
        self.show_user = True
        self.no_pick = False
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
            yield ListView(id="sidebar")
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
        self.sessions = list_sessions(path.parent)
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
        self.sessions = list_sessions(self.transcript.parent)
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


EPILOG = """\
keys: up/down or j/k move · f follow latest · s toggle sidebar · u toggle
      your prompts · c copy message · p session picker · q quit
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="claude-reader",
        description="Live reading pane for Claude Code sessions. Run it in a second terminal pane, "
                    "from the same directory as your Claude Code session.",
        epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("transcript", nargs="?", metavar="transcript.jsonl",
                   help="open a specific transcript instead of auto-detecting")
    p.add_argument("--mouse", action="store_true",
                   help="enable in-app mouse (off by default: Textual's mouse tracking can break "
                        "drag-selection in neighbouring panes)")
    p.add_argument("--no-pick", action="store_true", help="never show the session picker at startup")
    p.add_argument("--max-messages", type=int, default=500, metavar="N",
                   help="keep/show only the most recent N messages (default 500)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)     # unknown options / extra positionals: usage error, exit 2
    if args.max_messages < 1:
        build_parser().error("--max-messages must be >= 1")
    try:
        transcript, sessions = find_transcript(args.transcript)
    except StartupError as e:
        print(f"claude-reader: {e}", file=sys.stderr)
        return 1
    app = ReaderApp(transcript, sessions, max_messages=args.max_messages)
    app.no_pick = args.no_pick
    app.run(mouse=args.mouse)
    return 0


if __name__ == "__main__":
    sys.exit(main())
