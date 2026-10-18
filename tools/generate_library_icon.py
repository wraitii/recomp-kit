import argparse
import math
from pathlib import Path
import random
import xml.etree.ElementTree as ET


SVG = "http://www.w3.org/2000/svg"
ICON = Path(__file__).resolve().parents[1] / "desktop/library/icons/icon.svg"
FLOW_START = (168, 333)
FLOW_DELTA = (189, -139)
WINDOW_TRANSFORMS = ((1.196, -16.024, -61.48), (1.4605, -172.05625, -50.90875))
FLOW_LENGTH = math.hypot(*FLOW_DELTA)
FLOW_AXIS = tuple(value / FLOW_LENGTH for value in FLOW_DELTA)
ET.register_namespace("", SVG)


def element(name, **attributes):
    return ET.Element(f"{{{SVG}}}{name}", {k.replace("_", "-"): str(v) for k, v in attributes.items()})


def intersects(box, occupied, gap=3):
    x, y, width, height = box
    return any(x < ox + ow + gap and x + width + gap > ox
               and y < oy + oh + gap and y + height + gap > oy
               for ox, oy, ow, oh in occupied)


def progress(x, y, modern=False):
    scale, dx, dy = WINDOW_TRANSFORMS[modern]
    x, y = x * scale + dx, y * scale + dy
    along = sum((value - start) * axis for value, start, axis in zip((x, y), FLOW_START, FLOW_AXIS))
    distance = FLOW_LENGTH + 10 - along if modern else along + 62
    return max(0, min(1, distance / (145 if modern else 160)))


def mask(name, bounds, rng, modern=False, step=16):
    x0, y0, width, height = bounds
    result = element("mask", id=name, maskUnits="userSpaceOnUse", x=x0, y=y0,
                     width=width, height=height)
    occupied = []
    fragments = []
    scale, dx, dy = WINDOW_TRANSFORMS[modern]
    for y in range(y0, y0 + height, step):
        for x in range(x0, x0 + width, step):
            t = progress(x + step / 2, y + step / 2, modern)
            eased = t * t * (3 - 2 * t)
            if rng.random() < .5 * t ** 3:
                continue
            size = (16.5 - 5.5 * eased) * step / 16
            px = x + (step - size) / 2
            py = y + (step - size) / 2
            if t > .06:
                drift = 48 * t ** 3
                direction = -1 if modern else 1
                fragments.append((px, py, size, drift, direction, scale, dx, dy, modern))
                continue
            result.append(element("rect", x=f"{px:.2f}", y=f"{py:.2f}",
                                  width=f"{size:.2f}", height=f"{size:.2f}",
                                  fill="white", opacity=f"{1 - .18 * eased:.2f}"))
            occupied.append((px * scale + dx, py * scale + dy, size * scale, size * scale))
    return result, occupied, fragments


def moving_fragments(fragments, occupied, max_drift=48):
    result = element("g", id="moving-fragments")
    for x, y, size, drift, direction, scale, dx, dy, modern in sorted(fragments, key=lambda f: f[3]):
        if drift > max_drift:
            continue
        rendered_size = size * scale * (1 - .35 * drift / 48)
        for extra in (0, 1, 2, 4, 7):
            px = x * scale + dx + direction * (drift + extra) * FLOW_AXIS[0]
            py = y * scale + dy + direction * (drift + extra) * FLOW_AXIS[1]
            box = (px, py, rendered_size, rendered_size)
            if drift > 2 and intersects(box, occupied, gap=-.6 + 2 * drift / 48):
                continue
            tile = element("svg", x=f"{px:.2f}", y=f"{py:.2f}",
                           width=f"{rendered_size:.2f}", height=f"{rendered_size:.2f}",
                           viewBox=f"{x:.2f} {y:.2f} {size:.2f} {size:.2f}", overflow="hidden")
            tile.append(element("use", href="#modern-art" if modern else "#legacy-art"))
            result.append(tile)
            occupied.append(box)
            break
    return result


