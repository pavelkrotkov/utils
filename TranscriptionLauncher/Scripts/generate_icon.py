#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow>=10"]
# ///
"""Generate the TranscriptionLauncher app icon (microphone + waveform).

Renders a 1024x1024 master image and downscales it to every size required
by Packaging/AppIcon.appiconset. The PNGs are committed to the repo, so this
script only needs to be re-run when the artwork changes:

    uv run TranscriptionLauncher/Scripts/generate_icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

MASTER = 1024
APPICONSET = Path(__file__).resolve().parent.parent / "Packaging" / "AppIcon.appiconset"

# (filename, pixel size) pairs matching Contents.json.
ICON_FILES = [
    ("icon_16x16.png", 16),
    ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32),
    ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128),
    ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256),
    ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512),
    ("icon_512x512@2x.png", 1024),
]

TOP_COLOR = (74, 60, 199)  # indigo
BOTTOM_COLOR = (32, 26, 99)  # deep indigo
FOREGROUND = (255, 255, 255, 255)


def rounded_rect_mask(size: int, box: tuple[int, int, int, int], radius: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(box, radius=radius, fill=255)
    return mask


def vertical_gradient(
    size: int, top: tuple[int, int, int], bottom: tuple[int, int, int]
) -> Image.Image:
    gradient = Image.new("RGB", (1, size))
    for y in range(size):
        t = y / max(size - 1, 1)
        gradient.putpixel(
            (0, y),
            tuple(round(top[c] + (bottom[c] - top[c]) * t) for c in range(3)),
        )
    return gradient.resize((size, size))


def draw_master() -> Image.Image:
    image = Image.new("RGBA", (MASTER, MASTER), (0, 0, 0, 0))

    # Background: the standard macOS icon grid keeps artwork inside a
    # 824x824 rounded rectangle centered in the 1024 canvas.
    inset = (MASTER - 824) // 2
    box = (inset, inset, MASTER - inset, MASTER - inset)
    background = vertical_gradient(MASTER, TOP_COLOR, BOTTOM_COLOR)
    image.paste(background, (0, 0), rounded_rect_mask(MASTER, box, radius=186))

    draw = ImageDraw.Draw(image)
    cx = MASTER // 2

    # Microphone capsule.
    cap_w, cap_h = 150, 280
    cap_top = 280
    draw.rounded_rectangle(
        (cx - cap_w // 2, cap_top, cx + cap_w // 2, cap_top + cap_h),
        radius=cap_w // 2,
        fill=FOREGROUND,
    )

    # Microphone cradle: an open arc below the capsule.
    arc_r = 130
    arc_cy = cap_top + cap_h - arc_r + 26
    draw.arc(
        (cx - arc_r, arc_cy - arc_r, cx + arc_r, arc_cy + arc_r),
        start=20,
        end=160,
        fill=FOREGROUND,
        width=34,
    )

    # Stem and base.
    stem_top = arc_cy + arc_r - 10
    draw.rounded_rectangle((cx - 17, stem_top, cx + 17, stem_top + 70), radius=17, fill=FOREGROUND)
    draw.rounded_rectangle(
        (cx - 90, stem_top + 70, cx + 90, stem_top + 104), radius=17, fill=FOREGROUND
    )

    # Waveform bars flanking the microphone.
    bar_w = 44
    mid_y = cap_top + cap_h // 2
    for side in (-1, 1):
        for i, bar_h in enumerate((140, 240, 100)):
            x = cx + side * (170 + i * 90)
            draw.rounded_rectangle(
                (x - bar_w // 2, mid_y - bar_h // 2, x + bar_w // 2, mid_y + bar_h // 2),
                radius=bar_w // 2,
                fill=FOREGROUND,
            )

    return image


def main() -> None:
    APPICONSET.mkdir(parents=True, exist_ok=True)
    master = draw_master()
    for filename, size in ICON_FILES:
        scaled = master if size == MASTER else master.resize((size, size), Image.LANCZOS)
        scaled.save(APPICONSET / filename)
        print(f"wrote {filename} ({size}x{size})")


if __name__ == "__main__":
    main()
