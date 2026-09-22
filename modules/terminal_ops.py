# ==============================================================================
# terminal_ops.py - Synchronous shell runner, Chrome profile discovery,
#                   CDP-enabled Chrome launcher
# ==============================================================================
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from .. import config
except ImportError:  # direct script execution
    import config  # type: ignore


def execute_shell(command: str, cwd: Optional[str] = None, timeout: int = 120) -> str:
    """
    Synchronously run a shell command with a configurable timeout.
    Returns combined stdout/stderr plus the exit code.
    """
    try:
        res = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        parts = [f"[exit code: {res.returncode}]"]
        if res.stdout and res.stdout.strip():
            parts.append("STDOUT:\n" + res.stdout.strip())
        if res.stderr and res.stderr.strip():
            parts.append("STDERR:\n" + res.stderr.strip())
        return "\n".join(parts) if len(parts) > 1 else "[no output]"
    except subprocess.TimeoutExpired:
        return f"[timeout] Command exceeded {timeout}s and was terminated."
    except Exception as e:
        return f"Shell error: {e}"


def list_chrome_profiles() -> List[Dict[str, str]]:
    """
    Inspect Chrome's 'Local State' JSON from %LOCALAPPDATA% to identify profile
    folder names, display labels and associated emails.
    """
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    state_file = Path(local_app_data) / "Google" / "Chrome" / "User Data" / "Local State"
    if not state_file.exists():
        return [{"error": f"Chrome Local State not found at {state_file}"}]
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        info_cache = data.get("profile", {}).get("info_cache", {})
        profiles: List[Dict[str, str]] = []
        for dir_name, details in info_cache.items():
            profiles.append(
                {
                    "directory_name": dir_name,
                    "display_name": details.get("name", "Unnamed"),
                    "email": details.get("user_name", "No email attached"),
                }
            )
        return profiles if profiles else [{"error": "No Chrome profiles found in Local State."}]
    except Exception as e:
        return [{"error": f"Failed reading Chrome profiles: {e}"}]


def launch_chrome_with_cdp(url: Optional[str] = None, profile_dir: Optional[str] = None) -> str:
    """
    Launch Chrome with --remote-debugging-port=9222 against the real User Data
    directory (preserves cookies/sessions). If Chrome is already running without
    CDP, Windows reuses the existing process - in that case fully exit Chrome
    first (or use a dedicated CDP profile) and relaunch.
    """
    user_data = Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"
    chrome_exe = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google" / "Chrome" / "Application" / "chrome.exe"
    if not chrome_exe.exists():
        alt = Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Google" / "Chrome" / "Application" / "chrome.exe"
        if alt.exists():
            chrome_exe = alt
        else:
            return "chrome.exe not found in Program Files."

    cmd = [
        str(chrome_exe),
        f"--remote-debugging-port={config.CDP_PORT}",
        f'--user-data-dir="{user_data}"',
        "--restore-last-session",
    ]
    if profile_dir:
        cmd.append(f'--profile-directory="{profile_dir}"')
    if url:
        cmd.append(f'"{url}"')

    try:
        subprocess.Popen(" ".join(cmd), shell=True)
        return (
            f"Chrome launched with CDP on port {config.CDP_PORT} "
            f"(profile: {profile_dir or 'default'}). Note: if Chrome was already "
            "running, it must be fully exited first for CDP to activate."
        )
    except Exception as e:
        return f"Failed to launch Chrome: {e}"
