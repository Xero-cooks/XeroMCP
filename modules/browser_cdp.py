# ==============================================================================
# browser_cdp.py - Playwright CDP hooks for the active Chrome instance.
# Native async only: tools are coroutines that await directly on FastMCP's
# running ASGI loop - no synchronous event-loop bootstrapping allowed here.
# ==============================================================================
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

try:
    from .. import config
except ImportError:  # direct script execution
    import config  # type: ignore

# Standard editable-element candidates for Tier 1 / Tier 2
_INPUT_SELECTORS = (
    'textarea',
    'input[type="text"]',
    'input[type="search"]',
    'input:not([type])',
    '[contenteditable="true"]',
    '[role="textbox"]',
)

# Heuristic DOM search: visible, enabled, editable elements
_DOM_HEURISTIC_JS = """
() => {
    const candidates = document.querySelectorAll(
        'textarea, input[type="text"], input[type="search"], input:not([type]), [contenteditable="true"], [role="textbox"]'
    );
    const visible = [];
    for (const el of candidates) {
        const r = el.getBoundingClientRect();
        const style = getComputedStyle(el);
        const isVisible = r.width > 10 && r.height > 10 &&
            style.visibility !== 'hidden' && style.display !== 'none' &&
            !el.disabled && !el.readOnly;
        if (isVisible) visible.push(el);
    }
    if (visible.length === 0) return null;
    // Pick the largest visible editable element (most likely the composer)
    let best = visible[0], bestArea = 0;
    for (const el of visible) {
        const r = el.getBoundingClientRect();
        const area = r.width * r.height;
        if (area > bestArea) { bestArea = area; best = el; }
    }
    // Tag it for lookup from Python
    best.setAttribute('data-mcp-target', '1');
    return {
        tag: best.tagName,
        id: best.id || null,
        name: best.name || null,
        classes: best.className ? String(best.className).slice(0, 120) : null,
        contenteditable: best.isContentEditable,
        rect: (() => { const r = best.getBoundingClientRect(); return {x: r.x, y: r.y, width: r.width, height: r.height}; })(),
    };
}
"""

# Read-back of the currently focused editable element's value
_READ_FOCUSED_JS = """
() => {
    const el = document.activeElement;
    if (!el) return { focused: false };
    const isEditable = el.tagName === 'TEXTAREA' || el.tagName === 'INPUT' || el.isContentEditable;
    if (!isEditable) return { focused: false, tag: el.tagName || null };
    const value = el.isContentEditable ? (el.innerText || '') : (el.value || '');
    return {
        focused: true,
        tag: el.tagName,
        id: el.id || null,
        value: value,
        selection_start: (typeof el.selectionStart === 'number') ? el.selectionStart : null,
    };
}
"""

_VERIFY_INPUT_JS = """
(expected) => {
    const el = document.activeElement;
    if (!el) return { focused: false, matches: false, value: '' };
    const isEditable = el.tagName === 'TEXTAREA' || el.tagName === 'INPUT' || el.isContentEditable;
    const value = isEditable ? (el.isContentEditable ? (el.innerText || '') : (el.value || '')) : '';
    return {
        focused: isEditable,
        tag: el.tagName,
        value: value,
        matches: isEditable && value.trim() === String(expected).trim(),
    };
}
"""

# Post-submit verification: focused element value cleared OR DOM grew with new content
_POST_SUBMIT_JS = """
() => {
    const el = document.activeElement;
    const cleared = el && (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT')
        ? (el.value || '').trim() === ''
        : false;
    const bodyLen = (document.body.innerText || '').length;
    return { input_cleared: cleared, body_text_length: bodyLen };
}
"""


# ==============================================================================
# Connection handling (async, loop-safe)
# ==============================================================================

async def _connect():
    """Connect Playwright to the running Chrome over CDP (keeps real sessions)."""
    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    return await pw.chromium.connect_over_cdp(config.CDP_ENDPOINT)


async def _find_page(browser, url_match: str):
    for ctx in browser.contexts:
        for page in ctx.pages:
            if url_match in (page.url or ""):
                return page
    return None


def _cdp_unavailable(error: Exception) -> Dict[str, Any]:
    return {
        "status": "failed",
        "method_used": None,
        "error": f"CDP connection failed: {error}",
        "hint": f"Chrome must be running with --remote-debugging-port={config.CDP_PORT}. "
                "Use the launch_chrome_with_cdp tool first (fully exit Chrome before relaunching).",
    }


