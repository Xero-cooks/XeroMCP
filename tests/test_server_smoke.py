"""In-process server smoke test: auth, discovery, tools/list, spatial_point
round-trip over real Streamable HTTP (JSON mode). Platform independent."""
from __future__ import annotations

import json

import pytest

starlette_testclient = pytest.importorskip("starlette.testclient")


@pytest.fixture(scope="module")
def client():
    import config
    import main
    with starlette_testclient.TestClient(main.asgi_app) as c:
        c.token = config.BEARER_TOKEN
        yield c


def _rpc(client, method, params=None, rid=1, session=None):
    h = {"Authorization": f"Bearer {client.token}", "Accept": "application/json, text/event-stream",
         "Content-Type": "application/json"}
    if session:
        h["mcp-session-id"] = session
    r = client.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": rid, "method": method,
                                             "params": params or {}})
    return r


def _session(client):
    r = _rpc(client, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                    "clientInfo": {"name": "pytest", "version": "0"}})
    assert r.status_code == 200, r.text
    sid = r.headers.get("mcp-session-id")
    h = {"Authorization": f"Bearer {client.token}", "Accept": "application/json, text/event-stream",
         "Content-Type": "application/json", "mcp-session-id": sid}
    client.post("/mcp", headers=h, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    return sid


def test_health_and_discovery_are_public(client):
    assert client.get("/health").json()["version"] == "2.5.0"
    d = client.get("/.well-known/mcp.json").json()
    assert "spatial_point" in d["tools"] and len(d["tools"]) == 7


def test_mcp_requires_token(client):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401
    r = client.post("/mcp", headers={"Authorization": "Bearer wrong"}, json={})
    assert r.status_code == 401


def test_websocket_scope_is_authenticated(client):
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/mcp") as ws:
            ws.receive_text()


def test_tools_list_and_spatial_call(client):
    sid = _session(client)
    r = _rpc(client, "tools/list", rid=2, session=sid)
    names = [t["name"] for t in r.json()["result"]["tools"]]
    assert names == ["web_task", "see", "point", "spatial_point", "xero", "chrome_session", "pc"]
    r = _rpc(client, "tools/call", {"name": "spatial_point", "arguments": {"action": "stats"}}, rid=3, session=sid)
    body = r.json()["result"]
    payload = json.loads(body["content"][0]["text"])
    assert payload["status"] == "ok" and "telemetry" in payload
    r = _rpc(client, "tools/call", {"name": "spatial_point", "arguments": {"cell": "Z99"}}, rid=4, session=sid)
    payload = json.loads(r.json()["result"]["content"][0]["text"])
    assert payload["status"] == "bad_request"
    r = _rpc(client, "tools/call", {"name": "spatial_point", "arguments": {"action": "cancel"}}, rid=5, session=sid)
    assert json.loads(r.json()["result"]["content"][0]["text"])["status"] == "ok"


def _call(client, sid, name, args, rid):
    r = _rpc(client, "tools/call", {"name": name, "arguments": args}, rid=rid, session=sid)
    return json.loads(r.json()["result"]["content"][0]["text"])


def test_xero_runtime_tool_contract(client):
    sid = _session(client)
    st = _call(client, sid, "xero", {"op": "status"}, 10)
    assert st["status"] == "ok" and "capabilities" in st and "runtime" in st
    ob = _call(client, sid, "xero", {"op": "observe"}, 11)
    assert {"frame_id", "ts", "age_ms", "window", "browser", "visual", "interaction", "events"} <= set(ob)
    assert "image" not in ob and len(json.dumps(ob)) < 6000                       # compact by default
    assert _call(client, sid, "xero", {"op": "stream", "steps": []}, 12)["status"] == "bad_request"
    assert _call(client, sid, "xero", {"op": "stream", "steps": [{"type": "explode"}]}, 13)["status"] == "bad_request"
    assert _call(client, sid, "xero", {"op": "nope"}, 14)["status"] == "bad_request"
    assert _call(client, sid, "xero", {"op": "caps"}, 15)["status"] == "ok"
    assert _call(client, sid, "xero", {"op": "cancel"}, 16)["cancelled"] is True


def test_finalize_shrinks_oversized_images():
    import base64, io
    import main
    from PIL import Image
    import os
    im = Image.frombytes("RGB", (1600, 1200), os.urandom(1600 * 1200 * 3))     # incompressible noise
    buf = io.BytesIO(); im.save(buf, format="JPEG", quality=95)
    assert buf.tell() > main.MAX_IMAGE_BYTES
    out = main._finalize({"image": {"data": base64.b64encode(buf.getvalue()).decode()}, "status": "ok"})
    assert out[1]["image_bytes"] <= main.MAX_IMAGE_BYTES and "image" not in out[1]
