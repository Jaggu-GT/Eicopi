#!/usr/bin/env python3
"""
pihud.py - dual-display heads-up daemon for a Raspberry Pi 3B+.

  OLED  (SSD1306 128x32, SPI0 CE0)  : live clock HH:MM:SS + date + sensor-driven
                                      day-phase icon, cyber-neon styling, 1 s tick,
                                      contrast auto-ridden from the light sensor.
  e-ink (Waveshare 2.7" V2, SPI0 CE1): four stacked, independently-triggered frames
        header  -> user@host | centred date+time (minute res) | SSID / DISCONNECTED
        sensor  -> temperature, humidity, pressure, light (+ day-phase word)
        system  -> CPU%, RAM%, SoC temp, top-RAM process (GPU/VRAM not exposed on VC4)
        ai      -> last `ollama run` question + post-"...done thinking." answer,
                   scrollable with the HAT keys.

Refresh policy: e-ink repaints the moment any displayed value changes or an AI
message arrives (no-flash full-frame partial update); a full flushing refresh runs
every EINK_FULL_REFRESH_SEC to clear ghosting.

Pixel convention everywhere: PIL '1', background 255, foreground 0.
Tunables live in DEFAULTS and may be overridden by /etc/pihud/pihud.toml.
"""
import json
import math
import os
import re
import signal
import threading
import time
import unicodedata

import psutil
from PIL import Image, ImageDraw, ImageFont

from pi_displays import OLED128x32, EPD2in7V2

# --------------------------------------------------------------------------- #
# Configuration                                                               #
# --------------------------------------------------------------------------- #
DEFAULTS = {
    # I2C
    "i2c_bus": 1,
    "hdc2010_addr": 0x40,
    "opt3002_addr": 0x44,
    # light -> 0..100 log mapping (CALIBRATE: cover sensor / room / phone torch)
    "light_min_nwcm2": 10.0,
    "light_max_nwcm2": 300000.0,
    "light_ema_alpha": 0.2,
    # OLED contrast band
    "contrast_min": 15,
    "contrast_max": 255,
    # cadence
    "poll_sec": 2.0,
    "eink_min_refresh_sec": 1.5,
    "eink_full_refresh_sec": 600,
    "eink_busy_timeout_sec": 30,
    # AI
    "ai_fifo": "/run/pihud/ai.fifo",
    "ai_answer_lines": 3,
    "ai_max_line_bytes": 8192,
    "ai_max_drop_bytes": 1048576,
    "ai_max_model_chars": 64,
    "ai_max_question_chars": 512,
    "ai_max_answer_chars": 2048,
    "ai_min_refresh_sec": 2.0,
    "scroll_min_refresh_sec": 0.5,
    # HAT keys (BCM); set enable_keys False if not wired on your bench setup
    "enable_keys": True,
    "key_scroll_up": 5,      # KEY1
    "key_scroll_down": 6,    # KEY2
    "key_full_refresh": 19,  # KEY4
    # e-ink pins (BCM)
    "epd_dc": 23, "epd_rst": 17, "epd_busy": 22, "epd_ce": 1,
    # OLED pins (BCM)
    "oled_dc": 24, "oled_rst": 25, "oled_ce": 0,
}


def load_config():
    cfg = dict(DEFAULTS)
    path = "/etc/pihud/pihud.toml"
    try:
        import tomllib
        with open(path, "rb") as f:
            cfg.update(tomllib.load(f))
    except FileNotFoundError:
        pass
    except Exception as e:
        log("config %s ignored: %s" % (path, e))
    return cfg


def log(msg):
    print("[pihud] %s" % msg, flush=True)


# --------------------------------------------------------------------------- #
# Fonts                                                                       #
# --------------------------------------------------------------------------- #
_FDIR = "/usr/share/fonts/truetype/dejavu"


def _font(name, size):
    try:
        return ImageFont.truetype(os.path.join(_FDIR, name), size)
    except Exception:
        return ImageFont.load_default()


def load_fonts():
    return {
        "oled_time": _font("DejaVuSansMono-Bold.ttf", 18),
        "oled_date": _font("DejaVuSansMono.ttf", 11),
        "head": _font("DejaVuSans-Bold.ttf", 9),
        "data": _font("DejaVuSans.ttf", 11),
        "hdr": _font("DejaVuSans.ttf", 10),
        "dt": _font("DejaVuSansMono-Bold.ttf", 12),
    }


