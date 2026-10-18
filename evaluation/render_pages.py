"""Render the 20 synthetic notebook pages from facts.json.

Everything here is made up. The text comes from facts.json and is drawn word by word with the
Caveat handwriting font (OFL, see fonts/OFL.txt), then roughened up so it looks like a phone
photo of a page: each word sits a little off the line, the page is rotated by up to 1.5
degrees, then blurred, given paper tint, uneven light and grain, and saved as a JPEG.
Seeds come from the page number, so the output is the same every run.

    python -m evaluation.render_pages            # writes evaluation/pages/*.jpg + index.json

index.json maps the sha256 of each image to its page id. The fake model server uses it to
answer with the true page (the "oracle"); the real model never sees it.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import random
from datetime import date
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = Path(__file__).resolve().parent
FONT = HERE / "fonts" / "Caveat[wght].ttf"
W, H = 1100, 1500
LINE_H = 46
TITLE_LINE = 80
LEFT, RIGHT = 120, W - 60
INK = (28, 32, 70)

DATE_FORMATS = ["{d} {mon} {y}", "{mon} {d}, {y}", "{y}-{m:02d}-{d:02d}", "{d} {mon} {y}"]
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def font(size: int, weight: int = 500) -> ImageFont.FreeTypeFont:
    f = ImageFont.truetype(str(FONT), size)
    with contextlib.suppress(OSError, AttributeError):
        f.set_variation_by_axes([weight])
    return f


def show_date(iso: str, index: int) -> str:
    d = date.fromisoformat(iso)
    fmt = DATE_FORMATS[index % len(DATE_FORMATS)]
    return fmt.format(d=d.day, m=d.month, y=d.year, mon=MONTHS[d.month - 1])


def num(x: float) -> str:
    return f"{x:g}"


class Writer:
    def __init__(self, img: Image.Image, rng: random.Random) -> None:
        self.img, self.draw, self.rng = img, ImageDraw.Draw(img), rng

    def word(self, x: float, y: float, text: str, f: ImageFont.FreeTypeFont) -> float:
        dy = self.rng.uniform(-3, 3)
        shade = tuple(max(0, c + self.rng.randint(-10, 10)) for c in INK)
        self.draw.text((x, y + dy), text, font=f, fill=shade)
        return self.draw.textlength(text + " ", font=f) + self.rng.uniform(-1.5, 3)

    def paragraph(
        self, x: float, y: float, text: str, f, max_x: float, line_h: int = LINE_H
    ) -> float:
        cx = x
        for w in text.split():
            width = self.draw.textlength(w + " ", font=f)
            if cx + width > max_x:
                cx, y = x, y + line_h
            cx += self.word(cx, y, w, f)
        return y + line_h


def draw_page(page: dict, index: int) -> Image.Image:
    rng = random.Random(1000 + index)
    big = Image.new("RGB", (W, H), (244, 240, 226))
    d = ImageDraw.Draw(big)
    for y in range(150, H - 40, LINE_H):  # ruled lines and margin
        d.line([(50, y), (W - 40, y)], fill=(176, 196, 222), width=2)
    d.line([(100, 0), (100, H)], fill=(214, 120, 120), width=3)

    wr = Writer(big, rng)
    big_f, mid_f, body_f = font(66, 600), font(44, 500), font(40, 500)

    y = 40
    y = wr.paragraph(LEFT, y, page["title"], big_f, RIGHT, TITLE_LINE) + 10
    if page["date"]:
        wr.word(LEFT, y, "Date: " + show_date(page["date"], index), mid_f)
    y += 70
    y = wr.paragraph(LEFT, y, page["narrative"], body_f, RIGHT) + 20

    # reagent table
    cols = [LEFT, 600, 790, 920, RIGHT]
    heads = ["Reagent", "Amount", "MW", "Equiv."]
    top = y
    for cx, h in zip(cols, heads, strict=False):
        wr.word(cx + 8, y, h, mid_f)
    y += LINE_H + 10
    for r in page["reagents"]:
        cells = [
            r["name"],
            f"{num(r['amount'])} {r['unit']}",
            num(r["molar_mass_g_mol"]),
            num(r["equivalents"]),
        ]
        small = font(36 if len(r["name"]) > 22 else 40, 500)
        for cx, text in zip(cols, cells, strict=False):
            wr.word(cx + 8, y, text, small)
        y += LINE_H + 6
    for cx in cols:
        d.line([(cx, top), (cx, y)], fill=INK, width=2)
    d.line([(cols[0], top), (cols[-1], top)], fill=INK, width=2)
    d.line([(cols[0], top + LINE_H + 8), (cols[-1], top + LINE_H + 8)], fill=INK, width=2)
    d.line([(cols[0], y), (cols[-1], y)], fill=INK, width=2)
    y += 40
    if page["notes"]:
        wr.word(LEFT, y, "Notes:", mid_f)
        wr.paragraph(LEFT + 130, y + 2, page["notes"], body_f, RIGHT)
    return big


def roughen(img: Image.Image, index: int) -> Image.Image:
    rng = random.Random(5000 + index)
    angle = rng.uniform(-1.5, 1.5)
    img = img.rotate(angle, resample=Image.BICUBIC, expand=False, fillcolor=(120, 112, 100))
    img = img.filter(ImageFilter.GaussianBlur(rng.uniform(0.7, 1.3)))
    arr = np.asarray(img).astype(np.float32)
    nrng = np.random.default_rng(9000 + index)
    # uneven light: a soft gradient across the page
    gx = np.linspace(rng.uniform(0.88, 1.0), rng.uniform(0.94, 1.04), W)[None, :, None]
    gy = np.linspace(rng.uniform(0.9, 1.0), rng.uniform(0.94, 1.02), H)[:, None, None]
    arr = arr * gx * gy
    arr += nrng.normal(0, rng.uniform(5, 9), arr.shape)  # grain
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))


def main(out: Path = HERE / "pages") -> None:
    facts = json.loads((HERE / "facts.json").read_text())["pages"]
    out.mkdir(exist_ok=True)
    for old in out.glob("*.jpg"):
        old.unlink()
    index: dict[str, str] = {}
    for i, page in enumerate(facts):
        img = roughen(draw_page(page, i), i)
        path = out / f"{page['id']}.jpg"
        img.save(path, "JPEG", quality=85, optimize=False)
        index[hashlib.sha256(path.read_bytes()).hexdigest()] = page["id"]
    (out / "index.json").write_text(json.dumps(index, indent=1) + "\n")
    print(f"wrote {len(facts)} pages to {out}")


if __name__ == "__main__":
    main()
