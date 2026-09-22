# ==============================================================================
# main.py - Autonomous Workstation MCP Hub v2.0
#
# DESIGN: 4 fat tools, not 31 primitives. Complexity lives INSIDE the tools.
#   web_task        - open page, act, verify, wait for the product (autopilot)
#   see             - vision: capture + OCR + landmarks + real MCP image content
#   chrome_session  - one Chrome session engine (status/debug/tabs/focus)
#   pc              - shell, files, search, processes, kill, notify
#
# Agent-facing rules honored here:
#   * Screenshots are returned as REAL MCP image content blocks (multimodal
#     models look at pixels) - never base64-inside-JSON, never a localhost URL.
#   * The old 31 primitives stay as internal module functions; they are NOT
#     advertised via tools/list (Notion caches the list - fat tools must show).
# ==============================================================================
from __future__ import annotations

import base64
import contextlib
import secrets
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from mcp.server.fastmcp import FastMCP

import config
from modules import chrome_control, desktop_native, mouse_runtime, pc_ops
from modules import see as see_mod
from modules import web_task as web_task_mod

# Force DPI awareness as early as possible (import side effect, idempotent)
_ = desktop_native  # noqa: F401  (import forces SetProcessDpiAwareness(2))

mcp = FastMCP(config.SERVER_NAME, host=config.HOST, port=config.PORT,
              # Plain-JSON responses (not SSE-framed) on POST /mcp: some cloud
              # clients fail to parse 'event: message\ndata:' bodies and then
              # report an empty tools list. JSON is the maximally compatible
              # mode; GET streams remain SSE per the Streamable HTTP spec.
              json_response=True)


# ------------------------------------------------------------------------------
# Helper: turn {"image": {...}} dicts into [Image content, JSON text] blocks so
# multimodal clients receive pixels as pixels.
# ------------------------------------------------------------------------------

def _finalize(result: Any):
    if isinstance(result, dict) and isinstance(result.get("image"), dict):
        img = result.pop("image")
        try:
            raw = base64.b64decode(img.get("data", ""))
            from mcp.server.fastmcp import Image
            return [Image(data=raw, format="jpeg"), result]
        except Exception:
            result["image_error"] = "image payload could not be encoded; re-run with include_image=false"
    return result


# ==============================================================================
# Tool 1: web_task - the nokeep tool
# ==============================================================================

@mcp.tool()
async def web_task(
    url: str,
    message: Optional[str] = None,
    selector: Optional[str] = None,
    submit: bool = True,
    wait_reply: bool = True,
    timeout_ms: int = 20000,
    include_image: bool = True,
) -> Any:
    """
    ONE round-trip web engine: open a page, type a message, submit it, wait for
    the assistant's reply, and return the actual product text.

    Internally (hidden from you): attaches Chrome via CDP using a dedicated
    debug profile - it NEVER kills or relaunches your Chrome. If CDP is
    unavailable it falls back to OCR + physical mouse/keyboard on the existing
    window automatically.

    Args:
        url: page to open or switch to (e.g. https://nokeep.ai/chat)
        message: text to type into the composer (omit for read-only visits)
        selector: optional exact CSS selector for the input; omit to let the
            engine find the composer heuristically
        submit: press Enter / click send after typing (default true)
        wait_reply: poll until a NEW assistant bubble appears and stabilizes
        timeout_ms: max wait for the reply (default 20000)
        include_image: attach a cropped screenshot of the conversation as a
            real image content block (default true)

    Returns JSON: status (ok|fallback_used|failed), method_used
    (cdp|vision), url, typed, typed_verified (read-back proof), submitted,
    assistant_reply (extracted text), elapsed_ms - plus an image block when
    include_image=true. No localhost URLs, no base64-in-JSON, no scale math.
    """
    result = await web_task_mod.run_web_task(
        url=url, message=message, selector=selector, submit=submit,
        wait_reply=wait_reply, timeout_ms=timeout_ms, include_image=include_image,
    )
    return _finalize(result)


# ==============================================================================
# Tool 2: see - vision, OCR and landmarks in one call
# ==============================================================================

