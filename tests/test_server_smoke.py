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
    assert client.get("/health").json()["version"] == "2.4.0"
    d = client.get("/.well-known/mcp.json").json()
    assert "spatial_point" in d["tools"] and len(d["tools"]) == 6


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
    assert names == ["web_task", "see", "point", "spatial_point", "chrome_session", "pc"]
    r = _rpc(client, "tools/call", {"name": "spatial_point", "arguments": {"action": "stats"}}, rid=3, session=sid)
    body = r.json()["result"]
    payload = json.loads(body["content"][0]["text"])
    assert payload["status"] == "ok" and "telemetry" in payload
    r = _rpc(client, "tools/call", {"name": "spatial_point", "arguments": {"cell": "Z99"}}, rid=4, session=sid)
    payload = json.loads(r.json()["result"]["content"][0]["text"])
    assert payload["status"] == "bad_request"
    r = _rpc(client, "tools/call", {"name": "spatial_point", "arguments": {"action": "cancel"}}, rid=5, session=sid)
    assert json.loads(r.json()["result"]["content"][0]["text"])["status"] == "ok"
