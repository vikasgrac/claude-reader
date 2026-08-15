# claude-reader

A live reading pane for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) sessions. It shows only the text Claude writes *to you*, stripped of tool calls, bash output and diffs, with a sidebar to jump back to any earlier message.

Works in any terminal, next to any Claude Code session, with no plugins and no changes to Claude Code itself.

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
pipx install git+https://github.com/vikasgrac/claude-reader
# or
uv tool install git+https://github.com/vikasgrac/claude-reader
```

Or run without installing:

```
uvx --from git+https://github.com/vikasgrac/claude-reader claude-reader
```

Requires Python 3.10+. The only dependency is [Textual](https://textual.textualize.io/).

## Use

Open a second pane next to your Claude Code session. Any pane works: a tmux split, your terminal's native split (Ghostty, Kitty, iTerm2, Warp, Windows Terminal), or a second window. Then, **from the same directory the Claude session was started in**, run:

```
claude-reader
```

It finds the newest session for that project and follows it. If several sessions in the project were active in the last hour, a picker asks which one you want.

Keys:

| Key | Action |
| --- | --- |
| `↑`/`↓` or `k`/`j` | move through messages (reading pane follows) |
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
claude-reader --no-pick                  # skip the startup session picker
claude-reader --mouse                    # enable in-app mouse (see below)
```

## How it works

Claude Code logs every session to `~/.claude/projects/<project>/<session-id>.jsonl`, one JSON object per line, append-only. Each assistant turn is a list of typed blocks: `text`, `tool_use`, `thinking`. claude-reader keeps only the `text` blocks (plus your prompts, with injected system context stripped), which is exactly the separation you want. It remembers its byte offset into the file and re-reads whatever was appended, twice a second, so it stays live with no hooks into Claude Code or the terminal.

Because it only reads a file, it is not tied to any terminal app, works over ssh, and does not break when Claude Code's UI changes.

## Notes for tmux and ssh users

**Clipboard.** `c` copies through `tmux load-buffer -w` when running inside tmux, which forwards to the outer terminal's clipboard via OSC 52. This works over ssh if your terminal supports OSC 52 (most do). Outside tmux it uses Textual's clipboard support.

**Selecting text in the Claude pane.** Two things in a neighbouring TUI can break drag-selection in the Claude Code pane, and claude-reader avoids both by default:

- Terminal mouse tracking. Textual normally enables any-motion mouse reporting. claude-reader runs with the mouse off unless you pass `--mouse`. Under tmux the scroll wheel still moves through messages, because tmux converts wheel events to arrow keys for full-screen apps that do not track the mouse.
- Periodic output. A ticking clock in the header would repaint the pane every second, and output landing under an in-progress drag kills the selection. claude-reader draws nothing unless a new message arrives.

## Status

Iteration 1, built and used the same day. Things it does not do yet: search, a compressed trace of tool activity between messages, a `claude-watch` wrapper that opens Claude and the reader in one tmux command. Issues and PRs welcome.

## License

MIT