@mcp.tool()
async def see(
    target: str = "foreground",
    want: Optional[List[str]] = None,
    click_text: Optional[str] = None,
    ocr_lang: str = "en-US",
    thumbnail_width: int = 800,
    quality: int = 60,
) -> Any:
    """
    The hub's eyes: bring the target to front, capture physical pixels, run
    OCR server-side, guess UI landmarks, and return a real image you can look
    at. Use when web_task is the wrong shape (native apps, unknown UI).

    Args:
        target: "foreground" | "window:<title substring>" | "x,y,w,h" region
            in physical pixels
        want: list from ["ocr", "landmarks", "thumbnail"] (default all)
        click_text: optional label to click internally (e.g. "Ask anything")
            - returns exactly where it clicked
        ocr_lang: OCR language tag (default en-US)
        thumbnail_width: max image width returned (default 800px)
        quality: JPEG quality of the returned image (default 60)

    Returns JSON with: ocr {full_text, lines[{text,x,y,w,h}]} where boxes are
    PHYSICAL pixels (feed straight into mouse_click - zero scale math),
    landmarks {composer, send_button, latest_text_block} so you never guess
    mid-screen coordinates again, clicked {...} when click_text was used -
    plus a real image content block of the capture. No localhost URLs.
    """
    result = await see_mod.see(
        target=target, want=want, click_text=click_text,
        ocr_lang=ocr_lang, thumbnail_width=thumbnail_width, quality=quality,
    )
    return _finalize(result)


# ==============================================================================
# Tool 3: point - the hands (resident mouse runtime: aim, fire, prove)
# ==============================================================================

@mcp.tool()
async def point(
    do: str = "click",
    target: Any = None,
    target2: Any = None,
    near: str = "",
    window: str = "",
    until: str = "",
    role: str = "",
    motion: str = "sniper",
    timeout_ms: int = 1500,
    dx: int = 0,
    dy: int = 0,
    amount: int = 0,
) -> Any:
    """
    The hub's hands. You send WHAT to hit and WHAT MUST BE TRUE AFTER - never
    raw coordinates (those are a debug escape hatch). The resident runtime
    resolves the target (UIA control bounds -> OCR label -> color-blob for
    avatars/icons -> last-seen memory), fires via ghost/warp motion, and proves
    the result IN THE SAME CALL with jittered retries - you only hear back when
    it's done or it failed.

    Args:
        do: "click" | "double" | "right" | "move" | "drag" | "scroll" | "status"
        target: what to hit: "Kartik Raghav" (text), {"text": "..."},
            {"x":..,"y":..} (debug raw), {"x":..,"y":..,"w":..,"h":..} (debug box)
        target2: drag end target (same shapes as target)
        near: disambiguation anchor text, e.g. "Who's using Chrome?" - when two
            candidates match, the one nearest this anchor wins; without it an
            ambiguous match is REFUSED (returns candidates) rather than guessed
        window: window title substring to lock first (focus proof or abort)
        until: proof condition: "Who's using Chrome? gone" | "url contains
            notebook.google" | "some label text" (must be visible after)
        role: "avatar"|"icon" hits the colored circle ABOVE the label, never
            the caption; also steers retry offsets 14px above text
        motion: "sniper" (teleport+click, default) | "human" (flick, only when
            a site watches the path)
        timeout_ms: total budget for fire+proof+retries (default 1500)
        dx, dy: pixel offset from the resolved sweet spot
        amount: scroll amount for do="scroll" (positive = up)

    Returns: {status: hit|fired_unverified|ambiguous|..., hit:{x,y,method,box,
    resolved_name}, motion (ghost:invoke|warp|flick), tries, proof:{until_ok,
    how}, window_under_point_after, visual_delta, ms}. Status "hit" means the
    until-condition was PROVEN. "fired_unverified" means the click fired but
    the condition could not be confirmed - investigate, don't assume success.
    """
    return await mouse_runtime.point_async(
        do=do, target=target, target2=target2, near=near, window=window,
        until=until, role=role, motion=motion, timeout_ms=timeout_ms,
        dx=dx, dy=dy, amount=amount,
    )


