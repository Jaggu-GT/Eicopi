#!/usr/bin/env python3
"""
ollama-hud-run - run a model with `ollama run`, mirror it to your terminal, and
push the question + final answer to the pihud e-ink FIFO.

Usage:   ollama-hud-run MODEL [PROMPT ...]
Wire it to `ollama run` by adding the shell function from install-pihud.sh, then:
         ollama run qwenfast 'capital of sweden'

The model keeps its chain-of-thought in YOUR terminal; only the text printed
after "...done thinking." is sent to the e-ink. While the model thinks, the HUD
shows the question with an "...thinking" placeholder.

The FIFO is opened non-blocking: if the HUD service is not running, this just
runs ollama normally and skips the push (never blocks, never fails the command).
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time

FIFO = os.environ.get("PIHUD_FIFO", "/run/pihud/ai.fifo")
OLLAMA_BIN = os.environ.get("PIHUD_OLLAMA_BIN") or shutil.which("ollama") or "ollama"
# Strip CSI (incl. cursor show/hide like ESC[?25l / ESC[?25h), OSC, and other
# single-char escapes that ollama's spinner emits; then scrub leftover controls.
ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")
CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
DONE = re.compile(r"\.\.\.\s*done thinking\.?", re.IGNORECASE)


def open_fifo():
    try:
        return os.open(FIFO, os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        return None          # no reader / no FIFO -> HUD not listening


def push(fd, rec):
    if fd is None:
        return
    try:
        os.write(fd, (json.dumps(rec) + "\n").encode("utf-8", "replace"))
    except OSError:
        pass


def extract_answer(raw):
    text = ANSI.sub("", raw)
    matches = list(DONE.finditer(text))
    if matches:
        text = text[matches[-1].end():]
    text = re.sub(r"^\s*Thinking\.\.\.\s*", "", text, flags=re.IGNORECASE)
    text = CTRL.sub(" ", text)
    return " ".join(text.split())


def resolve_model(name):
    """Map an alias (e.g. qwenfast) to its base tag (e.g. qwen3:0.6b) via
    `ollama show --modelfile`. Falls back to the given name on anything odd."""
    try:
        out = subprocess.run([OLLAMA_BIN, "show", name, "--modelfile"],
                             capture_output=True, text=True, timeout=5)
        for raw in out.stdout.splitlines():
            s = raw.strip()
            if s.upper().startswith("FROM "):
                ref = s[5:].strip()
                if ref and "/" not in ref and "\\" not in ref:   # a tag, not a blob path
                    return ref
    except Exception:
        pass
    return name


def main():
    if len(sys.argv) < 2:
        print("usage: ollama-hud-run MODEL [PROMPT ...]", file=sys.stderr)
        return 2
    model = sys.argv[1]
    prompt = " ".join(sys.argv[2:])
    display_model = resolve_model(model)

    fd = open_fifo()
    push(fd, {"model": display_model, "q": prompt, "status": "thinking"})

    cmd = [OLLAMA_BIN, "run", model] + ([prompt] if prompt else [])
    captured = []
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1)
    except FileNotFoundError:
        print("ollama not found in PATH", file=sys.stderr)
        push(fd, {"model": display_model, "q": prompt, "a": "[ollama not found]", "status": "done"})
        if fd is not None:
            os.close(fd)
        return 127

    try:
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            captured.append(line)
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()

    answer = extract_answer("".join(captured)) or "[no answer]"
    push(fd, {"model": display_model, "q": prompt, "a": answer, "status": "done"})
    if fd is not None:
        time.sleep(0.05)
        os.close(fd)
    return proc.returncode or 0


if __name__ == "__main__":
    sys.exit(main())