def particles(rng, density, occupied, offsets, width):
    result = element("g", id="particle-flow")
    accepted = []
    colors = ((184, 190, 198), (110, 181, 246), (242, 250, 255))
    start = tuple(a + b for a, b in zip(FLOW_START, offsets[0]))
    delta = tuple(a + b - c for a, b, c in zip(FLOW_DELTA, offsets[1], offsets[0]))
    axis = tuple(value / math.hypot(*delta) for value in delta)

    def draw(x, y, size, t, opacity):
        shade = max(0, min(1, (t - .2) / .45)) * 2
        index = min(1, int(shade))
        blend = shade - index
        color = "#" + "".join(f"{round(a + (b - a) * blend):02x}" for a, b in zip(colors[index], colors[index + 1]))
        return element("rect", x=f"{x - size / 2:.2f}", y=f"{y - size / 2:.2f}",
                       width=f"{size:.2f}", height=f"{size:.2f}",
                       rx=".6" if t > .5 else "0", fill=color, opacity=f"{opacity:.2f}")

    if density:
        for t, lane in ((.25, -12), (.3, 12), (.65, 0)):
            if len(accepted) >= density:
                break
            x = start[0] + delta[0] * t - lane * axis[1]
            y = start[1] + delta[1] * t + lane * axis[0]
            size = 13.26
            box = (x - size / 2, y - size / 2, size, size)
            if not intersects(box, occupied + accepted):
                accepted.append(box)
                result.append(draw(x, y, size, t, .95))
    candidates = []
    for column, row in ((column, row) for column in range(16) for row in range(12)):
        phase = (column + rng.uniform(.35, .65)) / 16
        t = .25 + .5 * phase
        waist = .33
        if phase < waist:
            flare = .28 + .27 * ((waist - phase) / waist) ** 1.5
        else:
            flare = .28 + .97 * ((phase - waist) / (1 - waist)) ** 1.5
        lane = ((row + rng.uniform(.35, .65)) / 12 - .5) * width * flare
        x = start[0] + delta[0] * t - lane * axis[1]
        y = start[1] + delta[1] * t + lane * axis[0]
        size = 1.872 * (rng.uniform(7, 10) + 5 * abs(2 * t - 1) ** 1.4) * (.85 + .15 * phase)
        if not intersects((x - size / 2, y - size / 2, size, size), occupied):
            candidates.append((x, y, size, t))
    while candidates and len(accepted) < density:
        def clearance(candidate):
            x, y, size, _ = candidate
            return min((math.hypot(x - ox - ow / 2, y - oy - oh / 2) - (size + ow) / 2
                        for ox, oy, ow, oh in accepted), default=0)

        chosen = max(candidates, key=clearance)
        candidates.remove(chosen)
        x, y, size, t = chosen
        box = (x - size / 2, y - size / 2, size, size)
        if not intersects(box, occupied + accepted):
            accepted.append(box)
            result.append(draw(x, y, size, t, .95))
    return result


def main():
    parser = argparse.ArgumentParser(description="Regenerate the library SVG's gradual binary breakup and portal particles.")
    parser.add_argument("--seed", type=int, default=27)
    parser.add_argument("--density", type=int, default=50)
    parser.add_argument("--fragment-size", type=int, default=16)
    parser.add_argument("--max-drift", type=float, default=48)
    parser.add_argument("--corridor-width", type=float, default=145)
    parser.add_argument("--legacy-offset", type=float, nargs=2, default=(0, 0), metavar=("X", "Y"))
    parser.add_argument("--modern-offset", type=float, nargs=2, default=(0, 0), metavar=("X", "Y"))
    parser.add_argument("--output", type=Path, default=ICON)
    args = parser.parse_args()
    if not 0 <= args.density <= 250:
        parser.error("--density must be between 0 and 250")
    if not 4 <= args.fragment_size <= 64:
        parser.error("--fragment-size must be between 4 and 64")
    if not 0 <= args.max_drift <= 48 or not 0 < args.corridor_width <= 200:
        parser.error("--max-drift must be between 0 and 48; --corridor-width must be between 0 and 200")
    root = ET.parse(ICON).getroot()
    defs = root.find(f"{{{SVG}}}defs")
    occupied = []
    moving = []
    offsets = (args.legacy_offset, args.modern_offset)
    for name, bounds, modern in (("legacy-shape", (76, 263, 156, 134), False),
                                 ("modern-shape", (283, 91, 159, 153), True)):
        scale, dx, dy = WINDOW_TRANSFORMS[modern]
        ox, oy = offsets[modern]
        window = next(child for child in root if any(part.get("mask") == f"url(#{name})" for part in child))
        window.set("transform", f"translate({dx + ox:g} {dy + oy:g}) scale({scale})")
        old = next(child for child in defs if child.get("id") == name)
        index = list(defs).index(old)
        defs.remove(old)
        art_id = "modern-art" if modern else "legacy-art"
        if not any(child.get("id") == art_id for child in defs):
            source = next(child for child in root.iter() if child.get("mask") == f"url(#{name})")
            art = element("g", id=art_id)
            art.extend(list(source))
            source.clear()
            source.set("mask", f"url(#{name})")
            source.append(element("use", href=f"#{art_id}"))
            defs.append(art)
        replacement, fragments, detached = mask(name, bounds, random.Random(args.seed + modern), modern,
                                                args.fragment_size if modern else round(args.fragment_size * .83))
        defs.insert(index, replacement)
        occupied.extend((x + ox, y + oy, width, height) for x, y, width, height in fragments)
        moving.extend((*part[:6], part[6] + ox, part[7] + oy, part[8]) for part in detached)
    for child in list(root):
        if child.get("id") == "moving-fragments":
            root.remove(child)
    front = next(child for child in root if child.get("id") == "portal-front")
    root.insert(list(root).index(front), moving_fragments(moving, occupied, args.max_drift))
    old = next(child for child in root if child.get("id") == "particle-flow")
    root.remove(old)
    root.insert(list(root).index(front),
                particles(random.Random(args.seed + 2), args.density, occupied, offsets, args.corridor_width))
    sparkle = next(child for child in root if child.get("d") == "M381 65V81M373 73H389")
    modern_top = 91 * WINDOW_TRANSFORMS[1][0] + WINDOW_TRANSFORMS[1][2] + args.modern_offset[1]
    sparkle.set("transform", f"translate(0 {min(0, modern_top - 93):.2f})")
    ET.indent(root, space="  ")
    args.output.write_text(ET.tostring(root, encoding="unicode") + "\n")
    print(args.output)


if __name__ == "__main__":
    main()
