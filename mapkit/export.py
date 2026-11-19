"""Export a stitched map to PNG / JPEG / WebP / TIFF / BMP / GIF / PDF.

PNG is written by a small streaming encoder, so even a multi-gigapixel map can be
saved with very little RAM. The other formats need the whole image in memory, so
`validate()` checks each format's hard limits and your free RAM before we start.
"""

from __future__ import annotations

import os
import shutil
import struct
import threading
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Tuple

from PIL import Image, ImageChops

from .common import Cancelled, ProgressFn, check_cancel, fmt_bytes, sub_progress
from .stitch import Manifest, Plan, StitchStats, iter_strips, make_plan, stitch_image


@dataclass(frozen=True)
class Fmt:
    key: str
    label: str
    ext: str
    max_side: Optional[int] = None
    uses_quality: bool = False


FORMATS: Dict[str, Fmt] = {
    f.key: f
    for f in (
        Fmt("png", "PNG", ".png"),
        Fmt("jpeg", "JPEG", ".jpg", max_side=65500, uses_quality=True),
        Fmt("webp", "WebP", ".webp", max_side=16383, uses_quality=True),
        Fmt("tiff", "TIFF", ".tiff"),
        Fmt("bmp", "BMP", ".bmp"),
        Fmt("gif", "GIF", ".gif", max_side=65535),
        Fmt("pdf", "PDF", ".pdf", max_side=65500, uses_quality=True),
    )
}

GIF_MAX_PIXELS = 250_000_000
BMP_MAX_BYTES = 4_294_967_295
PDF_MAX_POINTS = 14400.0  # Acrobat's 200 inch page limit


def available_ram_bytes() -> Optional[int]:
    """Free physical memory, or None if it can't be determined."""
    try:
        if os.name == "nt":
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            ms = MEMORYSTATUSEX()
            ms.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))
            return int(ms.ullAvailPhys)
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except Exception:
        pass
    return None


def validate(keys: Iterable[str], width: int, height: int, free_ram: Optional[int] = None) -> List[str]:
    """Return human-readable reasons this export can't work (empty list = OK)."""
    keys = list(keys)
    problems: List[str] = []
    for k in keys:
        f = FORMATS[k]
        if f.max_side and max(width, height) > f.max_side:
            problems.append(f"{f.label} supports at most {f.max_side:,} px per side. Choose a lower resolution or untick it.")
    if "bmp" in keys and width * height * 3 + 54 > BMP_MAX_BYTES:
        problems.append("BMP files can't exceed 4 GB; this resolution is too large for BMP.")
    if "gif" in keys and width * height > GIF_MAX_PIXELS:
        problems.append("GIF is limited to about 250 megapixels here (palette conversion gets too heavy).")
    if any(k != "png" for k in keys) and free_ram:
        need = int(width * height * 3 * 1.3)
        if need > free_ram:
            problems.append(
                f"These formats need about {fmt_bytes(need)} of free RAM (about {fmt_bytes(free_ram)} available). "
                "Choose a lower resolution, or export PNG only, since PNG streams to disk using very little memory."
            )
    return problems


# --------------------------------------------------------------------------- PNG

def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def write_png_stream(
    path: Path,
    width: int,
    height: int,
    strips: Iterator[Tuple[int, Image.Image]],
    cancel: Optional[threading.Event] = None,
    on_rows=None,
    level: int = 4,
) -> None:
    """Encode an 8-bit RGB PNG from top-to-bottom strips without holding the image in RAM.

    Uses the PNG 'Up' filter (type 2), which compresses flat map art far better than
    unfiltered rows.
    """
    rowlen = width * 3
    comp = zlib.compressobj(level)
    written = 0
    prev_row: Optional[Image.Image] = None

    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n")
        fh.write(_png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)))

        for _y, strip in strips:
            check_cancel(cancel)
            if strip.width != width:
                raise ValueError("Strip width doesn't match PNG width.")
            h = strip.height
            # Up filter: each byte minus the byte directly above it (previous row; zeros for row 0).
            above = Image.new("RGB", (width, h), (0, 0, 0))
            if prev_row is not None:
                above.paste(prev_row, (0, 0))
            if h > 1:
                above.paste(strip.crop((0, 0, width, h - 1)), (0, 1))
            diff = ImageChops.subtract_modulo(strip, above)
            raw = diff.tobytes()
            data = b"\x02" + b"\x02".join(raw[i * rowlen:(i + 1) * rowlen] for i in range(h))
            out = comp.compress(data)
            if out:
                fh.write(_png_chunk(b"IDAT", out))
            prev_row = strip.crop((0, h - 1, width, h))
            written += h
            if on_rows:
                on_rows(written / height)

        if written != height:
            raise ValueError(f"PNG stream ended after {written} of {height} rows.")
        tail = comp.flush()
        if tail:
            fh.write(_png_chunk(b"IDAT", tail))
        fh.write(_png_chunk(b"IEND", b""))


