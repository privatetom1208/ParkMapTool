"""Read a downloaded map bundle and stitch its tiles into one image.

Bundle layout (from manifest.json):
  extent  - tile grid at zoom `extent.zoom`: left/top (first tile x/y), width/height (tile counts)
  textures.path - e.g. "textures/{z}.{x}.{y}.webp", tiles are textures.size px square
Tiles at lower zoom levels (z-1) cover 2x2 tiles of level z, so any level present on
disk can be used to build a smaller version of the same map.

Memory model: tiles are decoded one tile-row at a time (`iter_strips`), so callers can
either paste strips into a canvas or stream them straight to disk.
"""

from __future__ import annotations

import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

from PIL import Image

from .common import Cancelled, ProgressFn, check_cancel  # noqa: F401  (Cancelled re-exported)

RESAMPLE = Image.Resampling.LANCZOS
_TILE_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)\.[A-Za-z0-9]+$")
_SKIP = object()


@dataclass
class Manifest:
    folder: Path
    tex_dir: Path
    base_zoom: int          # zoom level the extent is expressed in
    min_zoom: int
    max_zoom: int
    left: int
    top: int
    width: int              # in tiles at base_zoom
    height: int
    tile_size: int
    bg: Tuple[int, int, int]
    levels: Dict[int, Dict[Tuple[int, int], str]] = field(default_factory=dict)

    @classmethod
    def load(cls, folder) -> "Manifest":
        folder = Path(folder)
        mf = folder / "manifest.json"
        if not mf.is_file():
            raise FileNotFoundError(f"No manifest.json found in:\n{folder}")
        try:
            data = json.loads(mf.read_text(encoding="utf-8"))
            ext = data["extent"]
            base_zoom = int(ext.get("zoom", ext.get("maxZoom")))
            left, top = int(ext["left"]), int(ext["top"])
            width, height = int(ext["width"]), int(ext["height"])
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError(f"manifest.json isn't in the expected format ({exc!r}).") from exc

        tex = data.get("textures") or {}
        tex_rel = Path(str(tex.get("path", "textures/{z}.{x}.{y}.webp"))).parent
        tex_dir = folder / tex_rel
        tile_size = int(tex.get("size", 512))

        bg_hex = str(tex.get("backgroundColor", "ffffff")).lstrip("#")
        try:
            bg = (int(bg_hex[0:2], 16), int(bg_hex[2:4], 16), int(bg_hex[4:6], 16))
        except ValueError:
            bg = (255, 255, 255)

        levels: Dict[int, Dict[Tuple[int, int], str]] = {}
        if tex_dir.is_dir():
            with os.scandir(tex_dir) as it:
                for entry in it:
                    mt = _TILE_RE.match(entry.name)
                    if mt and entry.is_file():
                        z, x, y = (int(g) for g in mt.groups())
                        if z <= base_zoom:
                            levels.setdefault(z, {})[(x, y)] = entry.path
        if not levels:
            raise FileNotFoundError(
                f"No tile files (like 20.307516.396474.webp) were found in:\n{tex_dir}\n"
                "If you deleted the raw tiles after an earlier export, download the map again."
            )

        return cls(
            folder=folder,
            tex_dir=tex_dir,
            base_zoom=base_zoom,
            min_zoom=int(ext.get("minZoom", min(levels))),
            max_zoom=int(ext.get("maxZoom", base_zoom)),
            left=left,
            top=top,
            width=width,
            height=height,
            tile_size=tile_size,
            bg=bg,
            levels=levels,
        )

    def available_levels(self) -> List[int]:
        return sorted(self.levels)

    def tile_count(self) -> int:
        return sum(len(v) for v in self.levels.values())