# --------------------------------------------------------------------------- #
# Sensor reads (all defensive: never raise, return None on failure)           #
# --------------------------------------------------------------------------- #
def open_i2c(bus_no):
    try:
        import smbus2 as smbus
    except Exception:
        import smbus
    return smbus.SMBus(bus_no)


def _swap16(v):
    return ((v & 0xFF) << 8) | (v >> 8)


def configure_opt3002(bus, addr):
    # continuous conversion, automatic full-scale range, 800 ms integration
    try:
        bus.write_word_data(addr, 0x01, _swap16(0xCE10))
    except Exception as e:
        log("OPT3002 config failed: %s" % e)


def read_hdc2010(bus, addr):
    try:
        bus.write_byte_data(addr, 0x0F, 0x01)   # trigger measurement
        time.sleep(0.02)
        t = (bus.read_byte_data(addr, 0x01) << 8) | bus.read_byte_data(addr, 0x00)
        h = (bus.read_byte_data(addr, 0x03) << 8) | bus.read_byte_data(addr, 0x02)
        return (t / 65536.0) * 165.0 - 40.0, (h / 65536.0) * 100.0
    except Exception:
        return None, None


def read_opt3002(bus, addr):
    try:
        raw = _swap16(bus.read_word_data(addr, 0x00))
        e = (raw >> 12) & 0x0F
        r = raw & 0x0FFF
        return 1.2 * (2 ** e) * r            # nW/cm^2  (NOT lux)
    except Exception:
        return None


def read_bmp280_iio():
    import glob
    try:
        for dev in glob.glob("/sys/bus/iio/devices/iio:device*"):
            try:
                name = open(os.path.join(dev, "name")).read().strip()
            except Exception:
                continue
            if "bmp280" in name or "bme280" in name:
                kpa = float(open(os.path.join(dev, "in_pressure_input")).read().strip())
                return kpa * 10.0            # kPa -> hPa
    except Exception:
        pass
    return None


# --------------------------------------------------------------------------- #
# System / network                                                            #
# --------------------------------------------------------------------------- #
def read_soc_temp():
    try:
        return int(open("/sys/class/thermal/thermal_zone0/temp").read().strip()) / 1000.0
    except Exception:
        return None


def read_top_proc():
    best, best_rss = None, 0
    try:
        for p in psutil.process_iter(["name", "memory_info"]):
            try:
                rss = p.info["memory_info"].rss
            except Exception:
                continue
            if rss > best_rss:
                best_rss, best = rss, p.info["name"]
    except Exception:
        pass
    return best, best_rss


