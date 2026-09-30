"""Label matching + candidate scoring shared by all resolver tiers."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .geometry import Rect

SOURCE_TRUST = {"uia": 0.95, "lock": 0.93, "ocr": 0.85, "ocr_refined": 0.9,
                "cell": 0.72, "memory": 0.3}


def _norm(s: str) -> str:
    return " ".join((s or "").lower().replace("\u2026", "...").split())


def match_score(query: str, text: str) -> float:
    """0..1 label similarity tolerant to OCR noise; 1.0 = exact."""
    q, t = _norm(query), _norm(text)
    if not q or not t:
        return 0.0
    if q == t:
        return 1.0
    if t.startswith(q) or t.endswith(q):
        return 0.8 + 0.15 * len(q) / len(t)
    if q in t:
        return 0.55 + 0.35 * len(q) / len(t)
    qw = [w for w in q.replace(":", " ").split() if w]
    tw = t.split()
    if qw:
        hits = 0.0
        for w in qw:
            if w in tw:
                hits += 1
            elif any(_edit_ok(w, x) for x in tw):
                hits += 0.8
        frac = hits / len(qw)
        if frac >= 0.7:
            return 0.45 + 0.35 * frac * min(1.0, len(q) / max(1, len(t)) + 0.3)
    return 0.0


def _edit_ok(a: str, b: str) -> bool:
    """<=1 edit for words >=4 chars, <=2 for >=8 (typical OCR slips)."""
    if min(len(a), len(b)) < 4 or abs(len(a) - len(b)) > 2:
        return False
    limit = 2 if len(a) >= 8 else 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
        if min(cur) > limit:
            return False
        prev = cur
    return prev[-1] <= limit


@dataclass
class Candidate:
    text: str
    rect: Rect
    source: str
    score: float = 0.0
    ctype: str = ""
    invoke: bool = False
    enabled: bool = True
    notes: List[str] = field(default_factory=list)

    def to_brief(self, grid=None) -> Dict[str, object]:
        d: Dict[str, object] = {"text": self.text[:40], "source": self.source,
                                "score": round(self.score, 3), "box": self.rect.to_dict()}
        if grid is not None:
            c = grid.cell_at(*self.rect.center)
            if c:
                d["cell"] = c
        return d


def rank(cands: Sequence[Candidate], query: str, near: Optional[Tuple[float, float]] = None,
         within: Optional[Rect] = None, exclusions: Sequence[Rect] = ()) -> List[Candidate]:
    out = []
    for c in cands:
        s = match_score(query, c.text)
        if s <= 0:
            continue
        if not c.enabled:
            s -= 0.4
            c.notes.append("disabled")
        cx, cy = c.rect.center
        if any(z.contains(cx, cy) for z in exclusions):
            s -= 0.6
            c.notes.append("excluded_zone")
        if within is not None and not within.contains(cx, cy):
            s -= 0.5
            c.notes.append("outside_cell")
        if near is not None:
            d = math.hypot(cx - near[0], cy - near[1])
            s += max(0.0, 0.25 - d / 2400.0)
        c.score = s
        out.append(c)
    out.sort(key=lambda c: -c.score)
    return out


def ambiguity(ranked: Sequence[Candidate], min_sep_px: float = 40.0) -> Optional[List[Candidate]]:
    """Return the competing set if the top two are near-equal and apart."""
    if len(ranked) < 2:
        return None
    a, b = ranked[0], ranked[1]
    if b.score < a.score - 0.08:
        return None
    d = math.hypot(a.rect.center[0] - b.rect.center[0], a.rect.center[1] - b.rect.center[1])
    if d < min_sep_px:
        return None   # same control seen twice (e.g. UIA + OCR, or label+inner text)
    return [c for c in ranked if c.score >= a.score - 0.08][:5]
