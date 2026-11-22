#!/usr/bin/env python3
"""Wake probes: can a message sent from outside wake a live agent session?

Run it on the computer whose apps you want to check:

    python probes/wake_probe.py                       # every app found on PATH
    python probes/wake_probe.py claude codex          # only these apps
    python probes/wake_probe.py codex --thread <id>   # a thread you opened yourself, e.g. in Codex Desktop

Each probe sends short "reply with exactly <TOKEN>" messages and looks for the reply
in the app's own session log, so a pass means the agent really answered. Results are
printed as a table and saved to wake-probe-results-<date>.json in the current folder.
Standard library only; needs Python 3.10+.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

APPS = ("claude", "codex", "gemini", "grok", "cursor")
TURN_WAIT = 180  # seconds to wait for one agent turn
CONNECT_WAIT = 300  # seconds for you to answer the start-up prompts
BUSY_DELAY = 1.0  # seconds between the long message and the one sent mid-turn
NO_TOOLS = "Use no tools and touch no files."


@dataclass
class Result:
    app: str
    check: str
    status: str  # pass, fail, skip, info
    detail: str


RESULTS: list[Result] = []


def record(app: str, check: str, status: str, detail: str) -> None:
    RESULTS.append(Result(app, check, status, detail))
    print(f"  [{status.upper():4}] {check}: {detail}", flush=True)


def token(label: str) -> str:
    return f"PCW{secrets.token_hex(3).upper()}{label}"


def kill_tree(p: subprocess.Popen) -> None:
    """Kill a process and everything it started (Windows .cmd shims start powershell and node)."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
    else:
        try:
            os.killpg(p.pid, 9)
        except OSError:
            p.kill()


def run(cmd: list[str], cwd: Path | None = None, timeout: int = TURN_WAIT, until=None) -> tuple[int, str]:
    """Run a command and return (exit code, output).

    With `until`, return as soon as until(output) is true and kill the process tree; some
    CLIs keep running for a while after printing their result.
    """
    try:
        p = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                             start_new_session=os.name != "nt")
    except OSError as e:
        return -1, str(e)
    chunks: list[str] = []
    reader = threading.Thread(target=lambda: chunks.extend(iter(p.stdout.readline, "")), daemon=True)
    reader.start()
    end, matched = time.monotonic() + timeout, False
    while p.poll() is None:
        if until and until("".join(chunks)):
            matched = True
            break
        if time.monotonic() >= end:
            chunks.append(f"\ntimed out after {timeout}s")
            break
        time.sleep(0.2)
    if p.poll() is None:
        kill_tree(p)
    reader.join(2)
    rc = 0 if matched else (p.returncode if p.returncode is not None else -1)
    return rc, "".join(chunks)


def cursor_done(text: str) -> bool:
    return '"type":"result"' in text.replace(" ", "")


def tail(text: str, n: int = 2) -> str:
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    return " | ".join(lines[-n:])[:240] or "(no output)"


def wait_for(check, timeout: float, every: float = 1.0):
    end = time.monotonic() + timeout
    while True:
        value = check()
        if value or time.monotonic() >= end:
            return value
        time.sleep(every)


def show_command(cmd: list[str]) -> str:
    return subprocess.list2cmdline(cmd) if os.name == "nt" else shlex.join(cmd)


def open_window(cmd: list[str], cwd: Path, spawn: bool) -> None:
    """Start an interactive app in its own console on Windows, or tell the user how."""
    if spawn and os.name == "nt":
        subprocess.Popen(cmd, cwd=cwd, creationflags=subprocess.CREATE_NEW_CONSOLE)
        print("  A new window opened. Answer its start-up prompts there (trust folder, update, etc.).")
    else:
        print(f"  In another terminal, from {cwd}, run:\n    {show_command(cmd)}")


def ask_enter(prompt: str) -> None:
    try:
        input(f"  {prompt} Press Enter to continue... ")
    except EOFError:
        pass


# --- reading the apps' own session logs ------------------------------------------

def agent_lines(path: Path | None) -> list[str]:
    """Lines of a JSONL session log that the agent wrote (not the prompts sent to it)."""
    if not path:
        return []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return [ln for ln in f if '"assistant"' in ln or '"agent_message"' in ln]
    except OSError:
        return []


def reply_at(path: Path | None, tok: str) -> int | None:
    for i, line in enumerate(agent_lines(path)):
        if tok in line:
            return i
    return None


