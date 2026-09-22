# ==============================================================================
# ocr_engine.py - Server-side OCR so agents never write vision scripts.
#
# Engine tiers (auto-selected, best available):
#   1. winsdk (Python WinRT)          - if the pip package happens to exist
#   2. PowerShell WinRT (ZERO deps)   - Windows.Media.Ocr is baked into Win10/11;
#                                       driven via modules/ocr_winrt.ps1
#   3. pytesseract                    - if installed
#
# Output contract: text lines with PHYSICAL pixel bounding boxes relative to
# the image passed in - see tool converts these straight into click targets.
# ==============================================================================
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from .. import config
except ImportError:  # direct script execution
    import config  # type: ignore

_PS_SCRIPT = Path(__file__).resolve().parent / "ocr_winrt.ps1"
_OCR_ENGINE = None          # cached Python-WinRT OcrEngine
_OCR_LANG = None
_WINSDK_OK = None           # None = untested, bool = tested


# ------------------------------------------------------------------------------
# Engine discovery
# ------------------------------------------------------------------------------

def _probe_winsdk() -> bool:
    global _WINSDK_OK
    if _WINSDK_OK is None:
        try:
            import winsdk  # noqa: F401
            _WINSDK_OK = True
        except Exception:
            _WINSDK_OK = False
    return _WINSDK_OK


def powershell_ocr_available() -> bool:
    """The zero-dependency tier: needs only the .ps1 (ships with the hub)."""
    return _PS_SCRIPT.is_file()


async def _get_engine(lang: str = "en-US"):
    global _OCR_ENGINE, _OCR_LANG
    if not _probe_winsdk():
        return None
    if _OCR_ENGINE is not None and _OCR_LANG == lang:
        return _OCR_ENGINE
    try:
        import winsdk.windows.globalization as globalization
        import winsdk.windows.media.ocr as media_ocr

        ocr_lang = globalization.Language(lang)
        engine = media_ocr.OcrEngine.try_create_from_language(ocr_lang)
        if engine is None:
            engine = media_ocr.OcrEngine.try_create_from_user_profile_languages()
        if engine is None:
            return None
        _OCR_ENGINE = engine
        _OCR_LANG = lang
        return engine
    except Exception:
        return None


def ocr_available() -> Dict[str, Any]:
    """Which OCR engines the hub can use right now (no round-trips wasted)."""
    status: Dict[str, Any] = {
        "winsdk_python": _probe_winsdk(),
        "windows_ocr_powershell": powershell_ocr_available(),
        "tesseract": False,
    }
    try:
        import pytesseract  # noqa: F401
        status["tesseract"] = True
    except Exception:
        pass
    status["engine"] = (
        "winsdk_python" if status["winsdk_python"]
        else "windows_ocr_powershell" if status["windows_ocr_powershell"]
        else "tesseract" if status["tesseract"]
        else "none"
    )
    return status


# ------------------------------------------------------------------------------
# Tier 1: Python WinRT (async native)
# ------------------------------------------------------------------------------

async def _ocr_windows(pil_img, lang: str) -> Optional[List[Dict[str, Any]]]:
    engine = await _get_engine(lang)
    if engine is None:
        return None
    try:
        import winsdk.windows.graphics.imaging as imaging
        import winsdk.windows.security.cryptography as crypto
    except Exception:
        return None

    w, h = pil_img.size
    # PIL RGBA bytes -> BGRA byte order (WinRT bitmap format)
    raw = bytearray(pil_img.convert("RGBA").tobytes())
    for i in range(0, len(raw), 4):
        raw[i], raw[i + 2] = raw[i + 2], raw[i]
    buffer = crypto.CryptographicBuffer.create_from_byte_array(bytes(raw))
    try:
        bitmap = imaging.SoftwareBitmap.create_copy_from_buffer(
            buffer, imaging.BitmapPixelFormat.bgra8, w, h
        )
    except Exception:
        return None

    loop = asyncio.get_running_loop()

    def _recognize():
        return engine.recognize_async(bitmap).get()

    try:
        result = await loop.run_in_executor(None, _recognize)
    except Exception:
        return None

    lines: List[Dict[str, Any]] = []
    for line in result.lines:
        r = line.bounding_rect
        lines.append({
            "text": line.text,
            "x": int(r.x), "y": int(r.y),
            "w": int(r.width), "h": int(r.height),
        })
    return lines


