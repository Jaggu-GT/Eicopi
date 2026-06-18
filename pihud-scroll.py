#!/usr/bin/env python3
# pihud-scroll - scroll the e-ink AI answer with the keyboard arrow keys.
#
# Why a separate tool: the pihud daemon runs as the locked-down 'huddisp'
# user with no controlling terminal, so it cannot read the keyboard. This
# helper runs as your normal login (you must be in the 'pihud' group), reads
# Up/Down in the terminal, and sends scroll commands over the same FIFO the
# daemon already listens on. Works on the console or over SSH.
#
#   pihud-scroll          Up / Down arrows scroll, q or Ctrl-C quits.
#
# The e-ink repaints roughly 1-2 s per step (panel refresh limit), so hold or
# tap and wait rather than spamming.
#
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Jaggu-GT

import json
import os
import select
import sys
import termios
import tty

FIFO = os.environ.get("PIHUD_FIFO", "/run/pihud/ai.fifo")


def send(direction):
    """Open the FIFO non-blocking, write one scroll record, close. Never blocks
    or raises if the daemon is not listening."""
    try:
        fd = os.open(FIFO, os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        return False
    try:
        os.write(fd, (json.dumps({"status": "scroll", "dir": direction}) + "\n").encode("utf-8"))
    except OSError:
        return False
    finally:
        os.close(fd)
    return True


def read_key():
    """Read one keypress. Resolves arrow escape sequences without blocking on a
    bare ESC (uses select to peek for the rest of the sequence)."""
    ch = sys.stdin.read(1)
    if ch == "\x1b":
        r, _, _ = select.select([sys.stdin], [], [], 0.05)
        if r:
            ch += sys.stdin.read(2)
    return ch


def main():
    if not os.path.exists(FIFO):
        print("pihud FIFO not found at %s - is the pihud service running?" % FIFO,
              file=sys.stderr)
        return 1
    if not sys.stdin.isatty():
        print("pihud-scroll needs an interactive terminal.", file=sys.stderr)
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
    except KeyboardInterrupt:
        pass
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old)
    return 0


if __name__ == "__main__":
    sys.exit(main())
