# ==============================================================================
# web_task.py - THE autopilot. One call: open page, do the thing, bring back
# the product. Internally tiers: CDP -> vision/OCR. Never kills Chrome.
# Also implements the generic assistant-reply poller that replaces every
# site-specific foo_ask_and_read macro.
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

# Selectors for editable "composer" elements, in priority order
_COMPOSER_SELECTORS = (
    'textarea',
    'input[type="text"]',
    'input[type="search"]',
    'input:not([type])',
    '[contenteditable="true"]',
    '[role="textbox"]',
)

# JS: find the best visible editable element (largest usable area wins),
# skipping decoys (display:none, tiny boxes, readOnly)
_FIND_COMPOSER_JS = """
() => {
    const candidates = document.querySelectorAll(
        'textarea, input[type="text"], input[type="search"], input:not([type]), [contenteditable="true"], [role="textbox"]'
    );
    let best = null, bestScore = -1;
    for (const el of candidates) {
        const r = el.getBoundingClientRect();
        const st = getComputedStyle(el);
        const visible = r.width >= 40 && r.height >= 12 && st.visibility !== 'hidden'
            && st.display !== 'none' && !el.disabled && !el.readOnly;
        if (!visible) continue;
        const score = r.width * r.height;
        if (score > bestScore) { bestScore = score; best = el; }
    }
    if (!best) return null;
    best.setAttribute('data-mcp-composer', '1');
    return {
        tag: best.tagName, id: best.id || null,
        contenteditable: !!best.isContentEditable,
        rect: (({x,y,width,height}) => ({x,y,width,height}))(best.getBoundingClientRect()),
    };
}
"""

_CLEAR_COMPOSER_MARK = "() => { const el = document.querySelector('[data-mcp-composer]'); if (el) el.removeAttribute('data-mcp-composer'); }"

_READ_FOCUS_JS = """
() => {
    const el = document.activeElement;
    if (!el) return { focused: false, value: '' };
    const editable = el.tagName === 'TEXTAREA' || el.tagName === 'INPUT' || el.isContentEditable;
    const value = editable ? (el.isContentEditable ? (el.innerText || '') : (el.value || '')) : '';
    return { focused: editable, tag: el.tagName || null, value };
}
"""

# Chat snapshot: capture visible conversation text before sending
_CHAT_TEXT_JS = """
() => {
    const main = document.querySelector('main') || document.body;
    return (main.innerText || '').slice(-12000);
}
"""


# ==============================================================================
# Attachment tier
# ==============================================================================

async def _attach() -> Dict[str, Any]:
    """Tier 0: get a CDP-attached Playwright browser, or report it's down."""
    from modules import chrome_control
    state = chrome_control.op_ensure_debug()
    if not state.get("cdp_available"):
        return {"ok": False, "error": state.get("error", "CDP unavailable")}
    try:
        from playwright.async_api import async_playwright
        pw = await async_playwright().start()
        browser = await pw.chromium.connect_over_cdp(config.CDP_ENDPOINT)
        return {"ok": True, "pw": pw, "browser": browser}
    except Exception as e:
        return {"ok": False, "error": f"CDP attach failed: {e}"}


async def _get_or_open_page(browser, url: str, timeout_ms: int):
    """Reuse a tab already on this URL, else open a new one."""
    ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
    for p in ctx.pages:
        if url.lower() in (p.url or "").lower():
            await p.bring_to_front()
            return p, "switched"
    page = await ctx.new_page()
    await page.goto(url, timeout=max(timeout_ms, 20000), wait_until="domcontentloaded")
    await page.bring_to_front()
    return page, "opened"


# ==============================================================================
# Composer interaction (CDP tiers)
# ==============================================================================

