"""
pi_displays.py - minimal, self-contained drivers for the Pi 3B+ dual-display HUD.

  * SSD1306 128x32 OLED          SPI0 CE0, DC=GPIO24, RST=GPIO25
  * Waveshare 2.7" e-Paper (V2)  SPI0 CE1, DC=GPIO23, RST=GPIO17, BUSY=GPIO22

The e-paper command / timing / RAM-window / waveform sequences and the getbuffer
rotation are transcribed from Waveshare's MIT-licensed reference driver
`epd2in7_V2.py` (github.com/waveshareteam/e-Paper). Kept deliberately small so the
entire hardware-trust surface is auditable in one file.

Pixel convention (BOTH panels): PIL mode '1', background = 255, foreground = 0.
  e-paper : 0 -> black ink on white paper.
  OLED    : 0 -> lit pixel (green) on a black panel.

Chip select is driven by the SPI peripheral (spidev opens CE0 / CE1); only
DC / RST / BUSY are GPIO. Pin numbers are BCM.
"""
import time

import spidev
from gpiozero import DigitalOutputDevice, DigitalInputDevice


# --------------------------------------------------------------------------- #
# SSD1306 128x32 OLED                                                          #
# --------------------------------------------------------------------------- #
class OLED128x32:
    WIDTH = 128
    HEIGHT = 32

    def __init__(self, dc=24, rst=25, spi_bus=0, spi_dev=0, speed_hz=4_000_000):
        self.dc = DigitalOutputDevice(dc, initial_value=False)
        self.rst = DigitalOutputDevice(rst, initial_value=True)
        self.spi = spidev.SpiDev()
        self.spi.open(spi_bus, spi_dev)
        self.spi.max_speed_hz = speed_hz
        self.spi.mode = 0
        self._pages = self.HEIGHT // 8

    def _cmd(self, c):
        self.dc.off()
        self.spi.writebytes([c & 0xFF])

    def _data(self, buf):
        self.dc.on()
        self.spi.writebytes2(list(buf))

    def reset(self):
        self.rst.on(); time.sleep(0.005)
        self.rst.off(); time.sleep(0.02)
        self.rst.on(); time.sleep(0.02)

    def init(self):
        self.reset()
        for c in (0xAE, 0xD5, 0x80, 0xA8, 0x1F, 0xD3, 0x00, 0x40,
                  0x8D, 0x14, 0x20, 0x00, 0xA1, 0xC8, 0xDA, 0x02,
                  0x81, 0x8F, 0xD9, 0xF1, 0xDB, 0x40, 0xA4, 0xA6,
                  0x2E, 0xAF):
            self._cmd(c)
        self.clear()

    def set_contrast(self, value):
        self._cmd(0x81)
        self._cmd(max(0, min(255, int(value))))

    def _blit(self, buf):
        self._cmd(0x21); self._cmd(0); self._cmd(self.WIDTH - 1)    # column range
        self._cmd(0x22); self._cmd(0); self._cmd(self._pages - 1)   # page range
        self._data(buf)

    def display(self, image):
        img = image.convert('1')
        if img.size != (self.WIDTH, self.HEIGHT):
            img = img.resize((self.WIDTH, self.HEIGHT))
        px = img.load()
        buf = bytearray(self.WIDTH * self._pages)
        for page in range(self._pages):
            base = page * self.WIDTH
            for x in range(self.WIDTH):
                bits = 0
                for bit in range(8):
                    if px[x, page * 8 + bit] == 0:   # 0 == lit
                        bits |= (1 << bit)
                buf[base + x] = bits
        self._blit(buf)

    def clear(self):
        self._blit(bytearray(self.WIDTH * self._pages))

    def off(self):
        self._cmd(0xAE)

    def close(self):
        try:
            self.off()
        except Exception:
            pass
        try:
            self.spi.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Waveshare 2.7" e-Paper HAT (V2)  -  264x176 in landscape use                 #
