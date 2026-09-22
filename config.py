# ==============================================================================
# config.py - Environment, ports, auth tokens, display metrics
# ==============================================================================
import os
import sys
import json
import secrets
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"

# --- Server settings ---------------------------------------------------------
HOST = "0.0.0.0"
PORT = int(os.environ.get("MCP_PORT", "8000"))
SERVER_NAME = "AutonomousWorkstationHub"
SERVER_VERSION = "1.0.0"

# --- Auth --------------------------------------------------------------------
# Token source priority: env var > persisted token file > fresh generation.
# The persisted file keeps the token stable across restarts so cloud
# orchestrators keep working without re-reading a new token each run.
_TOKEN_FILE = Path(__file__).resolve().parent / ".mcp_token"


def _resolve_token() -> str:
    env = os.environ.get("MCP_BEARER_TOKEN", "").strip()
    if env:
        return env
    try:
        if _TOKEN_FILE.exists():
            saved = _TOKEN_FILE.read_text(encoding="utf-8").strip()
            if len(saved) >= 32:
                return saved
    except Exception:
        pass
    token = secrets.token_hex(32)  # 32 bytes -> 64 hex chars
    try:
        _TOKEN_FILE.write_text(token, encoding="utf-8")
    except Exception:
        pass
    return token


BEARER_TOKEN = _resolve_token()

# --- Tunnel compatibility -----------------------------------------------------
# Cloudflare Tunnels / Tailscale rewrite Host headers; disable FastMCP's DNS
# rebinding protection so external tunnel requests are not rejected (HTTP 421).
os.environ["MCP_DNS_REBINDING_PROTECTION"] = os.environ.get(
    "MCP_DNS_REBINDING_PROTECTION", "false"
)

# --- Paths -------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent
WORKSPACE_ROOT = Path(os.environ.get("MCP_WORKSPACE", str(Path.home())))

# --- Browser / CDP -------------------------------------------------------------
CDP_ENDPOINT = os.environ.get("MCP_CDP_ENDPOINT", "http://127.0.0.1:9222")
CDP_PORT = int(os.environ.get("MCP_CDP_PORT", "9222"))
CHATGPT_URL_MATCH = "chatgpt.com"
NOTION_URL_MATCH = "notion.so"

# --- Display profile (15.6" 1080p laptop) -------------------------------------
DISPLAY_WIDTH = 1920
DISPLAY_HEIGHT = 1080
# Windows laptop scaling trap: physical 1920x1080 can report as 1536x864 when
# the process is not per-monitor DPI aware (125% scaling). We force awareness
# in desktop_native.py at import time and verify metrics at startup.
EXPECTED_SCALED_WIDTH = 1536  # 1920 / 1.25 -- symptom of missing DPI awareness

# --- Screenshot defaults --------------------------------------------------------
SCREENSHOT_TARGET_WIDTH = 1280  # token-efficient resize width
SCREENSHOT_JPEG_QUALITY = 70

# --- Inspection artifacts -------------------------------------------------
# Screenshots are written to disk and served statically so agents fetch them
# over HTTP instead of receiving 100k+ chars of base64 in the prompt.
INSPECTIONS_DIR = PROJECT_ROOT / ".mcp_inspections"
LOCAL_BASE_URL = os.environ.get("MCP_LOCAL_BASE_URL", f"http://127.0.0.1:{PORT}")
INSPECTIONS_URL_PATH = "/inspections"

# --- Process runner --------------------------------------------------------------
DEFAULT_LOG_TAIL_LINES = 50
MAX_TRACKED_PROCESSES = 64

# --- Shell runner ------------------------------------------------------------------
SHELL_DEFAULT_TIMEOUT = 120


def display_report() -> dict:
    """Snapshot of display metrics + calibration status for startup banner."""
    return {
        "expected_physical": f"{DISPLAY_WIDTH}x{DISPLAY_HEIGHT}",
        "per_monitor_dpi_aware": IS_WINDOWS,
        "scaling_trap_check": (
            f"scaled width {EXPECTED_SCALED_WIDTH}px indicates DPI awareness failure"
        ),
    }


def save_status_snapshot(extra: dict | None = None) -> None:
    """Persist connection info for external tools (token, URL, metrics)."""
    payload = {
        "bearer_token": BEARER_TOKEN,
        "mcp_url": f"http://127.0.0.1:{PORT}/mcp",
        "health_url": f"http://127.0.0.1:{PORT}/health",
        "server": SERVER_NAME,
        "version": SERVER_VERSION,
        "display": display_report(),
    }
    if extra:
        payload.update(extra)
    try:
        (PROJECT_ROOT / ".mcp_status.json").write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )
    except Exception:
        pass
