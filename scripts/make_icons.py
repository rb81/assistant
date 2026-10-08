"""One-off generator for the chat PWA icons. Requires Pillow: pip install pillow."""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parents[1] / "agent/src/assistant_agent/ui/assets/icons"
BG = (16, 18, 26, 255)
ACCENT = (122, 162, 247, 255)
DOT = (240, 242, 248, 255)


def draw_icon(size: int, padded: bool) -> Image.Image:
    image = Image.new("RGBA", (size, size), BG)
    draw = ImageDraw.Draw(image)
    inset = size * (0.26 if padded else 0.18)
    left, top = inset, size * (0.30 if padded else 0.24)
    right, bottom = size - inset, size - size * (0.34 if padded else 0.30)
    radius = (bottom - top) * 0.32
    draw.rounded_rectangle((left, top, right, bottom), radius=radius, fill=ACCENT)
    tail = [
        (left + (right - left) * 0.22, bottom - 2),
        (left + (right - left) * 0.40, bottom - 2),
        (left + (right - left) * 0.24, bottom + (bottom - top) * 0.26),
    ]
    draw.polygon(tail, fill=ACCENT)
    dot_radius = (bottom - top) * 0.075
    center_y = (top + bottom) / 2
    for factor in (0.35, 0.5, 0.65):
        center_x = left + (right - left) * factor
        draw.ellipse(
            (center_x - dot_radius, center_y - dot_radius, center_x + dot_radius, center_y + dot_radius),
            fill=DOT,
        )
    return image


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    draw_icon(512, padded=False).resize((192, 192), Image.LANCZOS).save(OUT / "icon-192.png")
    draw_icon(512, padded=False).save(OUT / "icon-512.png")
    draw_icon(512, padded=True).save(OUT / "icon-maskable-512.png")
    draw_icon(512, padded=False).resize((180, 180), Image.LANCZOS).save(OUT / "apple-touch-icon.png")
    print("wrote icons to", OUT)


if __name__ == "__main__":
    main()
