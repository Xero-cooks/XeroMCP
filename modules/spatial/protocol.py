"""Compact semantic action protocol.

  CLICK G7 32 48            DOUBLE_CLICK G7 32 48      RIGHT_CLICK G7
  MOVE G7 32 48             HOVER "Settings"
  DRAG G7 32 48 G9 41 21    DRAG "file.txt" "Trash"
  SCROLL G8 -3              SCROLL G8 32 32 5          (amount = wheel notches, + = up)
  TYPE "hello"              KEY ENTER                  KEY CTRL+L
  CLICK "Settings"          CLICK "Send" IN G14        CLICK "OK" NEAR "Save changes?"
  WAIT 300                  OBSERVE
  ... any action ... UNTIL "Settings window visible"

Several actions may be chained with newlines or ';'. Cells accept refinement
("G4/B2") and an optional display prefix ("1:G4").
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

VERBS = {
    "CLICK": "click", "LEFT_CLICK": "click", "TAP": "click",
    "DOUBLE_CLICK": "double_click", "DOUBLE": "double_click", "DBLCLICK": "double_click",
    "RIGHT_CLICK": "right_click", "RIGHT": "right_click", "CONTEXT": "right_click",
    "MOVE": "move", "HOVER": "hover",
    "DRAG": "drag", "SCROLL": "scroll",
    "TYPE": "type", "KEY": "key", "KEYS": "key", "PRESS": "key",
    "WAIT": "wait", "OBSERVE": "observe", "SEE": "observe",
}
POINTER_VERBS = {"click", "double_click", "right_click", "move", "hover", "drag", "scroll"}
_CELL = re.compile(r"^(?:(\d):)?([A-Za-z]\d{1,2}(?:[/.>][A-Za-z]\d{1,2})*)$")
_NUM = re.compile(r"^-?\d+(?:\.\d+)?$")


class ProtocolError(ValueError):
    pass


@dataclass
class TargetSpec:
    label: str = ""
    cell: str = ""
    x: Optional[float] = None
    y: Optional[float] = None
    display: Optional[int] = None
    near: str = ""

    @property
    def is_spatial(self) -> bool:
        return bool(self.cell)

    @property
    def empty(self) -> bool:
        return not self.label and not self.cell

    def to_dict(self):
        return {k: v for k, v in self.__dict__.items() if v not in (None, "")}


@dataclass
class Action:
    verb: str
    target: TargetSpec = field(default_factory=TargetSpec)
    target2: Optional[TargetSpec] = None
    amount: int = 0
    text: str = ""
    keys: str = ""
    until: str = ""
    ms: int = 0
    raw: str = ""

    def to_dict(self):
        d = {"verb": self.verb}
        if not self.target.empty:
            d["target"] = self.target.to_dict()
        if self.target2 and not self.target2.empty:
            d["target2"] = self.target2.to_dict()
        for k in ("amount", "text", "keys", "until", "ms"):
            v = getattr(self, k)
            if v:
                d[k] = v if k != "text" else f"<{len(v)} chars>"
        return d


def _split_statements(src: str) -> List[str]:
    out, buf, q = [], [], ""
    for ch in src:
        if q:
            buf.append(ch)
            if ch == q:
                q = ""
            continue
        if ch in "\"'":
            q = ch
            buf.append(ch)
        elif ch in ";\n":
            s = "".join(buf).strip()
            if s:
                out.append(s)
            buf = []
        else:
            buf.append(ch)
    if q:
        raise ProtocolError("unterminated quote")
    s = "".join(buf).strip()
    if s:
        out.append(s)
    return out


def _is_cell(tok: str) -> bool:
    return bool(_CELL.match(tok))


def _take_target(toks: List[str], i: int, quoted: List[bool]) -> tuple:
    """Parse one target starting at toks[i]. Returns (TargetSpec, next_i)."""
    if i >= len(toks):
        raise ProtocolError("missing target")
    t = TargetSpec()
    tok = toks[i]
    if quoted[i] or not _is_cell(tok):
        if not quoted[i] and _NUM.match(tok):
            raise ProtocolError(f"expected a cell or \"label\", got number {tok}")
        t.label = tok
        i += 1
        # optional IN <cell> / NEAR "<anchor>"
        while i + 1 < len(toks) and not quoted[i] and toks[i].upper() in ("IN", "NEAR"):
            kw = toks[i].upper()
            if kw == "IN":
                if not _is_cell(toks[i + 1]):
                    raise ProtocolError(f"IN expects a cell, got {toks[i + 1]!r}")
                m = _CELL.match(toks[i + 1])
                t.display = int(m.group(1)) if m.group(1) else None
                t.cell = m.group(2).upper()
            else:
                t.near = toks[i + 1]
            i += 2
        return t, i
    m = _CELL.match(tok)
    t.display = int(m.group(1)) if m.group(1) else None
    t.cell = m.group(2).upper()
    i += 1
    if i + 1 < len(toks) and _NUM.match(toks[i]) and _NUM.match(toks[i + 1]) and not quoted[i]:
        t.x, t.y = float(toks[i]), float(toks[i + 1])
        i += 2
    return t, i


def tokenize(line: str) -> List[tuple]:
    """Whitespace tokenizer with "double"/'single' quotes and \\" escapes.
    Returns [(token, was_quoted)] so a quoted "G4" stays a label."""
    out: List[tuple] = []
    i, n = 0, len(line)
    while i < n:
        if line[i].isspace():
            i += 1
            continue
        if line[i] in "\"'":
            q, i, buf = line[i], i + 1, []
            while i < n and line[i] != q:
                if line[i] == "\\" and i + 1 < n and line[i + 1] in (q, "\\"):
                    i += 1
                buf.append(line[i])
                i += 1
            if i >= n:
                raise ProtocolError("unterminated quote")
            i += 1
            out.append(("".join(buf), True))
        else:
            j = i
            while j < n and not line[j].isspace():
                j += 1
            out.append((line[i:j], False))
            i = j
    return out


def parse_line(line: str) -> Action:
    pairs = tokenize(line)
    raw_toks = [t for t, _ in pairs]
    quoted = [q for _, q in pairs]
    toks = raw_toks
    if not toks:
        raise ProtocolError("empty statement")
    verb = VERBS.get(toks[0].upper())
    if not verb:
        raise ProtocolError(f"unknown verb {toks[0]!r}; valid: {sorted(set(VERBS))}")
    a = Action(verb=verb, raw=line)
    # trailing UNTIL "<cond>"
    for k in range(1, len(toks) - 1):
        if toks[k].upper() == "UNTIL" and not quoted[k]:
            a.until = " ".join(toks[k + 1:])
            toks, quoted = toks[:k], quoted[:k]
            break
    i = 1
    if verb in ("click", "double_click", "right_click", "move", "hover"):
        a.target, i = _take_target(toks, i, quoted)
    elif verb == "drag":
        a.target, i = _take_target(toks, i, quoted)
        if i < len(toks) and not quoted[i] and toks[i].upper() == "TO":
            i += 1
        a.target2, i = _take_target(toks, i, quoted)
    elif verb == "scroll":
        if i < len(toks) and not _NUM.match(toks[i]):
            a.target, i = _take_target(toks, i, quoted)
        if i >= len(toks) or not _NUM.match(toks[i]):
            raise ProtocolError("SCROLL needs an amount (wheel notches, + = up)")
        a.amount = int(float(toks[i]))
        i += 1
    elif verb == "type":
        if i >= len(toks):
            raise ProtocolError("TYPE needs \"text\"")
        a.text = toks[i]
        i += 1
    elif verb == "key":
        if i >= len(toks):
            raise ProtocolError("KEY needs a key or chord, e.g. CTRL+L")
        a.keys = toks[i]
        i += 1
    elif verb == "wait":
        if i >= len(toks) or not _NUM.match(toks[i]):
            raise ProtocolError("WAIT needs milliseconds")
        a.ms = max(0, min(10000, int(float(toks[i]))))
        i += 1
    if i != len(toks):
        raise ProtocolError(f"unexpected tokens: {toks[i:]}")
    return a


def parse(src: str) -> List[Action]:
    stmts = _split_statements(src or "")
    if not stmts:
        raise ProtocolError("empty command")
    if len(stmts) > 20:
        raise ProtocolError("at most 20 actions per command")
    return [parse_line(s) for s in stmts]
