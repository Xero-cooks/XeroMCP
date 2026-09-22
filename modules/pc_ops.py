# ==============================================================================
# pc_ops.py - THE computer tool: shell, files, processes, notifications in one.
#
# Rules from the incident report, enforced here:
#   * run() ALWAYS returns {exit_code, stdout, stderr, timed_out} - never a bare
#     string, never "[no output]".
#   * One shell, explicitly: PowerShell 5+ (universal on Win11). No GNU `timeout`
#     mashups, no bash quoting death.
#   * Guard rail: commands touching MCP port 8000 from the agent side are
#     rejected (they knocked the Tailscale funnel off last session) unless the
#     caller explicitly sets allow_port_8000=True.
# ==============================================================================
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from .. import config
except ImportError:  # direct script execution
    import config  # type: ignore

IGNORED_DIRS = {".git", "node_modules", ".venv", "venv", "dist", "build",
                "__pycache__", ".mcp_inspections", ".chrome_debug_profile",
                ".next", "site-packages"}


# ------------------------------------------------------------------------------
# op: run
# ------------------------------------------------------------------------------

def _run(command: str, cwd: Optional[str], timeout: int,
         allow_port_8000: bool) -> Dict[str, Any]:
    if not allow_port_8000 and "8000" in command:
        return {
            "exit_code": -1,
            "stdout": "",
            "stderr": "REJECTED: command references port 8000 (the MCP hub's own port). "
                      "Probing/killing it from inside a tool call has killed the Tailscale "
                      "funnel before. Set allow_port_8000=true if you REALLY mean it.",
            "timed_out": False,
            "rejected_by_guard": True,
        }

    t0 = time.monotonic()
    timed_out = False
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=cwd or str(config.PROJECT_ROOT), timeout=timeout, shell=False,
        )
        exit_code = proc.returncode
        stdout = (proc.stdout or "")[-20000:]
        stderr = (proc.stderr or "")[-8000:]
    except subprocess.TimeoutExpired as e:
        timed_out = True
        exit_code = 124
        stdout = ((e.stdout or b"").decode("utf-8", "replace")
                  if isinstance(e.stdout, bytes) else (e.stdout or ""))[-20000:]
        stderr = ((e.stderr or b"").decode("utf-8", "replace")
                  if isinstance(e.stderr, bytes) else (e.stderr or ""))[-8000:]
        stderr += f"\n[hub] timed out after {timeout}s (process tree terminated)."
    except Exception as e:
        exit_code = -1
        stdout = ""
        stderr = f"[hub] shell spawn failed: {type(e).__name__}: {e}"

    return {
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "timed_out": timed_out,
        "elapsed_ms": int((time.monotonic() - t0) * 1000),
        "shell": "powershell",
        "cwd": cwd or str(config.PROJECT_ROOT),
    }


# ------------------------------------------------------------------------------
# op: read / write / patch / tree / search
# ------------------------------------------------------------------------------

def _read(path: str, start_line: int, line_count: int) -> Dict[str, Any]:
    p = Path(os.path.expandvars(path))
    if not p.is_file():
        return {"error": f"file not found: {path}"}
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return {"error": str(e)}
    lines = text.splitlines()
    total = len(lines)
    start = max(1, start_line)
    end = min(total, start + line_count - 1)
    window = [f"{i:>6}\t{lines[i - 1]}" for i in range(start, end + 1)]
    return {
        "path": str(p),
        "total_lines": total,
        "start_line": start,
        "end_line": end,
        "truncated": end < total,
        "content": "\n".join(window) if window else "(empty range)",
    }


def _write(path: str, content: str, append: bool) -> Dict[str, Any]:
    p = Path(os.path.expandvars(path))
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if append else "w"
        with open(p, mode, encoding="utf-8") as f:
            f.write(content)
        return {"ok": True, "path": str(p), "bytes": p.stat().st_size,
                "appended": append}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _patch(path: str, target_block: str, replacement_block: str,
           replace_all: bool) -> Dict[str, Any]:
    p = Path(os.path.expandvars(path))
    if not p.is_file():
        return {"ok": False, "error": f"file not found: {path}"}
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return {"ok": False, "error": str(e)}
    count = text.count(target_block)
    if count == 0:
        return {"ok": False, "error": "target_block not found (must match exactly, including whitespace)"}
    if count > 1 and not replace_all:
        return {"ok": False, "error": f"target_block matches {count} times; pass replace_all=true or include more context"}
    new_text = text.replace(target_block, replacement_block) if replace_all \
        else text.replace(target_block, replacement_block, 1)
    p.write_text(new_text, encoding="utf-8")
    return {"ok": True, "path": str(p), "replacements": count if replace_all else 1}


def _tree(root: str, depth: int, max_entries: int = 2000) -> Dict[str, Any]:
    root_p = Path(os.path.expandvars(root))
    if not root_p.is_dir():
        return {"error": f"directory not found: {root}"}
    out: List[str] = [str(root_p)]
    entries = 1

    def walk(d: Path, level: int) -> None:
        nonlocal entries
        if level > depth or entries >= max_entries:
            return
        try:
            children = sorted(d.iterdir(),
                              key=lambda c: (c.is_file(), c.name.lower()))
        except Exception:
            return
        for c in children:
            if entries >= max_entries:
                out.append("... (truncated)")
                return
            if c.is_dir():
                if c.name.lower() in IGNORED_DIRS:
                    continue
                out.append("  " * level + c.name + "/")
                entries += 1
                walk(c, level + 1)
            else:
                out.append("  " * level + c.name)
                entries += 1

    walk(root_p, 1)
    return {"root": str(root_p), "tree": "\n".join(out), "entries": entries}


