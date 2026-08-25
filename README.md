# claude-reader

A live reading pane for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) sessions. It shows only the text Claude writes *to you*, stripped of tool calls, bash output and diffs, with a sidebar to jump back to any earlier message, search inside a session, and a command-line search across every session you have ever run.

Works in any terminal, next to any Claude Code session, with no plugins and no changes to Claude Code itself. (Independent project, not affiliated with Anthropic.)

```
┌────────────────────────────────┬───────────────────────────────────────────────┐
│ claude-reader — Improve message readability  [5e86140a]                        │
├────────────────────────────────┼───────────────────────────────────────────────┤
│ Can we make this a TUI?        │  Claude · 07:25:25                            │
│ you · 07:25:01                 │  ─────────────────────────────────────────    │
│ You're right that a web page…  │                                               │
│ claude · 07:25:25              │  You're right that a web page adds friction,  │
│ So I run it in a second pane?  │  and the terminal-native version is just as   │
│ you · 07:27:37                 │  feasible. But one framing correction first…  │
│ Almost. One nuance to get…     │                                               │
│ claude · 07:27:50              │  - tmux split                                 │
│ Let's build it.                │  - your terminal's native split               │
│ you · 07:30:15                 │  - or a second window                         │
│ ▶ Setting up in the project…   │                                               │
│ claude · 07:30:38              │  So the deliverable is one command…           │
└────────────────────────────────┴───────────────────────────────────────────────┘
```

## Why

In a Claude Code session, the prose that matters to you as the human (findings, questions, summaries) is buried in a stream of tool calls and command output. Scrolling back to re-read an earlier message means paging through hundreds of lines of noise, and the terminal never renders Claude's markdown.

claude-reader fixes that by tailing the session transcript that Claude Code already writes to disk. It keeps the assistant text blocks and your own prompts, drops everything else, and renders the result as markdown in a two-pane TUI that updates in real time.

## Install