async def _fill_composer_cdp(page, selector: Optional[str], text: str) -> Dict[str, Any]:
    """Try exact selector, then DOM heuristic. Returns fill info or failure."""
    for method, sel in (("cdp_selector", selector), ("dom_heuristic", None)):
        if sel is None and method == "cdp_selector":
            continue
        try:
            if sel:
                locator = page.locator(sel).first
                await locator.wait_for(state="visible", timeout=4000)
            else:
                info = await page.evaluate(_FIND_COMPOSER_JS)
                if not info:
                    continue
                locator = page.locator('[data-mcp-composer="1"]').first

            await locator.click(timeout=3000)
            await asyncio.sleep(0.1)
            try:
                await locator.fill(text, timeout=3000)
            except Exception:
                await page.evaluate("(t) => document.execCommand('insertText', false, t)", text)
            await asyncio.sleep(0.15)

            read = await page.evaluate(_READ_FOCUS_JS)
            if read.get("focused") and (read.get("value") or "").strip() == text.strip():
                return {"ok": True, "method": method, "read": read}
            # Read-back mismatch: one clipboard-paste retry
            try:
                await locator.click(timeout=2000)
                await page.keyboard.press("Control+a")
                await page.keyboard.press("Delete")
                from modules import desktop_native
                desktop_native.set_clipboard_and_paste(text)
                await asyncio.sleep(0.25)
                read = await page.evaluate(_READ_FOCUS_JS)
                if read.get("focused") and (read.get("value") or "").strip() == text.strip():
                    return {"ok": True, "method": method + "+clipboard_retry", "read": read}
            except Exception:
                pass
            return {"ok": False, "method": method, "read": read,
                    "error": "typed value did not read back as the message"}
        except Exception:
            continue
        finally:
            try:
                await page.evaluate(_CLEAR_COMPOSER_MARK)
            except Exception:
                pass
    return {"ok": False, "error": "no visible editable composer found via CDP"}


async def _read_composer_value(page):
    """Value of the focused editable element: None if none/editable-gone."""
    return await page.evaluate(_READ_FOCUS_JS)


async def _submit(page) -> bool:
    """
    Submit with PROOF, not vibes:
      1. Press Enter. If the composer disappears (navigation/CE swap) or gets
         cleared by the site -> submitted.
      2. If the value is unchanged (plain textarea newline, no submit handler)
         scan for a send control: standard attrs first, then ANY element with
         role=button (or native button) whose text/aria-label looks like a
         send control (covers text-labeled and icon-only buttons).
      3. After each fallback click, re-verify the composer state.
    """
    async def _composer_state():
        read = await _read_composer_value(page)
        if not read.get("focused"):
            return "gone"
        v = (read.get("value") or "").strip()
        return "cleared" if v == "" else "has_text"

    before = await _composer_state()

    # 1) Enter first (works on every real chat app; contenteditable or
    #    submit-on-enter boxes clear or lose focus)
    await page.keyboard.press("Enter")
    await asyncio.sleep(0.6)
    after = await _composer_state()
    if before != "gone" and after in ("gone", "cleared"):
        return True

    # 2) Button fallback: standard attributes, then role/text scan
    for btn_sel in ('button[type="submit"]',
                    'button[aria-label*="send" i]',
                    'button[data-testid*="send" i]',
                    '[role="button"][aria-label*="send" i]'):
        try:
            btn = page.locator(btn_sel).first
            await btn.click(timeout=1200)
            await asyncio.sleep(0.5)
            if await _composer_state() in ("gone", "cleared"):
                return True
        except Exception:
            continue

    try:
        clicked = await page.evaluate(
            """
            () => {
                const isSendy = (t) => /^(send|submit|post|reply)$/i.test((t || '').trim())
                    || ['\\u27A4','\\u279C','\\u2192','\\u23CE','\\u27A0','\\u2794'].some(ch => (t || '').includes(ch));
                const nodes = document.querySelectorAll('[role="button"], button, input[type="submit"]');
                for (const el of nodes) {
                    if (el.disabled) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width < 4 || r.height < 4) continue;
                    const label = el.getAttribute('aria-label') || el.innerText || el.value || '';
                    if (isSendy(label)) {
                        const proto = el instanceof HTMLButtonElement ? HTMLElement.prototype : HTMLElement.prototype;
                        el.click();
                        return { clicked: true, label: String(label).slice(0, 40) };
                    }
                }
                return { clicked: false };
            }
            """
        )
        if isinstance(clicked, dict) and clicked.get("clicked"):
            await asyncio.sleep(0.6)
            if await _composer_state() in ("gone", "cleared"):
                return True
    except Exception:
        pass

    # 3) Last resort: Ctrl+Enter (some composers use it)
    await page.keyboard.press("Control+Enter")
    await asyncio.sleep(0.6)
    if await _composer_state() in ("gone", "cleared"):
        return True
    return False


