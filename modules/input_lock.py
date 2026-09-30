"""One process-wide lock for ALL synthetic input (mouse + keyboard).

Held for the duration of a single atomic gesture (a click, a chord, a typed
string, a drag) so two concurrent MCP requests can never interleave their
events - e.g. a `chrome_session type` landing in the middle of a `point` drag.
Re-entrant so composite gestures can nest.
"""
import threading

INPUT_LOCK = threading.RLock()