# ==============================================================================
# Tool: list_active_tabs (async native)
# ==============================================================================

async def _list_active_tabs() -> List[Dict[str, str]]:
    browser = await _connect()
    try:
        tabs: List[Dict[str, str]] = []
        for ctx in browser.contexts:
            for page in ctx.pages:
                tabs.append({"url": page.url, "title": await page.title()})
        return tabs
    finally:
        await browser.close()


# ==============================================================================
# Tool: switch_or_open_tab (Requirement E)
# ==============================================================================

async def _switch_or_open_tab(url_pattern: str, new_tab_if_missing: bool):
    browser = await _connect()
    try:
        # 1) Try to find an existing page
        for ctx in browser.contexts:
            for page in ctx.pages:
                if url_pattern.lower() in (page.url or "").lower():
                    await page.bring_to_front()
                    try:
                        await page.wait_for_load_state("domcontentloaded", timeout=10000)
                    except Exception:
                        pass  # page already loaded / SPA already interactive
                    return {
                        "status": "success",
                        "action": "switched",
                        "url": page.url,
                        "title": await page.title(),
                    }

        # 2) Not found: open a new tab if allowed
        if new_tab_if_missing:
            ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = await ctx.new_page()
            try:
                await page.goto(url_pattern, timeout=20000, wait_until="domcontentloaded")
            except Exception as e:
                return {"status": "failed", "action": "open", "error": f"Navigation failed: {e}"}
            await page.bring_to_front()
            return {"status": "success", "action": "opened_new_tab", "url": page.url, "title": await page.title()}

        return {"status": "failed", "error": f"No tab matching '{url_pattern}' and new_tab_if_missing=False."}
    finally:
        await browser.close()


async def switch_or_open_tab(url_pattern: str, new_tab_if_missing: bool = True) -> Dict[str, Any]:
    """
    CDP-backed tab switcher: bring an existing tab matching url_pattern to the
    front and wait for domcontentloaded, or optionally open it in a new tab.
    """
    try:
        return await _switch_or_open_tab(url_pattern, new_tab_if_missing)
    except Exception as e:
        return _cdp_unavailable(e)


# ==============================================================================
# Tool: browser_open_and_act (Requirement A) - atomic 3-tier action engine
# ==============================================================================

async def _tier1_cdp_selector(page, selector: str, text: Optional[str]) -> Optional[Dict[str, Any]]:
    """Fill via an exact CSS selector."""
    try:
        locator = page.locator(selector).first
        await locator.wait_for(state="visible", timeout=4000)
        if text is not None:
            try:
                await locator.fill(text, timeout=3000)
            except Exception:
                # contenteditable elements: focus + insertText
                await locator.click(timeout=3000)
                await page.evaluate("(t) => document.execCommand('insertText', false, t)", text)
        else:
            await locator.click(timeout=3000)
        return {"tag": await locator.evaluate("el => el.tagName")}
    except Exception:
        return None


async def _tier2_dom_heuristic(page, text: Optional[str]) -> Optional[Dict[str, Any]]:
    """Find the largest visible editable element via JS heuristics and fill it."""
    try:
        info = await page.evaluate(_DOM_HEURISTIC_JS)
        if not info:
            return None
        locator = page.locator('[data-mcp-target="1"]').first
        if text is not None:
            try:
                await locator.fill(text, timeout=3000)
            except Exception:
                await locator.click(timeout=3000)
                await page.evaluate("(t) => document.execCommand('insertText', false, t)", text)
        else:
            await locator.click(timeout=3000)
        return info
    except Exception:
        return None
    finally:
        try:
            await page.evaluate("() => { const el = document.querySelector('[data-mcp-target]'); if (el) el.removeAttribute('data-mcp-target'); }")
        except Exception:
            pass


