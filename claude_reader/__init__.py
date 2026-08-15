#!/usr/bin/env python3
"""claude-reader: live TUI viewer for the prose Claude Code writes to you.

Reads the session transcript (~/.claude/projects/<munged-cwd>/<session>.jsonl),
keeps only assistant text blocks and your own prompts, and shows them in a
sidebar + reading pane. Tails the file so new messages appear live.

Usage:
    claude_reader.py                  # newest session for the current directory
    claude_reader.py <transcript.jsonl>  # a specific transcript file
"""

import json
import os
import re
import subprocess
import sys
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


def session_title(path: Path) -> str:
    """Claude Code writes an `ai-title` line once it has named the session."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if '"ai-title"' in line:
                    try:
                        return json.loads(line).get("aiTitle", "").strip() or "(untitled)"
                    except json.JSONDecodeError:
                        continue
    except OSError:
        pass
    return "(untitled)"


def project_dir_for_cwd() -> Path:
    project_dir = PROJECTS_DIR / munge_path(Path.cwd())
    if not project_dir.is_dir():
        sys.exit(f"no Claude Code transcripts for {Path.cwd()}\n(looked in {project_dir})")
    return project_dir


def list_sessions(project_dir: Path) -> list[SessionInfo]:
    """All main-conversation transcripts in a project, newest first."""
    sessions = []
    for p in project_dir.glob("*.jsonl"):
        if p.name.startswith("agent-"):
            continue
        sessions.append(SessionInfo(p, session_title(p), p.stat().st_mtime))
    return sorted(sessions, key=lambda s: s.mtime, reverse=True)


RECENT_SECONDS = 3600  # sessions active within the last hour count as "live"


def find_transcript() -> tuple[Path, list[SessionInfo]]:
    """Return (transcript to open, all sessions). Explicit path wins."""
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if args:
        p = Path(args[0]).expanduser()
        if not p.exists():
            sys.exit(f"transcript not found: {p}")
        return p, list_sessions(p.parent) if p.parent.is_dir() else []
    sessions = list_sessions(project_dir_for_cwd())
    if not sessions:
        sys.exit("no session files found")
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


def parse_line(line: str) -> list[Message]:
    """Extract zero or more display messages from one transcript JSONL line."""
    try:
        d = json.loads(line)
    except json.JSONDecodeError:
        return []
    if not isinstance(d, dict) or d.get("isSidechain") or d.get("isMeta"):
        return []

    kind = d.get("type")
    ts = local_time(d.get("timestamp", ""))
    uuid = d.get("uuid", "")
    msgs: list[Message] = []

    if kind == "assistant":
        for block in d.get("message", {}).get("content", []):
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "").strip()
                if text:
                    msgs.append(Message("assistant", text, ts, uuid))

    elif kind == "user" and "toolUseResult" not in d:
        content = d.get("message", {}).get("content")
        parts: list[str] = []
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
        text = clean_user_text("\n".join(parts))
        # Skip interruption notices and command-invocation husks.
        if text and not text.startswith("[Request interrupted"):
            msgs.append(Message("user", text, ts, uuid))

    return msgs


class TranscriptTail:
    """Incremental reader: each poll() returns messages from lines added since last call."""

    def __init__(self, path: Path):
        self.path = path
        self._pos = 0
        self._buf = ""

    def poll(self) -> list[Message]:
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return []
        if size < self._pos:  # truncated/rewritten: start over
            self._pos = 0
            self._buf = ""
        if size == self._pos:
            return []
        with open(self.path, encoding="utf-8") as f:
            f.seek(self._pos)
            chunk = f.read()
            self._pos = f.tell()
        self._buf += chunk
        lines = self._buf.split("\n")
        self._buf = lines.pop()  # keep any partial trailing line for next poll
        msgs = []
        for line in lines:
            if line.strip():
                msgs.extend(parse_line(line))
        return msgs


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

    def __init__(self, transcript: Path, sessions: list[SessionInfo]):
        super().__init__()
        self.transcript = transcript
        self.sessions = sessions
        self.tail = TranscriptTail(transcript)
        self.messages: list[Message] = []
        self.follow = True
        self.show_user = True
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
        if len(live) > 1 and "--no-pick" not in sys.argv:
            self.action_pick_session()

    def switch_session(self, path: Path | None) -> None:
        if path is None or path == self.transcript:
            return
        self.transcript = path
        self.tail = TranscriptTail(path)
        self.messages = []
        self.follow = True
        self.sessions = list_sessions(path.parent)
        self._set_subtitle()
        self.query_one("#sidebar", ListView).clear()
        self.query_one("#body", Markdown).update("*Waiting for messages…*")
        self.poll()

    # ---- data flow ----------------------------------------------------

    def poll(self) -> None:
        new = self.tail.poll()
        if not new:
            return
        self.messages.extend(new)
        sidebar = self.query_one("#sidebar", ListView)
        for msg in new:
            if msg.role == "user" and not self.show_user:
                continue
            sidebar.append(self._make_item(msg))
        if self.follow and len(sidebar) > 0:
            sidebar.index = len(sidebar) - 1

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
        # the outer terminal's clipboard.
        if os.environ.get("TMUX"):
            subprocess.run(["tmux", "load-buffer", "-w", "-"], input=text.encode())
        else:
            super().copy_to_clipboard(text)

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


USAGE = """\
usage: claude-reader [transcript.jsonl] [--mouse] [--no-pick]

Live reading pane for Claude Code sessions. Run it in a second terminal pane,
from the same directory as your Claude Code session.

  transcript.jsonl  open a specific transcript instead of auto-detecting
  --mouse           enable in-app mouse (off by default: Textual's mouse
                    tracking can break drag-selection in neighbouring panes)
  --no-pick         never show the session picker at startup

keys: up/down or j/k move · f follow latest · s toggle sidebar · u toggle
      your prompts · c copy message · p session picker · q quit
"""


def main() -> None:
    if "-h" in sys.argv or "--help" in sys.argv:
        print(USAGE)
        return
    transcript, sessions = find_transcript()
    ReaderApp(transcript, sessions).run(mouse="--mouse" in sys.argv)


if __name__ == "__main__":
    main()
