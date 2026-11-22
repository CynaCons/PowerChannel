# Wake probes

`wake_probe.py` checks, on one computer, whether each agent app can be woken by a message
sent from outside the session. That is what a central PowerChannel hub would rely on instead
of a listener agent per session. It uses the Python standard library only.

```powershell
python probes/wake_probe.py                       # every app found on PATH
python probes/wake_probe.py claude codex          # only these apps
python probes/wake_probe.py --skip-live           # background checks only, no windows
python probes/wake_probe.py codex --thread <id>   # a thread you opened yourself, e.g. in Codex Desktop
```

Run it from a folder the apps already trust, such as this repo. Each probe asks the agent to
"reply with exactly" a random token, and passes only when that reply shows up in the app's own
session log. Probe turns use no tools and touch no files. Results print as a table and are
saved to `wake-probe-results-<date>.json` in the current folder; send that file back.

## What each app is asked to do

| App | Live session | Closed session |
|---|---|---|
| Claude Code | MCP server pushes `notifications/claude/channel` into a session started with `--dangerously-load-development-channels` | `claude -p --resume <id>` |
| Codex | `codex queue --thread <id> --message …` | `codex queue` to a closed thread, then `codex exec resume <id>` |
| Gemini CLI | not tested: no known push | `gemini --resume latest` |
| Grok | not tested: lists leader processes only | `grok -p --resume <id>` |
| Cursor | not tested: no known trigger for IDE chats | `cursor-agent -p --resume <chat>` |

For the live checks, an idle session gets one message, then a long task followed one second
later by a second message. The second check passes when that message waits for the long turn
to end and is then answered.

## What you need to do while it runs

- **Claude Code:** a new window opens (`--no-spawn` prints the command instead). Choose
  "I am using this for local development" at the channel warning, and log in if asked. On a
  Team or Enterprise account an admin must turn channels on first, or the probe server never
  connects.
- **Codex:** a new window opens on the probe thread. Answer any start-up prompt, wait for the
  input box, then press Enter in the probe's own terminal. To test Codex Desktop instead, open
  the probe thread there, or pass `--thread` with a Desktop thread id.
- Close the probe windows when the summary prints.

## Limits

- The Claude desktop Code tab can't start a session with a development channel, so only the
  CLI is checked. The desktop app needs the plugin route (`--channels`), which isn't tested.
- Claude probe turns use `--claude-model haiku` by default. Codex, Gemini, Grok and Cursor use
  each app's default model.
