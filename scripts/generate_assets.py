"""Generate crisp anti-aliased branding assets for GD Quiet (PNG, multi-size ICO, Inno Setup BMPs)."""
from __future__ import annotations

import math
from pathlib import Path
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
ASSETS_DIR = ROOT / "app" / "assets"


def _arc_points(
    cx: float,
    cy: float,
    r: float,
    start_deg: float,
    end_deg: float,
    steps: int = 36,
) -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    for i in range(steps + 1):
        ang = math.radians(start_deg + (end_deg - start_deg) * (i / steps))
        pts.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
    return pts


def _draw_thick_arc(
    draw: ImageDraw.ImageDraw,
    cx: float,
    cy: float,
    r: float,
    start_deg: float,
    end_deg: float,
    thickness: float,
    fill: tuple[int, int, int, int],
) -> None:
    outer = _arc_points(cx, cy, r + thickness / 2.0, start_deg, end_deg)
    inner = list(reversed(_arc_points(cx, cy, r - thickness / 2.0, start_deg, end_deg)))
    draw.polygon(outer + inner, fill=fill)
    for deg in (start_deg, end_deg):
        ang = math.radians(deg)
        px = cx + r * math.cos(ang)
        py = cy + r * math.sin(ang)
        rad = thickness / 2.0
        draw.ellipse([px - rad, py - rad, px + rad, py + rad], fill=fill)


def _draw_capsule(
    draw: ImageDraw.ImageDraw,
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    width: float,
    fill: tuple[int, int, int, int],
) -> None:
    dx = x1 - x0
    dy = y1 - y0
    length = math.hypot(dx, dy)
    if length <= 0:
        return
    nx = -dy / length * (width / 2.0)
    ny = dx / length * (width / 2.0)
    draw.polygon(
        [
            (x0 + nx, y0 + ny),
            (x1 + nx, y1 + ny),
            (x1 - nx, y1 - ny),
            (x0 - nx, y0 - ny),
        ],
        fill=fill,
    )
    r = width / 2.0
    draw.ellipse([x0 - r, y0 - r, x0 + r, y0 + r], fill=fill)
    draw.ellipse([x1 - r, y1 - r, x1 + r, y1 + r], fill=fill)


def render_master_logo(size: int = 256) -> Image.Image:
    # 8x supersampling eliminates any stair-stepping or jagged rounded borders.
    s = 2048
    margin = 64
    radius = 400
    border_w = 88

    bg_fill = (17, 25, 43, 255)      # #11192b
    border_col = (99, 102, 241, 255)  # #6366f1
    speaker_col = (248, 250, 252, 255)  # #f8fafc
    wave_col = (100, 116, 139, 255)   # #64748b
    slash_col = (239, 68, 68, 255)    # #ef4444

    canvas = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(canvas)

    # Outer indigo border rounded square
    d.rounded_rectangle(
        [margin, margin, s - margin, s - margin],
        radius=radius,
        fill=border_col,
    )
    # Inner dark rounded square
    inner_margin = margin + border_w
    inner_radius = max(40, radius - border_w)
    d.rounded_rectangle(
        [inner_margin, inner_margin, s - inner_margin, s - inner_margin],
        radius=inner_radius,
        fill=bg_fill,
    )

    # Draw foreground layer so cutout around the slash erases cleanly back to bg_fill
    fg = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    fd = ImageDraw.Draw(fg)

    # Rounded speaker back-box + flared cone (shifted slightly left to leave room for sound waves)
    fd.rounded_rectangle([460, 810, 790, 1238], radius=96, fill=speaker_col)
    fd.polygon(
        [
            (720, 810),
            (1085, 515),
            (1145, 515),
            (1145, 1533),
            (1085, 1533),
            (720, 1238),
        ],
        fill=speaker_col,
    )
    # Round off outer tips of the speaker cone cleanly
    fd.ellipse([1045, 505, 1145, 605], fill=speaker_col)
    fd.ellipse([1045, 1443, 1145, 1543], fill=speaker_col)

    # Sound wave arcs
    _draw_thick_arc(fd, 1125, 1024, 240, -40, 40, 84, wave_col)
    _draw_thick_arc(fd, 1125, 1024, 425, -43, 43, 84, wave_col)

    # Knockout halo behind the mute slash so slash never blurs awkwardly into the speaker
    slash_start = (475.0, 485.0)
    slash_end = (1565.0, 1575.0)
    _draw_capsule(fd, *slash_start, *slash_end, width=230, fill=(0, 0, 0, 0))
    # Punch transparent pixels back out by compositing a mask for the halo
    halo_mask = Image.new("L", (s, s), 255)
    hm_d = ImageDraw.Draw(halo_mask)
    _draw_capsule(hm_d, *slash_start, *slash_end, width=240, fill=0)
    r_ch, g_ch, b_ch, a_ch = fg.split()
    from PIL import ImageChops

    fg.putalpha(ImageChops.darker(a_ch, halo_mask))

    # Crisp coral-red mute slash
    _draw_capsule(fd, *slash_start, *slash_end, width=136, fill=slash_col)

    canvas.alpha_composite(fg)
    return canvas.resize((size, size), Image.Resampling.LANCZOS)


def generate_all() -> None:
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    logo_256 = render_master_logo(256)
    logo_256.save(ROOT / "logo.png", format="PNG")

    ico_sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    logo_1024 = render_master_logo(512)
    logo_1024.save(ASSETS_DIR / "gdquiet.ico", format="ICO", sizes=ico_sizes)

    # Inno Setup classic left banner (164x314) & small top-right icon (55x58)
    side = Image.new("RGB", (164, 314), (15, 22, 38))
    sd = ImageDraw.Draw(side)
    # Subtle indigo accent bar along right edge of sidebar banner
    sd.rectangle([161, 0, 163, 313], fill=(88, 101, 242))
    side_rgba = side.convert("RGBA")
    badge = render_master_logo(112)
    side_rgba.alpha_composite(badge, (26, 46))
    side_rgba.convert("RGB").save(ASSETS_DIR / "wizard_side.bmp", format="BMP")

    small = Image.new("RGBA", (55, 58), (255, 255, 255, 255))
    small_badge = render_master_logo(48)
    small.alpha_composite(small_badge, (3, 5))
    small.convert("RGB").save(ASSETS_DIR / "wizard_small.bmp", format="BMP")

    print("Generated:", ROOT / "logo.png", ASSETS_DIR / "gdquiet.ico", ASSETS_DIR / "wizard_side.bmp", ASSETS_DIR / "wizard_small.bmp")


if __name__ == "__main__":
    generate_all()
