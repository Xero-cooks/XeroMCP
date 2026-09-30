"""XeroSpatial - spatial computer-control subsystem for XeroMCP.

The agent reasons about *what* the target is; XeroSpatial reasons about
*where exactly* it is; Windows receives one exact physical coordinate.

Layers (pure modules are platform-independent and unit-tested on any OS):
  geometry   - Rect, Grid (16x8 A1..P8), local 0..64 coords, refinement
  displays   - monitor enumeration (bounds, DPI scale) + transforms
  frame      - SpatialFrame + VisionCache (TTL + fingerprint invalidation)
  locks      - short-lived TARGET LOCKs with pixel revalidation
  memory     - probabilistic per-app spatial priors (non-sensitive only)
  safe_point - safe interior click point + confidence
  protocol   - compact action language (CLICK G4 32 48, CLICK "Send", ...)
  resolver   - lock -> UIA -> cached OCR -> fresh OCR -> grid, with refinement
  verify     - BEFORE -> action -> AFTER comparison
  telemetry  - per-stage latency
  debug_render - engineering overlay image (returned in-band, never on disk)
  engine     - the orchestrator used by the `spatial_point` MCP tool
  backends   - the real Windows wiring (capture/OCR/UIA/mouse)
"""
