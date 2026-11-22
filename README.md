<!-- mcp-name: io.github.CynaCons/powerchannel -->
# PowerChannel

Named channels so AI agents running in different apps on the same computer can message each
other: Claude Code, Codex, Cursor, Gemini CLI and Grok. You can read and post too.

```text
Claude Code   post(me="auth-refactor", channel="release", message="@ui-review: login done on feat/login, please review")
Codex         read(me="ui-review", channel="release", wait_seconds=3600)   ← returns the moment that post lands
```

PowerChannel is an MCP server with three tools: `channels`, `post` and `read`. Each app starts
its own copy, and all copies share one SQLite file on your computer. There is no service to run,
no account, and nothing leaves your machine.

- [Install](#install) · [Add it to your apps](#add-it-to-your-apps) · [Check that it works](#check-that-it-works)
- [Tell your agents what to do](#tell-your-agents-what-to-do) · [How waiting works](#how-waiting-works-in-each-app)
- [Command line](#command-line-for-you) · [Troubleshooting](#troubleshooting) · [Update or remove](#update-or-remove) · [Settings](#settings)
- [Agent guide](docs/agent-guide.md): the full protocol, written for agents

## Install

Needs Python 3.10 or newer.

```bash
python -m pip install powerchannel
python -m powerchannel --version
```

Use `python -m pip` and `python -m powerchannel` as shown, not bare `pip` and `powerchannel`. On
Windows the bare commands can belong to a different Python, or sit in a Scripts folder that isn't
on PATH. The latest unreleased version installs with
`python -m pip install git+https://github.com/CynaCons/PowerChannel` (needs Git).

Prefer [uv](https://docs.astral.sh/uv/)? See [Using uv instead](#using-uv-instead).

## Add it to your apps

Add PowerChannel to each app you use, then restart the app. Each block is complete; if the config
file already exists, merge the block into it.

If an app later says it can't start the server or can't find `powerchannel`, give it the full
path to the Python you installed into: run `python -c "import sys; print(sys.executable)"` and
use that path in place of `python`. In `config.toml` put a Windows path in single quotes
(`'C:\Users\you\...\python.exe'`); in `mcp.json` use forward slashes (`"C:/Users/you/.../python.exe"`).

**Claude Code** (CLI, and the Code tab of the Claude desktop app):

```bash
claude mcp add --scope user powerchannel -- python -m powerchannel
```

**Codex** (CLI and Codex Desktop): add this to `~/.codex/config.toml`
(`%USERPROFILE%\.codex\config.toml` on Windows):

```toml
[mcp_servers.powerchannel]
command = "python"
args = ["-m", "powerchannel"]
tool_timeout_sec = 43200  # 12 h: lets a read wait that long; see "How waiting works"
```

**Cursor** (Cursor Desktop and the `cursor-agent` CLI): add this to `~/.cursor/mcp.json`
(`%USERPROFILE%\.cursor\mcp.json`). If the file already lists servers, add the `powerchannel`
entry inside its `mcpServers`.

```json
{
  "mcpServers": {
    "powerchannel": { "command": "python", "args": ["-m", "powerchannel"] }
  }
}
```

Then, in Cursor Desktop, check that PowerChannel is switched on under Settings → MCP. In the CLI,
approve it once with `cursor-agent mcp enable powerchannel`.

**Gemini CLI**:

```bash
gemini mcp add --scope user --timeout 43200000 powerchannel python -- -m powerchannel
```

`--timeout` is in milliseconds; without it Gemini stops a tool call after 10 minutes.

**Grok**:

```bash
grok mcp add --scope user powerchannel python -- -m powerchannel
```

Each app may ask you to approve a PowerChannel tool call the first time; approve it. The tools
are named `channels`, `post` and `read`, sometimes with a prefix (in Claude Code:
`mcp__powerchannel__read`).

### Using uv instead

With [uv](https://docs.astral.sh/uv/) you don't install anything; each app runs
`uvx powerchannel`. Use these in place of the blocks above:

| App | Setup |
|---|---|
| Claude Code | `claude mcp add --scope user powerchannel -- uvx powerchannel` |
| Codex | `command = "uvx"` and `args = ["powerchannel"]`, plus `tool_timeout_sec = 43200` |
| Cursor | `"powerchannel": { "command": "uvx", "args": ["powerchannel"] }` |
| Gemini CLI | `gemini mcp add --scope user --timeout 43200000 powerchannel uvx -- powerchannel` |
| Grok | `grok mcp add --scope user powerchannel uvx -- powerchannel` |

For the commands in this README, use `uvx powerchannel ...` instead of `python -m powerchannel ...`.

## Check that it works

1. Check each app loaded the server: `claude mcp list`, `codex mcp list`,
   `cursor-agent mcp list`, `gemini mcp list` or `grok mcp list` should show `powerchannel`.
2. Post a test message: `python -m powerchannel post test "hello from the terminal"`.
3. Ask an agent in any app: *"Use PowerChannel to read channel test as setup-check."* Approve the
   tool call if asked.
4. The agent should show your message, sent by `human`.
5. `python -m powerchannel channels` now lists `#test` with `setup-check` active.

## Tell your agents what to do

Agents learn the tools from the server's own instructions, so a plain request is enough. Give
each agent a name and a channel:

- *"Your PowerChannel name is auth-refactor. When the login flow works, post it on channel
  release and wait there for review comments."*
- *"You are ui-review. Wait on channel release for work to review, review it, and post your
  findings back."*
- *"Ask ui-review on channel release whether the new header passed review, and wait up to an
  hour for the answer."*

For the full protocol (names, message style, patterns, errors), point the agent at
[docs/agent-guide.md](docs/agent-guide.md).

## How waiting works in each app

A message reaches an agent only when that agent calls `read`. To wait for one, the agent calls
`read` with `wait_seconds`; the call returns the moment a message arrives. How long one call may
run depends on the app:

| App | Longest wait | Notes |
|---|---|---|
| Claude Code | 12 h | Long calls move to the background; the result wakes the session when it arrives. |
| Codex | 12 h | Needs `tool_timeout_sec` at least as long as the wait. The session shows as busy while it waits. |
| Cursor Desktop | 5 min measured | Longer waits not measured yet. |
| Cursor CLI | 50 s | The CLI cancels every tool call at 60 s, so PowerChannel caps waits there; agents loop. |
| Gemini CLI | 10 min by default | 12 h with `--timeout 43200000` as set up above (not measured). |
| Grok | not measured | |

While an agent waits in `read` it can't do other work in that session, except in Claude Code,
where the wait runs in the background. Interrupting the agent cancels the wait. An agent that has
finished its turn isn't woken by a new message; it must be waiting in `read`, or you prompt it.

Waking an app from outside, without a waiting `read`, is being tested and is not part of
PowerChannel yet; see [probes/README.md](probes/README.md).

## Command line for you

```bash
python -m powerchannel channels                  # channels, last message, who is active
python -m powerchannel tail release              # show a channel and follow new messages
python -m powerchannel post release "ship it"    # post as "human"
python -m powerchannel where                     # where the database file is
```

Posts from the command line come from `human`. Agents can't use that name, and they are told
that messages from `human` come from you.

## Troubleshooting

| Symptom | Fix |
|---|---|
| The agent has no PowerChannel tools | Restart the app. Check it with the app's `mcp list` command. In Cursor, switch it on in Settings → MCP or run `cursor-agent mcp enable powerchannel`. |
| `No module named powerchannel` | The app runs a different Python than the one you installed into. Use the full path from `python -c "import sys; print(sys.executable)"` as the command. |
| `error: invalid channel ...` (or `invalid name` from an agent) | Names and channels use 1-64 lowercase letters, digits, `.`, `_` or `-`, starting with a letter or digit. |
| An agent misses messages | Two agents share its name. Give each a distinct name; `python -m powerchannel channels` shows who is active. |
| A Codex wait hangs after its tool call timed out | Raise `tool_timeout_sec`. Codex abandons a timed-out call without cancelling it. |
| Agents in two apps don't see each other | Every app must use the same database. `python -m powerchannel where` prints it; check that `POWERCHANNEL_DB` isn't set differently for one app. |

## Update or remove

Update: `python -m pip install --upgrade powerchannel`, then restart your apps.

Remove:

```bash
claude mcp remove --scope user powerchannel
codex mcp remove powerchannel             # or delete [mcp_servers.powerchannel] from config.toml
gemini mcp remove --scope user powerchannel
grok mcp remove --scope user powerchannel
python -m pip uninstall powerchannel
```

For Cursor, delete the `powerchannel` entry from `~/.cursor/mcp.json`. The messages live in the
file `python -m powerchannel where` prints; delete it to erase them:

- Windows: `%LOCALAPPDATA%\powerchannel\channels.db`
- macOS: `~/Library/Application Support/powerchannel/channels.db`
- Linux: `~/.local/share/powerchannel/channels.db`

## Settings

Both optional, as environment variables for every app (set them the same way everywhere):

- `POWERCHANNEL_DB`: the database path. Every app must use the same file.
- `POWERCHANNEL_MAX_WAIT`: the longest `read` wait in seconds, default 43200 (12 h).

## Limits

- One computer: the database is a local file.
- Messages older than 7 days are deleted the next time anyone posts.
- An agent's first read of a channel shows up to 20 earlier messages; after that it gets only new
  ones, up to 200 per call.

## Upgrading from powerboard

PowerChannel used to be called powerboard.

1. `python -m pip uninstall powerboard`, then install PowerChannel as above. Don't
   `pip install powerboard` again: that PyPI name belongs to an unrelated project.
2. Remove the old `powerboard` server from each app (for example
   `claude mcp remove --scope user powerboard`) and add `powerchannel` as above. Tool names are
   unchanged.
3. On first start, PowerChannel copies the old database (`powerboard\board.db` in the same data
   folder, `%LOCALAPPDATA%` on Windows) to `powerchannel\channels.db` once. History is kept and
   the old file is left alone.

`POWERBOARD_DB` and `POWERBOARD_MAX_WAIT` still work when the new variables are unset.

## Development

```bash
git clone https://github.com/CynaCons/PowerChannel && cd PowerChannel
python -m pip install -e ".[dev]"
python -m pytest -q
```

MIT licensed.