@dataclass(frozen=True)
class Plan:
    """A concrete stitch job: which level, which pixel window, what scale."""

    z: int
    tile_size: int
    gx0: int   # window in global pixels of level z (scale 1)
    gy0: int
    gx1: int
    gy1: int
    scale: float = 1.0

    def S(self, g: int) -> int:
        return int(round(g * self.scale))

    @property
    def width(self) -> int:
        return max(1, self.S(self.gx1) - self.S(self.gx0))

    @property
    def height(self) -> int:
        return max(1, self.S(self.gy1) - self.S(self.gy0))

    @property
    def tile_range(self) -> Tuple[int, int, int, int]:
        ts = self.tile_size
        return (self.gx0 // ts, -(-self.gx1 // ts), self.gy0 // ts, -(-self.gy1 // ts))


def make_plan(m: Manifest, z: int, scale: float = 1.0, trim: bool = True) -> Plan:
    if z not in m.levels:
        raise ValueError(f"Zoom level {z} has no tiles in this bundle.")
    s = 2 ** (m.base_zoom - z)
    ts = m.tile_size
    if trim:
        gx0 = int(round(m.left / s * ts))
        gy0 = int(round(m.top / s * ts))
        gx1 = int(round((m.left + m.width) / s * ts))
        gy1 = int(round((m.top + m.height) / s * ts))
    else:
        tx0, ty0 = m.left // s, m.top // s
        tx1, ty1 = -(-(m.left + m.width) // s), -(-(m.top + m.height) // s)
        gx0, gy0, gx1, gy1 = tx0 * ts, ty0 * ts, tx1 * ts, ty1 * ts
    if gx1 <= gx0:
        gx1 = gx0 + 1
    if gy1 <= gy0:
        gy1 = gy0 + 1
    return Plan(z=z, tile_size=ts, gx0=gx0, gy0=gy0, gx1=gx1, gy1=gy1, scale=float(scale))


def describe_levels(m: Manifest, trim: bool = True) -> List[dict]:
    out = []
    for z in m.available_levels():
        p = make_plan(m, z, 1.0, trim)
        out.append({"z": z, "width": p.width, "height": p.height, "tiles": len(m.levels[z])})
    return out


@dataclass
class StitchStats:
    drawn: int = 0
    missing: int = 0   # grid positions with no tile file (left as background)
    corrupt: int = 0   # files that couldn't be decoded


def load_tile(path: str, bg: Tuple[int, int, int]) -> Optional[Image.Image]:
    """Decode a tile to RGB, flattening any transparency onto the map background."""
    try:
        with Image.open(path) as im:
            im.load()
            if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
                rgba = im.convert("RGBA")
                base = Image.new("RGB", rgba.size, bg)
                base.paste(rgba, mask=rgba.getchannel("A"))
                return base
            return im.convert("RGB")
    except Exception:
        return None


def _scaled_tile(m: Manifest, plan: Plan, tx: int, ty: int, path: str):
    """Decode one tile and size it to exactly its slot in the (scaled) output.

    Returns an RGB image, None if the file is unreadable, or _SKIP if the slot is empty.
    """
    S, ts = plan.S, plan.tile_size
    w = S((tx + 1) * ts) - S(tx * ts)
    h = S((ty + 1) * ts) - S(ty * ts)
    if w <= 0 or h <= 0:
        return _SKIP
    img = load_tile(path, m.bg)
    if img is None:
        return None
    if img.size != (w, h):
        img = img.resize((w, h), RESAMPLE)
    return img


def iter_strips(
    m: Manifest,
    plan: Plan,
    progress: ProgressFn = None,
    cancel: Optional[threading.Event] = None,
    stats: Optional[StitchStats] = None,
    workers: Optional[int] = None,
) -> Iterator[Tuple[int, Image.Image]]:
    """Yield (y_offset, strip) top to bottom; strips are full output width, RGB.

    Each strip is one row of tiles (clipped to the plan's window). Peak memory is a
    single strip regardless of how large the final image is.
    """
    idx = m.levels[plan.z]
    ts = plan.tile_size
    S = plan.S
    ox, oy = S(plan.gx0), S(plan.gy0)
    tx0, tx1, ty0, ty1 = plan.tile_range
    rows = max(1, ty1 - ty0)
    workers = workers or min(8, os.cpu_count() or 2)

    def work(job):
        tx, ty, path = job
        return tx, _scaled_tile(m, plan, tx, ty, path)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for n, ty in enumerate(range(ty0, ty1)):
            check_cancel(cancel)
            if progress:
                progress(n / rows, f"Stitching tile row {n + 1} of {rows}")
            top = S(max(ty * ts, plan.gy0))
            bot = S(min((ty + 1) * ts, plan.gy1))
            if bot - top <= 0:
                continue
            strip = Image.new("RGB", (plan.width, bot - top), m.bg)

            jobs = []
            for tx in range(tx0, tx1):
                path = idx.get((tx, ty))
                if path is None:
                    if stats:
                        stats.missing += 1
                else:
                    jobs.append((tx, ty, path))

            for tx, img in pool.map(work, jobs):
                if img is _SKIP:
                    continue
                if img is None:
                    if stats:
                        stats.corrupt += 1
                    continue
                strip.paste(img, (S(tx * ts) - ox, S(ty * ts) - top))
                if stats:
                    stats.drawn += 1
            yield top - oy, strip

    if progress:
        progress(1.0, "Stitching complete")


def stitch_image(
    m: Manifest,
    plan: Plan,
    progress: ProgressFn = None,
    cancel: Optional[threading.Event] = None,
    stats: Optional[StitchStats] = None,
) -> Image.Image:
    """Stitch the whole plan into one in-memory RGB image."""
    canvas = Image.new("RGB", (plan.width, plan.height), m.bg)
    for y, strip in iter_strips(m, plan, progress, cancel, stats):
        canvas.paste(strip, (0, y))
    return canvas


def make_preview(
    m: Manifest,
    target: int = 1400,
    trim: bool = True,
    progress: ProgressFn = None,
    cancel: Optional[threading.Event] = None,
) -> Image.Image:
    """Cheap overview image: the smallest level that reaches ~target px, scaled to fit."""
    levels = m.available_levels()
    chosen = levels[-1]
    for z in levels:
        p = make_plan(m, z, 1.0, trim)
        if max(p.width, p.height) >= target:
            chosen = z
            break
    p = make_plan(m, chosen, 1.0, trim)
    scale = min(1.0, target / max(p.width, p.height))
    return stitch_image(m, make_plan(m, chosen, scale, trim), progress, cancel)


@dataclass
class Overview:
    """Whole-map overview, rendered once untrimmed so the trim toggle can crop it instantly."""

    image: Image.Image                     # whole tile grid (untrimmed)
    trim_box: Tuple[int, int, int, int]    # (left, top, right, bottom) crop that equals the trimmed view
    z: int
    scale: float

    def view(self, trim: bool) -> Image.Image:
        return self.image.crop(self.trim_box) if trim else self.image


def make_overview(
    m: Manifest,
    target: int = 1400,
    progress: ProgressFn = None,
    cancel: Optional[threading.Event] = None,
) -> Overview:
    levels = m.available_levels()
    chosen = levels[-1]
    for z in levels:
        p = make_plan(m, z, 1.0, False)
        if max(p.width, p.height) >= target:
            chosen = z
            break
    full = make_plan(m, chosen, 1.0, False)
    scale = min(1.0, target / max(full.width, full.height))
    uplan = make_plan(m, chosen, scale, False)
    tplan = make_plan(m, chosen, scale, True)
    img = stitch_image(m, uplan, progress, cancel)
    left = max(0, tplan.S(tplan.gx0) - uplan.S(uplan.gx0))
    top = max(0, tplan.S(tplan.gy0) - uplan.S(uplan.gy0))
    box = (left, top, min(img.width, left + tplan.width), min(img.height, top + tplan.height))
    return Overview(image=img, trim_box=box, z=chosen, scale=scale)


def render_region(m: Manifest, plan: Plan, x: int, y: int, w: int, h: int) -> Image.Image:
    """Render just the output-space window [x, x+w) x [y, y+h) of a plan (pixel-identical to
    cropping the full stitched image, but only decodes the tiles it needs)."""
    ts, S = plan.tile_size, plan.S
    ox, oy = S(plan.gx0), S(plan.gy0)
    idx = m.levels[plan.z]
    out = Image.new("RGB", (w, h), m.bg)
    ptx0, ptx1, pty0, pty1 = plan.tile_range
    gx_lo, gx_hi = (x + ox) / plan.scale, (x + w + ox) / plan.scale
    gy_lo, gy_hi = (y + oy) / plan.scale, (y + h + oy) / plan.scale
    for ty in range(max(pty0, int(gy_lo // ts) - 1), min(pty1, int(gy_hi // ts) + 2)):
        dy0, dy1 = S(ty * ts) - oy, S((ty + 1) * ts) - oy
        if dy1 <= y or dy0 >= y + h:
            continue
        for tx in range(max(ptx0, int(gx_lo // ts) - 1), min(ptx1, int(gx_hi // ts) + 2)):
            dx0, dx1 = S(tx * ts) - ox, S((tx + 1) * ts) - ox
            if dx1 <= x or dx0 >= x + w:
                continue
            path = idx.get((tx, ty))
            if path is None:
                continue
            tile = _scaled_tile(m, plan, tx, ty, path)
            if tile is None or tile is _SKIP:
                continue
            out.paste(tile, (dx0 - x, dy0 - y))
    return out


def closeup_center(m: Manifest, plan: Plan) -> Tuple[int, int]:
    """A representative spot (output pixels): among the tiles nearest the middle of the map,
    the one with the most detail (largest file), so the close-up isn't an empty patch."""
    tx0, tx1, ty0, ty1 = plan.tile_range
    S, ts = plan.S, plan.tile_size
    ox, oy = S(plan.gx0), S(plan.gy0)
    pts = [k for k in m.levels[plan.z] if tx0 <= k[0] < tx1 and ty0 <= k[1] < ty1]
    if not pts:
        return plan.width // 2, plan.height // 2
    cx = sum(p[0] for p in pts) / len(pts)
    cy = sum(p[1] for p in pts) / len(pts)
    near = sorted(pts, key=lambda p: (p[0] - cx) ** 2 + (p[1] - cy) ** 2)[:30]

    def size(p):
        try:
            return os.path.getsize(m.levels[plan.z][p])
        except OSError:
            return 0

    tx, ty = max(near, key=size)
    return (S(tx * ts) + S((tx + 1) * ts)) // 2 - ox, (S(ty * ts) + S((ty + 1) * ts)) // 2 - oy


def render_closeup(m: Manifest, plan: Plan, w: int, h: int) -> Image.Image:
    """A w x h crop of the real output pixels for this plan, taken near the middle of the map."""
    w, h = min(w, plan.width), min(h, plan.height)
    cx, cy = closeup_center(m, plan)
    x = max(0, min(plan.width - w, cx - w // 2))
    y = max(0, min(plan.height - h, cy - h // 2))
    return render_region(m, plan, x, y, w, h)
