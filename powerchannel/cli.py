"""Command line: `powerchannel` (MCP server), plus channels / tail / post for the human."""

from __future__ import annotations

import argparse
import sys
import time

from . import __version__
from .store import HUMAN, Board, BoardError, flat


def _line(m) -> str:
    to = f" → {m.recipient}" if m.recipient and not m.text.lower().lstrip().startswith("@" + m.recipient) else ""
    return f"[{time.strftime('%m-%d %H:%M', time.localtime(m.created))}] {m.author}{to}: {flat(m.text)}"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="powerchannel", description=__doc__)
    parser.add_argument("--version", action="version", version=f"powerchannel {__version__}")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="run the MCP server over stdio (the default)")
    sub.add_parser("channels", help="list channels and who is active")
    tail = sub.add_parser("tail", help="show a channel and follow new messages")
    tail.add_argument("channel")
    post = sub.add_parser("post", help='post a message as "human"')
    post.add_argument("channel")
    post.add_argument("message")
    post.add_argument("--to", help='one agent name, or "all" (default: a leading @name, else all)')
    sub.add_parser("where", help="print the database location")
    wait = sub.add_parser("wait", help="wait for the next message to an agent, print it and exit "
                                       "(the background command the standby tool gives an agent)")
    wait.add_argument("--me", required=True, help="the agent's name")
    wait.add_argument("--channel", required=True)
    wait.add_argument("--timeout", type=float, default=12 * 3600, help="seconds, default 43200 (12 h)")
    wait.add_argument("--db", help="database path (default: POWERCHANNEL_DB or the per-user data folder)")
    sub.add_parser("who", help="list the agents that get messages delivered, and those on standby")
    sub.add_parser("standby", help="same as who")
    hook = sub.add_parser("cursor-hook", help="Cursor stop hook (Cursor runs this; see install-cursor-hook)")
    hook.add_argument("--db", help=argparse.SUPPRESS)
    inst = sub.add_parser("install-cursor-hook", help="add the delivery hook to ~/.cursor/hooks.json")
    inst.add_argument("--db", help="database path to bake into the hook (default: none)")
    sub.add_parser("uninstall-cursor-hook", help="remove the delivery hook from ~/.cursor/hooks.json")
    args = parser.parse_args(argv)

    if args.cmd in (None, "serve"):
        from .server import serve
        serve()
        return
    if args.cmd == "cursor-hook":
        from .cursor_hook import main as hook_main
        hook_main(args.db)
        return
    if args.cmd in ("install-cursor-hook", "uninstall-cursor-hook"):
        from . import cursor_hook
        if args.cmd == "install-cursor-hook":
            path = cursor_hook.install(args.db)
            print(f"installed the PowerChannel stop hook in {path}. Restart Cursor (Desktop) or start a new "
                  "cursor-agent chat for it to take effect.")
        else:
            print("removed" if cursor_hook.uninstall() else "the PowerChannel hook wasn't installed")
        return

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    board = Board(getattr(args, "db", None))
    try:
        if args.cmd == "wait":
            from .wake import run_wait
            print(run_wait(board, args.me, args.channel, max(0.0, args.timeout)), flush=True)
        elif args.cmd in ("who", "standby"):
            members = board.members()
            for m in members:
                how = {"claude": "Claude Code session inbox", "codex": f"codex queue --thread {m.address}",
                       "cursor": f"Cursor stop hook, chat {m.address}"}.get(m.route, m.route)
                if board.is_fresh(m):  # type: ignore[arg-type]
                    state = "  (holding now)" if m.route == "cursor" else ""
                elif m.route == "cursor":
                    state = "  (not holding: delivered after the chat's next turn ends)"
                else:
                    state = "  (stale: its session has closed)"
                if m.error:
                    state += f"  (last delivery failed: {m.error})"
                print(f"{m.reader} on #{m.channel}: delivered by {how}{state}")
            rows = board.standby_rows()
            if not rows and not members:
                print("nobody gets messages delivered or is on standby")
            for r in rows:
                how = f"codex queue --thread {r.address}" if r.route == "codex" else "background wait command"
                state = "" if board.is_fresh(r) else "  (stale: its process stopped)"
                print(f"{r.reader} on #{r.channel} on standby since {time.strftime('%m-%d %H:%M', time.localtime(r.since))}, "
                      f"woken by {how} (pid {r.owner}){state}")
        elif args.cmd == "where":
            print(board.path)
        elif args.cmd == "channels":
            rows = board.channels()
            if not rows:
                print("no channels yet")
            for c in rows:
                live = f"; live: {', '.join(c['live'])}" if c["live"] else ""
                standby = f"; on standby: {', '.join(c['standby'])}" if c["standby"] else ""
                print(f"#{c['channel']} ({c['messages']} msgs) active: {', '.join(c['active']) or '-'}{live}{standby}")
                print(f"   last {_line(c['last'])}")
        elif args.cmd == "post":
            m = board.post(HUMAN, args.channel, args.message, to=args.to)
            print(f"posted to #{m.channel} as {HUMAN} (to {m.recipient or 'all'})")
        elif args.cmd == "tail":
            last_id = 0
            for m in board.recent(args.channel):
                print(_line(m), flush=True)
                last_id = m.id
            while True:
                time.sleep(1)
                for m in board.recent(args.channel, after_id=last_id):
                    print(_line(m), flush=True)
                    last_id = m.id
    except BoardError as e:
        sys.exit(f"error: {e}")
    except KeyboardInterrupt:
        pass
    finally:
        board.close()


def main_deprecated(argv: list[str] | None = None) -> None:
    """Deprecated `powerboard` entry point: warn, then run the real CLI."""
    print("powerboard was renamed powerchannel; use the powerchannel command.",
          file=sys.stderr)
    main(argv)