async def _tier3_physical_fallback(page, selector_hint: Optional[str], text: Optional[str]) -> Optional[Dict[str, Any]]:
    """
    Last resort: get the element's bounding box, physical-click its center with
    pyautogui, then paste text from the clipboard.
    """
    if text is None:
        return None
    try:
        # Locate element (selector hint or Tier-2 heuristic target)
        if selector_hint:
            locator = page.locator(selector_hint).first
            try:
                await locator.wait_for(state="attached", timeout=3000)
            except Exception:
                locator = None
        else:
            info = await page.evaluate(_DOM_HEURISTIC_JS)
            if not info:
                return None
            locator = page.locator('[data-mcp-target="1"]').first

        if locator is None:
            return None
        box = await locator.bounding_box()
        if not box:
            return None

        cx_vp = box["x"] + box["width"] / 2
        cy_vp = box["y"] + box["height"] / 2

        # Convert viewport CSS px -> physical screen px. Under CDP with a
        # maximized window on a DPI-aware 1080p screen, 1 CSS px ~= 1 device px,
        # but window chrome offsets differ; use window.screen metrics.
        metrics = await page.evaluate(
            "() => ({ sx: window.screenX, sy: window.screenY, "
            "ow: window.outerWidth, iw: window.innerWidth, "
            "oh: window.outerHeight, ih: window.innerHeight })"
        )
        # device pixel ratio for physical mapping
        dpr = await page.evaluate("() => window.devicePixelRatio || 1")
        # viewport origin in screen coords (CSS px), then scale to physical
        vx_css = metrics["sx"] + (metrics["ow"] - metrics["iw"]) / 2
        vy_css = metrics["sy"] + (metrics["oh"] - metrics["ih"]) - (metrics["ow"] - metrics["iw"]) / 2
        phys_x = int((vx_css + cx_vp) * dpr)
        phys_y = int((vy_css + cy_vp) * dpr)

        from modules import desktop_native
        desktop_native.mouse_click(phys_x, phys_y, "left", 1)
        time.sleep(0.15)
        if text:
            desktop_native.set_clipboard_and_paste(text)
            time.sleep(0.1)
        return {
            "tag": "physical_click",
            "rect": {"x": phys_x, "y": phys_y, "width": int(box["width"]), "height": int(box["height"])},
        }
    except Exception:
        return None
    finally:
        try:
            await page.evaluate("() => { const el = document.querySelector('[data-mcp-target]'); if (el) el.removeAttribute('data-mcp-target'); }")
        except Exception:
            pass


def _inspection_artifacts() -> Dict[str, str]:
    from modules import desktop_native
    path = desktop_native.get_inspections_dir() / "latest.jpg"
    return {
        "inspection_image_path": str(path),
        "inspection_image_url": f"{config.LOCAL_BASE_URL}/inspections/latest.jpg",
    }


async def _browser_open_and_act(
    url: str,
    selector: Optional[str],
    text: Optional[str],
    submit: bool,
    wait_for: Optional[str],
    timeout_ms: int,
) -> Dict[str, Any]:
    browser = await _connect()
    try:
        ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = None

        # Reuse a tab already on this URL; else open a new one
        for p in ctx.pages:
            if url.lower() in (p.url or "").lower():
                page = p
                break
        if page is None:
            page = await ctx.new_page()
            try:
                await page.goto(url, timeout=max(timeout_ms, 20000), wait_until="domcontentloaded")
            except Exception as e:
                return {"status": "failed", "method_used": None, "error": f"Navigation to {url} failed: {e}",
                        "current_url": page.url}
        else:
            await page.bring_to_front()

        if wait_for:
            try:
                await page.wait_for_selector(wait_for, timeout=timeout_ms)
            except Exception:
                pass  # non-fatal; tiered logic below handles absence

        result: Dict[str, Any] = {
            "status": "failed",
            "method_used": None,
            "current_url": None,
            "element_focused": False,
            "verified_input_value": None,
            "submitted": False,
        }

        method_used = None
        info = None
        if selector:
            info = await _tier1_cdp_selector(page, selector, text)
            method_used = "cdp_selector" if info else None
        if info is None:
            info = await _tier2_dom_heuristic(page, text)
            method_used = "dom_heuristic" if info else None
        if info is None and text is not None:
            info = await _tier3_physical_fallback(page, selector, text)
            method_used = "physical_fallback" if info else None

        if info is None:
            result["error"] = "All 3 tiers failed: no visible editable element found and no selector provided."
            result["current_url"] = page.url
            return result

        result["method_used"] = method_used
        result["status"] = "success" if method_used == "cdp_selector" else "fallback_used"

        # --- Read-back verification (never trust blind input) ---------------
        await asyncio.sleep(0.15)
        verification = await page.evaluate(_VERIFY_INPUT_JS, text or "")
        result["element_focused"] = bool(verification.get("focused"))
        result["verified_input_value"] = verification.get("value")

        if text is not None and not result["element_focused"]:
            result["status"] = "failed"
            result["error"] = "Input executed but focus verification failed (no editable element focused)."
        elif text is not None and not (verification.get("matches") if "matches" in verification else True):
            result["status"] = "failed"
            result["error"] = f"Input value mismatch after fill. Expected: {text[:80]!r}"

        # --- Optional submit + post-submit check -----------------------------
        if submit and result["status"] != "failed":
            await page.keyboard.press("Enter")
            await asyncio.sleep(0.6)
            post = await page.evaluate(_POST_SUBMIT_JS)
            result["submitted"] = True
            result["post_submit"] = post
            # Navigations change focus; report current URL after submit
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=3000)
            except Exception:
                pass

        result["current_url"] = page.url

        # --- Inspection artifact (path + URL, not base64 spam) ---------------
        try:
            from modules import desktop_native
            await page.screenshot(path=str(desktop_native.get_inspections_dir() / "latest.jpg"), type="jpeg", quality=70)
            result.update(_inspection_artifacts())
        except Exception:
            artifacts = _inspection_artifacts()
            result["inspection_image_path"] = artifacts["inspection_image_path"]
            result["inspection_image_url"] = artifacts["inspection_image_url"]

        return result
    finally:
        await browser.close()