With [pipx](https://pipx.pypa.io/) or [uv](https://docs.astral.sh/uv/):

```
pipx install claude-reader
# or
uv tool install claude-reader
```

Or run without installing:

```
uvx claude-reader
```

(From source: `pipx install git+https://github.com/vikasgrac/claude-reader`.)

Requires Python 3.10+ on Linux/macOS (or WSL). The only dependency is [Textual](https://textual.textualize.io/). Tests: `pip install -e . pytest pytest-asyncio && python -m pytest`.

## Use

Open a second pane next to your Claude Code session. Any pane works: a tmux split, your terminal's native split (Ghostty, Kitty, iTerm2, Warp, Windows Terminal hosting WSL or an ssh session), or a second window. Then, **from the same directory the Claude session was started in**, run:

```
claude-reader
```

It finds the newest session for that project and follows it. If several sessions in the project were active in the last hour, a picker asks which one you want.

Keys:

| Key | Action |
| --- | --- |
| `↑`/`↓` or `k`/`j` | move through messages (reading pane follows) |
| `ctrl+d` / `ctrl+u` | jump 5 messages down / up (`--jump N` to change) |
| `g` / `G` | first / last message (`Home` and `End` also work) |
| `/` | search this session; type to filter, `↑↓` to pick, `Enter` to jump |
| `n` / `N` | next / previous match of the last search (wraps) |
| `f` | jump to the latest message and resume auto-following |
| `s` | show/hide the sidebar for full-width reading |
| `u` | show/hide your own prompts |
| `c` | copy the current message to the clipboard |
| `p` | session picker: switch to another session in this project |
| `q` | quit |

Auto-follow pauses whenever you select an older message, so new messages keep appending to the sidebar without pulling you away from what you're reading.

Options:

```
claude-reader path/to/transcript.jsonl   # open a specific transcript
claude-reader 7c66ca9c                   # open a session by id, or any unique prefix
claude-reader --no-pick                  # skip the startup session picker
claude-reader --mouse                    # enable in-app mouse (see below)
claude-reader --max-messages 200         # keep only the newest N messages (default 500)
claude-reader --jump 10                  # ctrl+d / ctrl+u move 10 messages (default 5)
```

Unknown options and extra arguments are usage errors (exit 2); a transcript that cannot be
opened is reported on stderr (exit 1). Only the newest `--max-messages` messages are kept
in memory and shown in the sidebar, so a very long transcript opens fast and stays cheap.

## Finding an old session

Inside the reader, `/` searches the session you are in. To find *which* session said
something, search from the command line — it prints the sessions that match, with a
preview of each hit and the id to open:

```
claude-reader -s "flaky test"        # every session for this directory's project
claude-reader -s "flaky test" -a     # every session in every project
```

```
/home/vikas/work/myapp
  7c66ca9c-7e89-4a1a-bfdf-ccef52d3ef13  Chasing a flaky integration test
    3 matches · last active 2026-08-18 21:29
      claude 17:30:56  …the flaky test is flaky because the fixture reuses a port…
         you 17:34:02  …can we make the flaky test deterministic instead?…
      open: claude-reader 7c66ca9c-7e89-4a1a-bfdf-ccef52d3ef13
```

Copy the `open:` line and you are reading that conversation.

| Flag | Meaning |
| --- | --- |
| `-s`, `--search TEXT` | search for TEXT and print matching sessions instead of opening the TUI |
| `-a`, `--all-projects` | search every project, not just this directory's |
| `-E`, `--regex` | treat TEXT as a regular expression |
| `-c`, `--case-sensitive` | match case (default: ignore it) |
| `--role you\|claude` | only your prompts, or only Claude's replies |
| `--per-session N` | previews per session, `0` for all (default 3) |
| `--max-sessions N` | stop after N matching sessions |

Search covers every config root the reader knows about (`$CLAUDE_CONFIG_DIR`, `~/.claude`,
and per-account profiles under `~/.claude-profiles/`), and matches the same messages the
reader shows: Claude's prose and your prompts, never tool output. It exits 0 when something
matched, 1 when nothing did, so `claude-reader -s foo -a >/dev/null && echo found` works.
A few hundred megabytes of transcripts search in about two seconds.

## Two-minute tour

1. Start Claude Code in a project as usual: `cd ~/work/myapp && claude`.
2. Split your terminal (tmux: `prefix %`; or a second tab/window) and in the new pane run:
   ```
   cd ~/work/myapp && claude-reader
   ```
   The sidebar fills with the conversation so far, newest at the bottom, and the reading pane shows the latest message rendered as markdown.
3. Ask Claude something long — a code review, a plan. While it works, the tool calls scroll by in the Claude pane; in the reader nothing moves until Claude writes prose, and then that message appears, formatted, with headings, lists and code blocks intact.
4. Press `k` a few times to walk back through earlier messages. Auto-follow pauses while you read; press `f` to jump back to the newest and resume following.
5. Press `u` to hide your own prompts and see only Claude's side, `s` to hide the sidebar and read full width, `c` to copy the current message to your clipboard for a note or a commit message.
6. Looking for something specific in this session? `/`, type a few characters, and the matches appear with their context; `Enter` jumps to one, `n`/`N` walk the rest. Long conversation? `g` and `G` go to the first and last message, `ctrl+d`/`ctrl+u` move five at a time.
7. Ran several sessions in this project today? `p` opens the session picker; the live ones are marked. Pick one and the reader switches to it.
8. Can't remember which session it was in? Quit and run `claude-reader -s "that phrase" -a`; it prints the sessions that mention it, with previews and an id you can hand straight back to `claude-reader`.
9. `q` quits. Claude Code never noticed any of this — the reader only ever read a file.

## How it works

Claude Code logs every session to `~/.claude/projects/<project>/<session-id>.jsonl`, one JSON object per line, normally appended to. Each assistant turn is a list of typed blocks: `text`, `tool_use`, `thinking`. claude-reader keeps only the `text` blocks (plus your prompts, with injected system context stripped), which is exactly the separation you want. It remembers its byte offset into the file and re-reads whatever was appended, twice a second, so it stays live with no hooks into Claude Code or the terminal. If the file is truncated or replaced (it tracks size and inode) it starts over and reloads; records it does not understand — nulls, new record types, a half-written last line, stray bytes — are skipped, never fatal.

Command-line search reads those same files directly. Before parsing a line as JSON it checks whether the raw bytes could contain the query at all, which is what keeps a full sweep of every project down to a couple of seconds; queries that JSON might have escaped (quotes, backslashes, non-ASCII, regular expressions) skip that shortcut and parse everything, a few seconds more.

Because it only reads a file, it is not tied to any terminal app, works over ssh, and does not break when Claude Code's UI changes.

## Notes for tmux and ssh users

**Clipboard.** `c` copies through `tmux load-buffer -w` when running inside tmux, which forwards to the outer terminal's clipboard via OSC 52 (a temporary named tmux buffer is used and deleted right after, so the text does not linger in tmux's paste history). This works over ssh if your terminal supports OSC 52 (most do). Outside tmux it uses Textual's clipboard support. Copying is the only time text leaves the app: it reads transcripts locally and makes no network calls.

**Selecting text in the Claude pane.** Two things in a neighbouring TUI can break drag-selection in the Claude Code pane, and claude-reader avoids both by default:

- Terminal mouse tracking. Textual normally enables any-motion mouse reporting. claude-reader runs with the mouse off unless you pass `--mouse`. Under tmux the scroll wheel still moves through messages, because tmux converts wheel events to arrow keys for full-screen apps that do not track the mouse.
- Periodic output. A ticking clock in the header would repaint the pane every second, and output landing under an in-progress drag kills the selection. claude-reader draws nothing unless a new message arrives.

## Status

0.3.0 — adds search (`/` inside a session, `--search` across a project or every project), jump-by-N and first/last navigation, and opening a session by id. 0.2.0 hardened it after an outside review: malformed or half-written transcript lines are skipped, truncated/replaced files reload, memory is bounded (`--max-messages`), and there is a test suite. Things it does not do yet: a compressed trace of tool activity between messages, a `claude-watch` wrapper that opens Claude and the reader in one tmux command. Issues and PRs welcome.

## License

MIT
