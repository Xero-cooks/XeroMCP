# ==============================================================================
# test_fat_tools.py - Live functional tests of the 4 fat tools over MCP.
# Requires: hub on :8000, test page server on :8123 (tests/test_page.html).
# ==============================================================================
import base64
import io
import json
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402

BASE = f"http://127.0.0.1:{config.PORT}"
MCP_URL = f"{BASE}/mcp"
HEADERS = {
    "Authorization": f"Bearer {config.BEARER_TOKEN}",
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}

PASSED, FAILED = [], []


def check(name, cond, detail=""):
    (PASSED if cond else FAILED).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name} {('- ' + str(detail)[:150]) if detail and not cond else ''}")


def parse_body(text):
    text = text.strip()
    if text.startswith("{"):
        return json.loads(text)
    for line in text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    raise ValueError("no JSON payload")


class Session:
    def __init__(self):
        r = requests.post(MCP_URL, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                       "clientInfo": {"name": "fat-test", "version": "1.0"}},
        }, headers=HEADERS, timeout=20)
        r.raise_for_status()
        self.sid = r.headers["mcp-session-id"]
        self.n = 10
        requests.post(MCP_URL, json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                      headers={**HEADERS, "Mcp-Session-Id": self.sid}, timeout=10)

    def call(self, tool, args):
        self.n += 1
        r = requests.post(MCP_URL, json={
            "jsonrpc": "2.0", "id": self.n, "method": "tools/call",
            "params": {"name": tool, "arguments": args},
        }, headers={**HEADERS, "Mcp-Session-Id": self.sid}, timeout=120)
        r.raise_for_status()
        body = parse_body(r.text)
        content = body.get("result", {}).get("content", [])
        texts = [c.get("text", "") for c in content if c.get("type") == "text"]
        images = [c for c in content if c.get("type") == "image"]
        data = json.loads(texts[-1]) if texts else {}
        return data, images


def main():
    s = Session()

    # ---- 1. pc: structured run + port guard ---------------------------------
    print("[pc] run + guard")
    res, _ = s.call("pc", {"op": "run", "command": "Write-Output hello-fat"})
    check("pc.run returns exit_code 0", res.get("exit_code") == 0, res)
    check("pc.run stdout has 'hello-fat'", "hello-fat" in res.get("stdout", ""), res)
    res, _ = s.call("pc", {"op": "run", "command": "curl http://127.0.0.1:8000/health"})
    check("pc.run port-8000 guard rejects", res.get("rejected_by_guard") is True, res)

    # ---- 2. pc: read/tree/search --------------------------------------------
    print("[pc] files")
    res, _ = s.call("pc", {"op": "read", "path": str(Path(__file__).parent.parent / "config.py"),
                           "start_line": 1, "line_count": 5})
    check("pc.read returns numbered lines", res.get("start_line") == 1 and "config" in res.get("content", ""), res)
    res, _ = s.call("pc", {"op": "search", "query": "BEARER_TOKEN", "extension": "py"})
    check("pc.search finds BEARER_TOKEN", any("config.py" in m for m in res.get("matches", [])), res)
    res, _ = s.call("pc", {"op": "tree", "path": str(Path(__file__).parent.parent), "depth": 2})
    check("pc.tree lists modules", "modules/" in res.get("tree", ""), res)

    # ---- 3. chrome_session: status (no Chrome kill involved) ------------------
    print("[chrome_session]")
    res, _ = s.call("chrome_session", {"op": "status"})
    check("chrome_session.status has cdp_available key", "cdp_available" in res, res)
    check("chrome_session policy never_kill", res.get("policy") == "never_kill", res)

    # ---- 4. see: OCR + landmarks + REAL image content -------------------------
    print("[see] vision + OCR")
    res, images = s.call("see", {"target": "foreground", "want": ["ocr", "landmarks", "thumbnail"]})
    check("see.status ok", res.get("status") == "ok", res)
    check("see returns ocr engine", res.get("ocr_engine") in ("winsdk_python", "windows_ocr_powershell", "tesseract"), res)
    check("see has landmarks.composer", "composer" in res.get("landmarks", {}), res)
    check("see returns a REAL image content block", len(images) == 1, f"{len(images)} images")
    if images:
        try:
            raw = base64.b64decode(images[0]["data"])
            from PIL import Image
            Image.open(io.BytesIO(raw)).verify()
            check("see image decodes to a valid JPEG", raw[:2] == b"\xff\xd8")
        except Exception as e:
            check("see image decodes to a valid JPEG", False, e)
    check("see output has NO localhost URL", "127.0.0.1:8000" not in json.dumps(res), "localhost URL leaked")

    # ---- 5. web_task: THE money test (open -> type -> submit -> reply) --------
    print("[web_task] full loop vs local test page")
    res, images = s.call("web_task", {
        "url": "http://127.0.0.1:8123/test_page.html",
        "message": "hi bro",
        "submit": True,
        "wait_reply": True,
        "timeout_ms": 15000,
        "include_image": True,
    })
    check("web_task.status ok", res.get("status") == "ok", res)
    check("web_task.typed_verified", res.get("typed_verified") is True, res)
    check("web_task.submitted", res.get("submitted") is True, res)
    check("web_task got the assistant reply", "hi bro" in (res.get("assistant_reply") or ""), res)
    check("web_task method is cdp", res.get("method_used") == "cdp", res)
    check("web_task returns real image block", len(images) == 1)
    check("web_task output has NO localhost URL", "127.0.0.1:8000" not in json.dumps(res), "localhost URL leaked")

    print("\n" + "=" * 60)
    print(f"RESULTS: {len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        for f in FAILED:
            print(f"  - {f}")
        return 1
    print("ALL FAT-TOOL CHECKS PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