# ==============================================================================
# Generic assistant-reply polling (replaces chatgpt_*/notion_* macros)
# ==============================================================================

def _extract_new_content(before: str, after: str) -> str:
    """
    Diff two chat snapshots and return the NEW text. SequenceMatcher handles
    every layout: bubble appended at end, inserted above the composer, or
    mid-stream edits - unlike naive suffix/prefix chopping.
    NOTE: opcode j1/j2 index into AFTER (b); i1/i2 index into BEFORE (a).
    """
    if not before:
        return after.strip()
    from difflib import SequenceMatcher
    sm = SequenceMatcher(None, before, after, autojunk=False)
    pieces = [after[j1:j2] for tag, i1, i2, j1, j2 in sm.get_opcodes()
              if tag in ("insert", "replace")]
    return "\n".join(p.strip() for p in pieces if p.strip())


async def _wait_assistant_reply(page, before_text: str, timeout_ms: int) -> Dict[str, Any]:
    """
    Poll for a NEW assistant bubble: DOM text must grow beyond the pre-send
    snapshot AND then stay stable for ~1.2s (streaming finished).
    """
    deadline = time.monotonic() + timeout_ms / 1000
    stable_since = None
    last_text = before_text

    while time.monotonic() < deadline:
        await asyncio.sleep(0.5)
        try:
            current = await page.evaluate(_CHAT_TEXT_JS)
        except Exception:
            continue  # navigation hiccup
        if current != last_text:
            # Any change (including short replies) resets the stability clock;
            # the stability window itself filters spinner text churn.
            last_text = current
            stable_since = None
            continue
        if len(current) > len(before_text):
            if stable_since is None:
                stable_since = time.monotonic()
            elif time.monotonic() - stable_since >= 1.2:
                new_content = _extract_new_content(before_text, current)
                if new_content.strip():
                    return {"ok": True, "text": new_content}
        # else: nothing new yet, keep polling

    return {"ok": False, "error": f"no assistant reply detected within {timeout_ms}ms",
            "partial": _extract_new_content(before_text, last_text)}


# ==============================================================================
# Vision tier (no CDP at all)
# ==============================================================================

