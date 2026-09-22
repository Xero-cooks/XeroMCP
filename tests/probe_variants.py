# ==============================================================================
# probe_variants.py - Try every plausible MCP client request shape to find one
# that returns an empty tools list (agent reported {"tools": []}).
# ==============================================================================
import json
import sys

import requests

sys.path.insert(0, ".")
import config  # noqa: E402

BASE = "http://127.0.0.1:8000"
MCP = f"{BASE}/mcp"
AUTH = {
    "Authorization": f"Bearer {config.BEARER_TOKEN}",
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


def parse(text):
    text = text.strip()
    if text.startswith("{"):
        return json.loads(text)
    for line in text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    return {"_raw": text[:200]}


def fresh_session(extra_init_params=None, init_method="initialize"):
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": init_method,
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "variant-probe", "version": "1.0"}},
    }
    if extra_init_params:
        payload["params"].update(extra_init_params)
    r = requests.post(MCP, json=payload, headers=AUTH, timeout=15)
    sid = r.headers.get("mcp-session-id")
    if sid:
        requests.post(MCP, json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                      headers={**AUTH, "Mcp-Session-Id": sid}, timeout=10)
    return sid


def tools_list(sid, accept="application/json, text/event-stream"):
    h = {**AUTH, "Accept": accept}
    if sid:
        h["Mcp-Session-Id"] = sid
    r = requests.post(MCP, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                      headers=h, timeout=15)
    body = parse(r.text)
    tools = body.get("result", {}).get("tools")
    return r.status_code, tools


def report(name, sid=None, **kw):
    code, tools = tools_list(sid, **kw)
    n = None if tools is None else len(tools)
    print(f"{name:<55} HTTP {code}  tools={n}")
    return n


print("== Variant probe against live hub ==\n")

sid = fresh_session()
report("A. baseline (2025-03-26, SSE-accept)", sid)

sid = fresh_session()
report("B. JSON-only Accept header", sid, accept="application/json")

sid = fresh_session({"protocolVersion": "2024-11-05"})
report("C. old protocolVersion 2024-11-05")

sid = fresh_session()
report("D. no Accept header manipulation (requests default)")
# NOTE: D uses tools_list with default accept

# tools/list WITHOUT prior notifications/initialized
payload = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-03-26", "capabilities": {},
               "clientInfo": {"name": "variant-probe", "version": "1.0"}},
}
r = requests.post(MCP, json=payload, headers=AUTH, timeout=15)
sid2 = r.headers.get("mcp-session-id")
report("E. tools/list WITHOUT initialized notification", sid2)

# tools/list with NO session id at all
report("F. tools/list with NO session id", None)

# initialize with NO capabilities field at all
r = requests.post(MCP, json={
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-03-26",
               "clientInfo": {"name": "variant-probe", "version": "1.0"}},
}, headers=AUTH, timeout=15)
sid3 = r.headers.get("mcp-session-id")
if sid3:
    requests.post(MCP, json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                  headers={**AUTH, "Mcp-Session-Id": sid3}, timeout=10)
report("G. initialize without capabilities", sid3)

print("\nIf ALL show tools=4, the empty list came from a different server/port.")
