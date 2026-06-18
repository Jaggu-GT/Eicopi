#!/usr/bin/env python3
"""Send a bounded scroll command to the pihud AI display FIFO."""
import json
import os
import stat
import sys


DEFAULT_FIFO = "/run/pihud/ai.fifo"
FIFO = os.environ.get("PIHUD_FIFO", DEFAULT_FIFO)


def usage():
    print("usage: pihud-scroll.py up|down", file=sys.stderr)


def open_fifo(path):
    try:
        fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
    except OSError as exc:
        print("cannot open FIFO %s: %s" % (path, exc), file=sys.stderr)
        return None
    try:
        mode = os.fstat(fd).st_mode
    except OSError as exc:
        os.close(fd)
        print("cannot stat FIFO %s: %s" % (path, exc), file=sys.stderr)
        return None
    if not stat.S_ISFIFO(mode):
        os.close(fd)
        print("refusing to write to non-FIFO path: %s" % path, file=sys.stderr)
        return None
    return fd


def main(argv):
    if len(argv) != 2 or argv[1] not in ("up", "down"):
        usage()
        return 2

    fd = open_fifo(FIFO)
    if fd is None:
        return 1

    try:
        rec = {"status": "scroll", "dir": argv[1]}
        os.write(fd, (json.dumps(rec, separators=(",", ":")) + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
