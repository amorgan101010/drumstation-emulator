#!/usr/bin/env python3
"""Build the desktop panel's artwork: the plate, knob frames, buttons and lights.

    python3 tools/render_panel.py

Pillow, fontconfig and an installed Roboto are build tools only; tools/gui.py
loads the resulting PNGs with Tk.
The artwork is procedural, with no photographs, vendor artwork or firmware in
it. Geometry comes from gui.py, whose fingerprint is stored in skin.json so
tests/check_layout.py can tell when the plate no longer matches the layout.
"""
import json
import math
from pathlib import Path
import random
import subprocess
import sys

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gui  # noqa: E402

S = 4  # supersampling: everything is drawn S times larger, then reduced
LANCZOS, BICUBIC = Image.Resampling.LANCZOS, Image.Resampling.BICUBIC
SWEEP = (-230, 280)  # knob angle at zero and its travel, in canvas degrees


def px(value):
    return round(value * S)


def box(coords):
    return tuple(px(v) for v in coords)


def tile(w, h):
    return Image.new("RGBA", (px(w), px(h)))


def rgba(color, alpha=255):
    return tuple(int(color[i:i + 2], 16) for i in (1, 3, 5)) + (alpha,)


def rr(image, rect, radius, fill, outline=None, width=1):
    ImageDraw.Draw(image).rounded_rectangle(box(rect), px(radius), fill, outline, px(width))


def ellipse(image, cx, cy, r, fill):
    ImageDraw.Draw(image).ellipse(box((cx - r, cy - r, cx + r, cy + r)), fill)


def line(image, points, fill, width=1.0):
    ImageDraw.Draw(image).line([box(p) for p in points], fill, max(1, px(width)))


def blend(image, rect, radius, color, alpha):
    """A translucent rounded rectangle; ImageDraw alone would replace, not blend."""
    x0, y0, x1, y1 = rect
    layer = tile(x1 - x0, y1 - y0)
    rr(layer, (0, 0, x1 - x0, y1 - y0), radius, rgba(color, alpha))
    image.alpha_composite(layer, box((x0, y0)))


def vertical(size, top, bottom):
    return ImageOps.colorize(Image.linear_gradient("L").resize(size, BICUBIC), top, bottom).convert("RGBA")


def lit(size, light, dark, angle=35):
    """A gradient lit from the upper left, as every raised part of the panel is."""
    ramp = Image.linear_gradient("L").rotate(angle, BICUBIC).crop((38, 38, 218, 218))
    return ImageOps.colorize(ImageOps.autocontrast(ramp).resize(size, BICUBIC), light, dark).convert("RGBA")


def gradient_rr(image, rect, radius, top, bottom):
    x0, y0, x1, y1 = box(rect)
    fill = vertical((x1 - x0, y1 - y0), top, bottom)
    mask = Image.new("L", fill.size)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, fill.width - 1, fill.height - 1), px(radius), fill=255)
    fill.putalpha(mask)
    image.alpha_composite(fill, (x0, y0))


def disc(image, cx, cy, r, light, dark, angle=35):
    side = px(2 * r)
    fill = lit((side, side), light, dark, angle)
    mask = Image.new("L", (side, side))
    ImageDraw.Draw(mask).ellipse((0, 0, side - 1, side - 1), fill=255)
    fill.putalpha(mask)
    image.alpha_composite(fill, box((cx - r, cy - r)))


def shadow(image, rect, radius, blur, alpha, dx=0.0, dy=0.0, color="#000000"):
    x0, y0, x1, y1 = rect
    margin = blur * 3
    layer = tile(x1 - x0 + 2 * margin, y1 - y0 + 2 * margin)
    rr(layer, (margin, margin, margin + x1 - x0, margin + y1 - y0), radius, rgba(color, alpha))
    image.alpha_composite(layer.filter(ImageFilter.GaussianBlur(blur * S)),
                          box((x0 - margin + dx, y0 - margin + dy)))