async def browser_open_and_act(
    url: str,
    selector: Optional[str] = None,
    text: Optional[str] = None,
    submit: bool = False,
    wait_for: Optional[str] = None,
    timeout_ms: int = 15000,
) -> Dict[str, Any]:
    """
    Atomic, deterministic browser action with 3-tier fallback:
      Tier 1: CDP exact selector (or standard editable roles)
      Tier 2: heuristic DOM search for visible editable elements
      Tier 3: physical mouse click at bounding-box center + clipboard paste
    Returns verified state: focused element, read-back input value, submit
    result, current URL and an inspection image path/URL. Single round-trip.
    """
    try:
        return await _browser_open_and_act(url, selector, text, submit, wait_for, timeout_ms)
    except Exception as e:
        return _cdp_unavailable(e)


# ==============================================================================
# Tool: read_focused_element_value - proof-of-input helper for web contexts
# ==============================================================================

async def _read_focused_element_value() -> Dict[str, Any]:
    browser = await _connect()
    try:
        # Most recently active page (last in context.pages order)
        ctx = browser.contexts[0] if browser.contexts else None
        if not ctx or not ctx.pages:
            return {"focused": False, "error": "No open pages."}
        page = ctx.pages[-1]
        return await page.evaluate(_READ_FOCUSED_JS)
    finally:
        await browser.close()


async def read_focused_element_value() -> Dict[str, Any]:
    """
    Read back the value of the currently focused editable element in the active
    Chrome tab. Proof-of-action for keyboard_type / set_clipboard_and_paste on
    web contexts.
    """
    try:
        return await _read_focused_element_value()
    except Exception as e:
        return _cdp_unavailable(e)


# ==============================================================================
# Legacy polling tools (kept for the ChatGPT/Notion loop) - async native
# ==============================================================================

_CHATGPT_INJECT_JS = """
(prompt) => {
    const ta = document.querySelector('#prompt-textarea');
    if (!ta) return 'ERR: #prompt-textarea not found';
    ta.focus();
    ta.textContent = prompt;
    ta.dispatchEvent(new Event('input', { bubbles: true }));
    return 'ok';
}
"""

_CHATGPT_STATUS_JS = """
() => {
    const stopBtn = document.querySelector('button[data-testid="stop-button"]');
    if (stopBtn) return { done: false };
    const messages = document.querySelectorAll('[data-message-author-role="assistant"]');
    if (!messages.length) return { done: false, note: 'no assistant messages yet' };
    const last = messages[messages.length - 1];
    return { done: true, markdown: last.innerText || '' };
}
"""