# ------------------------------------------------------------------------------
# Tier 2: PowerShell WinRT (zero dependencies - ships inside Windows)
# ------------------------------------------------------------------------------

def _ocr_powershell_sync(pil_img, lang: str) -> Optional[List[Dict[str, Any]]]:
    """Blocking; call via run_in_executor. Returns lines or None on failure."""
    if not powershell_ocr_available():
        return None
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=".png", prefix="mcp_ocr_")
        os.close(fd)
        pil_img.convert("RGB").save(tmp_path, format="PNG")

        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
             "Bypass", "-File", str(_PS_SCRIPT), tmp_path, lang],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30,
        )
        raw = (proc.stdout or "").strip()
        if not raw:
            return None
        # strict=False: PS 5.1 ConvertTo-Json can emit raw control characters
        # when screen text contains special Unicode - tolerate them.
        data = json.loads(raw, strict=False)
        if data.get("error"):
            # engine-level problem (e.g. no language pack) -> treat as no result
            return [] if data.get("engine") == "none" else None
        lines = [
            {"text": l["text"], "x": int(l["x"]), "y": int(l["y"]),
             "w": int(l["w"]), "h": int(l["h"])}
            for l in data.get("lines", [])
        ]
        return lines
    except Exception:
        return None
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


# ------------------------------------------------------------------------------
# Tier 3: Tesseract fallback (grouped words -> lines)
# ------------------------------------------------------------------------------

def _ocr_tesseract(pil_img) -> Optional[List[Dict[str, Any]]]:
    try:
        import pytesseract
    except Exception:
        return None
    try:
        data = pytesseract.image_to_data(pil_img, output_type=pytesseract.Output.DICT)
    except Exception:
        return None

    rows: Dict[Any, List[Dict[str, Any]]] = {}
    n = len(data.get("text", []))
    for i in range(n):
        word = (data["text"][i] or "").strip()
        if not word or int(data["conf"][i]) < 0:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        rows.setdefault(key, []).append({
            "text": word,
            "x": int(data["left"][i]), "y": int(data["top"][i]),
            "w": int(data["width"][i]), "h": int(data["height"][i]),
        })

    lines: List[Dict[str, Any]] = []
    for words in rows.values():
        x0 = min(wd["x"] for wd in words)
        y0 = min(wd["y"] for wd in words)
        x1 = max(wd["x"] + wd["w"] for wd in words)
        y1 = max(wd["y"] + wd["h"] for wd in words)
        lines.append({
            "text": " ".join(wd["text"] for wd in words),
            "x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0,
        })
    return lines


# ------------------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------------------

async def ocr_image_async(pil_img, lang: str = "en-US") -> Dict[str, Any]:
    """
    OCR a PIL image. Returns:
      { engine, full_text, lines: [{text, x, y, w, h}], elapsed_ms }
    Boxes are in PHYSICAL pixels relative to the image (1:1 with pyautogui
    offsets derived from the same image origin).
    """
    t0 = time.monotonic()
    lines = None
    engine = "none"
    loop = asyncio.get_running_loop()

    if _probe_winsdk():
        lines = await _ocr_windows(pil_img, lang)
        if lines is not None:
            engine = "winsdk_python"
    if lines is None and powershell_ocr_available():
        lines = await loop.run_in_executor(None, _ocr_powershell_sync, pil_img, lang)
        if lines is not None:
            engine = "windows_ocr_powershell"
    if lines is None:
        lines = await loop.run_in_executor(None, _ocr_tesseract, pil_img)
        if lines is not None:
            engine = "tesseract"

    if lines is None:
        return {
            "engine": "none",
            "error": "No OCR engine available (winsdk/powershell/tesseract all failed).",
            "full_text": "",
            "lines": [],
            "elapsed_ms": int((time.monotonic() - t0) * 1000),
        }

    lines.sort(key=lambda l: (l["y"], l["x"]))
    return {
        "engine": engine,
        "full_text": "\n".join(l["text"] for l in lines),
        "lines": lines,
        "elapsed_ms": int((time.monotonic() - t0) * 1000),
    }