def recess(image, rect, radius, top, bottom, depth=3.0, dark=150):
    """A well sunk into the plate: shaded under its upper lip, catching light on the lower one."""
    x0, y0, x1, y1 = rect
    w, h = x1 - x0, y1 - y0
    rr(image, (x0 - 1, y0 - 1, x1 + 1, y1 + 2), radius + 1, "#4a5653")
    rr(image, (x0 - 1, y0 - 1, x1 + 1, y1 + 1), radius + 1, "#07090a")
    gradient_rr(image, rect, radius, top, bottom)
    inside = Image.new("L", box((w, h)))
    ImageDraw.Draw(inside).rounded_rectangle((0, 0, inside.width - 1, inside.height - 1), px(radius), fill=255)
    lip = Image.new("L", inside.size, 255)
    ImageDraw.Draw(lip).rounded_rectangle((px(depth * .4), px(depth), lip.width - 1 - px(depth * .4),
                                           lip.height + px(depth)), px(radius), fill=0)
    lip = ImageChops.multiply(lip.filter(ImageFilter.GaussianBlur(depth * S * .7)), inside)
    shade = Image.new("RGBA", inside.size, (0, 0, 0, 255))
    shade.putalpha(lip.point(lambda v: v * dark // 255))
    image.alpha_composite(shade, box((x0, y0)))


def noise(w, h, sigma, rng):
    data = bytes(max(0, min(255, round(rng.gauss(128, sigma)))) for _ in range(w * h))
    return Image.frombytes("L", (w, h), data)


def brushed(w, h, top, bottom, seed=1996):
    """Dark anodised metal: long horizontal brush marks under a fine grain."""
    rng = random.Random(seed)
    grain = noise(w // 16 + 1, h, 4.5, rng).resize((w, h), Image.Resampling.BILINEAR)
    texture = ImageChops.add(grain, noise(w, h, 2.2, rng), 1, -128)
    base = ImageOps.colorize(Image.linear_gradient("L").resize((w, h), BICUBIC), top, bottom)
    return ImageChops.add(base, Image.merge("RGB", (texture,) * 3), 1, -128).convert("RGBA")


def sweep(value):
    return math.radians(SWEEP[0] + value * SWEEP[1])


def knob_frames(r):
    """One knob at every position. Its lighting stays put; the grip and pointer turn."""
    size = 2 * r + 4
    c = size / 2
    body = tile(size, size)
    ellipse(body, c, c, r, "#020303")
    disc(body, c, c, r - .7, "#3f494b", "#06080a")
    caught = lit(body.size, "#aab5b4", "#1a2022")  # what the grip's ridges catch
    cap = tile(size, size)
    ellipse(cap, c, c, r * .77, "#050607")
    disc(cap, c, c, r * .72, "#87938f", "#2b3436")
    disc(cap, c, c, r * .61, "#566163", "#6a7676")  # a shallow dish, lit from its far side
    ridges = 30 if r >= 16 else 18
    width = 2 * math.pi * r * .88 / ridges * .42
    mark = max(1.7, r * .12)
    frames = []
    for index in range(gui.KNOB_FRAMES):
        angle = sweep(index / (gui.KNOB_FRAMES - 1))
        grip = Image.new("L", body.size)
        for ridge in range(ridges):
            a = angle + (ridge + .5) * 2 * math.pi / ridges
            line(grip, [(c + math.cos(a) * r * .80, c + math.sin(a) * r * .80),
                        (c + math.cos(a) * r * .965, c + math.sin(a) * r * .965)], 150, width)
        frame = body.copy()
        frame.alpha_composite(Image.composite(caught, Image.new("RGBA", body.size), grip))
        frame.alpha_composite(cap)
        ends = [(c + math.cos(angle) * r * k, c + math.sin(angle) * r * k) for k in (.2, .92)]
        for fill, extra in ((rgba("#0a0b08", 190), 1.3), (rgba("#f7d95f"), 0)):
            layer = tile(size, size)
            line(layer, ends, fill, mark + extra)
            for ex, ey in ends:
                ellipse(layer, ex, ey, (mark + extra) / 2, fill)
            frame.alpha_composite(layer)
        frames.append((frame.resize((size, size), LANCZOS), -size // 2, -size // 2))
    return frames


BUTTON_PAD = 4
FACES = {  # style: (top, bottom, pressed top, pressed bottom, glow)
    "plain": ("#56636a", "#2a3337", "#2a3337", "#38444a", None),
    "lit": ("#f0cf5c", "#a47f1f", "#b18b28", "#d6b247", "#f3c53a"),
    "red": ("#e0604f", "#8c2219", "#992a20", "#c04a3c", None),
}


def button(w, h, style, pressed):
    pad = BUTTON_PAD
    image = tile(w + 2 * pad, h + 2 * pad)
    top, bottom, down_top, down_bottom, glow = FACES[style]
    radius = 3 if h >= 16 else 2
    if glow:
        shadow(image, (pad, pad, pad + w, pad + h), radius, 1.3, 120, color=glow)
    rr(image, (pad, pad, pad + w, pad + h), radius, "#040505")
    travel = 1 if pressed else 0
    face = (pad + 1, pad + 1 + travel, pad + w - 1, pad + h - 2 + travel)
    if not pressed:  # the cap's own side wall, seen below its face
        rr(image, (pad + 1, pad + 2, pad + w - 1, pad + h - 1), radius - .5, "#161b1d")
    gradient_rr(image, face, radius - .5, *((down_top, down_bottom) if pressed else (top, bottom)))
    if not pressed:
        blend(image, (face[0] + 1.5, face[1] + .6, face[2] - 1.5, face[1] + 1.5), .4, "#ffffff", 70)
    return image.resize((w + 2 * pad, h + 2 * pad), LANCZOS), -pad, -pad


def grid_button(w, h, style, pressed):
    """Tight metal caps for the overview grid, without an overlapping glow pad."""
    top, bottom, down_top, down_bottom, _glow = FACES[style]
    image = tile(w, h)
    rr(image, (0, 0, w, h), 2, "#060909")
    gradient_rr(image, (1, 1, w - 1, h - 1), 1.5,
                *((down_top, down_bottom) if pressed else (top, bottom)))
    if not pressed:
        line(image, [(2, 2), (w - 2, 2)], rgba("#d2dfd9", 90), .5)
    return image.resize((w, h), LANCZOS), 0, 0


def lamp(r, color, core):
    """A lit LED: a hot centre in its lens and a soft halo on the plate around it."""
    size = 2 * math.ceil(r) + 16
    c = size / 2
    image = tile(size, size)
    halo = tile(size, size)
    ellipse(halo, c, c, r + 2.5, rgba(color, 150))
    image.alpha_composite(halo.filter(ImageFilter.GaussianBlur(3 * S)))
    ellipse(image, c, c, r, rgba(color))
    glare = tile(size, size)
    ellipse(glare, c - r * .15, c - r * .2, r * .6, rgba(core))
    image.alpha_composite(glare.filter(ImageFilter.GaussianBlur(r * .28 * S)))
    return image.resize((size, size), LANCZOS), -size // 2, -size // 2


def socket(image, cx, cy, r, lens):
    ellipse(image, cx, cy + .6, r + 2, "#4a5653")
    ellipse(image, cx, cy, r + 2, "#050607")
    disc(image, cx, cy, r, lens[0], lens[1])
    blend(image, (cx - r * .5, cy - r * .6, cx, cy - r * .15), r * .2, "#ffffff", 46)


def segment_shapes():
    """The eight display segments as polygons in a digit's own coordinates, keyed by firmware bit."""
    left, right, top, middle, bottom, half = 5, 23, 5, 22, 39, 2.1

    def across(y):
        return [(left + 1, y), (left + 3, y - half), (right - 3, y - half), (right - 1, y),
                (right - 3, y + half), (left + 3, y + half)]

    def down(x, y0, y1):
        return [(x, y0 + 1), (x + half, y0 + 3), (x + half, y1 - 3), (x, y1 - 1),
                (x - half, y1 - 3), (x - half, y0 + 3)]

    flat = {7: across(top), 0: across(middle), 2: across(bottom),
            1: down(left, top, middle), 3: down(right, top, middle),
            5: down(left, middle, bottom), 4: down(right, middle, bottom),
            6: [(right + 4 + 2 * math.cos(a * math.pi / 6), bottom + 2 * math.sin(a * math.pi / 6))
                for a in range(12)]}
    # Lean the digit like a real seven-segment display.
    return {bit: [(x + (middle - y) * .09, y) for x, y in points] for bit, points in flat.items()}


def segment(points):
    pad = 5
    w, h = gui.DIGIT_W + 2 * pad, gui.DIGIT_H + 2 * pad
    shape = tile(w, h)
    ImageDraw.Draw(shape).polygon([box((x + pad, y + pad)) for x, y in points], fill=rgba("#ff4a3d"))
    image = shape.filter(ImageFilter.GaussianBlur(2.2 * S))
    image.alpha_composite(shape)
    core = tile(w, h)
    cx, cy = (sum(v) / len(points) for v in zip(*points))
    ImageDraw.Draw(core).polygon([box((cx + (x - cx) * .7 + pad, cy + (y - cy) * .7 + pad)) for x, y in points],
                                 fill=rgba("#ffb3a4", 170))
    image.alpha_composite(core.filter(ImageFilter.GaussianBlur(.7 * S)))
    return image.resize((w, h), LANCZOS), -pad, -pad


def ticks(image, cx, cy, r):
    for index in range(11):
        a = sweep(index / 10)
        ends = [(cx + math.cos(a) * (r + k), cy + math.sin(a) * (r + k)) for k in (3.2, gui.TICK - .4)]
        line(image, ends, "#b9c0b8" if index in (0, 5, 10) else "#8a938d", 1.3 if r > gui.MINI_R else 1.0)


def card(image, kit, x, y, w, h):
    title = gui.CARD_TITLE_H
    shadow(image, (x, y, x + w, y + h), 5, 3, 150, dy=2)
    rr(image, (x - .5, y - .5, x + w + .5, y + h + .5), 5, "#080b0c")
    gradient_rr(image, (x + .5, y + .5, x + w - .5, y + h - .5), 4, "#5f6b67", "#333d3d")  # bevelled rim
    gradient_rr(image, (x + 1.5, y + 1.5, x + w - 1.5, y + h - 1.5), 3, "#263033", "#1d2628")
    top, bottom = ("#46585c", "#33424a") if kit == "909" else ("#5c4f37", "#443a29")
    gradient_rr(image, (x + 2, y + 2, x + w - 2, y + title), 2.5, top, bottom)
    blend(image, (x + 4, y + 2.6, x + w - 4, y + 3.4), .3, "#ffffff", 34)
    line(image, [(x + 2, y + title + .5), (x + w - 2, y + title + .5)], "#0d1112", 1)
    strip = y + h - gui.CARD_STRIP_H
    recess(image, (x + 4, strip + 2, x + w - 4, y + h - 4), 3, "#12181a", "#182022", depth=2.2, dark=120)


def typeface(style, size):
    path = subprocess.check_output(["fc-match", "-f", "%{file}", f"Roboto:style={style}"], text=True)
    if "Roboto" not in Path(path).name:
        raise SystemExit(f"the wordmark needs Roboto {style}; fontconfig offered {path}")
    return ImageFont.truetype(path, size * S)


def wordmark(image, x, y, width):
    """The panel's name as printed lettering: a heavy slanted DRUM over a spaced-out STATION."""
    heavy = typeface("Black Italic", 40)
    heavy = heavy.font_variant(size=round(heavy.size * px(width) / heavy.getlength("DRUM")))
    cap = -heavy.getbbox("DRUM", anchor="ls")[1]
    ink = Image.new("L", image.size)
    ImageDraw.Draw(ink).text((px(x), px(y) + cap), "DRUM", font=heavy, fill=255, anchor="ls")
    under = Image.new("RGBA", image.size, (0, 0, 0, 255))
    under.putalpha(ImageChops.offset(ink, px(.6), px(1.4)).point(lambda v: v * 200 // 255))
    image.alpha_composite(under)
    face = vertical((image.width, cap), "#fbfaf2", "#aeb7b0")
    image.paste(face, (0, px(y)), ink.crop((0, px(y), image.width, px(y) + cap)))
    light = typeface("Medium Italic", 14.5)
    letters = "STATION"
    gap = (px(width) - sum(light.getlength(ch) for ch in letters)) / (len(letters) - 1)
    at, baseline = px(x) - px(1.5), px(y) + cap + px(8) - light.getbbox("S", anchor="ls")[1]
    for ch in letters:
        ImageDraw.Draw(image).text((at, baseline), ch, font=light, fill="#f0d15b", anchor="ls")
        at += light.getlength(ch) + gap


def plate():
    geometry = gui.plate_geometry()
    width, height = geometry["size"]
    image = Image.new("RGBA", (px(width), px(height)), "#0b0e0f")
    x0, y0, x1, y1 = geometry["chassis"]
    shadow(image, (x0, y0, x1, y1), 8, 5, 230, dy=3)
    rr(image, (x0 - 1, y0 - 1, x1 + 1, y1 + 1), 8, "#050607")
    gradient_rr(image, (x0, y0, x1, y1), 7, "#74817c", "#1c2325")  # the chassis edge
    metal = brushed(x1 - x0 - 3, y1 - y0 - 3, "#2f3b3e", "#232d30").resize(box((x1 - x0 - 3, y1 - y0 - 3)), BICUBIC)
    mask = Image.new("L", metal.size)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, metal.width - 1, metal.height - 1), px(6), fill=255)
    metal.putalpha(mask)
    image.alpha_composite(metal, box((x0 + 1.5, y0 + 1.5)))
    for sx, sy in geometry["screws"]:
        shadow(image, (sx - 7, sy - 7, sx + 7, sy + 7), 7, 1.5, 170, dy=1.5)
        ellipse(image, sx, sy, 7.6, "#07090a")
        disc(image, sx, sy, 7, "#9aa5a0", "#3a4444")
        disc(image, sx, sy, 5.4, "#5d6866", "#7a8683")
        turn = math.radians(25 + 40 * (sx % 3))
        line(image, [(sx - math.cos(turn) * 4.6, sy - math.sin(turn) * 4.6),
                     (sx + math.cos(turn) * 4.6, sy + math.sin(turn) * 4.6)], "#101415", 1.7)
    recess(image, geometry["header"], 4, "#141b1d", "#1a2224", depth=4, dark=170)
    wordmark(image, *geometry["wordmark"])
    recess(image, geometry["status"], 3, "#090d0e", "#0d1213", depth=3, dark=170)
    lens = geometry["lens"]
    recess(image, lens, 3, "#1d0a0a", "#120606", depth=3, dark=200)
    blend(image, (lens[0] + 2, lens[1] + 2, lens[2] - 2, lens[1] + 14), 2, "#ffffff", 10)  # glass
    shapes = segment_shapes()
    for dx, dy in geometry["digits"]:
        for points in shapes.values():
            ImageDraw.Draw(image).polygon([box((dx + x, dy + y)) for x, y in points], fill="#3a1412")
    for lx, ly in geometry["mode_leds"]:
        socket(image, lx, ly, 4.5, ("#5a3b14", "#2a1a08"))
    socket(image, *geometry["engine_led"], 6, ("#3a2622", "#1a100e"))
    for kit, _title, x, y, w, h in geometry["cards"]:
        card(image, kit, x, y, w, h)
    for kx, ky, r in geometry["knobs"]:
        shadow(image, (kx - r, ky - r, kx + r, ky + r), r, r * .16 + 1, 190, dx=r * .12, dy=r * .2 + 1)
        ticks(image, kx, ky, r)
    return image.resize((width, height), LANCZOS).convert("RGB")


def sequencer_plate(height=gui.SEQUENCER_SIZE[1]):
    """Matching brushed-metal well for compact, full-pattern, and all-track views."""
    width = gui.SEQUENCER_SIZE[0]
    x0, y0, x1, y1 = 10, 10, width - 10, height - 10
    image = Image.new("RGBA", (px(width), px(height)), "#090c0d")
    shadow(image, (x0 - 1, y0 - 1, x1 + 1, y1 + 1), 8, 5, 220, dy=3)
    rr(image, (x0 - 1, y0 - 1, x1 + 1, y1 + 1), 8, "#050607")
    gradient_rr(image, (x0, y0, x1, y1), 7, "#74817c", "#1c2325")
    metal = brushed(x1 - x0 - 3, y1 - y0 - 3, "#344043", "#20292b", seed=2010).resize(
        box((x1 - x0 - 3, y1 - y0 - 3)), BICUBIC)
    mask = Image.new("L", metal.size)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, metal.width - 1, metal.height - 1), px(6), fill=255)
    metal.putalpha(mask)
    image.alpha_composite(metal, box((x0 + 1.5, y0 + 1.5)))
    for sx, sy in ((23, 23), (width - 23, 23), (23, height - 23), (width - 23, height - 23)):
        shadow(image, (sx - 6, sy - 6, sx + 6, sy + 6), 6, 1.2, 160, dy=1)
        ellipse(image, sx, sy, 6.3, "#07090a")
        disc(image, sx, sy, 5.7, "#9aa5a0", "#394344")
        line(image, [(sx - 3.4, sy - 1.2), (sx + 3.4, sy + 1.2)], "#101415", 1.4)
    recess(image, (39, 19, 1401, 71), 4, "#111719", "#20292b", depth=3, dark=160)
    recess(image, (39, 82, 1401, 139), 4, "#101617", "#1a2224", depth=3, dark=160)
    recess(image, (39, 150, 1401, 231), 4, "#111719", "#20292b", depth=3, dark=160)
    help_top = height - 87
    recess(image, (39, 242, 1401, help_top - 12), 5, "#141b1d", "#1b2325", depth=4, dark=175)
    recess(image, (39, help_top, 1401, height - 16), 4, "#101617", "#1a2224", depth=3, dark=160)
    return image.resize((width, height), LANCZOS).convert("RGB")


def sprites():
    out = {}
    for radius in sorted({r for _x, _y, r in gui.plate_geometry()["knobs"]}):
        for index, frame in enumerate(knob_frames(radius)):
            out[f"knob{radius}:{index}"] = frame
    for kind, (w, h) in gui.BUTTON_SIZES.items():
        for style in gui.BUTTON_STYLES:
            for pressed in (0, 1):
                make = grid_button if kind in ("seqcell", "seqrow") else button
                out[f"{kind}:{style}:{pressed}"] = make(w, h, style, pressed)
    out["led:amber"] = lamp(4.5, "#f2b632", "#fff3c4")
    out["led:green"] = lamp(6, "#3ddc6c", "#d9ffe2")
    out["led:red"] = lamp(6, "#c2362c", "#ff9a8c")
    for bit, points in segment_shapes().items():
        out[f"segment:{bit}"] = segment(points)
    return out


def pack(images, width=2048):
    """Shelf-pack the sprites into one atlas; returns it and each sprite's place and origin offset."""
    places, x, y, shelf = {}, 0, 0, 0
    for name, (image, dx, dy) in sorted(images.items(), key=lambda item: -item[1][0].height):
        if x + image.width > width:
            x, y, shelf = 0, y + shelf, 0
        places[name] = (x, y, image.width, image.height, dx, dy)
        x, shelf = x + image.width, max(shelf, image.height)
    atlas = Image.new("RGBA", (width, y + shelf))
    for name, (image, _dx, _dy) in images.items():
        atlas.paste(image, places[name][:2])
    return atlas, places


def main():
    gui.ASSETS.mkdir(exist_ok=True)
    plate().save(gui.ASSETS / "plate.png", optimize=True)
    sequencer_plate().save(gui.ASSETS / "sequencer.png", optimize=True)
    sequencer_plate(gui.SEQUENCER_FULL_SIZE[1]).save(gui.ASSETS / "sequencer_full.png", optimize=True)
    sequencer_plate(gui.SEQUENCER_ALL_SIZE[1]).save(gui.ASSETS / "sequencer_all.png", optimize=True)
    atlas, places = pack(sprites())
    atlas.save(gui.ASSETS / "sprites.png", optimize=True)
    (gui.ASSETS / "skin.json").write_text(json.dumps(
        {"fingerprint": gui.layout_fingerprint(), "sprites": places}, separators=(",", ":")) + "\n")
    print(f"wrote {gui.ASSETS} for layout {gui.layout_fingerprint()}")


if __name__ == "__main__":
    main()