def _vision_fallback(url: str, text: str, submit: bool) -> Dict[str, Any]:
    """OCR-driven fallback: focus Chrome window, screenshot, find composer by
    OCR, click it, paste, verify via OCR diff, submit, diff for reply."""
    import pyautogui
    from modules import chrome_control, desktop_native
    from modules.ocr_engine import box_center, find_text_box, ocr_image_async

    focused = chrome_control.focus_chrome_window()
    if not focused.get("verified"):
        return {"ok": False, "error": f"vision tier could not focus a Chrome window: {focused}"}

    result: Dict[str, Any] = {"ok": False, "method_used": "vision", "steps": []}

    # 1) Open the URL in a fresh tab via keyboard (no CDP available)
    pyautogui.hotkey("ctrl", "t")
    time.sleep(0.6)
    pyautogui.typewrite(url, interval=0.01)
    pyautogui.press("enter")
    time.sleep(5.0)  # crude but dependency-free page load wait

    shot = desktop_native.take_screenshot(scaled_width=0, quality=80)  # full res
    img_path = shot.get("inspection_image_path") or shot.get("path")
    if not img_path:
        return {"ok": False, "error": "vision tier could not capture the screen", **result}
    from PIL import Image
    img = Image.open(img_path)
    loop = asyncio.new_event_loop()
    try:
        ocr = loop.run_until_complete(ocr_image_async(img))
    finally:
        loop.close()
    result["steps"].append({"ocr_lines": len(ocr.get("lines", []))})

    # 2) Find the composer: lowest editable-looking OCR hit (chat UIs put it at
    #    the bottom), guided by common labels; else click bottom-center strip.
    composer_box = None
    for label in ("Ask anything", "Message", "Type a message", "Send a message", "Chat"):
        hit = find_text_box(ocr.get("lines", []), label)
        if hit and hit["y"] > img.size[1] * 0.5:  # bottom half only
            composer_box = hit
            break
    if composer_box is None:
        w, h = img.size
        composer_box = {"x": int(w * 0.3), "y": int(h * 0.85), "w": int(w * 0.4), "h": 40}
        result["steps"].append({"composer": "bottom_strip_guess"})
    else:
        result["steps"].append({"composer": composer_box["text"]})

    cx, cy = box_center(composer_box)
    desktop_native.mouse_click(cx, cy, "left", 1)
    time.sleep(0.3)

    # 3) Paste + verify via OCR (text must appear on screen)
    desktop_native.set_clipboard_and_paste(text)
    time.sleep(0.4)
    shot2 = desktop_native.take_screenshot(scaled_width=0, quality=80)
    from PIL import Image as _I
    img2 = _I.open(shot2.get("inspection_image_path") or shot2.get("path"))
    loop = asyncio.new_event_loop()
    try:
        ocr2 = loop.run_until_complete(ocr_image_async(img2))
    finally:
        loop.close()
    needle = text.strip().split("\n")[0][:40].lower()
    typed_verified = needle in ocr2.get("full_text", "").lower()
    result["typed_verified"] = typed_verified
    result["steps"].append({"typed_verified": typed_verified})

    # 4) Submit
    if submit:
        desktop_native.keyboard_hotkey(["enter"])
        time.sleep(1.0)

    result["ok"] = True
    result["note"] = "vision tier completed; reply extraction requires CDP for reliable diffing"
    return result


# ==============================================================================
# MCP image content helper (real image content, NOT base64-in-JSON, NOT :8000)
# ==============================================================================

def _image_content(pil_img, max_width: int = 800, quality: int = 60) -> Optional[Dict[str, Any]]:
    try:
        from PIL import Image
        img = pil_img
        if img.width > max_width:
            ratio = max_width / img.width
            img = img.resize((max_width, int(img.height * ratio)), Image.LANCZOS)
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=quality)
        import base64
        return {
            "type": "image",
            "data": base64.b64encode(buf.getvalue()).decode("ascii"),
            "mimeType": "image/jpeg",
        }
    except Exception:
        return None


def _crop_bottom(pil_img, fraction: float = 0.45):
    """Composer + latest bubbles usually live in the bottom 45% of the page."""
    w, h = pil_img.size
    return pil_img.crop((0, int(h * (1 - fraction)), w, h))


# ==============================================================================
# Main orchestrator
# ==============================================================================