def find_text_box(lines: List[Dict[str, Any]], query: str, partial: bool = True) -> Optional[Dict[str, Any]]:
    """
    Find the best text line matching a query; returns its box in image/physical
    pixels. Matching is case-insensitive substring by default. None if absent.
    Used by the see tool's click_text and web_task's vision tier.
    """
    q = (query or "").strip().lower()
    if not q:
        return None
    best = None
    best_score = 0.0
    for line in lines:
        text = line["text"].lower()
        score = 0.0  # MUST pre-initialize: partial-miss left it unbound (crash)
        if partial:
            if q in text:
                score = len(q) / max(len(text), 1)
        else:
            score = 1.0 if text == q else 0.0
        if score > best_score:
            best_score = score
            best = line
    return best


def box_center(box: Dict[str, Any]) -> tuple:
    """Center of a text box -> physical click coordinates."""
    return int(box["x"] + box["w"] / 2), int(box["y"] + box["h"] / 2)


def ocr_image_sync(pil_img, lang: str = "en-US") -> Dict[str, Any]:
    """Blocking OCR for code paths that already run on worker threads
    (mouse_runtime aiming/proof, click storms). Same contract as the async API."""
    t0 = time.monotonic()
    lines = None
    engine = "none"

    if _probe_winsdk():
        lines = _ocr_windows_blocking(pil_img, lang)
        if lines is not None:
            engine = "winsdk_python"
    if lines is None and powershell_ocr_available():
        lines = _ocr_powershell_sync(pil_img, lang)
        if lines is not None:
            engine = "windows_ocr_powershell"
    if lines is None:
        lines = _ocr_tesseract(pil_img)
        if lines is not None:
            engine = "tesseract"

    if lines is None:
        return {"engine": "none", "error": "no OCR engine available",
                "full_text": "", "lines": [],
                "elapsed_ms": int((time.monotonic() - t0) * 1000)}

    lines.sort(key=lambda l: (l["y"], l["x"]))
    return {"engine": engine, "full_text": "\n".join(l["text"] for l in lines),
            "lines": lines, "elapsed_ms": int((time.monotonic() - t0) * 1000)}


def _ocr_windows_blocking(pil_img, lang: str) -> Optional[List[Dict[str, Any]]]:
    """Synchronous WinRT OCR path used by worker threads (no running loop)."""
    engine = None
    if _probe_winsdk():
        try:
            import winsdk.windows.globalization as globalization
            import winsdk.windows.media.ocr as media_ocr
            ocr_lang = globalization.Language(lang)
            engine = media_ocr.OcrEngine.try_create_from_language(ocr_lang)
            if engine is None:
                engine = media_ocr.OcrEngine.try_create_from_user_profile_languages()
        except Exception:
            engine = None
    if engine is None:
        return None
    try:
        import winsdk.windows.graphics.imaging as imaging
        import winsdk.windows.security.cryptography as crypto
    except Exception:
        return None
    w, h = pil_img.size
    raw = bytearray(pil_img.convert("RGBA").tobytes())
    for i in range(0, len(raw), 4):
        raw[i], raw[i + 2] = raw[i + 2], raw[i]
    buffer = crypto.CryptographicBuffer.create_from_byte_array(bytes(raw))
    try:
        bitmap = imaging.SoftwareBitmap.create_copy_from_buffer(
            buffer, imaging.BitmapPixelFormat.bgra8, w, h)
    except Exception:
        return None
    try:
        result = engine.recognize_async(bitmap).get()
    except Exception:
        return None
    lines: List[Dict[str, Any]] = []
    for line in result.lines:
        r = line.bounding_rect
        lines.append({"text": line.text, "x": int(r.x), "y": int(r.y),
                      "w": int(r.width), "h": int(r.height)})
    return lines