def claude_transcript(session_id: str) -> Path | None:
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"
    return next(iter(base.glob(f"*/{session_id}.jsonl")), None)


def codex_rollout(thread: str) -> Path | None:
    base = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "sessions"
    return next(iter(base.glob(f"**/rollout-*{thread}.jsonl")), None)


def check_idle_and_busy(app: str, send, log_path, how: str) -> None:
    """Send one message to an idle session, then two in a row to test a busy one."""
    idle = token("IDLE")
    send(f"PowerChannel wake probe, sent with {how}. {NO_TOOLS} Reply with exactly {idle}")
    if wait_for(lambda: reply_at(log_path(), idle) is not None, TURN_WAIT):
        record(app, "wake an idle session", "pass", f"replied {idle}")
    else:
        record(app, "wake an idle session", "fail", f"no reply with {idle} within {TURN_WAIT}s")
        return
    time.sleep(3)
    long_tok, after_tok = token("LONG"), token("AFTER")
    send(f"Busy probe. {NO_TOOLS} Write the numbers 1 to 200, one per line, then the word {long_tok}.")
    time.sleep(BUSY_DELAY)
    send(f"Sent while you were busy. {NO_TOOLS} Reply with exactly {after_tok}")
    both = wait_for(lambda: reply_at(log_path(), long_tok) is not None and reply_at(log_path(), after_tok) is not None,
                    TURN_WAIT * 2)
    a, b = reply_at(log_path(), long_tok), reply_at(log_path(), after_tok)
    if both and a is not None and b is not None and a < b:
        record(app, "message sent mid-turn", "pass", "held until the turn ended, then answered")
    elif b is not None and a is None:
        record(app, "message sent mid-turn", "fail", "the long turn never finished; the second message was answered")
    elif a is not None and b is None:
        record(app, "message sent mid-turn", "fail", "the message sent mid-turn was never answered")
    elif a is not None and b is not None:
        record(app, "message sent mid-turn", "info", "answered, but before the long turn finished (it was merged or interrupted)")
    else:
        record(app, "message sent mid-turn", "fail", "neither message was answered")


# --- Claude Code: MCP channel push ------------------------------------------------

