# Third-Party Licenses

This project includes code derived from third-party open-source software.
The original notices and license texts are reproduced below, as required.

--------------------------------------------------------------------------------

## Waveshare e-Paper driver library

- Component : e-paper command / timing / RAM-window / waveform sequences and the
              `getbuffer()` pixel-rotation logic in `pi_displays.py`
              (class `EPD2in7V2`), derived from the reference file `epd2in7_V2.py`.
- Source    : https://github.com/waveshareteam/e-Paper
- Author    : Waveshare team
- License   : MIT

MIT License

Copyright (c) Waveshare team

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.

--------------------------------------------------------------------------------

The SSD1306 OLED initialisation in `pi_displays.py` and all other files in this
repository (`pihud.py`, `ollama-hud-run.py`, `install-pihud.sh`,
`smoke-test-pihud.sh`) are original work and are not covered by the notice above;
they fall under this project's own LICENSE.
