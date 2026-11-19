"""Park Map Tool - download, stitch and export Six Flags / Cedar Fair park maps."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk
from PIL import Image

from mapkit import api, export, stitch
from mapkit.common import Cancelled, fmt_bytes
from mapkit.settings import load_settings, save_settings

# ---- look -------------------------------------------------------------------
BG = "#0f131a"
PANEL = "#151a23"
CARD = "#1b222d"
CARD_HOVER = "#263040"
BORDER = "#2a3342"
TEXT = "#e8edf4"
MUTED = "#8a95a7"
ACCENT = "#3b82f6"
ACCENT_HOVER = "#2f6fd6"
DANGER = "#b42318"
DANGER_HOVER = "#912018"
WARN = "#f5a524"
GOOD = "#34d399"

PREVIEW_PX = 1800            # resolution of the generated overview
INFO_WRAP = 320              # text width of the info pop-ups
DISABLED_TEXT = "#4b5565"    # greyed-out labels
SLIDER_OFF_KNOB = "#3a4352"
SLIDER_OFF_BAR = "#2f3846"
PICK = "Select a downloaded map…"
NO_MAPS = "No downloaded maps yet"
SCALE_STEPS = [1.0, 0.75, 0.5, 0.25]
MAX_COMMON_SIDE = 16383      # largest side every common format (WebP) can hold

FORMAT_HINTS = {
    "png": "PNG  ·  lossless",
    "jpeg": "JPEG  ·  small",
    "webp": "WebP  ·  small",
    "tiff": "TIFF  ·  lossless",
    "bmp": "BMP  ·  raw",
    "gif": "GIF  ·  256 colors",
    "pdf": "PDF  ·  one page",
}

# ---- help text (shown beside the preview when an (i) button is clicked) -----------------
HELP = {
    "source": (
        "Map",
        "Choose one of the maps you've downloaded, or use Browse… to open a map folder from somewhere else.\n\n"
        "A map folder is the one that contains manifest.json and a textures folder.",
    ),
    "zoom": (
        "Zoom level",
        "Maps are stored as a stack of zoom levels, like zooming in and out in a map app. The highest "
        "zoom is the closest and sharpest, and also the biggest. Each step down has half the width and "
        "half the height (a quarter of the pixels).\n\n"
        "Higher zoom = sharper picture, but bigger files, slower exports and more memory. Some file "
        "types can't hold the very largest sizes.\n\n"
        "The Close-up panel on the right shows exactly how sharp your choice will be.",
    ),
    "scale": (
        "Scale",
        "Scale shrinks the picture by a percentage after the zoom level is chosen. 50% makes it half as "
        "wide and half as tall.\n\n"
        "Zoom levels only come in steps of 2x, so use Scale when you want a size in between. Scale can "
        "only make the image smaller; it can't add detail.\n\n"
        "Final size = zoom level size x scale.",
    ),
    "trim": (
        "Crop to map area",
        "Maps are built from square tiles, and the edge of the tile grid doesn't always line up with the "
        "map's real edge. This cuts the picture to the area the map data says it covers, instead of "
        "keeping whole tiles.\n\n"
        "At the sharpest zoom it makes no difference. At lower zooms each tile covers more ground, so "
        "it removes the extra margin.",
    ),
    "formats": (
        "Export formats",
        "Tick every file type you want. They're all made from one stitch.\n\n"
        "PNG: lossless, perfect quality, any size. Best for the full-resolution map.\n\n"
        "JPEG: much smaller files with slight quality loss. Up to 65,500 px per side.\n\n"
        "WebP: small files too. Up to 16,383 px per side.\n\n"
        "TIFF: lossless, popular with print and mapping software.\n\n"
        "BMP: uncompressed and huge. Only for compatibility.\n\n"
        "GIF: only 256 colors, so it looks rougher. Best for small sizes.\n\n"
        "PDF: a single page, handy for sharing or printing.\n\n"
        "If a type can't hold the chosen size, the note under the Export button tells you.",
    ),
    "quality": (
        "Quality",
        "Only JPEG, WebP and PDF use this, so the slider is greyed out until one of them is ticked. "
        "Higher means sharper detail and a bigger file. 100 (the default) is the sharpest; 90 is a good "
        "balance for smaller files, and below about 70 you'll start to see blockiness.\n\n"
        "WebP at 100 is lossless.\n\n"
        "PNG, TIFF, BMP and GIF ignore it (they never lose quality, though GIF is limited to 256 colors).",
    ),
    "output": (
        "Save to",
        "The folder the finished files go in. By default that's an \"exports\" folder inside the map's own folder.\n\n"
        "File names look like GA_z18.png: the park, the zoom level, and the scale if you shrank it "
        "(GA_z20_50pct.png). The exact names are listed under \"Your export\".\n\n"
        "Open the folder when finished: opens the output folder as soon as the export is done. "
        "Your choice is remembered.",
    ),
    "delete": (
        "Delete raw tiles",
        "The downloaded tiles take up a lot of disk space. If you tick this, they're deleted after a "
        "successful export. Your exported files are never touched.\n\n"
        "You'd have to download the map again to stitch it a second time.",
    ),
    "stats": (
        "Your export",
        "A live summary of what you'll get.\n\n"
        "Size: the exact pixel dimensions of the final image.\n\n"
        "Uncompressed: how big the image is in memory. Saved files are usually much smaller.\n\n"
        "Memory needed: PNG streams to disk, so it needs very little. The other types need the whole "
        "image in memory, so this shows what they'll use compared to what's free.\n\n"
        "Map tiles: how many grid spots have map data. Spots with none are filled with the map's "
        "background color, which is normal around the edges of a park.",
    ),
    "preview": (
        "Preview",
        "The whole map, so you can see exactly what will be included. It grows and shrinks with the "
        "window, and it updates instantly when you change Crop to map area.",
    ),
    "closeup": (
        "Close-up",
        "A small piece of the real output pixels, taken near the middle of the map. Change the Zoom "
        "level or Scale and it re-renders, so you can judge how sharp the export will be before you "
        "make it.",
    ),
}

STAT_ROWS = [
    ("zoom", "Zoom level"),
    ("scale", "Scale"),
    ("raw", "Uncompressed"),
    ("mem", "Memory needed"),
    ("tiles", "Map tiles"),
]


def resource_path(*parts: str) -> Path:
    """Locate a bundled file both when running from source and from a PyInstaller build."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base.joinpath(*parts)


class QualitySlider(ctk.CTkSlider):
    """CTkSlider that ignores clicks, drags and the scroll wheel while `locked`."""

    locked = False

    def _clicked(self, event=None):
        if self.locked:
            return None
        return super()._clicked(event)

    def _mouse_wheel(self, event):
        if self.locked:
            return None
        return super()._mouse_wheel(event)


def describe_error(exc: BaseException) -> str:
    if isinstance(exc, MemoryError):
        return "Not enough memory for this export. Choose a lower zoom level or scale, or export PNG only."
    if isinstance(exc, (RuntimeError, FileNotFoundError, ValueError)):
        return str(exc)
    if isinstance(exc, OSError):
        return f"A file operation failed:\n{exc}"
    return f"{type(exc).__name__}: {exc}"


