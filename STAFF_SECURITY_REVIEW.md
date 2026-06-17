# Staff Security Re-review

Review date: 2026-06-17

## Scope

This is a fresh staff-engineer review of the current repository state after the first round of hardening. The deployment assumption remains a hardened Raspberry Pi 3B that normally runs offline, with local hardware attached over SPI/I2C/GPIO and local AI updates sent through `/run/pihud/ai.fifo`.

## Top findings

### High: FIFO directory permissions accidentally broke the least-privilege writer path

**Affected file:** `install-pihud.sh`

The previous hardening changed `RuntimeDirectoryMode` to `0750` and the FIFO to `0620`, but the systemd runtime directory is created for the service user/group (`huddisp:huddisp`) before `ExecStartPre` runs. That means a normal user who is only in the `pihud` group can have write permission on the FIFO itself but still be unable to traverse `/run/pihud`. The practical result is a security-control regression that can break the intended least-privilege model and push operators toward broader permissions or running helpers with elevated privileges.

**Fix applied:** `ExecStartPre` now changes `/run/pihud` to group `pihud`, keeps it `0750`, recreates the FIFO as `0620`, and keeps the FIFO group as `pihud`. This preserves no world access while allowing only the intended `pihud` group to traverse the runtime directory and write the FIFO.

### Medium: Root-run installer still trusts the current repository directory

**Affected file:** `install-pihud.sh`

The installer copies Python files from the directory it is run from into `/opt/pihud` and `/usr/local/bin`. This is common for a small project, but from a staff-review perspective it is still a privileged trust boundary: if the checkout is writable by another local user, or if the operator runs the installer from an unverified removable drive, root will install those files as trusted service code.

**Recommendation:** Document that installation must be run from a root-owned or single-user checkout. For a stronger release workflow, publish signed release archives or checksums and have the installer verify expected file hashes before installing.

### Medium: Config values are trusted without type/range validation

**Affected file:** `pihud.py`

`/etc/pihud/pihud.toml` can override timing values, AI size limits, GPIO pins, I2C addresses, and FIFO path. The file is normally root-owned, so this is not a remote risk. However, bad values can cause display thrash, high CPU usage, hardware lockups, writes to unexpected FIFOs, or daemon crashes. On a hardware appliance, config validation is also a safety and maintainability control.

**Recommendation:** Validate config after loading: clamp positive timing intervals, enforce sane AI maximums, validate GPIO/I2C numeric ranges, require `ai_fifo` to stay under `/run/pihud` unless explicitly allowed, and reject invalid types with clear logs.

### Medium: Oversized FIFO records are bounded, but discard behavior can still block on malicious writers

**Affected file:** `pihud.py`

The FIFO reader now uses bounded `readline()`, which is an improvement. When an oversized unterminated record is detected, it calls `fifo.readline()` to discard the rest of the record. If a malicious local writer keeps the FIFO open and streams data without a newline, the discard read can still block the FIFO thread. This is local-only and constrained by the `pihud` group, but it is still a denial-of-service edge case.

**Recommendation:** Replace the text-file FIFO loop with `os.open`/`os.read` buffering, or repeatedly drain fixed-size chunks until newline/EOF/limit without issuing an unbounded `readline()`.

### Medium: Physical privacy controls are still missing

**Affected file:** `pihud.py`

The display still shows local identity (`user@host`), network information, process names, and recent AI prompt/answer text. On an offline Pi this is mainly a physical-security issue, but the e-ink display persists content even when power is removed.

**Recommendation:** Add a privacy mode that hides or times out AI prompts/answers, masks host/user/SSID details, and provides a HAT-key or signal-triggered screen clear before moving or powering down the device.

### Low: `PIHUD_OLLAMA_BIN` is intentionally flexible but should be documented as user-trusted input

**Affected file:** `ollama-hud-run.py`

Allowing `PIHUD_OLLAMA_BIN` is useful for controlled deployments. It is not a privilege escalation by itself because `ollama-hud-run` runs as the invoking user, but operators should understand that setting this variable changes what executable runs.

**Recommendation:** Document the variable in README or installer output, and suggest using an absolute path for production installs.

## Current risk posture

No critical issue was found in the current repository under the stated offline, hardened-device assumptions. The most important issue found in this re-review was the FIFO runtime-directory permission regression; it has been fixed in this branch. The remaining medium issues are primarily operational hardening, config validation, and physical privacy controls.
