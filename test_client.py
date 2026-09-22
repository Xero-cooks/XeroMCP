# ==============================================================================
# test_client.py - Self-verification suite against http://127.0.0.1:8000
# ==============================================================================
"""
Verifies, in order:
  1. GET  /health                  -> 200 OK
  2. GET  /.well-known/mcp.json    -> valid JSON discovery metadata
  3. POST /mcp (no auth)           -> 401 Unauthorized
  4. POST /mcp (bad token)         -> 401 Unauthorized
  5. POST /mcp (bearer token)      -> 200 OK (MCP initialize handshake)
  6. POST /mcp initialize+tools/list -> tool inventory returned
"""

import json
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402

BASE = f"http://127.0.0.1:{config.PORT}"
MCP_URL = f"{BASE}/mcp"

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  [PASS] {name}")
    else:
        FAILED.append(name)
        print(f"  [FAIL] {name} {('- ' + detail) if detail else ''}")


def main() -> int:
    print(f"Testing MCP hub at {BASE}\n")

    # --- 1. Health ------------------------------------------------------------
    print("[1] GET /health")
    try:
        r = requests.get(f"{BASE}/health", timeout=10)
        check("health returns 200", r.status_code == 200, f"got {r.status_code}")
        check("health JSON valid", r.headers.get("content-type", "").startswith("application/json"))
    except Exception as e:
        check("health reachable", False, str(e))

    # --- 2. Discovery ---------------------------------------------------------
    print("[2] GET /.well-known/mcp.json")
    try:
        r = requests.get(f"{BASE}/.well-known/mcp.json", timeout=10)
        check("discovery returns 200", r.status_code == 200, f"got {r.status_code}")
        try:
            meta = r.json()
            check("discovery JSON has serverInfo", "serverInfo" in meta)
            check("discovery advertises /mcp endpoint",
                  any("/mcp" in ep.get("url", "") for ep in meta.get("endpoints", [])))
        except Exception as e:
            check("discovery JSON parse", False, str(e))
    except Exception as e:
        check("discovery reachable", False, str(e))

    # --- 3. Unauthenticated /mcp is rejected -----------------------------------
    print("[3] POST /mcp without auth")
    try:
        r = requests.post(
            MCP_URL,
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json"},
            timeout=10,
        )
        check("unauthenticated /mcp -> 401", r.status_code == 401, f"got {r.status_code}")
    except Exception as e:
        check("unauthenticated /mcp -> 401", False, str(e))

    # --- 4. Bad token rejected --------------------------------------------------
    print("[4] POST /mcp with wrong token")
    try:
        r = requests.post(
            MCP_URL,
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={
                "Authorization": "Bearer " + "0" * 64,
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            timeout=10,
        )
        check("bad token -> 401", r.status_code == 401, f"got {r.status_code}")
    except Exception as e:
        check("bad token -> 401", False, str(e))

    # --- 5. Valid bearer token -> MCP initialize handshake ----------------------
    print("[5] POST /mcp with valid Bearer token")
    session_id = None
    try:
        headers = {
            "Authorization": f"Bearer {config.BEARER_TOKEN}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        init_payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "1.0"},
            },
        }
        r = requests.post(MCP_URL, json=init_payload, headers=headers, timeout=15)
        check("authenticated /mcp -> 200", r.status_code == 200, f"got {r.status_code}: {r.text[:200]}")
        session_id = r.headers.get("mcp-session-id")

        # --- 6. Full handshake: initialized notification + tools/list -----------
        print("[6] initialize notification + tools/list")
        if session_id:
            headers["Mcp-Session-Id"] = session_id
            requests.post(
                MCP_URL,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                headers=headers,
                timeout=10,
            )
            r2 = requests.post(
                MCP_URL,
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                headers=headers,
                timeout=15,
            )
            check("tools/list -> 200", r2.status_code == 200, f"got {r2.status_code}")

            def parse_jsonrpc_or_sse(text: str) -> dict:
                """Streamable HTTP may answer as raw JSON or SSE-encoded JSON."""
                text = text.strip()
                if text.startswith("{"):
                    return json.loads(text)
                for line in text.splitlines():
                    if line.startswith("data:"):
                        return json.loads(line[5:].strip())
                raise ValueError("No JSON payload found in response")

            try:
                body = parse_jsonrpc_or_sse(r2.text)
                result = body.get("result", {})
                tools = result.get("tools", [])
                names = sorted(t["name"] for t in tools)
                print(f"\n  Tools registered ({len(tools)}):")
                for n in names:
                    print(f"    - {n}")
                check("tools/list returns tools", len(tools) > 0)
                check("hub advertises exactly the 5 fat tools",
                      names == ["chrome_session", "pc", "point", "see", "web_task"],
                      f"got {names}")
            except Exception as e:
                check("tools/list response parse", False, f"{e}; body head: {r2.text[:120]}")
        else:
            check("server issued mcp-session-id", False, "no session header on initialize")
    except Exception as e:
        check("authenticated /mcp -> 200", False, str(e))

    # --- Summary -----------------------------------------------------------------
    print("\n" + "=" * 60)
    print(f"RESULTS: {len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        print("Failed checks:")
        for f in FAILED:
            print(f"  - {f}")
        return 1
    print("ALL CHECKS PASSED - MCP hub is live and verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