class App(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Park Map Tool")
        self._set_window_icon()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w = min(1280, sw - 40) if isinstance(sw, int) else 1280
        h = min(800, sh - 90) if isinstance(sh, int) else 800
        self.geometry(f"{max(w, 1100)}x{max(h, 660)}")
        self.minsize(1100, 660)
        self.configure(fg_color=BG)

        self.settings = load_settings()
        self.parks: list = []
        self.park_error = ""
        self.park_btns: dict = {}
        self.selected_park = None

        self.manifest = None
        self.overview = None
        self.sources: dict = {}
        self.extra_sources: dict = {}
        self.level_map: dict = {}
        self.scale_map: dict = {}
        self._default_z = None
        self.out_custom = False
        self._preview_ref = None
        self._closeup_ref = None
        self._closeup_key = None
        self._closeup_job = None
        self._closeup_gen = 0
        self._quality = 100
        self._layout_job = None
        self._info_win = None
        self._info_job = None
        self._info_away = 0
        self._info_btn_ref = None
        self._info_toggle_btn = None

        self.busy = False
        self.q: queue.Queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker = None

        self.f_title = ctk.CTkFont(size=26, weight="bold")
        self.f_head = ctk.CTkFont(size=14, weight="bold")
        self.f_body = ctk.CTkFont(size=13)
        self.f_small = ctk.CTkFont(size=12)
        self.f_big = ctk.CTkFont(size=22, weight="bold")
        self.f_info = ctk.CTkFont(size=12, weight="bold")
        # CTkLabel can't clear an image with image=None, so swap in a transparent 1x1 instead.
        self._blank_img = ctk.CTkImage(light_image=Image.new("RGBA", (1, 1), (0, 0, 0, 0)),
                                       dark_image=Image.new("RGBA", (1, 1), (0, 0, 0, 0)), size=(1, 1))

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.pages: dict = {}
        self.nav_btns: dict = {}
        self._build_sidebar()
        self._build_download_page()
        self._build_stitch_page()
        self._build_statusbar()

        self.show_page("download")
        self.bind("<Button-1>", self._on_root_press, add="+")
        self.bind("<Escape>", lambda _e: self._close_info(), add="+")
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(80, self._poll)
        self._scan_library()
        self._load_parks()

    # ------------------------------------------------------------------ icon / logo
    def _set_window_icon(self) -> None:
        """Window + taskbar icon. Failures are harmless (the app just keeps the default icon)."""
        ico = resource_path("assets", "logo.ico")
        try:
            if sys.platform == "win32":
                import ctypes
                # Without this, Windows groups the window under python.exe's taskbar icon.
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ParkMapTool.App")
        except Exception:  # noqa: BLE001
            pass

        def apply() -> None:
            try:
                if ico.is_file():
                    self.iconbitmap(str(ico))
            except Exception:  # noqa: BLE001
                pass

        apply()
        # CustomTkinter swaps in its own default icon shortly after start-up; set ours again after that.
        self.after(250, apply)

    def _load_logo(self, size: int):
        path = resource_path("assets", "logo.png")
        try:
            with Image.open(path) as im:
                img = im.convert("RGBA")
            return ctk.CTkImage(light_image=img, dark_image=img, size=(size, size))
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------ layout
    def _build_sidebar(self) -> None:
        sb = ctk.CTkFrame(self, width=230, corner_radius=0, fg_color=PANEL)
        sb.grid(row=0, column=0, sticky="nsew")
        sb.grid_propagate(False)
        sb.grid_rowconfigure(4, weight=1)

        brand = ctk.CTkFrame(sb, fg_color="transparent")
        brand.grid(row=0, column=0, rowspan=2, padx=18, pady=(22, 22), sticky="w")
        self._logo_img = self._load_logo(46)
        text_col = 0
        if self._logo_img is not None:
            ctk.CTkLabel(brand, text="", image=self._logo_img).grid(row=0, column=0, rowspan=2, padx=(0, 10))
            text_col = 1
        ctk.CTkLabel(brand, text="Park Map Tool", font=ctk.CTkFont(size=18, weight="bold"),
                     text_color=TEXT, anchor="w").grid(row=0, column=text_col, sticky="sw")
        ctk.CTkLabel(brand, text="Download · Stitch · Export", font=self.f_small,
                     text_color=MUTED, anchor="w").grid(row=1, column=text_col, sticky="nw")

        for row, (key, text) in enumerate((("download", "1   Download"), ("stitch", "2   Stitch & Export")), start=2):
            btn = ctk.CTkButton(sb, text=text, anchor="w", height=40, corner_radius=8, font=self.f_head,
                                fg_color="transparent", hover_color=CARD_HOVER, text_color=TEXT,
                                command=lambda k=key: self.show_page(k))
            btn.grid(row=row, column=0, padx=14, pady=3, sticky="ew")
            self.nav_btns[key] = btn
        sb.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(sb, text="LIBRARY FOLDER", font=self.f_small, text_color=MUTED,
                     anchor="w").grid(row=5, column=0, padx=22, pady=(0, 2), sticky="w")
        self.lib_label = ctk.CTkLabel(sb, text="", font=self.f_small, text_color=TEXT, anchor="w",
                                      justify="left", wraplength=190)
        self.lib_label.grid(row=6, column=0, padx=22, sticky="w")
        ctk.CTkButton(sb, text="Change…", height=30, width=90, corner_radius=8, fg_color=CARD,
                      hover_color=CARD_HOVER, text_color=TEXT, command=self._change_library
                      ).grid(row=7, column=0, padx=22, pady=(8, 22), sticky="w")
        self._update_library_label()

    def _build_download_page(self) -> None:
        page = ctk.CTkFrame(self, fg_color=BG, corner_radius=0)
        page.grid(row=0, column=1, sticky="nsew")
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(3, weight=1)
        self.pages["download"] = page

        ctk.CTkLabel(page, text="Download a park map", font=self.f_title, text_color=TEXT,
                     anchor="w").grid(row=0, column=0, padx=32, pady=(28, 0), sticky="w")
        ctk.CTkLabel(page, text="Choose a park, then download its map tiles into your library folder.",
                     font=self.f_body, text_color=MUTED, anchor="w").grid(row=1, column=0, padx=32, pady=(2, 14), sticky="w")

        self.search_entry = ctk.CTkEntry(page, placeholder_text="Search parks…", height=38, corner_radius=8,
                                         fg_color=CARD, border_color=BORDER, text_color=TEXT)
        self.search_entry.grid(row=2, column=0, padx=32, pady=(0, 10), sticky="ew")
        self.search_entry.bind("<KeyRelease>", lambda _e: self._render_parks())

        self.park_list = ctk.CTkScrollableFrame(page, fg_color=CARD, corner_radius=12)
        self.park_list.grid(row=3, column=0, padx=32, sticky="nsew")

        foot = ctk.CTkFrame(page, fg_color="transparent")
        foot.grid(row=4, column=0, padx=32, pady=16, sticky="ew")
        foot.grid_columnconfigure(0, weight=1)

        opts = ctk.CTkFrame(foot, fg_color="transparent")
        opts.grid(row=0, column=0, sticky="w")
        self.var_delete_zip = ctk.BooleanVar(value=True)
        self.var_go_stitch = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(opts, text="Delete the .zip after extracting", variable=self.var_delete_zip,
                        font=self.f_body, text_color=TEXT).grid(row=0, column=0, sticky="w")
        ctk.CTkCheckBox(opts, text="Continue to stitching when done", variable=self.var_go_stitch,
                        font=self.f_body, text_color=TEXT).grid(row=1, column=0, pady=(6, 0), sticky="w")

        self.selected_label = ctk.CTkLabel(foot, text="No park selected", font=self.f_small, text_color=MUTED)
        self.selected_label.grid(row=0, column=1, padx=16, sticky="e")
        self.download_btn = ctk.CTkButton(foot, text="Download map", height=42, width=170, corner_radius=10,
                                          font=self.f_head, fg_color=ACCENT, hover_color=ACCENT_HOVER,
                                          state="disabled", command=self._start_download)
        self.download_btn.grid(row=0, column=2, sticky="e")

    # ---- stitch page building blocks
    def _info_btn(self, parent, topic: str):
        btn = ctk.CTkButton(parent, text="i", width=22, height=22, corner_radius=11, font=self.f_info,
                            fg_color=BORDER, hover_color=ACCENT, text_color=TEXT)
        btn.configure(command=lambda t=topic, b=btn: self._open_info(t, b))
        return btn

    def _section(self, parent, title: str, topic=None, first: bool = False) -> None:
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=(2 if first else 20, 2))
        row.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(row, text=title, font=self.f_head, text_color=ACCENT, anchor="w").grid(row=0, column=0, sticky="w")
        if topic:
            self._info_btn(row, topic).grid(row=0, column=1)

    def _field(self, parent, title: str, topic=None, caption: str = "", right=None):
        """Small heading with an (i) button and a muted one-line caption beneath it."""
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=(12, 0))
        row.grid_columnconfigure(0, weight=1)
        title_lbl = ctk.CTkLabel(row, text=title, font=self.f_body, text_color=TEXT, anchor="w")
        title_lbl.grid(row=0, column=0, sticky="w")
        col = 1
        if right is not None:
            right(row).grid(row=0, column=col, padx=(0, 6))
            col += 1
        if topic:
            self._info_btn(row, topic).grid(row=0, column=col)
        caption_lbl = None
        if caption:
            caption_lbl = ctk.CTkLabel(parent, text=caption, font=self.f_small, text_color=MUTED, anchor="w",
                                       justify="left", wraplength=310)
            caption_lbl.pack(fill="x", pady=(0, 4))
        return title_lbl, caption_lbl

    def _build_stitch_page(self) -> None:
        page = ctk.CTkFrame(self, fg_color=BG, corner_radius=0)
        page.grid(row=0, column=1, sticky="nsew")
        page.grid_columnconfigure(1, weight=1)
        page.grid_rowconfigure(1, weight=1)
        self.pages["stitch"] = page

        head = ctk.CTkFrame(page, fg_color="transparent")
        head.grid(row=0, column=0, columnspan=3, padx=32, pady=(28, 10), sticky="ew")
        ctk.CTkLabel(head, text="Stitch & export", font=self.f_title, text_color=TEXT, anchor="w").pack(anchor="w")
        ctk.CTkLabel(head, text="Choose a size and file types. Click an i button for a quick explanation of any setting.",
                     font=self.f_body, text_color=MUTED, anchor="w").pack(anchor="w", pady=(2, 0))

        # ---------------- left: settings (scrolls) + export button (always visible)
        left = ctk.CTkFrame(page, width=350, fg_color="transparent")
        left.grid(row=1, column=0, padx=(32, 12), pady=(0, 16), sticky="ns")
        left.grid_propagate(False)
        left.grid_rowconfigure(0, weight=1)
        left.grid_columnconfigure(0, weight=1)
        ctrl = ctk.CTkScrollableFrame(left, width=316, fg_color=CARD, corner_radius=12)
        ctrl.grid(row=0, column=0, sticky="nsew")

        # 1 Map
        self._section(ctrl, "1  ·  Map", "source", first=True)
        self.source_var = ctk.StringVar(value=PICK)
        src_row = ctk.CTkFrame(ctrl, fg_color="transparent")
        src_row.pack(fill="x", pady=(6, 0))
        src_row.grid_columnconfigure(0, weight=1)
        self.source_menu = ctk.CTkOptionMenu(src_row, values=[PICK], variable=self.source_var,
                                             command=self._on_source_menu, height=34, dynamic_resizing=False,
                                             fg_color=BG, button_color=BORDER, button_hover_color=CARD_HOVER)
        self.source_menu.grid(row=0, column=0, sticky="ew")
        ctk.CTkButton(src_row, text="Browse…", width=80, height=34, fg_color=BG, hover_color=CARD_HOVER,
                      text_color=TEXT, command=self._browse_source).grid(row=0, column=1, padx=(8, 0))
        self.source_info = ctk.CTkLabel(ctrl, text="", font=self.f_small, text_color=MUTED, anchor="w",
                                        justify="left", wraplength=310)
        self.source_info.pack(fill="x", pady=(4, 0))

        # 2 Size
        self._section(ctrl, "2  ·  Size", None)
        self._field(ctrl, "Zoom level", "zoom", "How much detail to use. Higher = sharper, but a bigger file.")
        self.level_var = ctk.StringVar(value="")
        self.level_menu = ctk.CTkOptionMenu(ctrl, values=[""], variable=self.level_var, height=34,
                                            dynamic_resizing=False, command=self._on_level,
                                            fg_color=BG, button_color=BORDER, button_hover_color=CARD_HOVER)
        self.level_menu.pack(fill="x")
        self.level_note = ctk.CTkLabel(ctrl, text="", font=self.f_small, text_color=MUTED, anchor="w",
                                       justify="left", wraplength=310)
        self.level_note.pack(fill="x", pady=(3, 0))

        self._field(ctrl, "Scale", "scale", "Optionally shrink the result further. Never adds detail.")
        self.scale_var = ctk.StringVar(value="")
        self.scale_menu = ctk.CTkOptionMenu(ctrl, values=[""], variable=self.scale_var, height=34,
                                            dynamic_resizing=False, command=lambda _v: self._refresh_estimate(),
                                            fg_color=BG, button_color=BORDER, button_hover_color=CARD_HOVER)
        self.scale_menu.pack(fill="x")
        self.final_label = ctk.CTkLabel(ctrl, text="", font=self.f_head, text_color=TEXT, anchor="w")
        self.final_label.pack(fill="x", pady=(8, 0))

        trim_row = ctk.CTkFrame(ctrl, fg_color="transparent")
        trim_row.pack(fill="x", pady=(14, 0))
        trim_row.grid_columnconfigure(0, weight=1)
        self.trim_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(trim_row, text="Crop to map area", variable=self.trim_var, font=self.f_body,
                        text_color=TEXT, command=self._on_trim).grid(row=0, column=0, sticky="w")
        self._info_btn(trim_row, "trim").grid(row=0, column=1)
        self.trim_note = ctk.CTkLabel(ctrl, text="", font=self.f_small, text_color=MUTED, anchor="w",
                                      justify="left", wraplength=310)
        self.trim_note.pack(fill="x", pady=(2, 0))

        # 3 Formats
        self._section(ctrl, "3  ·  File types", "formats")
        grid = ctk.CTkFrame(ctrl, fg_color="transparent")
        grid.pack(fill="x", pady=(6, 0))
        grid.grid_columnconfigure((0, 1), weight=1)
        self.fmt_vars: dict = {}
        for i, key in enumerate(export.FORMATS):
            var = ctk.BooleanVar(value=(key == "png"))
            self.fmt_vars[key] = var
            ctk.CTkCheckBox(grid, text=FORMAT_HINTS[key], variable=var, font=self.f_small, text_color=TEXT,
                            command=self._refresh_estimate).grid(row=i // 2, column=i % 2, pady=5, sticky="w")

        self.quality_label = None

        def quality_value(parent):
            self.quality_label = ctk.CTkLabel(parent, text="100", font=self.f_body, text_color=TEXT)
            return self.quality_label

        self.quality_title, self.quality_caption = self._field(
            ctrl, "Quality", "quality", "Tick JPEG, WebP or PDF to adjust quality.", right=quality_value)
        self.quality_slider = QualitySlider(ctrl, from_=50, to=100, number_of_steps=50, command=self._on_quality,
                                            fg_color=BORDER, progress_color=ACCENT, button_color=ACCENT,
                                            button_hover_color=ACCENT_HOVER)
        self.quality_slider.set(100)
        self.quality_slider.pack(fill="x", pady=(0, 2))
        # Safety net: whatever customtkinter's own wheel/click handlers did, a locked slider snaps back.
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>", "<ButtonRelease-1>"):
            self.quality_slider.bind(seq, lambda _e: self._enforce_quality_lock(), add="+")

        # 4 Save
        self._section(ctrl, "4  ·  Save", "output")
        self.out_var = ctk.StringVar(value="")
        out_row = ctk.CTkFrame(ctrl, fg_color="transparent")
        out_row.pack(fill="x", pady=(6, 0))
        out_row.grid_columnconfigure(0, weight=1)
        ctk.CTkEntry(out_row, textvariable=self.out_var, height=34, fg_color=BG, border_color=BORDER,
                     text_color=TEXT).grid(row=0, column=0, sticky="ew")
        ctk.CTkButton(out_row, text="Browse…", width=80, height=34, fg_color=BG, hover_color=CARD_HOVER,
                      text_color=TEXT, command=self._browse_output).grid(row=0, column=1, padx=(8, 0))
        self.open_var = ctk.BooleanVar(value=bool(self.settings.get("open_when_done", True)))
        ctk.CTkCheckBox(ctrl, text="Open the folder when finished", variable=self.open_var, font=self.f_body,
                        text_color=TEXT, command=self._on_open_pref).pack(anchor="w", pady=(14, 0))
        del_row = ctk.CTkFrame(ctrl, fg_color="transparent")
        del_row.pack(fill="x", pady=(10, 8))
        del_row.grid_columnconfigure(0, weight=1)
        self.delete_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(del_row, text="Delete raw tiles afterwards", variable=self.delete_var,
                        font=self.f_body, text_color=TEXT).grid(row=0, column=0, sticky="w")
        self._info_btn(del_row, "delete").grid(row=0, column=1)

        foot = ctk.CTkFrame(left, fg_color="transparent")
        foot.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        foot.grid_columnconfigure(0, weight=1)
        self.warn_label = ctk.CTkLabel(foot, text="", font=self.f_small, text_color=WARN, anchor="w",
                                       justify="left", wraplength=340)
        self.warn_label.grid(row=0, column=0, sticky="ew")
        self.export_btn = ctk.CTkButton(foot, text="Stitch & export", height=46, corner_radius=10,
                                        font=self.f_head, fg_color=ACCENT, hover_color=ACCENT_HOVER,
                                        state="disabled", command=self._start_export)
        self.export_btn.grid(row=1, column=0, sticky="ew", pady=(4, 0))

        # ---------------- middle: preview (the whole map, as big as the window allows)
        prev = ctk.CTkFrame(page, fg_color=CARD, corner_radius=12)
        prev.grid(row=1, column=1, padx=(0, 12), pady=(0, 16), sticky="nsew")
        prev.grid_columnconfigure(0, weight=1)
        prev.grid_rowconfigure(1, weight=1)
        self.prev_frame = prev

        ph = ctk.CTkFrame(prev, fg_color="transparent")
        ph.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 0))
        ph.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(ph, text="Preview", font=self.f_head, text_color=TEXT, anchor="w").grid(row=0, column=0, sticky="w")
        self._info_btn(ph, "preview").grid(row=0, column=1)

        self.preview_label = ctk.CTkLabel(prev, text="Pick a map to see a preview", font=self.f_body,
                                          text_color=MUTED, image=self._blank_img)
        self.preview_label.grid(row=1, column=0, padx=16, pady=8)
        self.preview_caption = ctk.CTkLabel(prev, text="", font=self.f_small, text_color=MUTED)
        self.preview_caption.grid(row=2, column=0, pady=(0, 12))
        prev.bind("<Configure>", self._on_layout)

        # ---------------- right: your export + real-pixel close-up
        info = ctk.CTkFrame(page, width=284, fg_color="transparent")
        info.grid(row=1, column=2, padx=(0, 24), pady=(0, 16), sticky="ns")
        info.grid_propagate(False)
        info.grid_rowconfigure(1, weight=1)
        info.grid_columnconfigure(0, weight=1)

        stats = ctk.CTkFrame(info, fg_color=CARD, corner_radius=12)
        stats.grid(row=0, column=0, sticky="ew")
        stats.grid_columnconfigure(0, weight=1)
        sh = ctk.CTkFrame(stats, fg_color="transparent")
        sh.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 0))
        sh.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(sh, text="Your export", font=self.f_head, text_color=TEXT, anchor="w").grid(row=0, column=0, sticky="w")
        self._info_btn(sh, "stats").grid(row=0, column=1)

        self.stat_size = ctk.CTkLabel(stats, text="—", font=self.f_big, text_color=TEXT, anchor="w")
        self.stat_size.grid(row=1, column=0, sticky="w", padx=16, pady=(8, 0))
        self.stat_mp = ctk.CTkLabel(stats, text="Pick a map to begin", font=self.f_small, text_color=MUTED, anchor="w")
        self.stat_mp.grid(row=2, column=0, sticky="w", padx=16)

        rows = ctk.CTkFrame(stats, fg_color="transparent")
        rows.grid(row=3, column=0, sticky="ew", padx=16, pady=(10, 0))
        rows.grid_columnconfigure(1, weight=1)
        self.stat_vals: dict = {}
        for i, (key, label) in enumerate(STAT_ROWS):
            ctk.CTkLabel(rows, text=label, font=self.f_small, text_color=MUTED, anchor="w"
                         ).grid(row=i, column=0, sticky="nw", pady=2)
            val = ctk.CTkLabel(rows, text="—", font=self.f_small, text_color=TEXT, anchor="e",
                               justify="right", wraplength=150)
            val.grid(row=i, column=1, sticky="e", pady=2, padx=(8, 0))
            self.stat_vals[key] = val

        self.stat_files = ctk.CTkLabel(stats, text="", font=self.f_small, text_color=MUTED, anchor="w",
                                       justify="left", wraplength=248)
        self.stat_files.grid(row=4, column=0, sticky="w", padx=16, pady=(10, 0))
        self.stat_status = ctk.CTkLabel(stats, text="", font=self.f_small, text_color=MUTED, anchor="w",
                                        justify="left", wraplength=248)
        self.stat_status.grid(row=5, column=0, sticky="w", padx=16, pady=(8, 14))

        cu = ctk.CTkFrame(info, fg_color=CARD, corner_radius=12)
        cu.grid(row=1, column=0, sticky="nsew", pady=(12, 0))
        cu.grid_columnconfigure(0, weight=1)
        cu.grid_rowconfigure(1, weight=1)
        self.closeup_frame = cu
        ch = ctk.CTkFrame(cu, fg_color="transparent")
        ch.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 0))
        ch.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(ch, text="Close-up", font=self.f_head, text_color=TEXT, anchor="w").grid(row=0, column=0, sticky="w")
        self._info_btn(ch, "closeup").grid(row=0, column=1)
        self.closeup_label = ctk.CTkLabel(cu, text="Pick a map to see a close-up", font=self.f_small,
                                          text_color=MUTED, image=self._blank_img, fg_color=BG, corner_radius=6)
        self.closeup_label.grid(row=1, column=0, padx=16, pady=8)
        self.closeup_caption = ctk.CTkLabel(cu, text="", font=self.f_small, text_color=MUTED, wraplength=248)
        self.closeup_caption.grid(row=2, column=0, padx=16, pady=(0, 12))
        cu.bind("<Configure>", self._on_layout)

    def _build_statusbar(self) -> None:
        bar = ctk.CTkFrame(self, height=46, corner_radius=0, fg_color=PANEL)
        bar.grid(row=1, column=0, columnspan=2, sticky="ew")
        bar.grid_columnconfigure(1, weight=1)
        self.status_label = ctk.CTkLabel(bar, text="Ready", font=self.f_small, text_color=MUTED, anchor="w",
                                         width=420)
        self.status_label.grid(row=0, column=0, padx=(22, 12), pady=12, sticky="w")
        self.progress = ctk.CTkProgressBar(bar, height=8)
        self.progress.set(0)
        self.progress.grid(row=0, column=1, padx=8, sticky="ew")
        self.cancel_btn = ctk.CTkButton(bar, text="Cancel", width=84, height=28, fg_color=DANGER,
                                        hover_color=DANGER_HOVER, command=self._cancel)
        self.cancel_btn.grid(row=0, column=2, padx=(8, 22))
        self.cancel_btn.grid_remove()

    # --------------------------------------------------------------- navigation
    def show_page(self, name: str) -> None:
        self._close_info()
        self.pages[name].tkraise()
        for key, btn in self.nav_btns.items():
            btn.configure(fg_color=ACCENT if key == name else "transparent",
                          hover_color=ACCENT_HOVER if key == name else CARD_HOVER)

    # ------------------------------------------------------- background helpers
    def _status(self, text: str, color: str = MUTED) -> None:
        self.status_label.configure(text=text, text_color=color)

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        if busy:
            self.cancel_btn.grid()
            self.download_btn.configure(state="disabled")
            self.export_btn.configure(state="disabled")
        else:
            self.cancel_btn.grid_remove()
            self.progress.set(0)
            self.download_btn.configure(state="normal" if self.selected_park else "disabled")
            self._refresh_estimate()

    def _run_bg(self, job, on_done, start_text: str, on_error=None) -> bool:
        """Run job(progress, cancel) on a worker thread; on_done(result) runs on the UI thread."""
        if self.busy:
            self._status("Please wait for the current task to finish.", WARN)
            return False
        self.cancel_event = threading.Event()
        ev = self.cancel_event
        self._set_busy(True)
        self.progress.set(0)
        self._status(start_text, TEXT)

        def progress(frac: float, msg: str) -> None:
            self.q.put(("progress", frac, msg))

        def target() -> None:
            try:
                result = job(progress, ev)
            except Cancelled:
                self.q.put(("cancelled",))
            except Exception as exc:  # noqa: BLE001 - surfaced to the user
                self.q.put(("error", describe_error(exc), on_error))
            else:
                self.q.put(("done", on_done, result))

        self.worker = threading.Thread(target=target, daemon=True)
        self.worker.start()
        return True

    def _poll(self) -> None:
        last = None
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == "progress":
                    last = item
                elif kind == "closeup":
                    _, gen, img, caption = item
                    if gen == self._closeup_gen:
                        self._show_closeup(img, caption)
                elif kind == "done":
                    self._set_busy(False)
                    try:
                        item[1](item[2])
                    except Exception as exc:  # noqa: BLE001
                        self._status("Something went wrong.", WARN)
                        messagebox.showerror("Something went wrong", describe_error(exc))
                elif kind == "error":
                    self._set_busy(False)
                    self._status("Failed. " + item[1].splitlines()[0], WARN)
                    if item[2]:
                        item[2](item[1])
                    else:
                        messagebox.showerror("Something went wrong", item[1])
                elif kind == "cancelled":
                    self._set_busy(False)
                    self._status("Cancelled.", MUTED)
        except queue.Empty:
            pass
        if last is not None and self.busy:
            self.progress.set(max(0.0, min(1.0, last[1])))
            self._status(last[2], TEXT)
        self.after(80, self._poll)

    def _cancel(self) -> None:
        self.cancel_event.set()
        self._status("Cancelling…", WARN)

    def _on_close(self) -> None:
        if self.busy and not messagebox.askyesno("Quit", "A task is still running. Cancel it and quit?"):
            return
        self.cancel_event.set()
        self._close_info()
        self.destroy()

    # ------------------------------------------------------------------ library
    def _update_library_label(self) -> None:
        self.lib_label.configure(text=self.settings["library"])

    def _change_library(self) -> None:
        folder = filedialog.askdirectory(title="Choose where downloaded maps are stored",
                                         initialdir=self.settings["library"] if Path(self.settings["library"]).exists() else None)
        if folder:
            self.settings["library"] = folder
            save_settings(self.settings)
            self._update_library_label()
            self._scan_library()

    def _label_for(self, folder: Path) -> str:
        name = folder.name
        try:
            info = json.loads((folder / "park.json").read_text(encoding="utf-8"))
            if info.get("parkName"):
                name = f"{info['parkName']} ({folder.name})"
        except (OSError, ValueError):
            pass
        return name

    def _scan_library(self, select=None) -> None:
        root = Path(self.settings["library"])
        found: dict = {}
        if root.is_dir():
            for d in sorted(root.iterdir()):
                if d.is_dir() and (d / "manifest.json").is_file():
                    found[self._label_for(d)] = d
        for label, d in self.extra_sources.items():
            found.setdefault(label, d)
        self.sources = found
        self.source_menu.configure(values=list(found) or [NO_MAPS])
        if select is not None:
            label = next((lbl for lbl, d in found.items() if Path(d) == Path(select)), None)
            if label:
                self.source_var.set(label)
                self._load_source(found[label])
                return
        if self.manifest is None:
            self.source_var.set(PICK if found else NO_MAPS)

    # ----------------------------------------------------------------- parks UI
    def _load_parks(self) -> None:
        self.park_error = ""
        self.parks = []
        self._render_parks(loading=True)

        def job(progress, cancel):
            progress(0.0, "Loading park list…")
            return api.fetch_parks()

        def done(parks):
            self.parks = parks
            self._status(f"Loaded {len(parks)} parks.", MUTED)
            self._render_parks()

        def failed(msg):
            self.park_error = msg
            self._render_parks()

        self._run_bg(job, done, "Loading park list…", on_error=failed)

    def _render_parks(self, loading: bool = False) -> None:
        for child in self.park_list.winfo_children():
            child.destroy()
        self.park_btns = {}
        term = self.search_entry.get().strip().lower()
        shown = [p for p in self.parks if term in f"{p.get('parkName', '')} {p.get('abbr', '')}".lower()]

        if not shown:
            if loading:
                text = "Loading parks…"
            elif self.park_error:
                text = self.park_error
            elif self.parks:
                text = "No parks match your search."
            else:
                text = "No parks found."
            ctk.CTkLabel(self.park_list, text=text, text_color=MUTED, font=self.f_body, wraplength=520,
                         justify="center").pack(pady=(30, 10))
            if self.park_error:
                ctk.CTkButton(self.park_list, text="Retry", width=100, command=self._load_parks).pack()
            return

        for p in shown:
            selected = self.selected_park is not None and p.get("parkId") == self.selected_park.get("parkId")
            btn = ctk.CTkButton(
                self.park_list, text=f"{p.get('parkName', '?')}    ·    {p.get('abbr') or p.get('parkId')}",
                anchor="w", height=38, corner_radius=8, font=self.f_body, text_color=TEXT,
                fg_color=ACCENT if selected else "transparent",
                hover_color=ACCENT_HOVER if selected else CARD_HOVER,
                command=lambda park=p: self._select_park(park))
            btn.pack(fill="x", padx=6, pady=2)
            self.park_btns[p.get("parkId")] = btn

    def _select_park(self, park: dict) -> None:
        self.selected_park = park
        for pid, btn in self.park_btns.items():
            sel = pid == park.get("parkId")
            btn.configure(fg_color=ACCENT if sel else "transparent",
                          hover_color=ACCENT_HOVER if sel else CARD_HOVER)
        self.selected_label.configure(text=park.get("parkName", ""), text_color=TEXT)
        if not self.busy:
            self.download_btn.configure(state="normal")

    def _start_download(self) -> None:
        park = self.selected_park
        if not park:
            return
        root = Path(self.settings["library"])
        delete_zip = self.var_delete_zip.get()

        def job(progress, cancel):
            return api.download_map(park, root, progress=progress, cancel=cancel, delete_zip=delete_zip)

        self._run_bg(job, self._download_done, f"Downloading {park.get('parkName', 'map')}…")

    def _download_done(self, folder: Path) -> None:
        self._status(f"Downloaded to {folder}", GOOD)
        self._scan_library(select=folder)
        if self.var_go_stitch.get():
            self.show_page("stitch")

    # ----------------------------------------------------------------- info pop-ups
    def _help_content(self, topic: str):
        title, body = HELP[topic]
        m = self.manifest
        z = self._current_z()
        if m is not None and z is not None:
            scale = self._scale_value()
            if topic == "zoom":
                info = sorted(stitch.describe_levels(m, self.trim_var.get()), key=lambda d: -d["z"])
                lines = "\n".join(f"  Zoom {d['z']}:  {d['width']:,} × {d['height']:,} px" for d in info)
                body += f"\n\nThis map's zoom levels:\n{lines}"
            elif topic == "scale":
                plan = stitch.make_plan(m, z, scale, self.trim_var.get())
                body += (f"\n\nRight now: zoom {z} at {int(round(scale * 100))}% = "
                         f"{plan.width:,} × {plan.height:,} px.")
            elif topic == "trim":
                body += "\n\nAt your current zoom: " + self._trim_note_text(z, scale)
        return title, body

    @staticmethod
    def _pointer_near(px, py, rects, margin: int = 10) -> bool:
        """True if the pointer is inside (or within `margin` px of) any (x, y, w, h) rectangle."""
        for x, y, w, h in rects:
            if x - margin <= px <= x + w + margin and y - margin <= py <= y + h + margin:
                return True
        return False

    def _screen_size(self):
        try:
            sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
            if isinstance(sw, int) and isinstance(sh, int):
                return sw, sh
        except Exception:  # noqa: BLE001
            pass
        return 1920, 1080

    def _on_root_press(self, event=None) -> None:
        """Any click closes the pop-up. If that click landed on the pop-up's own (i) button, remember it,
        so the button's click handler (which runs on release) doesn't immediately reopen the pop-up."""
        self._info_toggle_btn = None
        btn = self._info_btn_ref
        if self._info_win is not None and btn is not None and event is not None:
            try:
                rect = (btn.winfo_rootx(), btn.winfo_rooty(), btn.winfo_width(), btn.winfo_height())
                if self._pointer_near(event.x_root, event.y_root, [rect], 0):
                    self._info_toggle_btn = btn
            except Exception:  # noqa: BLE001
                pass
        self._close_info()

    def _open_info(self, topic: str, btn) -> None:
        """Show the explanation in a small pop-up right beside the (i) button."""
        if btn is self._info_toggle_btn:     # this click was the one that just closed it
            self._info_toggle_btn = None
            return
        self._close_info()
        title, body = self._help_content(topic)
        try:
            win = tk.Toplevel(self)
            self._info_win = win
            win.withdraw()
            win.overrideredirect(True)
            try:
                win.attributes("-topmost", True)
            except Exception:  # noqa: BLE001
                pass
            win.configure(bg=BORDER)  # 1px border
            frame = ctk.CTkFrame(win, fg_color=CARD, corner_radius=0)
            frame.pack(padx=1, pady=1)
            ctk.CTkLabel(frame, text=title, font=self.f_head, text_color=ACCENT, anchor="w", justify="left",
                         wraplength=INFO_WRAP).pack(anchor="w", padx=14, pady=(12, 4))
            ctk.CTkLabel(frame, text=body, font=self.f_small, text_color=TEXT, anchor="w", justify="left",
                         wraplength=INFO_WRAP).pack(anchor="w", padx=14, pady=(0, 14))
            win.update_idletasks()
            w, h = win.winfo_reqwidth(), win.winfo_reqheight()
            bx, by, bw = btn.winfo_rootx(), btn.winfo_rooty(), btn.winfo_width()
            sw, sh = self._screen_size()
            x = bx + bw + 8                      # to the right of the button...
            if x + w > sw - 8:                   # ...or to its left if there's no room
                x = max(8, bx - w - 8)
            y = max(8, min(by - 6, sh - h - 56))
            win.geometry(f"+{x}+{y}")
            win.deiconify()
        except Exception:  # noqa: BLE001
            self._close_info()
            return
        self._info_btn_ref = btn
        self._info_away = 0
        self._info_job = self.after(60, self._info_tick)

    def _info_tick(self) -> None:
        """Close the pop-up once the cursor has left both the button and the pop-up."""
        self._info_job = None
        win, btn = self._info_win, self._info_btn_ref
        if win is None or btn is None:
            return
        try:
            px, py = self.winfo_pointerxy()
            rects = [(btn.winfo_rootx(), btn.winfo_rooty(), btn.winfo_width(), btn.winfo_height()),
                     (win.winfo_rootx(), win.winfo_rooty(), win.winfo_width(), win.winfo_height())]
        except Exception:  # noqa: BLE001
            self._close_info()
            return
        self._info_away = 0 if self._pointer_near(px, py, rects) else self._info_away + 1
        if self._info_away >= 2:
            self._close_info()
        else:
            self._info_job = self.after(60, self._info_tick)

    def _close_info(self) -> None:
        if self._info_job is not None:
            try:
                self.after_cancel(self._info_job)
            except Exception:  # noqa: BLE001
                pass
            self._info_job = None
        win, self._info_win = self._info_win, None
        self._info_btn_ref = None
        if win is not None:
            try:
                win.destroy()
            except Exception:  # noqa: BLE001
                pass

    # ----------------------------------------------------------------- stitching
    def _on_source_menu(self, label: str) -> None:
        folder = self.sources.get(label)
        if folder is not None:
            self._load_source(folder)

    def _browse_source(self) -> None:
        d = filedialog.askdirectory(title="Choose a map folder (the one containing manifest.json)")
        if not d:
            return
        folder = Path(d)
        if not (folder / "manifest.json").is_file():
            messagebox.showwarning("Not a map folder", "That folder doesn't contain a manifest.json.\n"
                                   "Choose the folder that has manifest.json and a textures folder.")
            return
        self.extra_sources[f"{folder.name} (browsed)"] = folder
        self._scan_library(select=folder)

    def _load_source(self, folder: Path) -> None:
        def job(progress, cancel):
            progress(0.0, "Reading map…")
            m = stitch.Manifest.load(folder)
            overview = stitch.make_overview(m, target=PREVIEW_PX, progress=progress, cancel=cancel)
            return m, overview

        def failed(msg):
            self.manifest = None
            self._clear_source_ui()
            messagebox.showerror("Couldn't open map", msg)

        self._run_bg(job, self._source_loaded, "Reading map…", on_error=failed)

    def _clear_source_ui(self) -> None:
        self.manifest = None
        self.overview = None
        self._closeup_key = None
        self._closeup_gen += 1
        self.source_info.configure(text="")
        self.level_menu.configure(values=[""])
        self.level_var.set("")
        self.level_map = {}
        self.scale_menu.configure(values=[""])
        self.scale_var.set("")
        self.scale_map = {}
        self._preview_ref = None
        self._closeup_ref = None
        self.preview_label.configure(image=self._blank_img, text="Pick a map to see a preview")
        self.preview_caption.configure(text="")
        self.closeup_label.configure(image=self._blank_img, text="Pick a map to see a close-up")
        self.closeup_caption.configure(text="")
        self.final_label.configure(text="")
        self.level_note.configure(text="")
        self.trim_note.configure(text="")
        self._refresh_estimate()

    def _source_loaded(self, result) -> None:
        m, overview = result
        self.manifest = m
        self.overview = overview
        self.scale_map = {}
        self.source_info.configure(
            text=f"{m.width} × {m.height} tiles  ·  zoom {min(m.levels)}–{max(m.levels)}  ·  {m.tile_count():,} tile files")
        self._rebuild_levels()
        if not self.out_custom:
            self.out_var.set(str(m.folder / "exports"))
        self._update_overview()
        self._closeup_key = None
        self._status(f"Loaded {m.folder.name}.", GOOD)
        self._refresh_estimate()

    # ---- preview
    @staticmethod
    def _dim(widget):
        try:
            w, h = widget.winfo_width(), widget.winfo_height()
            if isinstance(w, int) and isinstance(h, int):
                return w, h
        except Exception:  # noqa: BLE001
            pass
        return 0, 0

    def _preview_box(self):
        """Largest size the overview can be drawn at inside its card."""
        w, h = self._dim(self.prev_frame)
        if w < 120 or h < 200:
            return 480, 440
        return max(160, w - 40), max(160, h - 96)

    def _closeup_box(self):
        """Pixel size of the close-up window, rounded so small resizes don't re-render it."""
        w, h = self._dim(self.closeup_frame)
        if w < 100 or h < 150:
            return 232, 200
        return max(96, min(360, w - 36) // 8 * 8), max(96, min(360, h - 110) // 8 * 8)

    def _on_layout(self, _event=None) -> None:
        """The window or a card was resized: refit the images once things settle."""
        if self._layout_job is not None:
            try:
                self.after_cancel(self._layout_job)
            except Exception:  # noqa: BLE001
                pass
        self._layout_job = self.after(150, self._apply_layout)

    def _apply_layout(self) -> None:
        self._layout_job = None
        self._update_overview()
        self._schedule_closeup()

    def _update_overview(self) -> None:
        ov = self.overview
        if ov is None:
            return
        img = ov.view(self.trim_var.get())
        w, h = img.size
        bw, bh = self._preview_box()
        k = min(bw / w, bh / h, 1.0)
        size = (max(1, int(w * k)), max(1, int(h * k)))
        self._preview_ref = ctk.CTkImage(light_image=img, dark_image=img, size=size)
        self.preview_label.configure(image=self._preview_ref, text="")
        self.preview_caption.configure(
            text="Whole map, cropped to the map area" if self.trim_var.get() else "Whole map, including whole tiles")

    def _schedule_closeup(self) -> None:
        """Re-render the real-pixel close-up shortly after the last change to zoom/scale/crop."""
        m, z = self.manifest, self._current_z()
        if m is None or z is None:
            return
        key = (str(m.folder), z, round(self._scale_value(), 4), self.trim_var.get(), self._closeup_box())
        if key == self._closeup_key:
            return
        self._closeup_key = key
        self.closeup_caption.configure(text="Rendering close-up…")
        if self._closeup_job is not None:
            try:
                self.after_cancel(self._closeup_job)
            except Exception:  # noqa: BLE001
                pass
        self._closeup_job = self.after(200, self._start_closeup)

    def _start_closeup(self) -> None:
        self._closeup_job = None
        m, z = self.manifest, self._current_z()
        if m is None or z is None:
            return
        scale, trim = self._scale_value(), self.trim_var.get()
        plan = stitch.make_plan(m, z, scale, trim)
        cw, ch = self._closeup_box()
        self._closeup_gen += 1
        gen = self._closeup_gen
        caption = f"Real pixels at zoom {z}, {int(round(scale * 100))}%"

        def run() -> None:
            try:
                img = stitch.render_closeup(m, plan, cw, ch)
            except Exception:  # noqa: BLE001
                img = None
            self.q.put(("closeup", gen, img, caption))

        threading.Thread(target=run, daemon=True).start()

    def _show_closeup(self, img, caption: str) -> None:
        if img is None:
            self.closeup_label.configure(image=self._blank_img, text="Close-up unavailable")
            self.closeup_caption.configure(text="")
            return
        self._closeup_ref = ctk.CTkImage(light_image=img, dark_image=img, size=img.size)
        self.closeup_label.configure(image=self._closeup_ref, text="")
        self.closeup_caption.configure(text=caption)

    # ---- zoom / scale / crop controls
    def _rebuild_levels(self, keep=None) -> None:
        m = self.manifest
        keep_scale = self._scale_value() if self.scale_map else 1.0
        info = sorted(stitch.describe_levels(m, self.trim_var.get()), key=lambda d: -d["z"])
        self._default_z = next((d["z"] for d in info if max(d["width"], d["height"]) <= MAX_COMMON_SIDE),
                               info[-1]["z"])
        self.level_map = {f"Zoom {d['z']}  ·  {d['width']:,} × {d['height']:,} px": d["z"] for d in info}
        self.level_menu.configure(values=list(self.level_map))
        z = keep if keep in self.level_map.values() else self._default_z
        self.level_var.set(next(lbl for lbl, zz in self.level_map.items() if zz == z))
        self._rebuild_scales(keep=keep_scale)

    def _rebuild_scales(self, keep=None) -> None:
        z = self._current_z()
        if self.manifest is None or z is None:
            return
        keep = 1.0 if keep is None else keep
        self.scale_map = {}
        for s in SCALE_STEPS:
            p = stitch.make_plan(self.manifest, z, s, self.trim_var.get())
            self.scale_map[f"{int(round(s * 100))}%  ·  {p.width:,} × {p.height:,} px"] = s
        self.scale_menu.configure(values=list(self.scale_map))
        label = next((lbl for lbl, s in self.scale_map.items() if abs(s - keep) < 1e-6), next(iter(self.scale_map)))
        self.scale_var.set(label)

    def _on_level(self, _value=None) -> None:
        self._rebuild_scales(keep=self._scale_value())
        self._refresh_estimate()

    def _on_trim(self) -> None:
        if self.manifest is not None:
            self._rebuild_levels(keep=self._current_z())
            self._update_overview()
        self._refresh_estimate()

    def _on_quality(self, value: float) -> None:
        if self.quality_slider.locked:
            self._enforce_quality_lock()
            return
        self._quality = int(round(value))
        if self.quality_label is not None:
            self.quality_label.configure(text=str(self._quality))

    def _enforce_quality_lock(self) -> None:
        """While locked, put the slider back to the saved value (covers wheel, click and drag)."""
        if self.quality_slider.locked:
            self.quality_slider.set(self._quality)

    def _set_quality_enabled(self, on: bool) -> None:
        sl = self.quality_slider
        sl.locked = not on
        sl.configure(state="normal" if on else "disabled",
                     button_color=ACCENT if on else SLIDER_OFF_KNOB,
                     button_hover_color=ACCENT_HOVER if on else SLIDER_OFF_KNOB,
                     progress_color=ACCENT if on else SLIDER_OFF_BAR)
        if not on:
            sl.set(self._quality)
        for lbl in (self.quality_title, self.quality_label):
            if lbl is not None:
                lbl.configure(text_color=TEXT if on else DISABLED_TEXT)
        if self.quality_caption is not None:
            self.quality_caption.configure(
                text="Higher = sharper detail, bigger file." if on else "Tick JPEG, WebP or PDF to adjust quality.",
                text_color=MUTED if on else DISABLED_TEXT)

    def _current_z(self):
        return self.level_map.get(self.level_var.get())

    def _scale_value(self) -> float:
        return self.scale_map.get(self.scale_var.get(), 1.0)

    def _selected_formats(self) -> list:
        return [k for k in export.FORMATS if self.fmt_vars[k].get()]

    def _trim_effect(self, z: int, scale: float):
        a = stitch.make_plan(self.manifest, z, scale, True)
        b = stitch.make_plan(self.manifest, z, scale, False)
        return b.width - a.width, b.height - a.height

    def _trim_note_text(self, z: int, scale: float) -> str:
        dw, dh = self._trim_effect(z, scale)
        if dw <= 0 and dh <= 0:
            return "no effect, the tiles already match the map's edges."
        return f"removes about {max(dw, 0):,} × {max(dh, 0):,} px of extra margin."

    def _level_note_text(self, z: int) -> str:
        levels = sorted(self.manifest.levels)
        if z == levels[-1] and z == self._default_z:
            return "Sharpest available, and small enough for every file type."
        if z == levels[-1]:
            return "Sharpest available. Largest files and slowest to export."
        if z == self._default_z:
            return "Recommended: sharp, and small enough for every file type."
        if z == levels[0]:
            return "Smallest and fastest. Good for a quick look."
        return "Each zoom level down halves the width and height."

    def _reset_stats(self) -> None:
        self.stat_size.configure(text="—")
        self.stat_mp.configure(text="Pick a map to begin")
        for lbl in self.stat_vals.values():
            lbl.configure(text="—", text_color=TEXT)
        self.stat_files.configure(text="")
        self.stat_status.configure(text="")

    def _refresh_estimate(self) -> None:
        m, z = self.manifest, self._current_z()
        if m is None or z is None:
            self._reset_stats()
            self.warn_label.configure(text="")
            self.export_btn.configure(state="disabled")
            return

        scale, trim = self._scale_value(), self.trim_var.get()
        plan = stitch.make_plan(m, z, scale, trim)
        fmts = self._selected_formats()
        free = export.available_ram_bytes()
        problems = export.validate(fmts, plan.width, plan.height, free)
        if not fmts:
            problems.append("Tick at least one file type.")

        pixels = plan.width * plan.height
        raw = pixels * 3
        pct = int(round(scale * 100))

        # live summary beside the preview
        self.stat_size.configure(text=f"{plan.width:,} × {plan.height:,} px")
        self.stat_mp.configure(text=f"{pixels / 1e6:,.1f} megapixels")
        self.stat_vals["zoom"].configure(text=f"{z}  (of {min(m.levels)}–{max(m.levels)})")
        self.stat_vals["scale"].configure(text=f"{pct}%")
        self.stat_vals["raw"].configure(text=fmt_bytes(raw))

        if not fmts:
            mem_text, mem_color = "—", TEXT
        elif all(k == "png" for k in fmts):
            mem_text, mem_color = "Low (PNG streams to disk)", TEXT
        else:
            need = int(raw * 1.3)
            mem_text = f"~{fmt_bytes(need)}" + (f" of {fmt_bytes(free)} free" if free else "")
            mem_color = WARN if free and need > free else TEXT
        self.stat_vals["mem"].configure(text=mem_text, text_color=mem_color)

        tx0, tx1, ty0, ty1 = plan.tile_range
        present = sum(1 for (x, y) in m.levels[z] if tx0 <= x < tx1 and ty0 <= y < ty1)
        self.stat_vals["tiles"].configure(text=f"{present:,} of {(tx1 - tx0) * (ty1 - ty0):,}")

        if fmts:
            base = export.output_name(m.folder.name, z, scale, "")
            exts = "  ".join(export.FORMATS[k].ext for k in fmts)
            self.stat_files.configure(text=f"Will create {base}\nas  {exts}")
        else:
            self.stat_files.configure(text="")
        if problems:
            self.stat_status.configure(text="⚠ Can't export yet. See the note under the button.", text_color=WARN)
        else:
            self.stat_status.configure(text="✓ Ready to export", text_color=GOOD)

        # controls
        self.final_label.configure(text=f"Final size:  {plan.width:,} × {plan.height:,} px")
        self.level_note.configure(text=self._level_note_text(z))
        self.trim_note.configure(text=self._trim_note_text(z, scale).capitalize())
        uses_quality = any(export.FORMATS[k].uses_quality for k in fmts)
        self._set_quality_enabled(uses_quality)
        self.warn_label.configure(text="\n".join("⚠ " + p for p in problems))
        self.export_btn.configure(state="normal" if not problems and not self.busy else "disabled")

        self._schedule_closeup()

    def _browse_output(self) -> None:
        d = filedialog.askdirectory(title="Choose where to save the exported maps")
        if d:
            self.out_var.set(d)
            self.out_custom = True

    def _start_export(self) -> None:
        m = self.manifest
        z = self._current_z()
        if m is None or z is None:
            return
        fmts = self._selected_formats()
        out_dir = Path(self.out_var.get().strip() or (m.folder / "exports")).expanduser()
        scale, trim, quality = self._scale_value(), self.trim_var.get(), self._quality
        delete_raw = self.delete_var.get()
        if delete_raw and not messagebox.askyesno(
                "Delete raw tiles?",
                "After a successful export, the downloaded tiles for this map will be deleted.\n"
                "You'd have to download the map again to re-stitch it. Continue?"):
            return

        def job(progress, cancel):
            return export.export_map(m, out_dir, fmts, z, scale, trim, quality,
                                     progress=progress, cancel=cancel, delete_raw=delete_raw)

        def done(res):
            self._export_done(res, out_dir)

        self._run_bg(job, done, "Starting export…")

    def _on_open_pref(self) -> None:
        self.settings["open_when_done"] = bool(self.open_var.get())
        save_settings(self.settings)

    def _export_done(self, res, out_dir: Path) -> None:
        note = "  ·  raw tiles deleted" if res.raw_deleted else ""
        self._status(f"Saved {len(res.files)} file(s) to {out_dir}  ·  {res.width:,} × {res.height:,} px{note}", GOOD)
        if res.raw_deleted:
            self._scan_library()
            self._clear_source_ui()
            self.source_var.set(PICK if self.sources else NO_MAPS)
        if self.open_var.get():
            self._open_folder(out_dir)
        if res.missing or res.corrupt:  # only interrupt the user when there's something to know
            messagebox.showwarning(
                "Export finished with gaps",
                f"{res.missing:,} grid spots had no tile and {res.corrupt:,} tiles couldn't be read. "
                "Those areas show the map's background color.")

    @staticmethod
    def _open_folder(path: Path) -> None:
        try:
            if hasattr(os, "startfile"):
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except OSError:
            pass


def main() -> None:
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    App().mainloop()


if __name__ == "__main__":
    main()
