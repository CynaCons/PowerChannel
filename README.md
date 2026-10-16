# PowerChannel

Named channels so AI agents running in different apps on the same computer (Claude Code,
Codex, Cursor) can talk to each other. Waiting for a reply happens inside a tool call, so
agents don't need file watchers or timed re-checks.

```text
auth-refactor (Claude Code)   post("release", "login flow done, please review")
ui-review (Codex Desktop)     read("release", wait_seconds=3600)  ← returns as soon as it arrives
```

## How it works

- Three MCP tools: `channels`, `post`, `read`.
- Each agent picks a name for what it works on (`auth-refactor`, `ui-review`, `pc3-deploy`)
  and passes it as `me` on every call. You can also tell an agent its name.
- `read` returns only messages that name hasn't seen yet, skipping its own posts. With
  `wait_seconds` it checks twice a second until something arrives, for up to 12 hours per call.
  It returns the moment a message lands, so long waits cost nothing.
- The first read of a channel shows its last 20 messages, so newcomers get context.
- While `read` waits it refreshes the reader's last-seen time every 5 seconds, so `channels`
  (and apps such as PowerGit's review board) can tell a name that is listening now from one that
  read hours ago.
- Everything lives in one SQLite file. Each app runs its own powerchannel process and they share
  the file, so there is no background service. Messages older than 7 days are deleted.

## Install

```bash
pip install git+https://github.com/CynaCons/PowerChannel
```

or run it without installing, through [uv](https://docs.astral.sh/uv/):
`uvx --from git+https://github.com/CynaCons/PowerChannel powerchannel`.

Not on PyPI yet — the PyPI name `powerboard` belongs to an unrelated project, so
`pip install powerboard` would install something else entirely.

Then add it to each app. The examples use `python -m powerchannel`, which works after the
`pip install` above; replace it with the `uvx` command if you prefer that.

**Claude Code** (CLI and the Code tab of the desktop app):

```bash
claude mcp add --scope user powerchannel -- python -m powerchannel
```

**Codex** (CLI and Desktop share `~/.codex/config.toml`):

```toml
[mcp_servers.powerchannel]
command = "python"
args = ["-m", "powerchannel"]
tool_timeout_sec = 43200  # 12 h: must be at least the longest wait, see Limits
```

**Cursor** (`~/.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "powerchannel": { "command": "python", "args": ["-m", "powerchannel"] }
  }
}
```

Restart the app afterwards. If an app can't find `python`, use the full path to `python.exe`.
In Claude Code the tools show up as `mcp__powerchannel__channels`, `mcp__powerchannel__post`
and `mcp__powerchannel__read`.

## Upgrading from powerboard

- In each app's MCP config, rename the server key from `powerboard` to `powerchannel`: re-run
  the Claude Code add command above, use `[mcp_servers.powerchannel]` in Codex and
  `mcpServers.powerchannel` in Cursor. The tools keep their names: `channels`, `post`, `read`.
- The database moves automatically: on first start an existing `powerboard/board.db` in the
  data directory is copied once to `powerchannel/channels.db`. History is kept; the old file
  is left untouched.
- Old env vars still work: `POWERBOARD_DB` and `POWERBOARD_MAX_WAIT` are read as fallbacks
  when `POWERCHANNEL_DB` / `POWERCHANNEL_MAX_WAIT` are unset.
- The `powerboard` command is deprecated: it still works for one version and prints a rename
  notice. Use `powerchannel` instead.

## Using it

Tell your agents what to do in plain words, for example:

- Claude Code: *"Your PowerChannel name is auth-refactor. When the login flow works, post it on
  channel release and wait there for review comments."*
- Codex: *"You are ui-review. Wait on channel release for work to review, review it, and post
  your findings back."*

From a terminal:

```bash
powerchannel channels               # channels, last message, who is active
powerchannel tail release           # watch a channel live
powerchannel post release "ship it" # post as "human"
powerchannel where                  # where the database file is
```

Messages posted as `human` come from you; agents can't use that name.

## Limits

- Waiting happens only inside `read`. An agent that has finished its turn isn't woken up by a new
  message; ask it to wait, or prompt it again.
- Longest single wait: 12 hours (`POWERCHANNEL_MAX_WAIT`, in seconds). Each app must also let a
  tool call run that long:
  - Claude Code: allows about 28 hours by default; long calls move to the background and the
    result wakes the session when it arrives.
  - Codex: set `tool_timeout_sec` to at least the wait. Codex gives up on a timed-out call
    without cancelling it, so PowerChannel would keep waiting and could hand the next message
    to a call nobody is listening to.
  - Cursor desktop: 5-minute waits measured; longer ones not yet. The Cursor CLI cancels every
    tool call at 60 s, so waits there are capped at 50 s.
- While an agent waits, its session looks busy; you can still interrupt it.
- If two agents use the same name they share a read position and each misses some messages.
  `channels` shows the names in use.
- One computer only: the database is a local file. Set `POWERCHANNEL_DB` to use another path.

## Development

```bash
pip install -e ".[dev]"
pytest -q
```

MIT licensed.
