# ==============================================================================
# see.py - The eyes. Server-side: focus, capture, OCR, landmarks, thumbnail.
# Replaces screenshot + region shot + metrics + agent-side OCR scripts.
#
# Contract with the agent:
#   * Returns a REAL MCP image content block (multimodal models look at pixels).
#   * Returns OCR lines with PHYSICAL boxes -> coordinates feed mouse_click 1:1.
#   * Returns guessed landmarks (composer, send button, latest text block) so
#     the model never guesses (750,590) mid-screen again.
#   * ZERO localhost URLs, ZERO scale_x math - all conversions happen here.
#   * click_text: click a matched label internally and return where it clicked.
# ==============================================================================
from __future__ import annotations

import asyncio
import io
import time
from typing import Any, Dict, List, Optional

try:
    from .. import config
except ImportError:  # direct script execution
    import config  # type: ignore

from modules import desktop_native
from modules.ocr_engine import box_center, find_text_box, ocr_image_async


# ------------------------------------------------------------------------------
# Target resolution: bring the right thing to the foreground first
# ------------------------------------------------------------------------------

def _focus_target(target: Any) -> Dict[str, Any]:
    """
    target: 'foreground' | 'window:<title substring>' | {'x','y','w','h'}
    For region targets we still focus nothing - coordinates are physical.
    """
    if isinstance(target, dict) and all(k in target for k in ("x", "y", "w", "h")):
        return {"mode": "region", "rect": {
            "x": int(target["x"]), "y": int(target["y"]),
            "w": int(target["w"]), "h": int(target["h"])}}

    t = str(target or "foreground")
    if t.startswith("window:"):
        hint = t.split(":", 1)[1]
        from modules import chrome_control
        proof = chrome_control.focus_chrome_window(hint)
        return {"mode": "window", "hint": hint, "focus": proof}
    return {"mode": "foreground"}


# ------------------------------------------------------------------------------
# Landmark guessing from OCR boxes (pure geometry + label heuristics)
# ------------------------------------------------------------------------------

_COMPOSER_LABELS = ("ask anything", "ask anything", "message", "type a message",
                    "send a message", "chat", "reply", "prompt", "say")  # 'search' removed:
# taskbar Search poisoned the landmark every single time (field report)
_SEND_LABELS = ("send", "submit", "arrow", "post")


def _in_exclusion(line: Dict[str, Any]) -> bool:
    """Drop OCR boxes inside the taskbar band / toast corner so landmarks and
    clicks never fire there (the 'bottom of screen + search' bug)."""
    try:
        from modules.mouse_runtime import _in_exclusions
        return _in_exclusions(line["x"] + line["w"] / 2, line["y"] + line["h"] / 2)
    except Exception:
        return False


def _guess_landmarks(ocr: Dict[str, Any], img_w: int, img_h: int) -> Dict[str, Any]:
    lines = [l for l in ocr.get("lines", []) if not _in_exclusion(l)]
    landmarks: Dict[str, Any] = {}
    landmarks["excluded_zones"] = "taskbar + toast area ignored (old false-positive source)"

    # Composer: a labeled hit in the bottom 60% of the capture
    composer = None
    for label in _COMPOSER_LABELS:
        for line in lines:
            if label in line["text"].lower() and line["y"] > img_h * 0.4:
                if composer is None or line["y"] > composer["y"]:
                    composer = line
    if composer:
        cx, cy = box_center(composer)
        landmarks["composer"] = {
            "text": composer["text"], "x": cx, "y": cy,
            "box": {k: composer[k] for k in ("x", "y", "w", "h")},
            "confidence": "ocr_label",
        }
    else:
        # Geometric fallback: chat UIs put the composer near bottom-center
        landmarks["composer"] = {
            "x": img_w // 2, "y": int(img_h * 0.88),
            "confidence": "geometric_guess",
        }

    # Send: rightmost 'send'-ish label near the composer's height
    send = None
    for label in _SEND_LABELS:
        for line in lines:
            if label in line["text"].lower():
                if composer and abs(line["y"] - composer["y"]) < 120:
                    if send is None or line["x"] > send["x"]:
                        send = line
    if send:
        sx, sy = box_center(send)
        landmarks["send_button"] = {"text": send["text"], "x": sx, "y": sy,
                                    "confidence": "ocr_label"}

    # Latest assistant bubble: heuristics - largest text mass in the upper 80%
    big = max(lines, key=lambda l: l["w"] * l["h"], default=None)
    if big and big["y"] < img_h * 0.8:
        landmarks["latest_text_block"] = {
            "text": big["text"][:120], "x": big["x"] + big["w"] // 2,
            "y": big["y"] + big["h"] // 2, "confidence": "largest_block",
        }

    landmarks["image_size"] = {"width": img_w, "height": img_h}
    landmarks["note"] = ("all boxes are PHYSICAL screen pixels - feed them "
                         "straight into mouse_click; no scale math")
    return landmarks


