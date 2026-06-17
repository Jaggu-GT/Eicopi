# Eicopi Security, Hardware, and License Review

Review date: 2026-06-17

## Scope and deployment assumptions

This review covers the repository files intended to run a Raspberry Pi 3B HUD with a Qwen/Ollama model, Waveshare 2.7 inch e-ink display, SSD1306 OLED, Coral environmental sensors, and an HDMI screen. The threat model assumes the Pi is security-hardened and normally offline, which lowers remote-attack likelihood but does not remove local, supply-chain, hardware-bus, removable-media, or misconfiguration risks.

The review used the OWASP Secure Coding Practices checklist as a baseline, especially input validation, output handling, least privilege/access control, error handling/logging, system configuration, file management, and general coding practices.

## Executive summary

The repository has a good security baseline for an offline appliance: the HUD daemon runs as a dedicated system user, the systemd unit applies multiple hardening controls, and the app avoids shell interpolation for its subprocess calls. The current implementation mitigates the original installer shell-wrapper, FIFO validation, e-ink AI refresh-rate, and Ollama binary-resolution findings. The main residual risks are operational and local-privilege risks caused by hardware-bus exposure and unpinned dependencies pulled from package repositories during installation.

No license violation was found in the checked-in files. The MIT license is compatible with the Waveshare MIT-derived code, and the repository already includes a third-party notice for the Waveshare-derived e-paper implementation. Recommended attribution additions are listed below for AI-assisted generation and documentation provenance.

## Critical findings

No critical-risk issue was identified under the stated offline, hardened Pi deployment model.

## High-risk findings

### H-1: Installer modifies a user's shell startup file to wrap `ollama`

**Affected file:** `install-pihud.sh`

The installer appends a shell function to the detected user's `.bashrc` that overrides `ollama run` and routes it through `ollama-hud-run`. This is convenient, but it is high impact because a privileged installer is modifying a human user's shell startup behavior. If the repository copy, installer invocation directory, or `/usr/local/bin/ollama-hud-run` is tampered with, future interactive shells silently change model execution behavior. In an offline device, this is primarily a local persistence and trust-boundary concern.

**OWASP mapping:** system configuration, file management, least privilege, and secure defaults.

**Status:** Mitigated. Shell wrapping is now opt-in via `PIHUD_INSTALL_SHELL_WRAPPER=1`; the default install leaves `.bashrc` unchanged and directs users to call `ollama-hud-run` explicitly.

### H-2: AI FIFO accepts arbitrary local JSON from any member of `pihud`

**Affected files:** `pihud.py`, `install-pihud.sh`, `ollama-hud-run.py`, `smoke-test-pihud.sh`

The service creates `/run/pihud/ai.fifo` with group write permissions for `pihud`, and the daemon trusts any JSON line written to the FIFO. This can be used by any local account in that group to display arbitrary content, force repeated e-ink refreshes, or feed very large strings that consume RAM/CPU while PIL measures and wraps text. The impact is local denial of service, display abuse, e-ink wear/ghosting, and possible disclosure of prompts/answers to anyone physically viewing the screen.

**OWASP mapping:** input validation, data validation from untrusted sources, resource management, and output handling.

**Status:** Mitigated. The FIFO is now group-write-only instead of group-readable, the runtime directory is not world-searchable, and the daemon validates JSON object shape, status values, total line size, per-field lengths, and AI repaint rate before updating the display.

### H-3: Installation performs live `apt-get update`/install without repository pinning or integrity policy

**Affected file:** `install-pihud.sh`

The installer updates package metadata and installs dependencies from configured APT sources. This is normal for Raspberry Pi setup, but it conflicts with the intended offline/hardened appliance model if run after hardening or from an untrusted network. A compromised mirror, weak repository policy, or unintended package version can change the runtime trust base.

**OWASP mapping:** system configuration, dependency management, secure deployment.

**Recommendation:** For the hardened offline Pi, build from a known-good image, snapshot APT repositories, pin package versions where practical, and record package versions in installation logs. Prefer running the installer only during a controlled provisioning phase, then disabling network and verifying installed package hashes where feasible.

