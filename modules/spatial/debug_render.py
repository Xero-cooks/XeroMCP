"""Engineering overlay: grid, cell labels, detected targets, chosen box/point,
confidence, verification and latency. Returned in-band as an MCP image; never
drawn on the desktop and never written to disk."""
from __future__ import annotations

import base64
import io
from typing import Any, Dict, Optional, Tuple

from .frame import SpatialFrame


def render(frame: SpatialFrame, res=None, pt: Optional[Tuple[int, int]] = None,
           out: Optional[Dict[str, Any]] = None, timing: Optional[Dict[str, float]] = None,
           max_width: int = 1280, quality: int = 70) -> Dict[str, str]:
    from PIL import Image, ImageDraw
    img = frame.image.convert("RGB").copy()
    d = ImageDraw.Draw(img, "RGBA")
    tr, g = frame.transform, frame.grid

    def box(r, color, width=2):
        x0, y0 = tr.to_image(r.x, r.y)
        x1, y1 = tr.to_image(r.right - 1, r.bottom - 1)
        d.rectangle([x0, y0, x1, y1], outline=color, width=width)

    for cid, cr in g.cells():
        box(cr, (0, 170, 255, 110), 1)
        x0, y0 = tr.to_image(cr.x, cr.y)
        d.text((x0 + 3, y0 + 2), cid, fill=(0, 120, 255, 220))
    for e in frame.elements[:400]:
        box(e.rect, (255, 200, 0, 150), 1)
    for c in (getattr(res, "candidates", None) or [])[:5]:
        box(c.rect, (255, 90, 0, 220), 2)
    if res is not None and getattr(res, "rect", None) is not None:
        box(res.rect, (0, 220, 90, 255), 3)
    if pt is not None:
        ix, iy = tr.to_image(*pt)
        d.line([ix - 12, iy, ix + 12, iy], fill=(255, 0, 60, 255), width=2)
        d.line([ix, iy - 12, ix, iy + 12], fill=(255, 0, 60, 255), width=2)
    lines = []
    if out:
        rz = out.get("resolved") or {}
        lines.append(f"status={out.get('status')} source={rz.get('source')} conf={rz.get('confidence')}")
        lines.append(f"cell={rz.get('cell')} local={rz.get('local')} addr={rz.get('address')} px={rz.get('point')}")
        pr = out.get("proof") or {}
        lines.append(f"verify={pr.get('how')}:{pr.get('until_ok')} evidence={out.get('evidence')}")
    if timing:
        lines.append("ms " + " ".join(f"{k}={v:.0f}" for k, v in timing.items()))
    if lines:
        h = 14 * len(lines) + 8
        d.rectangle([0, 0, img.width, h], fill=(0, 0, 0, 170))
        for i, ln in enumerate(lines):
            d.text((6, 4 + 14 * i), ln[:220], fill=(255, 255, 255, 255))
    if img.width > max_width:
        img = img.resize((max_width, int(img.height * max_width / img.width)), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return {"type": "image", "data": base64.b64encode(buf.getvalue()).decode("ascii"), "mimeType": "image/jpeg"}


def render_grid(frame: SpatialFrame, max_width: int = 1280, quality: int = 65) -> Dict[str, str]:
    """see(grid=true): annotated screenshot for the agent (grid + cell ids)."""
    return render(frame, None, None, None, None, max_width=max_width, quality=quality)