def _canvas_strips(canvas: Image.Image, rows: int = 512) -> Iterator[Tuple[int, Image.Image]]:
    for y in range(0, canvas.height, rows):
        yield y, canvas.crop((0, y, canvas.width, min(canvas.height, y + rows)))


# ------------------------------------------------------------------------ others

def _save_pillow(key: str, canvas: Image.Image, path: Path, quality: int) -> None:
    if key == "jpeg":
        canvas.save(path, "JPEG", quality=quality, subsampling=0)
    elif key == "webp":
        canvas.save(path, "WEBP", quality=quality, lossless=quality >= 100, method=4)
    elif key == "tiff":
        canvas.save(path, "TIFF", compression="tiff_adobe_deflate")
    elif key == "bmp":
        canvas.save(path, "BMP")
    elif key == "gif":
        canvas.quantize(256, method=Image.Quantize.FASTOCTREE).save(path, "GIF")
    elif key == "pdf":
        # Keep the page within PDF's 200 inch limit; resolution only changes the page size.
        res = max(72.0, max(canvas.size) * 72.0 / PDF_MAX_POINTS)
        canvas.save(path, "PDF", resolution=res, quality=quality)
    else:
        raise ValueError(f"Unknown format: {key}")


@dataclass
class ExportResult:
    files: List[Path] = field(default_factory=list)
    width: int = 0
    height: int = 0
    missing: int = 0
    corrupt: int = 0
    raw_deleted: bool = False
    seconds: float = 0.0


def output_name(base: str, z: int, scale: float, ext: str) -> str:
    suffix = f"_{int(round(scale * 100))}pct" if abs(scale - 1.0) > 1e-6 else ""
    return f"{base}_z{z}{suffix}{ext}"


def delete_raw_tiles(m: Manifest, out_dir: Path) -> None:
    """Remove the textures folder and the leftover *_map.zip (never manifest/routes/exports)."""
    tex = m.tex_dir.resolve()
    folder = m.folder.resolve()
    out = Path(out_dir).resolve()
    if tex == folder or tex.parent != folder:
        raise RuntimeError("Refusing to delete: textures folder isn't directly inside the map folder.")
    if tex == out or tex in out.parents:
        raise RuntimeError("Refusing to delete: the output folder is inside the textures folder.")
    shutil.rmtree(tex)
    for z in folder.glob("*_map.zip"):
        try:
            z.unlink()
        except OSError:
            pass


def export_map(
    m: Manifest,
    out_dir: Path,
    formats: List[str],
    z: int,
    scale: float = 1.0,
    trim: bool = True,
    quality: int = 90,
    progress: ProgressFn = None,
    cancel: Optional[threading.Event] = None,
    delete_raw: bool = False,
) -> ExportResult:
    if not formats:
        raise ValueError("Pick at least one export format.")
    plan = make_plan(m, z, scale, trim)
    problems = validate(formats, plan.width, plan.height, available_ram_bytes())
    if problems:
        raise RuntimeError("\n".join(problems))

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    base = m.folder.name
    started = time.time()
    stats = StitchStats()
    result = ExportResult(width=plan.width, height=plan.height)

    def target(key: str) -> Path:
        return out_dir / output_name(base, z, scale, FORMATS[key].ext)

    def guarded(path: Path, fn) -> None:
        """Run a writer; never leave a half-written file behind."""
        try:
            fn()
        except BaseException:
            try:
                path.unlink()
            except OSError:
                pass
            raise
        result.files.append(path)

    if formats == ["png"]:
        # Streaming path: tiles -> PNG on disk, no full canvas in memory.
        path = target("png")
        stitch_p = sub_progress(progress, 0.0, 1.0)

        def run():
            strips = iter_strips(m, plan, None, cancel, stats)
            write_png_stream(
                path, plan.width, plan.height, strips, cancel,
                on_rows=lambda f: stitch_p(f, f"Stitching and writing PNG… {f * 100:.0f}%") if stitch_p else None,
            )

        guarded(path, run)
    else:
        canvas = stitch_image(m, plan, sub_progress(progress, 0.0, 0.6), cancel, stats)
        n = len(formats)
        for i, key in enumerate(formats):
            check_cancel(cancel)
            lo = 0.6 + 0.4 * i / n
            hi = 0.6 + 0.4 * (i + 1) / n
            label = FORMATS[key].label
            if progress:
                progress(lo, f"Writing {label}…")
            path = target(key)
            if key == "png":
                p = sub_progress(progress, lo, hi)
                guarded(path, lambda: write_png_stream(
                    path, plan.width, plan.height, _canvas_strips(canvas), cancel,
                    on_rows=lambda f: p(f, f"Writing PNG… {f * 100:.0f}%") if p else None,
                ))
            else:
                guarded(path, lambda key=key, path=path: _save_pillow(key, canvas, path, int(quality)))
        del canvas

    result.missing = stats.missing
    result.corrupt = stats.corrupt

    if delete_raw:
        if progress:
            progress(0.99, "Deleting raw tiles…")
        delete_raw_tiles(m, out_dir)
        result.raw_deleted = True

    result.seconds = time.time() - started
    if progress:
        progress(1.0, "Export complete")
    return result