def _search(root: str, query: str, extension: Optional[str],
            max_results: int = 60) -> Dict[str, Any]:
    root_p = Path(os.path.expandvars(root))
    if not root_p.is_dir():
        return {"error": f"directory not found: {root}"}
    ql = query.lower()
    exts = {e.strip().lstrip(".").lower() for e in extension.split(",")} \
        if extension else None
    results: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root_p):
        dirnames[:] = [d for d in dirnames if d.lower() not in IGNORED_DIRS]
        for name in filenames:
            if exts and name.rsplit(".", 1)[-1].lower() not in exts:
                continue
            fp = Path(dirpath) / name
            try:
                if fp.stat().st_size > 2_000_000:
                    continue
                with open(fp, "r", encoding="utf-8", errors="replace") as f:
                    for i, line in enumerate(f, 1):
                        if ql in line.lower():
                            results.append(f"{fp}:{i} -> {line.strip()[:200]}")
                            if len(results) >= max_results:
                                return {"query": query, "matches": results,
                                        "truncated": True}
            except Exception:
                continue
    return {"query": query, "matches": results, "truncated": False}


# ------------------------------------------------------------------------------
# op: ps / kill / notify
# ------------------------------------------------------------------------------

def _ps(detail: bool) -> Dict[str, Any]:
    try:
        import psutil
    except Exception as e:
        return {"error": f"psutil unavailable: {e}"}
    procs = []
    for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info"]):
        try:
            info = p.info
            procs.append({
                "pid": info["pid"],
                "name": info["name"],
                "cpu_percent": info["cpu_percent"],
                "mem_mb": round((info["memory_info"].rss / 1_048_576), 1)
                          if info.get("memory_info") else None,
            })
        except Exception:
            continue
    procs.sort(key=lambda x: (x["cpu_percent"] or 0), reverse=True)
    ports = []
    if detail:
        seen = set()
        for conn in psutil.net_connections(kind="inet"):
            if conn.status == psutil.CONN_LISTEN and conn.lport not in seen:
                seen.add(conn.lport)
                pname = None
                try:
                    pname = psutil.Process(conn.pid).name() if conn.pid else None
                except Exception:
                    pass
                ports.append({"port": conn.lport, "pid": conn.pid, "process": pname})
        ports.sort(key=lambda x: x["port"])
    return {"processes": procs[:80], "listening_ports": ports}


def _kill(target_pid: Optional[int], port: Optional[int]) -> Dict[str, Any]:
    try:
        import psutil
    except Exception as e:
        return {"error": f"psutil unavailable: {e}"}
    if target_pid is None and port is not None:
        for conn in psutil.net_connections(kind="inet"):
            if conn.status == psutil.CONN_LISTEN and conn.lport == port:
                target_pid = conn.pid
                break
        if target_pid is None:
            return {"error": f"nothing listening on port {port}"}
    if target_pid is None:
        return {"error": "provide pid or port"}
    if target_pid == os.getpid():
        return {"error": "REFUSED: that is the MCP hub itself"}
    try:
        parent = psutil.Process(target_pid)
        children = parent.children(recursive=True)
        for c in children:
            try:
                c.kill()
            except Exception:
                pass
        parent.kill()
        psutil.wait_procs(children + [parent], timeout=5)
        return {"ok": True, "killed_pid": target_pid}
    except psutil.NoSuchProcess:
        return {"error": f"pid {target_pid} not found"}
    except Exception as e:
        return {"error": str(e)}


def _notify(title: str, message: str) -> Dict[str, Any]:
    from modules import notifier
    out = notifier.notify_user(title, message)
    return {"ok": True, "note": out}


# ------------------------------------------------------------------------------
# Dispatcher
# ------------------------------------------------------------------------------

def dispatch(op: str, **kw) -> Dict[str, Any]:
    ops = {
        "run": lambda: _run(kw["command"], kw.get("cwd"), int(kw.get("timeout", 120)),
                            bool(kw.get("allow_port_8000", False))),
        "read": lambda: _read(kw["path"], int(kw.get("start_line", 1)),
                              int(kw.get("line_count", 300))),
        "write": lambda: _write(kw["path"], kw.get("content", ""),
                                bool(kw.get("append", False))),
        "patch": lambda: _patch(kw["path"], kw["target_block"],
                                kw["replacement_block"], bool(kw.get("replace_all", False))),
        "tree": lambda: _tree(kw.get("path", str(config.PROJECT_ROOT)),
                              int(kw.get("depth", 4))),
        "search": lambda: _search(kw.get("path", str(config.PROJECT_ROOT)),
                                  kw["query"], kw.get("extension")),
        "ps": lambda: _ps(bool(kw.get("detail", True))),
        "kill": lambda: _kill(kw.get("pid"), kw.get("port")),
        "notify": lambda: _notify(kw.get("title", "MCP Hub"), kw.get("message", "")),
    }
    fn = ops.get(op)
    if fn is None:
        return {"error": f"Unknown op '{op}'. Valid: {sorted(ops)}",
                "exit_code": -1, "stdout": "", "stderr": "", "timed_out": False}
    try:
        return fn()
    except KeyError as e:
        return {"error": f"missing required parameter: {e}",
                "exit_code": -1, "stdout": "", "stderr": "", "timed_out": False}
