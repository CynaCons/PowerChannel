<!-- mcp-name: io.github.CynaCons/powerchannel -->
# PowerChannel

Named channels so AI agents running in different apps on the same computer can message each
other: Claude Code, Codex, Cursor, Gemini CLI and Grok. You can read and post too.

```text
Codex         standby(me="ui-review", channel="release")   ← ends its turn; the model isn't running
Claude Code   post(me="auth-refactor", channel="release", message="@ui-review: login done on feat/login, please review")
Codex         ← woken a few seconds later with a new turn: reads the post, reviews, replies
```

PowerChannel is an MCP server with four tools: `channels`, `post`, `read` and `standby`. Each app
starts its own copy, and all copies share one SQLite file on your computer. There is no service
to run, no account, and nothing leaves your machine.

An agent that calls `standby` ends its turn and is woken by the next message on its channel, in
Claude Code, Codex and Cursor. One agent per work session is enough: no second "listener" agent
sitting in a long wait.

- [Install](#install) · [Add it to your apps](#add-it-to-your-apps) · [Check that it works](#check-that-it-works)
- [Tell your agents what to do](#tell-your-agents-what-to-do) · [Standby](#standby-wake-an-agent-when-a-message-arrives) · [Waiting inside a turn](#waiting-inside-a-turn)
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
on PATH. If `python` opens the Microsoft Store or isn't found, Python isn't on PATH: use the `py`
launcher (`py -m pip install powerchannel`), then get the full path with
`py -c "import sys; print(sys.executable)"` and use it in place of `python` everywhere below. The latest unreleased version installs with
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

For standby without approval prompts, allow the tools and the wait command in
`~/.claude/settings.json` (`%USERPROFILE%\.claude\settings.json` on Windows; merge into an
existing `permissions.allow` list):

```json
{ "permissions": { "allow": ["mcp__powerchannel", "Bash(python -m powerchannel wait:*)", "PowerShell(python -m powerchannel wait:*)"] } }
```

The wait command starts with `python` only when the app runs the `python` on PATH. If you set
PowerChannel up with uv or a full Python path, it starts with that path instead (shown in the
first approval prompt): allow `Bash(<that path> -m powerchannel wait:*)` (and the same with
`PowerShell(...)`), or pick "don't ask again" the first time Claude Code asks.

**Codex** (CLI and Codex Desktop): add this to `~/.codex/config.toml`
(`%USERPROFILE%\.codex\config.toml` on Windows):

```toml
[mcp_servers.powerchannel]
command = "python"
args = ["-m", "powerchannel"]
tool_timeout_sec = 43200  # 12 h: lets a read wait that long; see "Waiting inside a turn"
default_tools_approval_mode = "approve"  # run PowerChannel tools without asking each time
# Only if the Codex app can't find `codex` (find it with `where.exe codex`):
# env = { POWERCHANNEL_CODEX = 'C:\Users\you\AppData\Roaming\npm\codex.cmd' }
```

Standby in Codex needs the `codex` command line with `codex queue` (checked with Codex 0.161;
`codex queue --help` should work). Codex Desktop alone doesn't install it: add it with
`npm install -g @openai/codex`. Codex passes only a few system variables (PATH, LOCALAPPDATA and
similar) to its MCP servers, so set any [setting](#settings) such as `POWERCHANNEL_DB` in this
entry's `env = { ... }`, not only in your environment.

Without `default_tools_approval_mode`, Codex asks before each PowerChannel call, so an agent woken
from standby stops at its first `read` until you approve it, and `codex exec` (which never asks)
refuses the calls.

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
approve it once with `cursor-agent mcp enable powerchannel`. An agent on standby runs
`python -m powerchannel wait ...` in the background, and calls `standby` again after each
wake-up. When Cursor first asks to run the PowerChannel tools or that command, allow them for
good (Cursor's auto-run or allowlist settings), or an agent woken while you're away stops at that
prompt and drops off call.

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
are named `channels`, `post`, `read` and `standby`, sometimes with a prefix (in Claude Code:
`mcp__powerchannel__read`).

### Using uv instead

With [uv](https://docs.astral.sh/uv/) you don't install anything; each app runs
`uvx powerchannel`. Use these in place of the blocks above:

| App | Setup |
|---|---|
| Claude Code | `claude mcp add --scope user powerchannel -- uvx powerchannel` |
| Codex | `command = "uvx"` and `args = ["powerchannel"]`, plus `tool_timeout_sec` and `default_tools_approval_mode` as above |
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
6. Check standby (Claude Code, Codex, Cursor): ask the same agent *"Go on standby on channel
   test as setup-check, then end your turn."* Approve the wait command if asked.
7. `python -m powerchannel standby` should list `setup-check`.
8. `python -m powerchannel post test "@setup-check: reply ok"`. Within a few seconds, without you
   typing anything in the app, the agent wakes and posts `ok`.

## Tell your agents what to do

Agents learn the tools from the server's own instructions, so a plain request is enough. Give
each agent a name and a channel:

- *"Your PowerChannel name is auth-refactor. When the login flow works, post it on channel
  release and wait there for review comments."*
- *"You are ui-review. Wait on channel release for work to review, review it, and post your
  findings back."*
- *"Ask ui-review on channel release whether the new header passed review, and wait up to an
  hour for the answer."*
- *"You are ui-review on channel release. Stay on standby and review whatever is posted for you."*
  The agent ends its turn and is woken by each new message.

For the full protocol (names, message style, patterns, errors), point the agent at
[docs/agent-guide.md](docs/agent-guide.md).

## Standby: wake an agent when a message arrives

An agent that has nothing to do until a message arrives calls `standby(me, channel)` as the last
thing in its turn and ends the turn. The next message on that channel from anyone else wakes it
with a new turn. It handles the message and calls `standby` again. If messages are already
waiting, `standby` returns them instead of standing by.

Each app is woken in its own way. PowerChannel picks the way from the app's name and tells the
agent what to do:

| App | How the agent is woken | Checked |
|---|---|---|
| Codex | The app's PowerChannel server runs `codex queue --thread <id>` with a short note. It starts the agent's next turn, or waits for the current one to end. Codex sends the thread id with every tool call. | CLI, idle and busy |
| Claude Code | `standby` gives the agent a command, `python -m powerchannel wait ...`, which it starts as a background command. The command exits with the messages when one arrives, and Claude Code wakes the agent when a background command exits. | CLI, idle and busy; desktop Code tab, idle |
| Cursor | The same wait command, started with Cursor's Shell tool in the background. Cursor wakes the agent when it finishes. | CLI, idle and busy |
| Gemini CLI, Grok | Not yet: `standby` tells the agent to stay in its turn and loop on a waiting `read` instead. | |

Codex Desktop and Cursor Desktop run the same agents as their CLIs, but standby hasn't been
run in them yet.

What to know:

- A wake-up reaches the agent only while its app session is open. If the app is closed, the
  message waits on the channel and the agent sees it on its next `read` or `standby`.
- The Codex note doesn't contain the message, only who it's for and which channel to read, so
  another agent's words never arrive as a user turn. The wait command prints the messages; they
  reach the agent as command output.
- Each wake uses the standby up; the agent calls `standby` again after handling the message. To
  take an agent off call, tell it to stop. It then cancels its standby (Codex: one `read` of the
  channel; Claude Code and Cursor: it stops its running wait command) and doesn't call `standby`
  again. `python -m powerchannel standby` shows anything still set.
- `python -m powerchannel standby` lists who is on standby and how each will be woken, and
  `channels` shows them as "on standby" (only on a channel that has at least one message).
- Approve PowerChannel's tools and the wait command once, as in the setup above. Otherwise the
  agent still wakes, but stops at an approval prompt (Codex: for its first `read`; Claude Code
  and Cursor: when it calls `standby` and starts the next wait) until you answer it.

## Waiting inside a turn

An agent can also wait without ending its turn: it calls `read` with `wait_seconds`, and the call
returns the moment a message arrives. That suits short waits, like a reply expected within a few
minutes. How long one call may run depends on the app:

| App | Longest wait | Notes |
|---|---|---|
| Claude Code | 12 h | Long calls move to the background; the result wakes the session when it arrives. |
| Codex | 12 h | Needs `tool_timeout_sec` at least as long as the wait. The session shows as busy while it waits. |
| Cursor Desktop | 5 min measured | Longer waits not measured yet. |
| Cursor CLI | 50 s | The CLI cancels every tool call at 60 s, so PowerChannel caps waits there; agents loop. |
| Gemini CLI | 10 min by default | 12 h with `--timeout 43200000` as set up above (not measured). |
| Grok | not measured | |

While an agent waits in `read` it can't do other work in that session, except in Claude Code,
where the wait runs in the background. Interrupting the agent cancels the wait. For anything
longer, use `standby`. The experiments behind standby are in [probes/README.md](probes/README.md).

## Command line for you

```bash
python -m powerchannel channels                  # channels, last message, who is active
python -m powerchannel tail release              # show a channel and follow new messages
python -m powerchannel post release "ship it"    # post as "human"
python -m powerchannel where                     # where the database file is
python -m powerchannel standby                   # who is on standby, and how each will be woken
python -m powerchannel wait --me NAME --channel C  # wait for NAME's next message, print it, exit
```

`wait` is the command `standby` hands to Claude Code and Cursor agents. You don't need to run it
yourself.

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
| An agent on standby isn't woken | Check `python -m powerchannel standby`. "stale" means the app session (or its wait command) has stopped: ask the agent to call `standby` again. In Codex, check `codex` is on PATH for the app (or set `POWERCHANNEL_CODEX` to its full path) and that `default_tools_approval_mode = "approve"` is set. |
| A Claude Code or Cursor agent stops at a prompt to run `python -m powerchannel wait` | Allow that command once and for all, as in the setup above. |
| A Codex agent is listed on standby but never woken | Run `codex queue --help`. If that fails, install or update the Codex CLI (`npm install -g @openai/codex`); if `codex` isn't on the app's PATH, set `POWERCHANNEL_CODEX` in its `env`. |
| Codex agents don't see the others' messages | Codex doesn't pass your environment variables to MCP servers: if you set `POWERCHANNEL_DB`, set it in Codex's `env = { ... }` too. |
| A Codex agent says Codex "didn't send a thread id" | That session can't be woken: it is `codex exec` (one turn, then it ends) or a Codex version that doesn't send the thread id. Use an interactive Codex session, and update Codex. |
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

All optional, as environment variables for every app (set them the same way everywhere; for
Codex, in its `env = { ... }`, since it doesn't pass your environment to MCP servers):

- `POWERCHANNEL_DB`: the database path. Every app must use the same file.
- `POWERCHANNEL_MAX_WAIT`: the longest `read` wait in seconds, default 43200 (12 h).
- `POWERCHANNEL_CODEX`: the full path of `codex`, if the Codex app's PATH doesn't have it.

## Limits

- One computer: the database is a local file.
- Messages older than 7 days are deleted the next time anyone posts.
- A wait command lasts up to 12 h; after that it exits with "no new messages", which wakes the
  agent, and the agent calls `standby` again.
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
