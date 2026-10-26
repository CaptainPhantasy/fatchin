#!/usr/bin/env python3
"""Render captured terminal output as a PNG screenshot.

Every screenshot in docs/screenshots/ is generated from REAL captured output
of the commands shown — nothing is mocked or hand-drawn.

Usage: termshot.py TITLE INFILE OUTFILE [PROMPT]
       echo "text" | termshot.py TITLE - OUTFILE
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
BG = (13, 17, 23)          # GitHub dark
FG = (201, 209, 217)
TITLE_BG = (33, 38, 45)
TITLE_FG = (139, 148, 158)
GREEN = (63, 185, 80)
ACCENT = (88, 166, 255)


def render(title: str, text: str, out: Path, prompt: str = "$") -> None:
    font_size = 15
    font = ImageFont.truetype(FONT_PATH, font_size)
    title_font = ImageFont.truetype(FONT_PATH, font_size - 1)
    lines = text.rstrip("\n").splitlines() or [""]

    pad = 16
    line_h = font_size + 6
    bar_h = 34
    longest = max((len(l) for l in lines), default=0)
    # command line shown at top of the body
    cmd_line = f"{prompt} {title}"
    body_w = max(longest, len(cmd_line)) * (font_size - 3) + pad * 2
    width = max(560, min(body_w, 1000))
    height = bar_h + pad + line_h + len(lines) * line_h + pad

    img = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, width, bar_h], fill=TITLE_BG)
    draw.ellipse([12, 11, 24, 23], fill=(255, 95, 86))
    draw.ellipse([30, 11, 42, 23], fill=(255, 189, 46))
    draw.ellipse([48, 11, 60, 23], fill=(39, 201, 63))
    draw.text((width // 2 - len(title) * 4, 8), title, font=title_font,
              fill=TITLE_FG)

    y = bar_h + pad
    draw.text((pad, y), cmd_line, font=font, fill=GREEN)
    y += line_h
    for line in lines:
        color = FG
        stripped = line.strip()
        if stripped.startswith(("OK", "PASS", "✓")) or "OK" == stripped[:2]:
            color = GREEN
        elif "endpoint:" in line or "model:" in line:
            color = ACCENT
        draw.text((pad, y), line, font=font, fill=color)
        y += line_h

    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, "PNG")
    print(f"wrote {out} ({width}x{height})")


if __name__ == "__main__":
    title, infile, outfile = sys.argv[1], sys.argv[2], sys.argv[3]
    if infile == "-":
        text = sys.stdin.read()
    else:
        text = Path(infile).read_text()
    render(title, text, Path(outfile))