def read_ssid(iface):
    import subprocess
    for cmd in (["iwgetid", "-r"], ["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
            if out.returncode == 0 and out.stdout.strip():
                if cmd[0] == "iwgetid":
                    return out.stdout.strip()
                for line in out.stdout.splitlines():
                    if line.startswith("yes:"):
                        return line.split(":", 1)[1] or None
        except Exception:
            continue
    return None


def read_net():
    up, ip, ssid, iface = False, None, None, None
    try:
        stats = psutil.net_if_stats()
        addrs = psutil.net_if_addrs()
        order = (["wlan0"] if "wlan0" in stats else []) + \
                [i for i in stats if i != "lo" and i != "wlan0"]
        for ifc in order:
            st = stats.get(ifc)
            if not st or not st.isup:
                continue
            for a in addrs.get(ifc, []):
                if a.family.name == "AF_INET" and not a.address.startswith("127."):
                    up, ip, iface = True, a.address, ifc
                    break
            if ip:
                break
        if iface:
            ssid = read_ssid(iface)
    except Exception:
        pass
    return up, ip, ssid


# --------------------------------------------------------------------------- #
# Light helpers                                                               #
# --------------------------------------------------------------------------- #
def light_to_pct(nw, lo, hi):
    if not nw or nw <= 0:
        return None
    nw = max(lo, min(hi, nw))
    return 100.0 * (math.log10(nw) - math.log10(lo)) / (math.log10(hi) - math.log10(lo))


def pct_to_phase(pct):
    if pct is None:
        return "----"
    if pct < 33:
        return "NIGHT"
    if pct < 50:
        return "DUSK"
    if pct < 67:
        return "DAWN"
    return "DAY"


def pct_to_contrast(pct, lo, hi):
    if pct is None:
        pct = 50.0
    return int(lo + (hi - lo) * pct / 100.0)


def fmt_light(nw):
    if nw is None:
        return "--"
    if nw >= 1e6:
        return "%.1fM" % (nw / 1e6)
    if nw >= 1e3:
        return "%.1fk" % (nw / 1e3)
    return "%.0f" % nw


# --------------------------------------------------------------------------- #
# Drawing helpers                                                             #
# --------------------------------------------------------------------------- #
# Matches CSI (incl. cursor codes like ESC[?25l / ESC[?25h), OSC, and other
# single-char escape sequences. Used to scrub anything that slips past the
# sender so it can never render as garbage on the panel (defense in depth).
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")


def clean_text(value):
    text = "" if value is None else str(value)
    text = _ANSI.sub("", text)
    out = []
    for ch in text:
        if ch == "\t":
            out.append(" ")
        elif ch < " " or ch == "\x7f" or unicodedata.category(ch) == "Cf":
            out.append(" ")          # C0 controls, DEL, zero-width / bidi format chars
        else:
            out.append(ch)
    return "".join(out)


def fit(draw, text, font, maxw):
    text = clean_text(text)
    if draw.textlength(text, font=font) <= maxw:
        return text
    while text and draw.textlength(text + "...", font=font) > maxw:
        text = text[:-1]
    return text + "..."


def wrap(draw, text, font, maxw):
    out, line = [], ""
    for word in clean_text(text).split():
        trial = (line + " " + word).strip()
        if draw.textlength(trial, font=font) <= maxw:
            line = trial
        else:
            if line:
                out.append(line)
            line = word if draw.textlength(word, font=font) <= maxw else fit(draw, word, font, maxw)
    if line:
        out.append(line)
    return out or [""]


def draw_wifi(draw, cx, cy, connected):
    # three stacked arcs + base dot; a slash if disconnected
    for i, rr in enumerate((9, 6, 3)):
        draw.arc([cx - rr, cy - rr, cx + rr, cy + rr], 225, 315, fill=0)
        _ = i
    draw.ellipse([cx - 1, cy - 1, cx + 1, cy + 1], fill=0)
    if not connected:
        draw.line([cx - 8, cy - 9, cx + 8, cy + 5], fill=0, width=2)


def draw_phase_icon(draw, cx, cy, phase, r=6):
    if phase == "NIGHT":
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=0)
        draw.ellipse([cx - r + 3, cy - r - 2, cx + r + 3, cy + r - 2], fill=255)
    elif phase == "DAY":
        draw.ellipse([cx - r + 2, cy - r + 2, cx + r - 2, cy + r - 2], fill=0)
        for ang in range(0, 360, 45):
            a = math.radians(ang)
            draw.line([cx + (r - 1) * math.cos(a), cy + (r - 1) * math.sin(a),
                       cx + (r + 2) * math.cos(a), cy + (r + 2) * math.sin(a)], fill=0)
    else:  # DUSK / DAWN -> low sun on the horizon
        draw.pieslice([cx - r, cy - r + 2, cx + r, cy + r + 2], 180, 360, fill=0)
        draw.line([cx - r - 2, cy + 2, cx + r + 2, cy + 2], fill=0)


# --------------------------------------------------------------------------- #
# Renderers                                                                   #
# --------------------------------------------------------------------------- #
def render_oled(fonts, phase, blink):
    img = Image.new("1", (OLED128x32.WIDTH, OLED128x32.HEIGHT), 255)
    d = ImageDraw.Draw(img)
    now = time.localtime()
    t = time.strftime("%H:%M:%S", now)
    date = time.strftime("%Y-%m-%d", now)
    tw = d.textlength(t, font=fonts["oled_time"])
    d.text(((OLED128x32.WIDTH - tw) / 2, -1), t, font=fonts["oled_time"], fill=0)
    d.text((2, 20), date, font=fonts["oled_date"], fill=0)
    draw_phase_icon(d, 118, 25, phase, r=5)
    if blink:
        d.rectangle([124, 0, 126, 2], fill=0)
    return img


def render_eink(fonts, st):
    W, H = 264, 176
    img = Image.new("1", (W, H), 255)
    d = ImageDraw.Draw(img)

    # ---- header --------------------------------------------------------
    d.text((3, 1), fit(d, st["userhost"], fonts["hdr"], 150), font=fonts["hdr"], fill=0)
    if st["net_up"]:
        draw_wifi(d, 254, 8, True)
        ssid = fit(d, st["ssid"] or st["ip"] or "wifi", fonts["hdr"], 78)
        sw = d.textlength(ssid, font=fonts["hdr"])
        d.text((242 - sw, 1), ssid, font=fonts["hdr"], fill=0)
    else:
        draw_wifi(d, 254, 8, False)
        msg = "DISCONNECTED"
        sw = d.textlength(msg, font=fonts["hdr"])
        d.text((242 - sw, 1), msg, font=fonts["hdr"], fill=0)
    dt = "%s  %s" % (st["date"], st["hhmm"])
    dw = d.textlength(dt, font=fonts["dt"])
    d.text(((W - dw) / 2, 15), dt, font=fonts["dt"], fill=0)
    d.line([0, 30, W - 1, 30], fill=0)

    # ---- sensor (2-column grid, divider at x=132, auto-shrink via fit) --
    LX, LW, RX, RW = 3, 125, 138, 121
    d.text((3, 32), "Sensor Readings", font=fonts["head"], fill=0)
    temp = "--" if st["temp"] is None else "%.1f" % st["temp"]
    hum = "--" if st["hum"] is None else "%.0f" % st["hum"]
    press = "--" if st["press"] is None else "%.0f" % st["press"]
    d.text((LX, 44), fit(d, "Temperature %s \u00b0C" % temp, fonts["data"], LW), font=fonts["data"], fill=0)
    d.text((RX, 44), fit(d, "Humidity %s %%" % hum, fonts["data"], RW), font=fonts["data"], fill=0)
    d.text((LX, 56), fit(d, "Pressure %s hPa" % press, fonts["data"], LW), font=fonts["data"], fill=0)
    d.text((RX, 56), fit(d, "Light %s  %s" % (fmt_light(st["light"]), st["phase"]), fonts["data"], RW),
           font=fonts["data"], fill=0)
    d.line([132, 43, 132, 70], fill=0)
    d.line([0, 72, W - 1, 72], fill=0)

    # ---- system (2-column grid; GPU/VRAM not exposed on VC4) ------------
    d.text((3, 74), "System", font=fonts["head"], fill=0)
    nav = "GPU/VRAM n/a"
    d.text((W - 3 - d.textlength(nav, font=fonts["head"]), 75), nav, font=fonts["head"], fill=0)
    cpu = "--" if st["cpu"] is None else "%.0f" % st["cpu"]
    ram = "--" if st["ram"] is None else "%.0f" % st["ram"]
    soc = "--" if st["soc"] is None else "%.1f" % st["soc"]
    top = st["top_name"] or "--"
    topm = "" if not st["top_rss"] else " %dM" % (st["top_rss"] // (1024 * 1024))
    d.text((LX, 86), fit(d, "CPU %s%%" % cpu, fonts["data"], LW), font=fonts["data"], fill=0)
    d.text((RX, 86), fit(d, "RAM %s%%" % ram, fonts["data"], RW), font=fonts["data"], fill=0)
    d.text((LX, 98), fit(d, "SoC %s \u00b0C" % soc, fonts["data"], LW), font=fonts["data"], fill=0)
    d.text((RX, 98), fit(d, "TOP %s%s" % (top, topm), fonts["data"], RW), font=fonts["data"], fill=0)
    d.line([132, 85, 132, 112], fill=0)
    d.line([0, 114, W - 1, 114], fill=0)

    # ---- ai ------------------------------------------------------------
    d.text((3, 116), "AI \u00b7 %s" % (st["model"] or "(idle)"), font=fonts["head"], fill=0)
    d.text((3, 130), fit(d, "Q  %s" % (st["question"] or "-"), fonts["data"], W - 6),
           font=fonts["data"], fill=0)
    ay = 143
    if st["status"] == "thinking":
        d.text((3, ay), "A  ...thinking", font=fonts["data"], fill=0)
    else:
        lines = wrap(d, st["answer"] or "-", fonts["data"], W - 16)
        n = st["ans_lines"]
        scroll = max(0, min(st["scroll"], max(0, len(lines) - n)))
        view = lines[scroll:scroll + n]
        d.text((3, ay), "A", font=fonts["data"], fill=0)
        for i, ln in enumerate(view):
            d.text((16, ay + i * 11), ln, font=fonts["data"], fill=0)
        if scroll > 0:
            d.polygon([(256, 132), (262, 132), (259, 128)], fill=0)
        if scroll + n < len(lines):
            d.polygon([(256, 170), (262, 170), (259, 174)], fill=0)
    return img


# --------------------------------------------------------------------------- #
# HUD daemon                                                                  #
# --------------------------------------------------------------------------- #
class HUD:
    def __init__(self, cfg):
        self.cfg = cfg
        self.fonts = load_fonts()
        self._stop = threading.Event()
        self._dirty = threading.Event()
        self._force_full = False
        self._state_lock = threading.Lock()
        self._spi_lock = threading.Lock()
        self._last_minute = ""
        self._last_sig = None
        self._last_ai_dirty = 0.0
        self._ai_dirty_timer = None
        try:
            self._user = os.environ.get("SUDO_USER") or os.getlogin()
        except Exception:
            self._user = os.environ.get("USER", "user")
        self._host = os.uname().nodename

        self.state = {
            "userhost": "%s@%s" % (self._user, self._host),
            "date": time.strftime("%Y-%m-%d"), "hhmm": time.strftime("%H:%M"),
            "net_up": False, "ip": None, "ssid": None,
            "temp": None, "hum": None, "press": None, "light": None,
            "phase": "----", "contrast": (cfg["contrast_min"] + cfg["contrast_max"]) // 2,
            "cpu": None, "ram": None, "soc": None, "top_name": None, "top_rss": 0,
            "model": None, "question": None, "answer": None, "status": "idle",
            "scroll": 0, "ans_lines": cfg["ai_answer_lines"],
        }

        self.oled = OLED128x32(dc=cfg["oled_dc"], rst=cfg["oled_rst"], spi_dev=cfg["oled_ce"])
        self.epd = EPD2in7V2(dc=cfg["epd_dc"], rst=cfg["epd_rst"], busy=cfg["epd_busy"],
                             spi_dev=cfg["epd_ce"])
        self.bus = open_i2c(cfg["i2c_bus"])
        configure_opt3002(self.bus, cfg["opt3002_addr"])
        self._light_ema = None
        self._buttons = []

    # -- helpers --
    def _snapshot(self):
        with self._state_lock:
            return dict(self.state)

    def _sleep(self, sec):
        self._stop.wait(sec)

    # -- sensor / system poll --
    def _read_all(self):
        c = self.cfg
        temp, hum = read_hdc2010(self.bus, c["hdc2010_addr"])
        light = read_opt3002(self.bus, c["opt3002_addr"])
        press = read_bmp280_iio()
        if light is not None:
            a = c["light_ema_alpha"]
            self._light_ema = light if self._light_ema is None else (a * light + (1 - a) * self._light_ema)
        pct = light_to_pct(self._light_ema, c["light_min_nwcm2"], c["light_max_nwcm2"])
        phase = pct_to_phase(pct)
        contrast = pct_to_contrast(pct, c["contrast_min"], c["contrast_max"])
        cpu = psutil.cpu_percent(interval=None)
        ram = psutil.virtual_memory().percent
        soc = read_soc_temp()
        top_name, top_rss = read_top_proc()
        up, ip, ssid = read_net()

        with self._state_lock:
            self.state.update(temp=temp, hum=hum, press=press, light=self._light_ema,
                              phase=phase, contrast=contrast, cpu=cpu, ram=ram, soc=soc,
                              top_name=top_name, top_rss=top_rss, net_up=up, ip=ip, ssid=ssid)
            st = dict(self.state)
        # only repaint e-ink when a *displayed* field changes (rounded)
        sig = (st["net_up"], st["ssid"], st["ip"],
               None if st["temp"] is None else round(st["temp"], 1),
               None if st["hum"] is None else round(st["hum"]),
               None if st["press"] is None else round(st["press"]),
               st["phase"], fmt_light(st["light"]),
               None if st["cpu"] is None else round(st["cpu"]),
               None if st["ram"] is None else round(st["ram"]),
               None if st["soc"] is None else round(st["soc"], 1),
               st["top_name"])
        if sig != self._last_sig:
            self._last_sig = sig
            self._dirty.set()

    def _poll_loop(self):
        psutil.cpu_percent(interval=None)   # prime
        self._sleep(0.5)
        while not self._stop.is_set():
            try:
                self._read_all()
            except Exception as e:
                log("poll error: %s" % e)
            self._sleep(self.cfg["poll_sec"])

    # -- oled --
    def _oled_loop(self):
        blink = False
        while not self._stop.is_set():
            blink = not blink
            phase = self._snapshot()["phase"]
            contrast = self._snapshot()["contrast"]
            try:
                img = render_oled(self.fonts, phase, blink)
                with self._spi_lock:
                    self.oled.set_contrast(contrast)
                    self.oled.display(img)
            except Exception as e:
                log("oled error: %s" % e)
            m = time.strftime("%H:%M")
            if m != self._last_minute:
                self._last_minute = m
                with self._state_lock:
                    self.state["hhmm"] = m
                    self.state["date"] = time.strftime("%Y-%m-%d")
                self._dirty.set()
            self._sleep(1.0)

    # -- e-ink --
    def _push_eink(self, full):
        st = self._snapshot()
        img = render_eink(self.fonts, st)
        buf = self.epd.getbuffer(img)
        with self._spi_lock:                 # hold the bus only for the byte transfer + trigger
            if full:
                self.epd.send_base(buf)
            else:
                self.epd.send_quick(buf)
        # BUSY is a GPIO poll with no SPI traffic, so wait with the lock released:
        # the OLED clock keeps updating during the multi-second e-ink refresh, and a
        # stuck panel can no longer freeze it (bounded by eink_busy_timeout_sec anyway).
        self.epd.wait_idle(self.cfg["eink_busy_timeout_sec"])

    def _eink_loop(self):
        try:
            with self._spi_lock:
                self.epd.init()
            self._push_eink(full=True)
        except Exception as e:
            log("e-ink init error: %s" % e)
        last_full = time.time()
        while not self._stop.is_set():
            self._dirty.wait(timeout=self.cfg["eink_full_refresh_sec"])
            if self._stop.is_set():
                break
            self._dirty.clear()
            full = self._force_full or (time.time() - last_full >= self.cfg["eink_full_refresh_sec"])
            self._force_full = False
            try:
                self._push_eink(full=full)
                if full:
                    last_full = time.time()
            except Exception as e:
                log("e-ink refresh error: %s" % e)
            self._sleep(self.cfg["eink_min_refresh_sec"])

    # -- ai fifo --
    def _truncate(self, value, max_chars):
        text = "" if value is None else str(value)
        return text[:max_chars]

    def _validate_ai(self, line):
        max_bytes = int(self.cfg["ai_max_line_bytes"])
        if len(line.encode("utf-8", "replace")) > max_bytes:
            raise ValueError("message too large")
        rec = json.loads(line)
        if not isinstance(rec, dict):
            raise ValueError("message must be a JSON object")
        status = rec.get("status")
        if status not in ("thinking", "done", "scroll"):
            raise ValueError("unsupported status")
        if status == "scroll":
            direction = rec.get("dir")
            if direction not in ("up", "down"):
                raise ValueError("unsupported scroll direction")
            return {"status": status, "dir": direction}
        return {
            "model": self._truncate(rec.get("model"), int(self.cfg["ai_max_model_chars"])),
            "q": self._truncate(rec.get("q"), int(self.cfg["ai_max_question_chars"])),
            "a": self._truncate(rec.get("a"), int(self.cfg["ai_max_answer_chars"])),
            "status": status,
        }

    def _mark_ai_dirty(self):
        now = time.monotonic()
        wait = max(0.0, float(self.cfg["ai_min_refresh_sec"]) - (now - self._last_ai_dirty))
        if wait <= 0.0:
            self._last_ai_dirty = now
            self._dirty.set()
            return
        if self._ai_dirty_timer and self._ai_dirty_timer.is_alive():
            return

        def delayed():
            if not self._stop.wait(wait):
                self._last_ai_dirty = time.monotonic()
                self._dirty.set()

        self._ai_dirty_timer = threading.Thread(target=delayed, daemon=True)
        self._ai_dirty_timer.start()

    def _apply_ai(self, rec):
        if rec["status"] == "scroll":
            self._scroll(-1 if rec["dir"] == "up" else 1)
            return
        with self._state_lock:
            if rec["model"]:
                self.state["model"] = rec["model"]
            if rec["status"] == "thinking":
                self.state.update(question=rec["q"], answer="", status="thinking", scroll=0)
            elif rec["status"] == "done":
                self.state.update(answer=rec["a"], status="done", scroll=0)
                if rec["q"]:
                    self.state["question"] = rec["q"]
        self._mark_ai_dirty()

    def _fifo_loop(self):
        path = self.cfg["ai_fifo"]
        while not self._stop.is_set():
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fifo:        # blocks until a writer opens
                    max_line = int(self.cfg["ai_max_line_bytes"]) + 1
                    while not self._stop.is_set():
                        line = fifo.readline(max_line + 1)
                        if line == "":
                            break
                        if len(line) > max_line and not line.endswith("\n"):
                            # Drop the rest of this oversized record in bounded chunks.
                            # Never an unbounded readline -> a newline-less flood from a
                            # pihud-group writer cannot exhaust memory.
                            drop_cap = int(self.cfg["ai_max_drop_bytes"])
                            dropped = 0
                            while dropped < drop_cap:
                                chunk = fifo.readline(4096)
                                if chunk == "" or chunk.endswith("\n"):
                                    break
                                dropped += len(chunk)
                            log("bad ai message: message too large")
                            continue
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            self._apply_ai(self._validate_ai(line))
                        except Exception as e:
                            log("bad ai message: %s" % e)
            except FileNotFoundError:
                self._sleep(1.0)
            except Exception as e:
                log("fifo error: %s" % e)
                self._sleep(1.0)

    # -- keys --
    def _scroll(self, delta):
        with self._state_lock:
            self.state["scroll"] = max(0, self.state["scroll"] + delta)
        self._mark_scroll_dirty()

    def _mark_scroll_dirty(self):
        now = time.monotonic()
        if now - getattr(self, "_last_scroll_dirty", 0.0) >= float(self.cfg["scroll_min_refresh_sec"]):
            self._last_scroll_dirty = now
            self._dirty.set()

    def _force_refresh(self):
        self._force_full = True
        self._dirty.set()

    def _setup_keys(self):
        if not self.cfg["enable_keys"]:
            return
        try:
            from gpiozero import Button
            up = Button(self.cfg["key_scroll_up"], pull_up=True, bounce_time=0.05)
            dn = Button(self.cfg["key_scroll_down"], pull_up=True, bounce_time=0.05)
            fr = Button(self.cfg["key_full_refresh"], pull_up=True, bounce_time=0.05)
            up.when_pressed = lambda: self._scroll(-1)
            dn.when_pressed = lambda: self._scroll(1)
            fr.when_pressed = self._force_refresh
            self._buttons = [up, dn, fr]
            log("HAT keys active (KEY1 up / KEY2 down / KEY4 full-refresh)")
        except Exception as e:
            log("HAT keys unavailable, scrolling disabled: %s" % e)

    # -- lifecycle --
    def run(self):
        signal.signal(signal.SIGTERM, lambda *_: self._stop.set())
        signal.signal(signal.SIGINT, lambda *_: self._stop.set())
        signal.signal(signal.SIGUSR1, lambda *_: self._force_refresh())
        try:
            with self._spi_lock:
                self.oled.init()
        except Exception as e:
            log("oled init error: %s" % e)
        self._setup_keys()

        threads = [threading.Thread(target=t, daemon=True)
                   for t in (self._poll_loop, self._oled_loop, self._eink_loop, self._fifo_loop)]
        for t in threads:
            t.start()
        log("running (pid %d)" % os.getpid())
        try:
            while not self._stop.is_set():
                self._stop.wait(1.0)
        finally:
            self.shutdown()

    def shutdown(self):
        self._stop.set()
        self._dirty.set()
        time.sleep(0.3)
        try:
            with self._spi_lock:
                self.oled.close()
        except Exception:
            pass
        try:
            with self._spi_lock:
                self.epd.close()
        except Exception:
            pass
        log("stopped")


def main():
    HUD(load_config()).run()


if __name__ == "__main__":
    main()