# --------------------------------------------------------------------------- #
class EPD2in7V2:
    WIDTH = 176     # native (portrait) width
    HEIGHT = 264    # native (portrait) height

    def __init__(self, dc=23, rst=17, busy=22, spi_bus=0, spi_dev=1, speed_hz=4_000_000):
        self.dc = DigitalOutputDevice(dc, initial_value=False)
        self.rst = DigitalOutputDevice(rst, initial_value=True)
        self.busy = DigitalInputDevice(busy)
        self.spi = spidev.SpiDev()
        self.spi.open(spi_bus, spi_dev)
        self.spi.max_speed_hz = speed_hz
        self.spi.mode = 0
        self._based = False

    def _cmd(self, c):
        self.dc.off()
        self.spi.writebytes([c & 0xFF])

    def _data1(self, d):
        self.dc.on()
        self.spi.writebytes([d & 0xFF])

    def _data(self, buf):
        self.dc.on()
        self.spi.writebytes2(list(buf))

    def reset(self):
        self.rst.on(); time.sleep(0.2)
        self.rst.off(); time.sleep(0.002)
        self.rst.on(); time.sleep(0.2)

    def _wait(self, timeout=30):
        t0 = time.time()
        while self.busy.value == 1:          # 1 == busy on this controller
            time.sleep(0.02)
            if time.time() - t0 > timeout:
                raise TimeoutError("e-paper BUSY stuck high")

    def init(self):
        self.reset()
        self._wait()
        self._cmd(0x12)                       # SWRESET
        self._wait()
        self._cmd(0x45)                       # RAM-Y start/end -> 0..263
        for d in (0x00, 0x00, 0x07, 0x01):
            self._data1(d)
        self._cmd(0x4F)                       # RAM-Y counter = 0
        self._data1(0x00); self._data1(0x00)
        self._cmd(0x11)                       # data entry mode
        self._data1(0x03)
        self._based = False

    def getbuffer(self, image):
        """Accept a 264x176 (landscape) or 176x264 (portrait) '1'-mode image."""
        buf = [0xFF] * (self.WIDTH // 8 * self.HEIGHT)
        img = image.convert('1')
        w, h = img.size
        px = img.load()
        if w == self.WIDTH and h == self.HEIGHT:           # portrait, as-is
            for y in range(h):
                for x in range(w):
                    if px[x, y] == 0:
                        buf[(x + y * self.WIDTH) // 8] &= ~(0x80 >> (x % 8))
        elif w == self.HEIGHT and h == self.WIDTH:         # landscape -> rotate
            for y in range(h):
                for x in range(w):
                    nx, ny = y, self.HEIGHT - x - 1
                    if px[x, y] == 0:
                        buf[(nx + ny * self.WIDTH) // 8] &= ~(0x80 >> (y % 8))
        else:
            raise ValueError("image must be 264x176 or 176x264, got %dx%d" % (w, h))
        return buf

    def _turn_on(self, mode):
        self._cmd(0x22); self._data1(mode); self._cmd(0x20); self._wait()

    def clear(self):
        n = self.WIDTH // 8 * self.HEIGHT
        self._cmd(0x24); self._data([0xFF] * n)
        self._cmd(0x26); self._data([0xFF] * n)
        self._turn_on(0xF7)
        self._based = True

    def display_base(self, buf):
        """Full (flashing) refresh that also writes the partial-mode baseline."""
        self._cmd(0x24); self._data(buf)
        self._cmd(0x26); self._data(buf)
        self._turn_on(0xF7)
        self._based = True

    def display_quick(self, buf):
        """No-flash full-frame update using the partial waveform."""
        if not self._based:
            self.display_base(buf)
            return
        self._cmd(0x24); self._data(buf)
        self._turn_on(0xFF)

    def sleep(self):
        self._cmd(0x10); self._data1(0x01)
        time.sleep(0.1)

    def close(self):
        try:
            self.sleep()
        except Exception:
            pass
        try:
            self.spi.close()
        except Exception:
            pass