### H-4: Hardware buses are exposed to the service account and shell user group

**Affected files:** `install-pihud.sh`, `pi_displays.py`, `pihud.py`, `smoke-test-pihud.sh`

The service account receives `spi`, `i2c`, and `gpio` access, and the human user is added to `pihud` for FIFO writes. Direct SPI/I2C/GPIO access is required for this project, but these interfaces can affect hardware state, interact with other devices on the same bus, and potentially induce device lockups. Malicious or buggy code running as the service account can drive GPIO pins, hold the e-ink BUSY wait path, or talk to unintended I2C devices.

**OWASP mapping:** least privilege, access control, system configuration.

**Recommendation:** Keep the service user dedicated and non-login, which the installer already does. Avoid adding broader users to `spi`, `i2c`, or `gpio` unless needed. Document the exact GPIO pins and I2C addresses reserved by Eicopi, and consider udev rules or device permissions that allow only the required device nodes. Add physical safeguards: correct voltage levels, resistor/current protection where applicable, stable power supply, and cable strain relief.

## Medium-risk findings

### M-1: FIFO message parsing lacks schema, length, and rate validation

**Affected file:** `pihud.py`

`_fifo_loop()` strips each FIFO line and passes it directly to `json.loads`; `_apply_ai()` converts fields to strings and stores them. This is simple and works, but it does not enforce maximum JSON line size, maximum prompt/answer/model lengths, known keys, or allowed value types. Very large local writes can increase memory use and rendering cost on a Pi 3B.

**Status:** Mitigated. The daemon now validates FIFO messages before applying them and truncates accepted model, question, and answer fields to bounded lengths.

### M-2: E-ink refresh abuse can reduce display life and usability

**Affected files:** `pihud.py`, `smoke-test-pihud.sh`

The code has an `eink_min_refresh_sec` throttle and full-refresh interval, which is good. However, local FIFO spam and frequently changing system metrics can still trigger many partial updates. E-paper displays are not intended for high-frequency updates, and excessive updates can cause ghosting, poor readability, and avoidable wear.

**Status:** Mitigated for AI-triggered updates. The daemon now coalesces AI repaint requests behind `ai_min_refresh_sec` while keeping the most recent accepted AI state. System metrics still use the existing displayed-field change detection and e-ink minimum refresh delay.

### M-3: Subprocess usage is mostly safe, but environment/PATH still matters

**Affected files:** `pihud.py`, `ollama-hud-run.py`, `install-pihud.sh`

The Python code correctly avoids `shell=True` for `iwgetid`, `nmcli`, and `ollama`. The service file's `ExecStartPre` uses `/bin/sh -c`, but with a constant command string rather than user input. Remaining risk is PATH/environment trust for the user-invoked `ollama-hud-run`, which launches `ollama` by name.

**Status:** Mitigated. `ollama-hud-run.py` now resolves the Ollama binary once using `PIHUD_OLLAMA_BIN`, then `shutil.which("ollama")`, and still invokes it without shell interpolation.

### M-4: Error handling hides useful detail and can mask hardware faults

**Affected files:** `pihud.py`, `pi_displays.py`, `smoke-test-pihud.sh`

Many hardware-read paths catch broad exceptions and return `None` or continue. That is good for daemon uptime, but it can hide repeated sensor, bus, or permission faults. In an offline hardware appliance, silent degradation can be a safety and maintenance issue.

**Recommendation:** Rate-limit but persist structured error counts for sensor reads, display errors, and FIFO parse failures. Consider rendering a small fault indicator on the display after repeated failures.

### M-5: Displayed local information may be sensitive in physical spaces

**Affected file:** `pihud.py`

The e-ink header displays `user@host`, SSID/IP-derived network status, top memory process, and recent AI prompt/answer. For a physically accessible device, this may leak usernames, hostnames, Wi-Fi SSIDs, local model queries, or operational details.

