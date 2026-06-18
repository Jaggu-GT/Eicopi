#!/usr/bin/env python3
"""pihud-scroll - scroll the e-ink AI answer.

  pihud-scroll            interactive: Up/Down arrows scroll, q / Ctrl-C quits
  pihud-scroll up|down    one-shot: send a single scroll step and exit

Runs as your login (you must be in the 'pihud' group). It sends a bounded
scroll command over the pihud FIFO; the daemon - locked-down 'huddisp', no
terminal of its own - repaints. The e-ink steps at ~1-2 s (panel refresh
limit), so tap and wait rather than holding the key down.
"""
import json
import os
import select
import stat
import sys
import termios
import tty

DEFAULT_FIFO = "/run/pihud/ai.fifo"
FIFO = os.environ.get("PIHUD_FIFO", DEFAULT_FIFO)


def send(direction):
    """Validate the path is really a FIFO, then write one bounded record.
    Never blocks or raises if the daemon is not listening."""
    try:
        if not stat.S_ISFIFO(os.stat(FIFO).st_mode):
            print("refusing to write to non-FIFO path: %s" % FIFO, file=sys.stderr)
            return False
        fd = os.open(FIFO, os.O_WRONLY | os.O_NONBLOCK)
    except OSError as exc:
        print("cannot open FIFO %s: %s" % (FIFO, exc), file=sys.stderr)
        return False
    try:
        rec = {"status": "scroll", "dir": direction}
        os.write(fd, (json.dumps(rec, separators=(",", ":")) + "\n").encode("utf-8"))
    except OSError:
        return False
    finally:
        os.close(fd)
    return True


def read_key():
    """Read one keypress; resolve arrow escape sequences without blocking on a
    bare ESC (select peeks for the rest of the sequence)."""
    ch = sys.stdin.read(1)
    if ch == "\x1b":
        r, _, _ = select.select([sys.stdin], [], [], 0.05)
        if r:
            ch += sys.stdin.read(2)
    return ch


def interactive():
    if not sys.stdin.isatty():
        print("pihud-scroll needs an interactive terminal (or pass up|down).", file=sys.stderr)
        return 1
    if not os.path.exists(FIFO):
        print("pihud FIFO not found at %s - is the pihud service running?" % FIFO, file=sys.stderr)
        return 1
    print("pihud-scroll: Up/Down = scroll AI answer,  q = quit")
    old = termios.tcgetattr(sys.stdin)
    try:
        tty.setcbreak(sys.stdin.fileno())
        while True:
            key = read_key()
            if key in ("q", "Q", "\x03", "\x04", "\x1b"):   # q / Ctrl-C / Ctrl-D / bare ESC
                break
            if key == "\x1b[A":
                send("up")
            elif key == "\x1b[B":
                send("down")
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old)
    return 0


def main(argv):
    if len(argv) == 2 and argv[1] in ("up", "down"):
        return 0 if send(argv[1]) else 1
    if len(argv) == 1:
        return interactive()
    print("usage: pihud-scroll [up|down]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
