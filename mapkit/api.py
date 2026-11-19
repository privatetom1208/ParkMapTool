"""Park directory + map bundle download (ported from the original sfdown.py).

These endpoints and the API key come from the parks' mobile app. They are not a
public, documented API, so they can change at any time. If downloads start
failing with 401/403/404, this is the file to update.
"""

from __future__ import annotations

import base64
import json
import os
import re
import zipfile
from pathlib import Path
from typing import List, Optional
import threading

import requests

from .common import Cancelled, ProgressFn, check_cancel, fmt_bytes, sub_progress

PARKS_URL = base64.b64decode(
    "aHR0cHM6Ly9jb250ZW50LnByb2Quc2l4LW10LmNvbS9jb250ZW50L2FsbC1wYXJrcw=="
).decode("utf-8")

MAP_BUNDLE_URL = base64.b64decode(
    "aHR0cHM6Ly9hcGkuYXR0cmFjdGlvbnMuaW8vdjIvbWFwLWJ1bmRsZS97bWFwX2lkfS92ZXJzaW9uL2xhdGVzdC9mb3JtYXQvd2VicA=="
).decode("utf-8")

ATTRACTIONS_API_KEY = "N0Briqwsui3RUFPZNKMZTV7HGDvfc9QaYQiEGhDb"

USER_AGENT = "okhttp/4.12.0"


def fetch_parks(timeout: int = 15) -> List[dict]:
    """Return parks that have an interactive map, unique by parkId, sorted by name."""
    try:
        resp = requests.get(
            PARKS_URL,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            timeout=timeout,
        )
        resp.raise_for_status()
        parks = resp.json()
    except requests.RequestException as exc:
        raise RuntimeError(f"Couldn't load the park list: {exc}") from exc
    except ValueError as exc:
        raise RuntimeError("The park list came back in an unexpected format.") from exc

    seen = set()
    valid = []
    for p in parks:
        pid = p.get("parkId")
        if pid not in seen and p.get("mapId"):
            seen.add(pid)
            valid.append(p)
    valid.sort(key=lambda x: x.get("parkName", ""))
    return valid


def folder_name_for(park: dict) -> str:
    raw = park.get("abbr") or str(park.get("parkId", "park"))
    return re.sub(r"[^\w.-]+", "_", raw).strip("._") or "park"


def download_map(
    park: dict,
    root: Path,
    progress: ProgressFn = None,
    cancel: Optional[threading.Event] = None,
    delete_zip: bool = True,
) -> Path:
    """Download and extract one park's map bundle. Returns the extracted folder."""
    name = folder_name_for(park)
    map_id = str(park["mapId"]).strip("/")
    out_dir = Path(root) / name
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"{name}_map.zip"
    part_path = out_dir / f"{name}_map.zip.part"

    dl_progress = sub_progress(progress, 0.0, 0.75)
    if dl_progress:
        dl_progress(0.0, f"Connecting for {park.get('parkName', name)}…")

    try:
        resp = requests.get(
            MAP_BUNDLE_URL.format(map_id=map_id),
            headers={"X-API-Key": ATTRACTIONS_API_KEY, "User-Agent": USER_AGENT},
            stream=True,
            timeout=30,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"Couldn't reach the map server: {exc}") from exc

    with resp:
        if resp.status_code in (401, 403):
            raise RuntimeError(
                f"The map server refused the request (HTTP {resp.status_code}). "
                "The app's API key may have changed; update ATTRACTIONS_API_KEY in mapkit/api.py."
            )
        if resp.status_code == 404:
            raise RuntimeError("No map bundle was found for this park (HTTP 404).")
        try:
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise RuntimeError(f"Download failed: {exc}") from exc

        total = int(resp.headers.get("Content-Length") or 0)
        got = 0
        try:
            with open(part_path, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=1 << 16):
                    check_cancel(cancel)
                    if not chunk:
                        continue
                    fh.write(chunk)
                    got += len(chunk)
                    if dl_progress:
                        if total:
                            dl_progress(got / total, f"Downloading… {fmt_bytes(got)} of {fmt_bytes(total)}")
                        else:
                            dl_progress(0.5, f"Downloading… {fmt_bytes(got)}")
            os.replace(part_path, zip_path)
        finally:
            if part_path.exists():
                try:
                    part_path.unlink()
                except OSError:
                    pass

    ex_progress = sub_progress(progress, 0.75, 1.0)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            members = zf.infolist()
            n = max(1, len(members))
            for i, member in enumerate(members):
                check_cancel(cancel)
                zf.extract(member, out_dir)  # zipfile sanitizes '..' and absolute paths
                if ex_progress and (i % 25 == 0 or i == n - 1):
                    ex_progress((i + 1) / n, f"Extracting… {i + 1:,} of {n:,} files")
    except zipfile.BadZipFile as exc:
        raise RuntimeError("The downloaded file wasn't a valid zip archive.") from exc

    try:
        (out_dir / "park.json").write_text(
            json.dumps(
                {
                    "parkName": park.get("parkName"),
                    "abbr": park.get("abbr"),
                    "parkId": park.get("parkId"),
                    "mapId": park.get("mapId"),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass

    if delete_zip:
        try:
            zip_path.unlink()
        except OSError:
            pass

    if progress:
        progress(1.0, "Download complete")
    return out_dir