**Recommendation:** Add configuration flags to hide user/host, SSID/IP, top process, and AI content. Defaulting to less detail may be better for shared rooms, labs, or demos.

### M-6: Shutdown does not join worker threads or close I2C explicitly

**Affected file:** `pihud.py`

The daemon sets stop events and closes displays, but daemon threads are not joined and the I2C bus object is not explicitly closed. This is unlikely to be exploitable directly, but can make restart behavior less deterministic on constrained hardware.

**Recommendation:** Keep references to worker threads, join them briefly during shutdown, and close the I2C bus if the backend exposes `close()`.

### M-7: Systemd hardening is strong but should be verified on target OS

**Affected file:** `install-pihud.sh`

The unit uses many strong controls: `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, `DevicePolicy=closed`, `DeviceAllow`, address-family restrictions, and IP deny. Compatibility depends on systemd and Raspberry Pi OS versions. A hardened Pi should verify these controls actually apply.

**Recommendation:** After installation run `systemd-analyze security pihud.service` and record the output. Consider adding `CapabilityBoundingSet=` and `PrivateDevices=` if compatible with the explicit `DeviceAllow` rules. Keep `IPAddressDeny=any` unless future features require networking.

### M-8: The project lacks automated static checks and formatting guidance

**Affected files:** `pihud.py`, `pi_displays.py`, `ollama-hud-run.py`, `install-pihud.sh`, `smoke-test-pihud.sh`

The repo is small and readable, but there is no CI or local check script for syntax, linting, shell linting, or basic security checks.

**Recommendation:** Add a `make check` or `scripts/check.sh` that runs `python3 -m py_compile`, `shellcheck` if available, and optionally `ruff`/`bandit` in development environments.

## Low-risk / quality-of-life recommendations

- Add a sample `/etc/pihud/pihud.toml` to the repository instead of only generating it in the installer.
- Add a wiring table to the README for GPIO pins, SPI chip-selects, I2C addresses, and expected voltage levels.
- Add an offline deployment checklist: provision packages, verify display/sensors, disable network, run hardening checks, then smoke test.
- Add a troubleshooting section for e-ink BUSY stuck high, I2C device missing, OLED blank, and FIFO permission issues.
- Add a privacy mode that blanks AI prompt/answer after a timeout or on a HAT key press.
- Add an emergency safe-mode configuration that disables keys, AI FIFO rendering, or one display if hardware is unstable.
- Add a power-quality note for Pi 3B: undervoltage can cause unexplained bus/display faults.

## License and attribution review

### Current license state

- The repository root `LICENSE` is MIT.
- `THIRD_PARTY_LICENSES.md` includes an MIT notice for the Waveshare e-paper reference driver and identifies the derived e-paper command/timing/buffer logic in `pi_displays.py`.
- MIT-to-MIT reuse is compatible as long as the original copyright and permission notice are preserved in copies or substantial portions.

### License concerns found

No license violation was identified in the checked-in repository contents.

### Recommended attribution improvements

Because the project was AI-assisted and based on Waveshare official documentation/reference material, consider adding an `ATTRIBUTION.md` or expanding the README with:

1. **Waveshare credit:** State that e-paper command sequences and buffer orientation logic are derived from Waveshare's MIT-licensed reference driver, with the existing GitHub link and license notice.
2. **AI assistance disclosure:** State that portions of the code/documentation were generated or refactored with Anthropic Claude under the user's direction, reviewed by the project maintainer before use. This is not usually a license requirement, but it is transparent and useful provenance.
3. **Model/runtime credit:** If distributing a complete image or bundle that includes Qwen/Ollama/Coral libraries, include their exact licenses and model terms in third-party notices. This repository currently references those components but does not vendor them.

## Suggested priority plan

1. Add privacy controls for displayed AI content.
2. Add an offline deployment checklist and target-device hardening verification commands.
3. Add automated syntax/shell checks for development.
4. Add `ATTRIBUTION.md` for Waveshare documentation/reference code and Claude-assisted generation provenance.
5. Consider additional per-user mediation for FIFO writes if the device will have multiple interactive local users.
