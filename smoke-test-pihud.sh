#!/bin/bash
# smoke-test-pihud.sh - staged bring-up checks for the dual-display HUD.
# Each stage is isolated so a failure points at one layer (deps / bus / panel / sensor).
# Panel + sensor stages need SPI/I2C/GPIO access -> run with sudo (or as a user in spi,i2c,gpio).
#
#   sudo ./smoke-test-pihud.sh
#
# Note: stop the service first if it is running, so it does not fight for the SPI bus:
#   sudo systemctl stop pihud   # (start it again afterwards)

export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
PYDIR=/opt/pihud
[[ -f "$PYDIR/pi_displays.py" ]] || PYDIR="$(cd "$(dirname "$0")" && pwd)"
export PYDIR

ok()   { echo -e "[\033[1;32m OK \033[0m] $*"; }
no()   { echo -e "[\033[1;31mFAIL\033[0m] $*"; }
dots() { echo -e "[ .. ] $*"; }

run_py() { cd "$PYDIR" && python3 - "$@"; }

echo "=== pihud smoke test (using $PYDIR) ==="

# 1 - Python deps + local modules
dots "checking python deps + drivers"
if run_py <<'PY'
import importlib, sys
for m in ("PIL", "psutil", "spidev", "gpiozero", "pi_displays"):
    importlib.import_module(m)
print("deps ok")
PY
then ok "deps present"; else no "deps/import - re-run install-pihud.sh"; fi

# 2 - device nodes
dots "checking device nodes"
miss=0
for n in /dev/spidev0.0 /dev/spidev0.1 /dev/i2c-1 /dev/gpiochip0; do
    if [[ -e "$n" ]]; then ok "$n present"; else no "$n MISSING"; miss=1; fi
done
[[ $miss -eq 1 ]] && echo "      (enable SPI/I2C in config.txt + reboot)"

# 3 - I2C scan (non-fatal)
dots "i2c scan (expect 0x40 HDC2010, 0x44 OPT3002, 0x76 BMP280)"
if command -v i2cdetect >/dev/null; then i2cdetect -y 1 || true; else echo "      i2cdetect not installed"; fi

# 4 - OLED
dots "OLED: init + draw test pattern (~2s)"
if run_py <<'PY'
import time
from PIL import Image, ImageDraw, ImageFont
from pi_displays import OLED128x32
o = OLED128x32(); o.init()
img = Image.new("1", (128, 32), 255); d = ImageDraw.Draw(img)
try: f = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", 14)
except Exception: f = ImageFont.load_default()
d.text((6, 0), "OLED OK", font=f, fill=0); d.text((6, 17), "pihud", font=f, fill=0)
o.set_contrast(200); o.display(img); time.sleep(2); o.clear(); o.close()
print("oled ok")
PY
then ok "OLED drew a frame"; else no "OLED - check CE0/DC=24/RST=25 wiring"; fi

# 5 - e-ink
dots "e-ink: init + clear + draw a frame (full refresh, ~6s)"
if run_py <<'PY'
from PIL import Image, ImageDraw, ImageFont
from pi_displays import EPD2in7V2
e = EPD2in7V2(); e.init(); e.clear()
img = Image.new("1", (264, 176), 255); d = ImageDraw.Draw(img)
try: f = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 16)
except Exception: f = ImageFont.load_default()
d.rectangle([0, 0, 263, 175], outline=0)
d.text((10, 70), "e-ink OK  264x176", font=f, fill=0)
e.display_base(e.getbuffer(img)); e.sleep(); e.close()
print("eink ok")
PY
then ok "e-ink drew a frame"; else no "e-ink - check CE1/DC=23/RST=17/BUSY=22 + BS=0"; fi

# 6 - sensors
dots "sensors: one read of HDC2010 / OPT3002 / BMP280"
run_py <<'PY' || no "sensor read raised"
from pihud import open_i2c, configure_opt3002, read_hdc2010, read_opt3002, read_bmp280_iio, fmt_light, DEFAULTS
b = open_i2c(DEFAULTS["i2c_bus"]); configure_opt3002(b, DEFAULTS["opt3002_addr"])
import time; time.sleep(0.9)
t, h = read_hdc2010(b, DEFAULTS["hdc2010_addr"])
print("  temp     :", "--" if t is None else "%.1f C" % t)
print("  humidity :", "--" if h is None else "%.0f %%" % h)
print("  light    :", fmt_light(read_opt3002(b, DEFAULTS["opt3002_addr"])), "nW/cm^2")
p = read_bmp280_iio(); print("  pressure :", "--" if p is None else "%.0f hPa" % p)
PY
ok "sensor stage ran (verify the values above are sane)"

# 7 - AI FIFO
dots "AI FIFO push test"
FIFO=/run/pihud/ai.fifo
if [[ -p "$FIFO" ]]; then
    if printf '%s\n' '{"model":"smoke-test","q":"smoke test question","status":"thinking"}' > "$FIFO" 2>/dev/null \
       && sleep 1 \
       && printf '%s\n' '{"model":"smoke-test","q":"smoke test question","a":"smoke test answer - if you can read this on the e-ink, the FIFO path works end to end.","status":"done"}' > "$FIFO" 2>/dev/null \
       && sleep 1 \
       && printf '%s\n' '{"status":"scroll","dir":"down"}' > "$FIFO" 2>/dev/null; then
        ok "pushed Q/A to FIFO - check the e-ink AI frame"
    else
        no "could not write FIFO (are you in the 'pihud' group? try: newgrp pihud)"
    fi
else
    echo "      $FIFO not present - start the service first: sudo systemctl start pihud"
fi

echo "=== done ==="