async def chatgpt_ask_and_read_status(prompt: str, timeout: float = 180.0) -> Dict[str, Any]:
    """Focus the chatgpt.com tab, inject a prompt, submit, poll until the
    stop-streaming indicator disappears, return the latest assistant Markdown."""
    try:
        browser = await _connect()
    except Exception as e:
        return _cdp_unavailable(e)
    try:
        page = await _find_page(browser, config.CHATGPT_URL_MATCH)
        if page is None:
            return {"error": f"No open tab matching '{config.CHATGPT_URL_MATCH}'.",
                    "hint": "Use switch_or_open_tab(url_pattern='chatgpt.com', new_tab_if_missing=True)."}
        await page.bring_to_front()

        result = await page.evaluate(_CHATGPT_INJECT_JS, prompt)
        if isinstance(result, str) and result.startswith("ERR"):
            return {"error": result}
        await page.keyboard.press("Enter")
        await asyncio.sleep(0.4)
        stop_visible = await page.evaluate("() => !!document.querySelector('button[data-testid=\"stop-button\"]')")
        if not stop_visible:
            send_btn = await page.query_selector('button[data-testid="send-button"]')
            if send_btn:
                try:
                    await send_btn.click(timeout=2000)
                except Exception:
                    pass

        deadline = asyncio.get_event_loop().time() + timeout
        last: Dict[str, Any] = {}
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.5)
            last = await page.evaluate(_CHATGPT_STATUS_JS)
            if last.get("done"):
                return {"status": "ok", "response_markdown": last.get("markdown", "")}
        return {"status": "timeout", "note": f"Generation did not finish within {timeout}s.",
                "partial_markdown": last.get("markdown", "")}
    finally:
        await browser.close()


_NOTION_OPEN_AI_JS = """
() => {
    const btn = document.querySelector('.notion-ai-button')
        || document.querySelector('[aria-label*="Notion AI"]')
        || document.querySelector('[data-cy="notion-ai-button"]');
    if (btn) { btn.click(); return 'ok:clicked'; }
    return 'no-button';
}
"""

_NOTION_INJECT_JS = """
(prompt) => {
    const box = document.querySelector('.notion-ai-editor div[contenteditable="true"]')
        || document.querySelector('div[contenteditable="true"][data-content-editable-root="true"]')
        || document.querySelector('div[contenteditable="true"]');
    if (!box) return 'ERR: no editable AI input found';
    box.focus();
    document.execCommand('insertText', false, prompt);
    return 'ok';
}
"""

_NOTION_DONE_JS = """
() => {
    const generating = document.querySelector('.notion-ai-generating')
        || document.querySelector('[data-cy="notion-ai-generating"]')
        || document.querySelector('div[aria-busy="true"]');
    if (generating) return { done: false };
    const root = document.querySelector('.notion-ai-editor')
        || document.querySelector('[data-cy="notion-ai-output"]')
        || document.querySelector('.notion-page-content');
    const text = root ? (root.innerText || '') : '';
    return { done: true, markdown: text };
}
"""


async def notion_ai_submit_and_poll(prompt: str, timeout: float = 240.0) -> Dict[str, Any]:
    """Locate the notion.so tab, activate Notion AI, inject prompt, poll until
    the generating indicator detaches, extract the testing guide."""
    try:
        browser = await _connect()
    except Exception as e:
        return _cdp_unavailable(e)
    try:
        page = await _find_page(browser, config.NOTION_URL_MATCH)
        if page is None:
            return {"error": f"No open tab matching '{config.NOTION_URL_MATCH}'.",
                    "hint": "Use switch_or_open_tab(url_pattern='notion.so', new_tab_if_missing=True)."}
        await page.bring_to_front()

        opened = await page.evaluate(_NOTION_OPEN_AI_JS)
        if str(opened).startswith("no"):
            await page.keyboard.press("Control+j")
            await asyncio.sleep(0.8)
        injected = await page.evaluate(_NOTION_INJECT_JS, prompt)
        if isinstance(injected, str) and injected.startswith("ERR"):
            return {"error": injected, "open_attempt": opened}
        await page.keyboard.press("Enter")
        await asyncio.sleep(0.5)

        deadline = asyncio.get_event_loop().time() + timeout
        last: Dict[str, Any] = {}
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.5)
            last = await page.evaluate(_NOTION_DONE_JS)
            if last.get("done") and (last.get("markdown") or "").strip():
                return {"status": "ok", "testing_guide_markdown": last["markdown"]}
        return {"status": "timeout", "note": f"Notion AI did not finish within {timeout}s.",
                "partial": last.get("markdown", "")}
    finally:
        await browser.close()
