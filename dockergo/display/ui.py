"""Render a status :class:`Snapshot` into a Pillow image for the LCD.

Layout (landscape, scales to the panel size):

    +------------------------------------------------+
    | (o) device-name                 SSID    72%    |  top bar + status "LED"
    |  STATE                                         |  state chip (accent colour)
    |  Downloading frigate                           |  headline
    |  2/7                                           |  detail
    |  [#############.................]        42%   |  progress bar
    |  --------------------------------------------  |
    |  10:04:01 Connecting to HOME                   |  scrolling log
    |  10:04:06 Downloading frigate                  |
    +------------------------------------------------+
"""

from __future__ import annotations

import os

from PIL import Image, ImageDraw, ImageFont

from ..status import Snapshot

BG = (14, 16, 22)
FG = (230, 234, 240)
DIM = (120, 130, 145)
BAR_BG = (40, 44, 54)
DASH = "\u2014"
ELLIPSIS = "\u2026"

FONT_DIRS = (
    "/usr/share/fonts/truetype/dejavu",
    "/usr/share/fonts/truetype/freefont",
    "/usr/share/fonts/TTF",
)


def _load_font(names, size):
    for directory in FONT_DIRS:
        for name in names:
            path = os.path.join(directory, name)
            if os.path.exists(path):
                try:
                    return ImageFont.truetype(path, size)
                except OSError:
                    pass
    return ImageFont.load_default()


def _ellipsize(text, font, max_width, draw):
    if draw.textlength(text, font=font) <= max_width:
        return text
    while text and draw.textlength(text + ELLIPSIS, font=font) > max_width:
        text = text[:-1]
    return text + ELLIPSIS


class StatusScreen:
    def __init__(self, size=(480, 320)):
        self.size = size
        scale = size[1] / 320.0
        self.f_small = _load_font(["DejaVuSansMono.ttf"], max(11, int(13 * scale)))
        self.f_body = _load_font(["DejaVuSans.ttf"], max(13, int(16 * scale)))
        self.f_head = _load_font(["DejaVuSans-Bold.ttf", "DejaVuSans.ttf"], max(20, int(26 * scale)))
        self.f_label = _load_font(["DejaVuSans-Bold.ttf", "DejaVuSans.ttf"], max(12, int(14 * scale)))

    def render(self, s: Snapshot) -> Image.Image:
        w, h = self.size
        img = Image.new("RGB", self.size, BG)
        d = ImageDraw.Draw(img)
        accent = s.color

        # --- top bar ---
        bar_h = int(h * 0.14)
        d.rectangle([0, 0, w, bar_h], fill=(22, 25, 33))
        cy = bar_h // 2
        d.ellipse([10, cy - 7, 24, cy + 7], fill=accent)  # status "LED"
        d.text((34, cy), s.device_name, font=self.f_label, fill=FG, anchor="lm")
        right = f"{s.ssid or DASH}  {s.rssi}%" if s.online else "offline"
        d.text((w - 8, cy), right, font=self.f_label, fill=FG if s.online else DIM, anchor="rm")

        # --- state chip + headline + detail ---
        y = bar_h + int(h * 0.04)
        d.text((12, y), s.state.value, font=self.f_label, fill=accent, anchor="lt")
        y += int(h * 0.075)
        d.text((12, y), _ellipsize(s.headline, self.f_head, w - 24, d),
               font=self.f_head, fill=FG, anchor="lt")
        if s.detail:
            y += int(h * 0.1)
            d.text((12, y), _ellipsize(s.detail, self.f_body, w - 24, d),
                   font=self.f_body, fill=DIM, anchor="lt")

        # --- progress bar ---
        pb_y = int(h * (0.64 if s.transfer is not None else 0.52))
        if s.progress is not None:
            p = max(0.0, min(1.0, s.progress))
            bar_right = w - 72
            d.rectangle([12, pb_y, bar_right, pb_y + 16], fill=BAR_BG)
            d.rectangle([12, pb_y, 12 + int((bar_right - 12) * p), pb_y + 16], fill=accent)
            d.text((w - 12, pb_y + 8), f"{int(p * 100)}%", font=self.f_small, fill=FG, anchor="rm")

        # --- scrolling log ---
        log_top = int(h * 0.6)
        if s.transfer is not None:
            t = s.transfer
            byte_text = f"{t.current:,} / {t.total:,} B" if t.total is not None else (
                f"{t.current:,} B copied (total unknown)"
            )
            lines = (
                byte_text,
                f"{t.rate / 1048576:.2f} MiB/s ({t.rate:,.0f} B/s)",
                f"elapsed {int(t.elapsed)}s  no bytes {int(t.idle)}s",
            )
            for text, relative_y in zip(lines, (0.44, 0.50, 0.56)):
                d.text((12, int(h * relative_y)),
                       _ellipsize(text, self.f_small, w - 24, d),
                       font=self.f_small,
                       fill=(240, 200, 0) if t.idle >= 5 else FG, anchor="lt")
            log_top = int(h * 0.74)
        d.line([12, log_top - 6, w - 12, log_top - 6], fill=(34, 38, 48))
        line_h = self.f_small.getbbox("Ag")[3] + 3
        max_lines = max(1, (h - log_top - 4) // line_h)
        for i, line in enumerate(s.log[-max_lines:]):
            d.text((12, log_top + i * line_h),
                   _ellipsize(line, self.f_small, w - 24, d),
                   font=self.f_small, fill=DIM, anchor="lt")
        return img
