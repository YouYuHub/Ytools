"""Trace the current transparent PNG into the sidebar's vector logo."""

from collections import defaultdict
from pathlib import Path

from PIL import Image


HERE = Path(__file__).resolve().parent
SOURCE = HERE / "favicon-squid.png"
TARGET = HERE / "favicon-squid.svg"


def components(mask, min_size=1):
    height = len(mask)
    width = len(mask[0])
    seen = set()
    kept = [[False] * width for _ in range(height)]
    for y in range(height):
        for x in range(width):
            if not mask[y][x] or (x, y) in seen:
                continue
            stack = [(x, y)]
            seen.add((x, y))
            pixels = []
            while stack:
                px, py = stack.pop()
                pixels.append((px, py))
                for nx, ny in ((px - 1, py), (px + 1, py), (px, py - 1), (px, py + 1)):
                    if (0 <= nx < width and 0 <= ny < height and mask[ny][nx]
                            and (nx, ny) not in seen):
                        seen.add((nx, ny))
                        stack.append((nx, ny))
            if len(pixels) >= min_size:
                for px, py in pixels:
                    kept[py][px] = True
    return kept


def trace(mask):
    height = len(mask)
    width = len(mask[0])
    edges = defaultdict(list)

    def filled(x, y):
        return 0 <= x < width and 0 <= y < height and mask[y][x]

    for y in range(height):
        for x in range(width):
            if not mask[y][x]:
                continue
            if not filled(x, y - 1):
                edges[x, y].append((x + 1, y))
            if not filled(x + 1, y):
                edges[x + 1, y].append((x + 1, y + 1))
            if not filled(x, y + 1):
                edges[x + 1, y + 1].append((x, y + 1))
            if not filled(x - 1, y):
                edges[x, y + 1].append((x, y))

    def direction(a, b):
        dx, dy = b[0] - a[0], b[1] - a[1]
        return {(1, 0): 0, (0, 1): 1, (-1, 0): 2, (0, -1): 3}[dx, dy]

    loops = []
    while edges:
        start = next(iter(edges))
        current = start
        previous = None
        loop = [start]
        while True:
            choices = edges[current]
            if previous is None or len(choices) == 1:
                nxt = choices.pop()
            else:
                incoming = direction(previous, current)
                nxt = min(choices, key=lambda point: [1, 0, 3, 2].index(
                    (direction(current, point) - incoming) % 4))
                choices.remove(nxt)
            if not choices:
                del edges[current]
            previous, current = current, nxt
            if current == start:
                break
            loop.append(current)
        if len(loop) >= 4:
            loops.append(loop)
    return loops


def simplify(loop):
    # Remove pixel-grid vertices that lie on a straight segment.
    result = []
    for i, point in enumerate(loop):
        before = loop[i - 1]
        after = loop[(i + 1) % len(loop)]
        if ((point[0] - before[0]) * (after[1] - point[1])
                != (point[1] - before[1]) * (after[0] - point[0])):
            result.append(point)
    return result


def path(mask):
    parts = []
    for loop in trace(mask):
        points = simplify(loop)
        if len(points) >= 3:
            parts.append("M" + " L".join(f"{x} {y}" for x, y in points) + "Z")
    return " ".join(parts)


image = Image.open(SOURCE).convert("RGBA")
width, height = image.size
pixel = image.load()


def build(test):
    return [[test(x, y, *pixel[x, y]) for x in range(width)] for y in range(height)]


blue = build(lambda x, y, r, g, b, a: a > 128 and y < 145
             and (x < 112 or x > 144) and b > g * 1.25 and b > r * 1.25)
green = build(lambda x, y, r, g, b, a: a > 128 and x > 180 and y < 80
              and g > r * 1.5 and g > b * 1.4)
body = build(lambda x, y, r, g, b, a: a > 128 and not blue[y][x] and not green[y][x])
cream = build(lambda x, y, r, g, b, a: a > 170 and r > 235 and g > 205
              and 145 < b < 210)
highlight = build(lambda x, y, r, g, b, a: a > 170 and r > 245
                  and 185 < g < 225 and b > 200)
face = build(lambda x, y, r, g, b, a: a > 100 and 108 <= x <= 143
             and 120 <= y <= 151 and r < 125 and g < 155 and b < 165)

layers = (
    ("#3e77c2", components(blue, 8)),
    ("#ee80b1", components(body, 8)),
    ("#1bba43", components(green, 8)),
    ("#ffd3e7", components(highlight, 5)),
    ("#ffe4b4", components(cream, 5)),
    ("#253548", components(face, 2)),
)
svg = [
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256" role="img" aria-label="Ytools 鱿鱼工具图标">',
]
for color, mask in layers:
    d = path(mask)
    if d:
        svg.append(f'  <path fill="{color}" fill-rule="evenodd" d="{d}"/>')
svg.append("</svg>")
TARGET.write_text("\n".join(svg) + "\n", encoding="utf-8")
print(f"Wrote {TARGET.name} ({TARGET.stat().st_size} bytes)")
