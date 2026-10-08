"""Draw Pillow images straight to a Linux framebuffer.

The GeeekPi 3.5" SPI panel is exposed by the kernel as a secondary framebuffer
(typically ``/dev/fb1``). We compose each frame with Pillow and write the raw
pixels here — no X, no SDL — so it works headless on Raspberry Pi OS Lite.

Off-device (e.g. a dev laptop with no panel) this falls back to writing PNG
frames so the UI can be developed and previewed anywhere.
"""

from __future__ import annotations

import mmap
import os
import struct
from typing import Optional

from PIL import Image

try:
    import fcntl  # Linux-only; the real FrameBuffer needs it. Absent on Windows dev boxes.
except ImportError:
    fcntl = None

try:
    import numpy as np
except ImportError:  # numpy makes RGB565 packing fast; strongly recommended on-device
    np = None

# ioctl request codes from <linux/fb.h>
FBIOGET_VSCREENINFO = 0x4600
FBIOGET_FIXSCREENINFO = 0x4602


class FrameBuffer:
    """A memory-mapped Linux framebuffer that accepts Pillow images."""

    def __init__(self, device: Optional[str] = None):
        self.device = device or os.environ.get("DOCKERGO_FB", "/dev/fb1")
        self._fd = os.open(self.device, os.O_RDWR)
        self.width, self.height, self.bpp = self._read_vinfo()
        self.line_length = self._read_finfo()
        self._map = mmap.mmap(
            self._fd,
            self.line_length * self.height,
            mmap.MAP_SHARED,
            mmap.PROT_READ | mmap.PROT_WRITE,
        )

    def _read_vinfo(self):
        # fb_var_screeninfo begins with 7 x uint32: xres, yres, xres_virtual,
        # yres_virtual, xoffset, yoffset, bits_per_pixel.
        buf = bytearray(256)
        fcntl.ioctl(self._fd, FBIOGET_VSCREENINFO, buf)
        xres, yres, _xv, _yv, _xo, _yo, bpp = struct.unpack_from("<7I", buf, 0)
        return xres, yres, bpp

    def _read_finfo(self):
        # line_length (uint32) sits after char id[16], one pointer-sized field,
        # four uint32, three uint16 and two bytes of padding.
        buf = bytearray(256)
        fcntl.ioctl(self._fd, FBIOGET_FIXSCREENINFO, buf)
        offset = 40 + struct.calcsize("P")
        (line_length,) = struct.unpack_from("<I", buf, offset)
        return line_length or self.width * max(1, self.bpp // 8)

    @property
    def size(self):
        return (self.width, self.height)

    def blit(self, image: Image.Image):
        if image.size != (self.width, self.height):
            image = image.resize((self.width, self.height))

        if self.bpp == 16:
            data = self._to_rgb565(image)
            stride = self.width * 2
        elif self.bpp == 32:
            data = image.convert("RGBA").tobytes("raw", "BGRA")
            stride = self.width * 4
        else:
            raise RuntimeError(f"Unsupported framebuffer depth: {self.bpp}bpp")

        if stride == self.line_length:
            self._map.seek(0)
            self._map.write(data)
        else:  # the device has padded scanlines; copy row by row
            for y in range(self.height):
                self._map.seek(y * self.line_length)
                self._map.write(data[y * stride:(y + 1) * stride])

    @staticmethod
    def _to_rgb565(image: Image.Image) -> bytes:
        rgb = image.convert("RGB")
        if np is not None:
            arr = np.asarray(rgb, dtype=np.uint16)
            r = (arr[..., 0] >> 3) << 11
            g = (arr[..., 1] >> 2) << 5
            b = arr[..., 2] >> 3
            return (r | g | b).astype("<u2").tobytes()
        out = bytearray()
        for (r, g, b) in rgb.getdata():
            out += struct.pack("<H", ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3))
        return bytes(out)

    def close(self):
        try:
            self._map.close()
        finally:
            os.close(self._fd)


class MockFrameBuffer:
    """Off-device backend: writes each frame to a PNG so the UI can be previewed anywhere."""

    def __init__(self, width: int = 480, height: int = 320, out: str = "dockergo-frame.png"):
        self.width, self.height, self.bpp = width, height, 16
        self._out = out

    @property
    def size(self):
        return (self.width, self.height)

    def blit(self, image: Image.Image):
        if image.size != (self.width, self.height):
            image = image.resize((self.width, self.height))
        image.convert("RGB").save(self._out)

    def close(self):
        pass


def open_framebuffer(device: Optional[str] = None, mock: bool = False):
    if mock:
        return MockFrameBuffer()
    try:
        return FrameBuffer(device)
    except (FileNotFoundError, PermissionError, OSError):
        # No panel present (dev laptop, missing overlay) — fall back to PNG frames.
        return MockFrameBuffer()
