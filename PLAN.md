# PowerChannel

**Goal:** Let AI agents in different apps (Claude Code, Codex, Cursor) on one machine talk to each other through named channels, with waiting built into a tool call.

**Philosophy:** Small and hard to misuse: three tools, one SQLite file, no daemon, no hooks.

---

## v0.1 — Foundation
> First iteration group

### v0.1.0 — Bootstrap (COMPLETE)
**Goal:** Let AI agents in different apps (Claude Code, Codex, Cursor) on one machine talk to each other through named channels, with waiting built into a tool call.
- [x] SQLite store: channels, messages, per-name read position, 7-day cleanup
- [x] MCP server: channels / post / read(wait_seconds) with capped in-call polling
- [x] CLI: serve, channels, tail, post (as human)
- [x] Tests (store, waiting across processes, MCP stdio round trip) + CI (Windows + Ubuntu)
- [x] README with setup for Claude Code, Codex, Cursor
- [x] Real check: Claude Code and Codex talk through a channel
- [x] Publish to GitHub (CynaCons/powerboard)
- [x] Owner setup: released 0.1.0 installed from GitHub; registered in Claude Code (user scope, connected), Codex (tool_timeout_sec 300) and Cursor; Codex and Cursor each called channels successfully

### v0.2.0 — Long waits: up to 12 hours (2026-10-10) (COMPLETE)
**Goal:** read(wait_seconds) can wait up to 12 h (owner: "a day or half a day"). Measured 2026-10-10: Claude Code sends notifications/cancelled at its timeout; Codex 0.161 abandons the call without cancelling, so Codex's tool_timeout_sec must be at least the longest wait.
- [x] Server: MAX_WAIT 12 h (POWERBOARD_MAX_WAIT), Cursor CLI stays 50 s, progress every 30 s; instructions + read description say how long to wait
- [x] README: Codex tool_timeout_sec = 43200 and why (Codex abandons without cancel); measured limits per app
- [x] Real check: 20-minute waits in Claude Code and Codex receive a message posted after ~18 minutes
- [x] Release 0.2.0: version bump, push, reinstall for the owner, Codex config tool_timeout_sec 43200

### v0.3.0 — Presence while waiting (2026-10-14) (COMPLETE)
**Goal:** PowerGit's agent review board (PowerGit v0.21.4, docs/design/agent-review-rounds.md §4.4) shows whether the agent is listening on the review's channel. seen.updated was written only when a read returned, so a long wait looked like an old read.
- [x] read refreshes seen.updated every 5 s while it waits (Board.touch; the read position does not move); test samples it mid-wait and fails without the heartbeat
- [x] README: the heartbeat under How it works

### v0.4.0 — Rename to PowerChannel
**Goal:** Rename powerboard to PowerChannel ("PowerBoard" now names a separate task-board app): new package/CLI `powerchannel`, new MCP server name and env vars, data folder with automatic migration of the old board.db, and compatibility kept for one version.
- [x] Code: package, CLI, MCP server name, env vars, data folder + legacy migration
- [x] Docs: README upgrade guide
- [x] GitHub repo renamed to CynaCons/PowerChannel
- [x] Owner configs switched (Claude Code, Codex, Cursor)
- [x] PowerGit reads the new database path

### v0.5.0 — Native wake probes (current) (ACTIVE)
**Goal:** Prove each app can be woken from outside before building the hub.
- [x] Probe Claude Code: MCP channel push into a live session [agent: claude]
- [x] Probe Claude Code: resume a session headless with a message [agent: claude]
- [x] Probe Codex: queue a message into an existing thread [agent: claude]
- [x] Probe Codex: queue wakes a live session, idle or busy [agent: claude]
- [ ] Probe Gemini CLI, Grok and the desktop apps on the work computer
- [x] Record probe results in the architecture draft [agent: claude]
- [x] Write a wake probe script to run on the work computer [agent: claude]
- [x] Document how to run the wake probes [agent: claude]
- [x] Probe Cursor CLI: stop hook follow-up wakes a session [agent: claude]
- [x] Probe Cursor CLI: watched background shell wakes a session [agent: claude]
- [x] Probe Cursor CLI: resume a chat headless [agent: claude]
- [x] Add Cursor wake paths to the probe script [agent: claude]
- [x] Rebuild the architecture canvas as an interactive status app [agent: claude]
- [x] Probe Cursor Desktop: stop hook wakes an agent chat [agent: claude]
- [x] Rewrite the agent-facing MCP instructions and tool docs [agent: claude]
- [x] Rewrite the setup guide for every app [agent: claude]
- [x] Review the docs with fresh agents and fix gaps [agent: claude]
- [ ] Push to GitHub and publish the MCP server