async def run_web_task(
    url: str,
    message: Optional[str] = None,
    selector: Optional[str] = None,
    submit: bool = True,
    wait_reply: bool = True,
    timeout_ms: int = 20000,
    include_image: bool = True,
) -> Dict[str, Any]:
    t0 = time.monotonic()

    # ---- Tier A: CDP path -----------------------------------------------------
    att = await _attach()
    if not att.get("ok"):
        # ---- Tier B: vision path (no CDP, never kill Chrome) -------------------
        vis = await asyncio.get_running_loop().run_in_executor(
            None, lambda: _vision_fallback(url, message or "", submit))
        out: Dict[str, Any] = {
            "status": "fallback_used" if vis.get("ok") else "failed",
            "method_used": "vision",
            "url": url,
            "elapsed_ms": int((time.monotonic() - t0) * 1000),
            **{k: v for k, v in vis.items() if k not in ("ok",)},
        }
        return out

    pw, browser = att["pw"], att["browser"]
    try:
        page, tab_action = await _get_or_open_page(browser, url, timeout_ms)

        # Pre-send snapshot for the reply diff
        before_text = await page.evaluate(_CHAT_TEXT_JS) if (message and wait_reply) else ""

        typed, typed_verified, submitted = None, False, False
        if message is not None:
            fill = await _fill_composer_cdp(page, selector, message)
            typed = message
            typed_verified = bool(fill.get("ok"))
            if not typed_verified:
                # Last CDP attempt: physical click at composer rect + paste
                try:
                    info = await page.evaluate(_FIND_COMPOSER_JS)
                    if info and info.get("rect"):
                        r = info["rect"]
                        from modules import desktop_native
                        # viewport->physical: use window screen offset + dpr
                        geo = await page.evaluate(
                            "() => ({sx: window.screenX, sy: window.screenY,"
                            " ow: window.outerWidth, iw: window.innerWidth,"
                            " oh: window.outerHeight, ih: window.innerHeight,"
                            " dpr: window.devicePixelRatio || 1})")
                        vx = geo["sx"] + (geo["ow"] - geo["iw"]) / 2
                        vy = geo["sy"] + (geo["oh"] - geo["ih"]) - (geo["ow"] - geo["iw"]) / 2
                        px = int((vx + r["x"] + r["width"] / 2) * geo["dpr"])
                        py = int((vy + r["y"] + r["height"] / 2) * geo["dpr"])
                        desktop_native.mouse_click(px, py, "left", 1)
                        await asyncio.sleep(0.25)
                        desktop_native.set_clipboard_and_paste(message)
                        await asyncio.sleep(0.3)
                        read = await page.evaluate(_READ_FOCUS_JS)
                        typed_verified = bool(read.get("focused")
                                              and (read.get("value") or "").strip() == message.strip())
                except Exception:
                    pass
            if not typed_verified:
                return {
                    "status": "failed",
                    "method_used": "cdp",
                    "url": url,
                    "error": "message could not be verified in the composer after fill, paste and physical-click attempts",
                    "elapsed_ms": int((time.monotonic() - t0) * 1000),
                }

            if submit:
                submitted = await _submit(page)

        # ---- Wait for the product ------------------------------------------
        reply = None
        if message is not None and wait_reply and submitted:
            # Front the tab before polling: occluded/hidden pages get their
            # timers throttled by Chrome, which would starve reply detection.
            try:
                await page.bring_to_front()
            except Exception:
                pass
            rep = await _wait_assistant_reply(page, before_text, timeout_ms)
            reply = rep.get("text") if rep.get("ok") else None
            reply_error = None if rep.get("ok") else rep.get("error")

        # ---- Image proof (MCP image content, cropped to the conversation) ----
        image = None
        if include_image:
            try:
                raw = await page.screenshot(type="png")
                from PIL import Image
                full = Image.open(io.BytesIO(raw))
                image = _image_content(_crop_bottom(full))
            except Exception:
                image = None

        out = {
            "status": "ok" if (message is None or (typed_verified and (submitted or not submit))) else "failed",
            "method_used": "cdp",
            "tab_action": tab_action,
            "url": page.url,
            "title": await page.title(),
            "typed": typed,
            "typed_verified": typed_verified,
            "submitted": submitted,
            "assistant_reply": reply,
            "elapsed_ms": int((time.monotonic() - t0) * 1000),
        }
        if message is not None and wait_reply and submitted and reply is None:
            out["reply_note"] = "submitted but no new assistant bubble detected in time"
            out["reply_error"] = (rep.get("error") if isinstance(rep, dict) else None)
        if image is not None:
            out["image"] = image
        return out
    except Exception as e:
        return {
            "status": "failed", "method_used": "cdp", "url": url,
            "error": f"{type(e).__name__}: {e}",
            "elapsed_ms": int((time.monotonic() - t0) * 1000),
        }
    finally:
        try:
            await browser.close()
        except Exception:
            pass
        try:
            await pw.stop()
        except Exception:
            pass
