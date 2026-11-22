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


def run(cmd: list[str], cwd: Path | None = None, timeout: int = TURN_WAIT) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, stdin=subprocess.DEVNULL)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return -1, f"timed out after {timeout}s"
    except OSError as e:
        return -1, str(e)


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


def probe_cursor(args) -> None:
    exe = cursor_cli()
    if not exe:
        record("cursor", "installed", "skip", "no Cursor CLI (cursor-agent) on PATH")
        return
    record("cursor", "installed", "info", tail(run([exe, "--version"], timeout=60)[1], 1))
    work = Path(tempfile.mkdtemp(prefix="pcw-cursor-"))
    word, ready = token("WORD"), token("READY")
    rc, out = run([exe, "-p", "--output-format", "json",
                   f"{NO_TOOLS} Remember the word {word}. Reply with exactly {ready}"], cwd=work)
    m = re.search(r'"(?:session_id|sessionId|chatId|chat_id)"\s*:\s*"([^"]+)"', out)
    if ready not in out or not m:
        record("cursor", "first headless turn", "fail", tail(out))
        return
    chat = m.group(1)
    record("cursor", "first headless turn", "pass", f"chat {chat}")
    resumed = token("RESUMED")
    rc, out = run([exe, "-p", "--resume", chat,
                   f"{NO_TOOLS} Reply with {resumed} followed by the word I asked you to remember."], cwd=work)
    ok = resumed in out and word in out
    record("cursor", "resume the chat headless", "pass" if ok else "fail",
           "replied with context kept" if ok else tail(out))
    record("cursor", "wake a live IDE chat", "info", "no known outside trigger for IDE chats; not tested")


PROBES = {"claude": probe_claude, "codex": probe_codex, "gemini": probe_gemini, "grok": probe_grok,
          "cursor": probe_cursor}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["channel-server"]:
        channel_server(Path(argv[1]))
        return 0
    ap = argparse.ArgumentParser(description="Check whether each agent app can be woken from outside.")
    ap.add_argument("apps", nargs="*", metavar="APP", help=f"apps to probe, default all of: {', '.join(APPS)}")
    ap.add_argument("--thread", help="Codex: use this existing thread instead of creating one")
    ap.add_argument("--claude-model", default="haiku", help="Claude model for probe turns (default: haiku)")
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