# ==============================================================================
# Tool 4: chrome_session - one session tool, never 4 CDP toys
# ==============================================================================

@mcp.tool()
def chrome_session(op: str = "status", url: str = "") -> Dict[str, Any]:
    """
    One Chrome session engine. Ops:
      status       - is CDP available, which browser, debug profile state
      ensure_debug - attach to CDP :9222 if present; else spawn a DEDICATED
                     debug-profile Chrome (never touches or kills your normal
                     Chrome window; log into sites in that profile once and
                     sessions persist)
      tabs         - list open tabs (url, title)
      focus        - bring the tab/window matching `url` to front (CDP activate
                     with Alt-pulse window fallback); returns focus proof
      open         - open `url` as a new tab
    CDP is an accelerator, not a dependency: if this is unhealthy, web_task
    still works through its vision fallback.
    """
    return chrome_control.dispatch(op, url=url)


# ==============================================================================
# Tool 5: pc - one computer tool (shell, files, processes, notify)
# ==============================================================================

@mcp.tool()
def pc(
    op: str,
    command: Optional[str] = None,
    cwd: Optional[str] = None,
    timeout: int = 120,
    allow_port_8000: bool = False,
    path: Optional[str] = None,
    content: Optional[str] = None,
    append: bool = False,
    target_block: Optional[str] = None,
    replacement_block: Optional[str] = None,
    replace_all: bool = False,
    start_line: int = 1,
    line_count: int = 300,
    depth: int = 4,
    query: Optional[str] = None,
    extension: Optional[str] = None,
    detail: bool = True,
    pid: Optional[int] = None,
    port: Optional[int] = None,
    title: str = "MCP Hub",
    message: str = "",
) -> Dict[str, Any]:
    """
    The whole computer in one tool. Ops:
      run    {command, cwd?, timeout?, allow_port_8000?} -> ALWAYS
             {exit_code, stdout, stderr, timed_out} - never "[no output]".
             PowerShell, explicitly; commands touching port 8000 are rejected
             (they killed the Tailscale funnel before) unless allow_port_8000.
      read   {path, start_line?, line_count?}    -> numbered lines
      write  {path, content, append?}
      patch  {path, target_block, replacement_block, replace_all?}
      tree   {path?, depth?}                     -> ignores node_modules etc.
      search {query, path?, extension?}          -> "file:line -> text"
      ps     {detail?}                           -> top processes + listening ports
      kill   {pid?} or {port?}                   -> kills child tree too
      notify {title, message}                    -> Windows toast

    Required params per op are enforced; unknown ops return the valid list.
    """
    kw: Dict[str, Any] = {k: v for k, v in dict(
        command=command, cwd=cwd, timeout=timeout, allow_port_8000=allow_port_8000,
        path=path, content=content, append=append, target_block=target_block,
        replacement_block=replacement_block, replace_all=replace_all,
        start_line=start_line, line_count=line_count, depth=depth, query=query,
        extension=extension, detail=detail, pid=pid, port=port,
        title=title, message=message,
    ).items() if v is not None or k in ("content", "message", "title")}
    return pc_ops.dispatch(op, **kw)


# ==============================================================================
# FastAPI app: health, discovery, lifespan, ASGI auth, /mcp mount
# ==============================================================================

mcp_streamable_app = mcp.streamable_http_app()
# Legacy HTTP+SSE transport for older clients (the streamable HTTP mount
# only speaks /mcp; legacy clients GET /sse and POST /messages/?session_id=).
mcp_sse_app = mcp.sse_app()


class _TransportRouter:
    """Route legacy-SSE paths to the SSE app, everything else to streamable."""

    def __init__(self, streamable, legacy):
        self.streamable = streamable
        self.legacy = legacy

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope.get("type") == "http" and (path == "/sse" or path.startswith("/messages")):
            await self.legacy(scope, receive, send)
            return
        await self.streamable(scope, receive, send)