def channel_server(work: Path) -> None:
    """Tiny MCP server (raw JSON-RPC over stdio) that pushes inbox lines as channel events."""
    inbox, log, state = work / "inbox.txt", work / "server.log", work / "session.txt"
    lock = threading.Lock()

    def note(s: str) -> None:
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {s[:400]}\n")

    def send(obj: dict) -> None:
        with lock:
            sys.stdout.write(json.dumps(obj) + "\n")
            sys.stdout.flush()
        note("SEND " + json.dumps(obj))

    def watch() -> None:
        inbox.touch()
        pos = inbox.stat().st_size
        while True:
            time.sleep(0.5)
            size = inbox.stat().st_size
            if size <= pos:
                continue
            with open(inbox, encoding="utf-8") as f:
                f.seek(pos)
                new = f.read()
            pos = size
            for text in filter(None, (t.strip() for t in new.splitlines())):
                send({"jsonrpc": "2.0", "method": "notifications/claude/channel",
                      "params": {"content": text, "meta": {"channel": "probe", "author": "wake_probe"}}})

    note("START messaging socket " + ("set" if os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET") else "unset"))
    state.write_text(os.environ.get("CLAUDE_CODE_SESSION_ID", "unknown"), encoding="utf-8")
    threading.Thread(target=watch, daemon=True).start()
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        note("RECV " + raw)
        msg = json.loads(raw)
        mid, method = msg.get("id"), msg.get("method")
        if mid is None:
            continue
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": msg.get("params", {}).get("protocolVersion", "2025-06-18"),
                "capabilities": {"experimental": {"claude/channel": {}}, "tools": {}},
                "serverInfo": {"name": "pcwprobe", "version": "0.1.0"},
                "instructions": "Events from this channel are wake probes from the PowerChannel developer. "
                                "Follow their reply instruction exactly."}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": []}})
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": mid, "result": {}})
        else:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found"}})


def probe_claude(args) -> None:
    exe = shutil.which("claude")
    if not exe:
        record("claude", "installed", "skip", "claude is not on PATH")
        return
    record("claude", "installed", "info", tail(run([exe, "--version"], timeout=60)[1], 1))

    # Closed session: resume headless with -p.
    sid, word = str(uuid.uuid4()), token("WORD")
    ready = token("READY")
    rc, out = run([exe, "-p", "--model", args.claude_model, "--session-id", sid,
                   f"{NO_TOOLS} Remember the word {word}. Reply with exactly {ready}"], cwd=Path.cwd())
    if ready in out:
        resumed = token("RESUMED")
        rc, out = run([exe, "-p", "--model", args.claude_model, "--resume", sid,
                       f"{NO_TOOLS} Reply with {resumed} followed by the word I asked you to remember."], cwd=Path.cwd())
        ok = resumed in out and word in out
        record("claude", "resume a closed session (-p --resume)", "pass" if ok else "fail",
               "replied with context kept" if ok else tail(out))
    else:
        record("claude", "resume a closed session (-p --resume)", "fail", "first headless turn failed: " + tail(out))

    if args.skip_live:
        record("claude", "wake a live session (channel push)", "skip", "--skip-live")
        return
    # Live session: push through a development channel.
    work =Path(tempfile.mkdtemp(prefix="pcw-claude-"))
    state = work / "session.txt"
    (work / "inbox.txt").touch()
    cfg = {"mcpServers": {"pcwprobe": {"command": sys.executable,
                                       "args": [str(Path(__file__).resolve()), "channel-server", str(work)]}}}
    (work / "mcp.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    first = token("READY")
    cmd = [exe, "--strict-mcp-config", "--mcp-config", str(work / "mcp.json"),
           "--dangerously-load-development-channels", "server:pcwprobe", "--model", args.claude_model,
           "-n", "pc-wake-probe", f"PowerChannel wake probe. {NO_TOOLS} Reply with exactly {first}, then stop."]
    print("\n  Live Claude Code session with a probe channel:")
    open_window(cmd, Path.cwd(), args.spawn)
    print("  Choose 'I am using this for local development' at the channel warning, and log in if asked.")
    sid = wait_for(lambda: state.exists() and state.read_text(encoding="utf-8").strip(), CONNECT_WAIT)
    if not sid:
        record("claude", "channel server connected", "fail",
               f"no session started the probe server within {CONNECT_WAIT}s (prompt not answered, or channels off by policy)")
        return
    record("claude", "channel server connected", "pass", f"session {sid}")
    log_path = lambda: claude_transcript(sid)
    if not wait_for(lambda: reply_at(log_path(), first) is not None, CONNECT_WAIT):
        record("claude", "first turn", "fail", f"no reply with {first}; is the session logged in?")
        return

    def push(text: str) -> None:
        with open(work / "inbox.txt", "a", encoding="utf-8") as f:
            f.write(text + "\n")

    check_idle_and_busy("claude", push, log_path, "an MCP channel push")
    print("  You can close the probe Claude window now.")


# --- Codex: codex queue -------------------------------------------------------------

def probe_codex(args) -> None:
    exe = shutil.which("codex")
    if not exe:
        record("codex", "installed", "skip", "codex is not on PATH")
        return
    record("codex", "installed", "info", tail(run([exe, "--version"], timeout=60)[1], 1))
    cwd = Path.cwd()
    thread = args.thread
    if not thread:
        ready = token("READY")
        rc, out = run([exe, "exec", "-s", "read-only", "--skip-git-repo-check",
                       f"PowerChannel wake probe thread. {NO_TOOLS} Reply with exactly {ready}"], cwd=cwd)
        m = re.search(r"session id:\s*([0-9a-fA-F-]{36})", out)
        if not m or ready not in out:
            record("codex", "create a probe thread", "fail", tail(out))
            return
        thread = m.group(1)
        record("codex", "create a probe thread", "pass", thread)

        # Closed thread: a queued message should wait; exec resume should run.
        parked = token("PARKED")
        run([exe, "queue", "--thread", thread, "--message", f"{NO_TOOLS} Reply with exactly {parked}"], timeout=60)
        time.sleep(15)
        ran = reply_at(codex_rollout(thread), parked) is not None
        record("codex", "queue to a closed thread", "info",
               "ran without anyone opening it" if ran else "accepted, not run while the thread is closed")
        resumed = token("RESUMED")
        rc, out = run([exe, "exec", "-s", "read-only", "--skip-git-repo-check", "resume", thread,
                       f"{NO_TOOLS} Reply with exactly {resumed}"], cwd=cwd)
        ok = reply_at(codex_rollout(thread), resumed) is not None or resumed in out
        record("codex", "resume a closed thread (exec resume)", "pass" if ok else "fail",
               "ran a turn on the saved thread" if ok else tail(out))
        if not ran:
            late = reply_at(codex_rollout(thread), parked) is not None
            record("codex", "queued message kept until the thread opens", "pass" if late else "fail",
                   "answered when exec resume opened the thread" if late else "never answered")

    if args.skip_live:
        record("codex", "wake a live session (codex queue)", "skip", "--skip-live")
        return
    print(f"\n  Live Codex session on thread {thread}:")
    if args.thread:
        print("  Open that thread where you want to test it (Codex Desktop or the terminal) and leave it idle.")
    else:
        open_window([exe, "resume", thread], cwd, args.spawn)
        print("  Or open the same thread in Codex Desktop instead, to test the desktop app.")
    ask_enter("When the thread is open and idle,")
    log_path = lambda: codex_rollout(thread)

    def queue(text: str) -> None:
        rc, out = run([exe, "queue", "--thread", thread, "--message", text], timeout=60)
        if rc != 0:
            print(f"  codex queue failed: {tail(out)}")

    check_idle_and_busy("codex", queue, log_path, "codex queue")


# --- Gemini CLI and Grok: headless resume -------------------------------------------

def two_turns(app: str, first_cmd, second_cmd, cwd: Path) -> None:
    word, ready, resumed = token("WORD"), token("READY"), token("RESUMED")
    rc, out = run(first_cmd(f"{NO_TOOLS} Remember the word {word}. Reply with exactly {ready}"), cwd=cwd)
    if ready not in out:
        record(app, "first headless turn", "fail", tail(out))
        return
    record(app, "first headless turn", "pass", f"replied {ready}")
    rc, out = run(second_cmd(f"{NO_TOOLS} Reply with {resumed} followed by the word I asked you to remember."), cwd=cwd)
    ok = resumed in out and word in out
    record(app, "resume the session headless", "pass" if ok else "fail",
           "replied with context kept" if ok else tail(out))


def probe_gemini(args) -> None:
    exe = shutil.which("gemini")
    if not exe:
        record("gemini", "installed", "skip", "gemini is not on PATH")
        return
    record("gemini", "installed", "info", tail(run([exe, "--version"], timeout=60)[1], 1))
    work = Path(tempfile.mkdtemp(prefix="pcw-gemini-"))
    two_turns("gemini", lambda p: [exe, p], lambda p: [exe, "--resume", "latest", p], work)
    record("gemini", "wake a live session", "info", "no known way to push into an interactive session; not tested")


def probe_grok(args) -> None:
    exe = shutil.which("grok")
    if not exe:
        record("grok", "installed", "skip", "grok is not on PATH")
        return
    record("grok", "installed", "info", tail(run([exe, "--version"], timeout=60)[1], 1))
    rc, out = run([exe, "leader", "list"], timeout=60)
    record("grok", "leader processes", "info", tail(out))
    sid = str(uuid.uuid4())
    work = Path(tempfile.mkdtemp(prefix="pcw-grok-"))
    two_turns("grok", lambda p: [exe, "-p", p, "-s", sid], lambda p: [exe, "-p", p, "-r", sid], work)
    record("grok", "wake a live session", "info",
           "a second ACP client on ~/.grok/leader.sock might prompt a live session; not tested")


# --- Cursor: cursor-agent resume -----------------------------------------------------

def cursor_cli() -> str | None:
    exe = shutil.which("cursor-agent")
    if exe:
        return exe
    agent = shutil.which("agent")  # newer installs name it "agent"; Grok also installs one
    if agent and "grok" not in run([agent, "--version"], timeout=30)[1].lower():
        return agent
    return None


HOOK_WAIT = 900  # seconds the probe stop hook waits for a message before letting the turn end


def cursor_stop_hook(work: Path) -> None:
    """Cursor stop hook: wait for the next inbox line and return it as followup_message.

    Cursor submits a non-empty followup_message as the next user message, so this wakes
    the session without another agent. stdout carries only the JSON answer.
    """
    inbox, offset, state = work / "inbox.txt", work / "inbox.offset", work / "hook.json"
    try:
        info = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        info = {}
    state.write_text(json.dumps({k: info.get(k) for k in ("conversation_id", "transcript_path", "loop_count")}),
                     encoding="utf-8")
    inbox.touch()
    pos = int(offset.read_text()) if offset.exists() else 0
    answer: dict = {}
    end = time.monotonic() + HOOK_WAIT
    while time.monotonic() < end and not answer:
        if inbox.stat().st_size > pos:
            with open(inbox, encoding="utf-8") as f:
                f.seek(pos)
                line = f.readline()
                pos = f.tell()
            if line.strip():
                answer = {"followup_message": line.strip()}
        else:
            time.sleep(0.5)
    offset.write_text(str(pos))
    sys.stdout.write(json.dumps(answer))
    sys.stdout.flush()


def wait_line(path: Path) -> None:
    """Print the next line appended to `path`, then exit (for the watched-shell probe)."""
    path.touch()
    pos = path.stat().st_size
    print("waiting for a PowerChannel message...", flush=True)
    while True:
        if path.stat().st_size > pos:
            with open(path, encoding="utf-8") as f:
                f.seek(pos)
                line = f.readline().strip()
            if line:
                print("POWERCHANNEL MESSAGE: " + line, flush=True)
                return
        time.sleep(0.5)


def cursor_transcript_with(tok: str, since: float) -> Path | None:
    base = Path.home() / ".cursor" / "projects"
    for p in base.glob("*/agent-transcripts/*/*.jsonl"):
        try:
            if p.stat().st_mtime >= since and reply_at(p, tok) is not None:
                return p
        except OSError:
            continue
    return None


def probe_cursor(args) -> None:
    exe = cursor_cli()
    if not exe:
        record("cursor", "installed", "skip", "no Cursor CLI (cursor-agent or agent) on PATH")
        return
    record("cursor", "installed", "info", tail(run([exe, "--version"], timeout=60)[1], 1))
    model = ["--model", args.cursor_model] if args.cursor_model else []

    # Closed chat: resume headless with -p --resume.
    work = Path(tempfile.mkdtemp(prefix="pcw-cursor-"))
    base = [exe, "-p", "--mode", "ask", "--trust", "--output-format", "json", *model]
    word, ready = token("WORD"), token("READY")
    rc, out = run(base + [f"{NO_TOOLS} Remember the word {word}. Reply with exactly {ready}"], cwd=work,
                  until=cursor_done)
    m = re.search(r'"session_id"\s*:\s*"([^"]+)"', out)
    if ready not in out or not m:
        record("cursor", "first headless turn", "fail", tail(out))
    else:
        chat = m.group(1)
        resumed = token("RESUMED")
        rc, out = run(base + ["--resume", chat,
                              f"{NO_TOOLS} Reply with {resumed} followed by the word I asked you to remember."],
                      cwd=work, until=cursor_done)
        ok = resumed in out and word in out
        record("cursor", "resume a closed chat (-p --resume)", "pass" if ok else "fail",
               "replied with context kept" if ok else tail(out))
    if args.skip_live:
        record("cursor", "wake a live session (stop hook, watched shell)", "skip", "--skip-live")
        return

    # Live session, path 1: a stop hook that returns the next message as followup_message.
    hook_ws = Path(tempfile.mkdtemp(prefix="pcw-cursor-hook-"))
    (hook_ws / ".cursor").mkdir()
    hook_cmd = subprocess.list2cmdline([sys.executable, str(Path(__file__).resolve()), "cursor-stop-hook", str(hook_ws)]) \
        if os.name == "nt" else shlex.join([sys.executable, str(Path(__file__).resolve()), "cursor-stop-hook", str(hook_ws)])
    (hook_ws / ".cursor" / "hooks.json").write_text(json.dumps(
        {"version": 1, "hooks": {"stop": [{"command": hook_cmd, "loop_limit": None, "timeout": HOOK_WAIT + 60}]}},
        indent=2), encoding="utf-8")
    first = token("READY")
    print("\n  Live Cursor CLI session with a probe stop hook:")
    open_window([exe, "--trust", *model, "--workspace", str(hook_ws),
                 f"PowerChannel wake probe. {NO_TOOLS} Reply with exactly {first}, then stop."], Path.cwd(), args.spawn)
    state = hook_ws / "hook.json"
    hooked = wait_for(lambda: state.exists() and json.loads(state.read_text(encoding="utf-8")).get("transcript_path"),
                      CONNECT_WAIT)
    if not hooked:
        record("cursor", "stop hook runs", "fail", f"the stop hook didn't run within {CONNECT_WAIT}s")
    else:
        record("cursor", "stop hook runs", "pass", "the hook ran when the first turn ended")
        transcript = Path(hooked)

        def push(text: str) -> None:
            with open(hook_ws / "inbox.txt", "a", encoding="utf-8") as f:
                f.write(text + "\n")

        check_idle_and_busy("cursor", push, lambda: transcript, "a stop hook followup_message")
        print("  You can close the probe Cursor window now.")

    # Live session, path 2: the agent watches its own background shell.
    watch_ws = Path(tempfile.mkdtemp(prefix="pcw-cursor-watch-"))
    started, woke = token("STARTED"), token("SHELL")
    waiter = show_command([sys.executable, str(Path(__file__).resolve()), "wait-line", str(watch_ws / "inbox.txt")])
    since = time.time()
    print("\n  Live Cursor CLI session watching a background shell:")
    open_window([exe, "--trust", *model, "--workspace", str(watch_ws),
                 f"Wake probe. Start this command as a background shell and watch it for output: {waiter} . "
                 "When it prints a line starting with POWERCHANNEL MESSAGE, follow the instruction in that line. "
                 f"Run no other commands. After starting the watch, reply {started}."], Path.cwd(), args.spawn)
    print("  Approve the one background command if Cursor asks.")
    started_in = wait_for(lambda: cursor_transcript_with(started, since), CONNECT_WAIT)
    if not started_in:
        record("cursor", "watched background shell", "fail", f"the agent never replied {started}")
        return
    # Wait until the turn has ended, so the reply below proves a wake-up and not the same turn.
    # Cursor wakes the agent with a "task result" user message when the watched output appears.
    wait_for(lambda: '"turn_ended"' in started_in.read_text(encoding="utf-8", errors="replace"), 60)
    time.sleep(10)
    with open(watch_ws / "inbox.txt", "a", encoding="utf-8") as f:
        f.write(f"{NO_TOOLS} Reply with exactly {woke}\n")
    if wait_for(lambda: cursor_transcript_with(woke, since), TURN_WAIT):
        record("cursor", "watched background shell wakes the agent", "pass", f"replied {woke}")
    else:
        record("cursor", "watched background shell wakes the agent", "fail", f"no reply with {woke} within {TURN_WAIT}s")
    print("  You can close the second probe Cursor window now.")


PROBES = {"claude": probe_claude, "codex": probe_codex, "gemini": probe_gemini, "grok": probe_grok,
          "cursor": probe_cursor}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["channel-server"]:
        channel_server(Path(argv[1]))
        return 0
    if argv[:1] == ["cursor-stop-hook"]:
        cursor_stop_hook(Path(argv[1]))
        return 0
    if argv[:1] == ["wait-line"]:
        wait_line(Path(argv[1]))
        return 0
    ap = argparse.ArgumentParser(description="Check whether each agent app can be woken from outside.")
    ap.add_argument("apps", nargs="*", metavar="APP", help=f"apps to probe, default all of: {', '.join(APPS)}")
    ap.add_argument("--thread", help="Codex: use this existing thread instead of creating one")
    ap.add_argument("--claude-model", default="haiku", help="Claude model for probe turns (default: haiku)")
    ap.add_argument("--cursor-model", default="auto",
                    help="Cursor model for probe turns (default: auto, which the Free plan includes; '' for your default)")
    ap.add_argument("--skip-live", action="store_true",
                    help="only run the background checks; open no interactive sessions")
    ap.add_argument("--no-spawn", dest="spawn", action="store_false",
                    help="print commands for interactive sessions instead of opening windows")
    args = ap.parse_args(argv)
    unknown = [a for a in args.apps if a not in APPS]
    if unknown:
        ap.error(f"unknown app {', '.join(unknown)}; choose from {', '.join(APPS)}")
    for app in args.apps or APPS:
        print(f"\n== {app}", flush=True)
        try:
            PROBES[app](args)
        except KeyboardInterrupt:
            record(app, "probe", "skip", "stopped with Ctrl-C")
    out = Path.cwd() / f"wake-probe-results-{datetime.now():%Y-%m-%d-%H%M}.json"
    out.write_text(json.dumps([asdict(r) for r in RESULTS], indent=2), encoding="utf-8")
    print("\nSummary")
    width = max((len(f"{r.app} - {r.check}") for r in RESULTS), default=10)
    for r in RESULTS:
        print(f"  {r.status.upper():4}  {f'{r.app} - {r.check}':<{width}}  {r.detail}")
    print(f"\nSaved {out}")
    return 1 if any(r.status == "fail" for r in RESULTS) else 0


if __name__ == "__main__":
    sys.exit(main())
