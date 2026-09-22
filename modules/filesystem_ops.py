# ==============================================================================
# filesystem_ops.py - Line-range file reader, surgical search-and-replace,
#                     directory tree, ripgrep-style search
# ==============================================================================
from __future__ import annotations

from pathlib import Path
from typing import List, Optional

IGNORED_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".next", ".cache"}
MAX_SEARCH_RESULTS = 50
MAX_TREE_ENTRIES = 2000


def get_directory_tree(root_path: str, depth: int = 4) -> str:
    """Formatted directory tree, skipping noisy folders (.git, node_modules, ...)."""
    root = Path(root_path).resolve()
    if not root.exists():
        return f"Path not found: {root}"
    if root.is_file():
        return str(root)

    lines: List[str] = [f"{root.name}/"]
    count = 0

    def walk(dir_path: Path, prefix: str, level: int) -> None:
        nonlocal count
        if level > depth or count >= MAX_TREE_ENTRIES:
            return
        try:
            entries = sorted(
                dir_path.iterdir(),
                key=lambda p: (p.is_file(), p.name.lower()),
            )
        except PermissionError:
            lines.append(prefix + "[permission denied]")
            return
        for entry in entries:
            if count >= MAX_TREE_ENTRIES:
                lines.append(prefix + "... [truncated]")
                return
            if entry.is_dir() and entry.name in IGNORED_DIRS:
                continue
            connector = "└── " if entry == entries[-1] or count + 1 == len(entries) else "├── "
            lines.append(f"{prefix}{connector}{entry.name}{'/' if entry.is_dir() else ''}")
            count += 1
            if entry.is_dir():
                walk(entry, prefix + "    ", level + 1)

    walk(root, "", 1)
    return "\n".join(lines)


def find_in_codebase(root_path: str, search_query: str, extension: Optional[str] = None) -> List[str]:
    """
    Recursively search source files for a string/symbol/function name.
    Returns up to 50 'path:line -> text' matches.
    """
    results: List[str] = []
    root = Path(root_path).resolve()
    if not root.exists():
        return [f"Path not found: {root}"]
    pattern = f"*{extension}" if extension and not extension.startswith("*") else (extension or "*")
    for p in root.rglob(pattern if pattern != "*" else "*"):
        if not p.is_file():
            continue
        if any(part in IGNORED_DIRS for part in p.parts):
            continue
        try:
            if p.stat().st_size > 5_000_000:
                continue
            with open(p, "r", encoding="utf-8", errors="ignore") as f:
                for num, line in enumerate(f, 1):
                    if search_query in line:
                        results.append(f"{p}:{num} -> {line.strip()[:140]}")
                        if len(results) >= MAX_SEARCH_RESULTS:
                            return results + ["... [capped at 50 results]"]
        except Exception:
            continue
    return results if results else ["No matches found."]


def read_file(filepath: str, start_line: int = 1, line_count: int = 300) -> str:
    """Read a slice of lines (with line numbers) to conserve context window."""
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        total = len(lines)
        start_idx = max(0, start_line - 1)
        end_idx = min(total, start_idx + max(1, line_count))
        selected = lines[start_idx:end_idx]
        numbered = [f"{i + start_idx + 1:4d} | {line}" for i, line in enumerate(selected)]
        header = f"=== File: {filepath} (lines {start_idx + 1}-{end_idx} of {total}) ==="
        return header + "\n" + "".join(numbered)
    except Exception as e:
        return f"Read error: {e}"


def write_file(filepath: str, content: str) -> str:
    """Create or overwrite a file anywhere on the system (parents auto-created)."""
    try:
        p = Path(filepath).resolve()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} characters to {p}."
    except Exception as e:
        return f"Write error: {e}"


def patch_file(filepath: str, target_block: str, replacement_block: str) -> str:
    """
    Surgical search-and-replace of a single code block, without rewriting
    the whole file. The target must match exactly (whitespace included).
    """
    try:
        p = Path(filepath).resolve()
        text = p.read_text(encoding="utf-8")
        occurrences = text.count(target_block)
        if occurrences == 0:
            return "Target block not found. Ensure whitespace and line breaks match."
        p.write_text(text.replace(target_block, replacement_block, 1), encoding="utf-8")
        note = f"Successfully patched {filepath}."
        if occurrences > 1:
            note += f" (warning: {occurrences} occurrences existed; replaced first only)"
        return note
    except Exception as e:
        return f"Patch error: {e}"
