# ==============================================================================
# process_runner.py - Async background processes, rolling log buffers,
#                     localhost port tracking
# ==============================================================================
from __future__ import annotations

import subprocess
import threading
import time
from typing import Any, Dict, List, Optional

import psutil

try:
    from .. import config
except ImportError:  # direct script execution
    import config  # type: ignore

# PID -> {"proc": Popen, "command": str, "cwd": str, "started": float,
#         "logs": deque, "lock": Lock, "reader": Thread}
PROCESSES: Dict[int, Dict[str, Any]] = {}
_LOG_LOCK = threading.Lock()


def _read_stream(pid: int, stream) -> None:
    """Background reader thread: append lines to the rolling buffer."""
    entry = PROCESSES.get(pid)
    if entry is None:
        return
    try:
        for raw_line in iter(stream.readline, ""):
            with entry["lock"]:
                entry["logs"].append(raw_line.rstrip("\r\n"))
    except Exception:
        pass
    finally:
        try:
            stream.close()
        except Exception:
            pass


def launch_background_process(command: str, cwd: Optional[str] = None) -> dict:
    """
    Launch a persistent command (npm run dev, docker compose up, python app.py)
    without blocking. Output is captured into an in-memory rolling buffer.
    Returns {"status", "pid", "command"}.
    """
    from collections import deque

    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except Exception as e:
        return {"status": "failed", "error": str(e)}

    entry = {
        "proc": proc,
        "command": command,
        "cwd": cwd,
        "started": time.time(),
        "logs": deque(maxlen=1000),
        "lock": threading.Lock(),
    }
    PROCESSES[proc.pid] = entry
    threading.Thread(
        target=_read_stream, args=(proc.pid, proc.stdout), daemon=True
    ).start()
    return {"status": "started", "pid": proc.pid, "command": command}


def check_process_status(pid: int, max_lines: int = 30) -> dict:
    """Return running state, exit code and the last N lines of output."""
    entry = PROCESSES.get(pid)
    if entry is None:
        return {"error": f"PID {pid} is not tracked."}
    proc = entry["proc"]
    with entry["lock"]:
        lines = list(entry["logs"])[-max_lines:]
    return {
        "pid": pid,
        "command": entry["command"],
        "is_running": proc.poll() is None,
        "exit_code": proc.returncode,
        "started_at": entry["started"],
        "recent_logs": lines,
    }


def kill_process(pid: int) -> str:
    """Terminate a process and all spawned children using psutil."""
    try:
        parent = psutil.Process(pid)
        children = parent.children(recursive=True)
        for child in children:
            try:
                child.terminate()
            except psutil.NoSuchProcess:
                pass
        parent.terminate()
        _gone, alive = psutil.wait_procs(children + [parent], timeout=5)
        for p in alive:
            try:
                p.kill()
            except psutil.NoSuchProcess:
                pass
        PROCESSES.pop(pid, None)
        return f"Process {pid} and its children terminated."
    except psutil.NoSuchProcess:
        PROCESSES.pop(pid, None)
        return f"Process {pid} not found (removed from tracker if present)."
    except Exception as e:
        return f"Error killing {pid}: {e}"


def list_active_ports() -> List[Dict[str, Any]]:
    """
    Scan listening localhost ports (3000, 5173, 8000, ...), returning binding IP,
    owning PID and process name for each.
    """
    results: List[Dict[str, Any]] = []
    seen = set()
    try:
        conns = psutil.net_connections(kind="inet")
    except psutil.AccessDenied:
        conns = []
    for conn in conns:
        if conn.status != psutil.CONN_LISTEN or not conn.laddr:
            continue
        key = (conn.laddr.ip, conn.laddr.port, conn.pid)
        if key in seen:
            continue
        seen.add(key)
        name = "Unknown"
        if conn.pid:
            try:
                name = psutil.Process(conn.pid).name()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        results.append(
            {"port": conn.laddr.port, "ip": conn.laddr.ip, "pid": conn.pid, "name": name}
        )
    return sorted(results, key=lambda x: x["port"])


def kill_port_process(port: int) -> str:
    """Free a local port by terminating whichever process is listening on it."""
    try:
        conns = psutil.net_connections(kind="inet")
    except psutil.AccessDenied:
        return "Access denied while enumerating connections (try running as admin)."
    killed = []
    for conn in conns:
        if conn.status == psutil.CONN_LISTEN and conn.laddr and conn.laddr.port == port:
            if conn.pid and conn.pid not in killed:
                try:
                    p = psutil.Process(conn.pid)
                    for child in p.children(recursive=True):
                        try:
                            child.terminate()
                        except psutil.NoSuchProcess:
                            pass
                    p.terminate()
                    killed.append(conn.pid)
                except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
                    return f"Failed to kill process on port {port}: {e}"
    if killed:
        return f"Killed PID(s) {killed} holding port {port}."
    return f"No process found on port {port}."