# ------------------------------------------------------------------------------
# MCP image content (what multimodal models actually consume)
# ------------------------------------------------------------------------------

def _image_content(pil_img, max_width: int = 800, quality: int = 60) -> Optional[Dict[str, Any]]:
    try:
        from PIL import Image
        img = pil_img
        if img.width > max_width:
            ratio = max_width / img.width
            img = img.resize((max_width, int(img.height * ratio)), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=quality)
        import base64
        return {"type": "image",
                "data": base64.b64encode(buf.getvalue()).decode("ascii"),
                "mimeType": "image/jpeg"}
    except Exception:
        return None


# ------------------------------------------------------------------------------
# Main entry
# ------------------------------------------------------------------------------

async def see(target: Any = "foreground",
              want: Optional[List[str]] = None,
              click_text: Optional[str] = None,
              ocr_lang: str = "en-US",
              thumbnail_width: int = 800,
              quality: int = 60) -> Dict[str, Any]:
    t0 = time.monotonic()
    want = want or ["ocr", "landmarks", "thumbnail"]

    # 1) Focus whatever we were asked to look at
    t = _focus_target(target)
    if t["mode"] == "window" and not t["focus"].get("verified"):
        return {"status": "failed",
                "error": f"could not bring target window to front: {t['focus']}"}

    # 2) Capture physical pixels
    if t["mode"] == "region":
        r = t["rect"]
        shot = desktop_native.take_region_screenshot(r["x"], r["y"], r["w"], r["h"],
                                                     quality=90)
    else:
        shot = desktop_native.take_screenshot(scaled_width=0, quality=90)  # full res
    path = shot.get("inspection_image_path")
    if not path:
        return {"status": "failed", "error": f"capture failed: {shot}"}

    from PIL import Image
    img = Image.open(path)
    out: Dict[str, Any] = {
        "status": "ok",
        "mode": t["mode"],
        "captured_size": {"width": img.width, "height": img.height},
        "region": t.get("rect"),
        "window_title": desktop_native._active_window_title(),
    }

    # 3) OCR (server-side - the agent never writes vision scripts)
    if "ocr" in want or "landmarks" in want or click_text:
        ocr = await ocr_image_async(img, ocr_lang)
        out["ocr_engine"] = ocr.get("engine")
        all_lines = ocr.get("lines", [])
        # feed the mouse runtime's eyes cache so `point` can pre-aim instantly
        try:
            from modules.mouse_runtime import feed_eyes
            feed_eyes(img, all_lines, origin=t.get("rect") and (t["rect"]["x"], t["rect"]["y"]) or (0, 0),
                      window=out.get("window_title", ""))
        except Exception:
            pass
        vis_lines = [l for l in all_lines if not _in_exclusion(l)]
        if "ocr" in want:
            out["ocr"] = {"full_text": ocr.get("full_text", ""),
                          "lines": vis_lines,
                          "excluded_count": len(all_lines) - len(vis_lines)}

        # 4) Landmarks
        if "landmarks" in want:
            out["landmarks"] = _guess_landmarks({**ocr, "lines": vis_lines}, img.width, img.height)

        # 5) Internal click on a matched label - through the point pipeline
        #    (teleport + proof + jitter retry; NOT a blind pyautogui click)
        if click_text:
            box = find_text_box([l for l in all_lines if not _in_exclusion(l)], click_text)
            if box:
                from modules.mouse_runtime import click_box_with_proof
                origin_x, origin_y = (t["rect"]["x"], t["rect"]["y"]) if t.get("rect") else (0, 0)
                phys_box = {"x": box["x"] + origin_x, "y": box["y"] + origin_y,
                            "w": box["w"], "h": box["h"]}
                click_result = click_box_with_proof(phys_box, window="", until="")
                out["clicked"] = {"text": box["text"],
                                  "x": click_result.get("hit", {}).get("x", phys_box["x"] + phys_box["w"] // 2),
                                  "y": click_result.get("hit", {}).get("y", phys_box["y"] + phys_box["h"] // 2),
                                  "verified": click_result.get("status") in ("hit", "ok"),
                                  "motion": click_result.get("motion"),
                                  "tries": click_result.get("tries"),
                                  "ms": click_result.get("ms")}
            else:
                out["clicked"] = {"verified": False,
                                  "error": f"text {click_text!r} not found in OCR"}

    # 6) Thumbnail as REAL MCP image content
    if "thumbnail" in want:
        image = _image_content(img, max_width=thumbnail_width, quality=quality)
        if image is not None:
            out["image"] = image
        else:
            out["thumbnail_error"] = "image encoding failed"

    out["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
    out["screenshot_saved_to"] = path  # for debugging only; the image content
    # above is the primary artifact - fetching URLs is NOT required anymore.
    return out