def _transport_router():
    return _TransportRouter(mcp_streamable_app, mcp_sse_app)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    async with contextlib.AsyncExitStack() as stack:
        # Required: run the MCP session manager alongside the FastAPI lifespan
        await stack.enter_async_context(mcp.session_manager.run())

        # Warm the resident mouse runtime (UIA bridge) - first click pays no
        # PowerShell cold-start tax.
        try:
            warm = mouse_runtime.warmup()
            print(f" [x] Mouse runtime   : UIA bridge {warm['bridge']}")
        except Exception as e:
            print(f" [!] Mouse runtime   : warmup failed ({e})")

        cal = desktop_native.verify_calibration()
        config.save_status_snapshot({"display": cal})
        print("\n" + "=" * 72)
        print(" [x] AUTONOMOUS WORKSTATION MCP HUB v2.1 (5 fat tools)")
        print(f" [x] Display         : {cal['physical_resolution']} "
              f"(DPI aware: {cal['dpi_aware']}, scaling trap: {cal['scaling_trap_active']})")
        print(f" [x] Bearer Token    : {config.BEARER_TOKEN}")
        print(f" [x] MCP Endpoint    : http://127.0.0.1:{config.PORT}/mcp")
        print(f" [x] Health          : http://127.0.0.1:{config.PORT}/health")
        try:
            tools = await mcp.list_tools()
            print(f" [x] Tools registered: {len(tools)} -> {[t.name for t in tools]}")
        except Exception:
            pass
        print("=" * 72)
        print(f"[*] SERVER READY & LISTENING: Streamable HTTP running at http://{config.HOST}:{config.PORT}/mcp")
        print()
        yield


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "running", "server": config.SERVER_NAME, "version": "2.0.0"}


@app.get("/ping")
async def ping():
    return {"pong": True}


@app.get("/.well-known/mcp.json")
async def mcp_discovery():
    return {
        "version": "2.0",
        "serverInfo": {"name": config.SERVER_NAME, "version": "2.0.0"},
        "protocol": "modelcontextprotocol",
        "transport": "streamable-http",
        "endpoints": [
            {"type": "streamable-http", "url": "/mcp", "methods": ["GET", "POST"]},
            {"type": "health", "url": "/health"},
        ],
        "auth": "bearer",
        "tools": ["web_task", "see", "point", "chrome_session", "pc"],
        "capabilities": {"tools": {"listChanged": False}},
    }


app.mount("/", _transport_router())


class BearerAuthMiddleware:
    """
    ASGI-level middleware validating 'Authorization: Bearer <TOKEN>'.
    /health, /ping and /.well-known/mcp.json are exempt so external proxies and
    tunnel agents can perform discovery handshakes. (No /inspections mount in
    v2 - vision artifacts travel as MCP image content, not URLs.)
    """

    EXEMPT_PATHS = {"/health", "/ping", "/.well-known/mcp.json"}

    def __init__(self, asgi_app, token: str):
        self.app = asgi_app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = scope.get("path", "")
            if path in self.EXEMPT_PATHS:
                await self.app(scope, receive, send)
                return

            headers = {k.lower(): v for k, v in scope.get("headers", [])}
            auth = headers.get(b"authorization", b"").decode("utf-8", "replace").strip()

            # Accept the token in ANY position/style clients send:
            #   'Bearer <tok>', 'Token <tok>', '<tok>', 'Bearer Bearer <tok>',
            #   extra whitespace, 'authorization: Bearer <tok>' pasted whole.
            supplied = None
            for word in auth.replace(",", " ").split():
                if secrets.compare_digest(word, self.token):
                    supplied = word
                    break

            if not supplied:
                # Diagnostic only - never log the secret itself.
                scheme = auth.split(" ")[0][:16] if auth else "(empty)"
                print(f"[auth] 401: scheme={scheme!r} header_len={len(auth)} "
                      f"expected_token_len={len(self.token)}", flush=True)
                resp = JSONResponse(
                    {"error": "Missing or malformed Authorization header. Expected 'Bearer <TOKEN>'."},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
                await resp(scope, receive, send)
                return

        await self.app(scope, receive, send)


asgi_app = BearerAuthMiddleware(app, config.BEARER_TOKEN)

if __name__ == "__main__":
    uvicorn.run(asgi_app, host=config.HOST, port=config.PORT, log_level="info")
