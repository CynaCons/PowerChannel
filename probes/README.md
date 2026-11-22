# Wake probes

An experiment, not part of the PowerChannel package. `wake_probe.py` checks, on one computer,
whether each agent app can be woken by a message sent from outside the session. A future
PowerChannel version could wake agents this way, instead of each agent waiting in `read`.

It uses the Python standard library only and runs from the root of a clone of this repository:

```powershell
git clone https://github.com/CynaCons/PowerChannel
cd PowerChannel
python probes/wake_probe.py                       # every app found on PATH
python probes/wake_probe.py claude codex          # only these apps
python probes/wake_probe.py --skip-live           # background checks only, no windows
python probes/wake_probe.py codex --thread <id>   # a thread you opened yourself, e.g. in Codex Desktop
```

Run it in your own terminal window. If you start it from a background or IDE shell, the windows
it opens may not appear.

Each probe asks the agent to "reply with exactly" a random token, and passes only when that reply
shows up in the app's own session log. Probe turns use no tools and touch no files. Results print
as a table and are saved to `wake-probe-results-<date>-<HHMM>.json` in the current folder.

## What each app is asked to do

| App | Live session | Background session |
|---|---|---|
| Claude Code | An MCP server pushes `notifications/claude/channel` into a session started with `--dangerously-load-development-channels` | `claude -p --resume <id>` |
| Codex | `codex queue --thread <id> --message …` | `codex queue` to a closed thread, then `codex exec resume <id>` |
| Cursor CLI | A stop hook in `.cursor/hooks.json` returns the message as `followup_message`. Separately, the agent watches a background shell that prints the message | `cursor-agent -p --resume <chat>` |
| Gemini CLI | Not tested: no known way to push | `gemini --resume latest` |
| Grok | Not tested: `grok leader list` shows processes, with no way to send a message | `grok -p --resume <id>` |

For the live checks, an idle session gets one message, then a long task followed one second
later by a second message. That check passes when the second message waits for the long turn to
end and is then answered.

## What you need to do while it runs

- **Claude Code:** a new window opens (`--no-spawn` prints the command instead). Choose
  "I am using this for local development" at the channel warning, and log in if asked. On a
  Team or Enterprise account an admin must turn channels on first, or the probe server never
  connects.
- **Codex:** a new window opens on the probe thread. Answer any start-up prompt, wait for the
  input box, then press Enter in the probe's own terminal. To test Codex Desktop instead, open
  the probe thread there, or pass `--thread` with a Desktop thread id.
- **Cursor:** two windows open in turn. The first needs nothing from you: its stop hook
  carries the messages. In the second, approve the one background command if Cursor asks.
- Close the probe windows when the summary prints.

## Limits

- The Claude desktop Code tab can't start a session with a development channel, so only the
  CLI is checked. The desktop app needs the plugin route (`--channels`), which isn't tested.
- Claude probe turns use `--claude-model haiku` and Cursor turns `--cursor-model auto` (the
  model Cursor's Free plan includes). Codex, Gemini and Grok use each app's default model.
- Each background Cursor call takes about 20 seconds end to end. One resume hung for minutes in
  an early run, for no reason we found; the script now times it out instead of hanging.
- Cursor Desktop isn't covered by the script. Cursor documents the same hooks for its IDE agent
  chats.
