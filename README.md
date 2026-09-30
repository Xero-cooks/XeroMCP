# XeroMCP / XeroPC

Paste this whole file at the start of a new agent chat. This is the **only** instruction file. Do not look for AGENT.md.

You are driving **XeroPC**, a local Windows hub tunneled into Notion as `mcpServer_xeropc`.
Repo: `Xero-cooks/XeroMCP` (public, branch `main`). Hub folder:
`C:\Users\User\Downloads\testers\MCPbridges\MCPbridges`

**Do not create a new MCP. Do not replace XeroPC. Do not open a pull request.**

---

## Tools (5 fat tools + `spatial_point` + `xero` — complexity lives inside them)

| Tool | Use for | Never use for |
|---|---|---|
| `chrome_session` `op=go` | Real Chrome identity (Kartik, Gmail, NotebookLM, Docs, Drive) | Debug profile / CDP / `web_task` |
| `chrome_session` `op=type` / `keys` / `urlbar` | Type and chords in **real** Chrome | Agent-side pyautogui |
| `web_task` | Chat composer on the **dedicated debug** Chrome (`:9222`) | Kartik / real Google account |
| `see` | Unknown native UI, proof screenshots | Driving the profile picker |
| `point` | Click a **named** control with `until` proof | Raw x,y as the normal path |
| `spatial_point` | XeroSpatial: act by **grid cell** (`G4 32 48`) or label, chained commands, locks, proof | Guessing cells without a fresh `see(grid=true)` |
| `pc` | Shell, files, processes, git | `taskkill chrome`, probing port 8000 |

If Notion's tools/list does not show all 6 tools (`web_task`, `see`, `point`, `spatial_point`, `chrome_session`, `pc`), the cache is stale. Ask the user to **restart the hub and fully reconnect XeroPC**. Do not invent a sixth tool while you wait. Do not call `point` until it appears in tools/list.

---

## Identity (Kartik / NotebookLM / Gmail) — the 1-second path

Kartik Raghav = Chrome **Profile 11** = `kartikraghav1st@gmail.com`.
Chrome binary: `C:\Program Files\Google\Chrome\Application\chrome.exe`
Never pick Default / Ghost Oftheuchiha / SAURABH RAGHAV / XERO / Vinayak / Saurabh.

This is the call. Use it first. It is `chrome.exe --profile-directory="Profile 11" <url>`.

```
chrome_session(
  op="go",
  profile="Kartik",
  url="https://notebook.google.com",
  until="url contains notebook.google",
)
```

Rules for `op=go`:
- If that profile is already on the page, it returns `already_open` and focuses the window.
- Never kill Chrome. Never `--profile-picker`. Never `--user-data-dir=.chrome_debug_profile`.
- Always `--profile-directory`. Never launch_chrome_with_cdp for Kartik.
- If `status=ok`, you are done. Do **not** `see` the picker. Do **not** `point` at avatars.

Then type without OCR:

```
chrome_session(op="type", text="summarize this notebook", submit=true)
chrome_session(op="keys", keys="ctrl+l")
chrome_session(op="urlbar")
```

`web_task` is the **wrong** tool for Kartik / Gmail / NotebookLM / Docs / Drive. It uses the debug twin on CDP `:9222`.

---

## Point — sniper mouse (ghost → warp → flick)

You send WHAT to hit and WHAT MUST BE TRUE AFTER. Never raw coordinates as the normal path.

1. **ghost** — UIA Invoke, cursor does not move (buttons/links).
2. **warp / sniper (default)** — one `SendInput` of `MOVE_ABSOLUTE + DOWN + UP`. Fast and accurate.
3. **flick** — only if `motion="human"` (a site watching the path). Cap ~70ms.

```
see(target="window:Chrome", want=["ocr"], thumbnail_width=400)
point(target="Ask anything", window="Chrome", until="Ask anything gone", motion="sniper")
```

- `role="avatar"` clicks the colored circle **above** the caption, never the name.
- Ambiguous matches are refused — pass `near=`.
- Only `status=hit` / `until_ok` counts. `fired_unverified` is a miss.
- Prefer OS/Chrome profile launch over clicking the picker when identity is known.

---

## XeroSpatial — `spatial_point` (v2.4)

The screen (or the focused window with `scope="window"`) is a **16×8 grid**:
columns `A..P` left→right, rows `1..8` top→bottom. Every cell has **local
coordinates 0..64** (`0,0` top-left, `32,32` centre, `64,64` bottom-right,
always inside the cell). `G4/B3` refines into a 4×4 sub-grid. `1:G4` = display 1.

```
see(target="window:Chrome", grid=true)            # image with grid + frame_id + cells{G4: ["Save"]}
spatial_point(cell="G4", x=32, y=48, frame_id="<id>", until="Saved")
spatial_point(target="Save", until="Save gone")   # semantic: lock -> UIA -> cached OCR -> fresh OCR
spatial_point(command='CLICK "Search" IN B1; TYPE "hello"; KEY ENTER')
spatial_point(action="drag", cell="B2", to_cell="C5")
spatial_point(action="observe", debug=true)        # fresh grid frame + annotated image
spatial_point(action="cancel")                     # cancels everything submitted before it
```

Protocol verbs: `CLICK DOUBLE_CLICK RIGHT_CLICK MOVE HOVER DRAG … TO … SCROLL [cell] n TYPE "…" KEY chord WAIT ms OBSERVE`,
modifiers `IN <cell>`, `NEAR "anchor"`, `UNTIL <condition>`; chain with `;` or newlines (≤ 20).

Safety contract:
- `frame_id` given and that region changed → **`stale_frame`**, nothing clicked. Re-observe.
- A label given with a cell (`CLICK "Delete" IN G4 32 32`) that is not actually there → **`refused_low_confidence`**.
- Two similar matches → **`ambiguous`** + candidates (pass `near=` or a cell). Never a guess.
- Another window over the point → `occluded`; focus not provable → `focus_failed`; keyboard with focus lost → `focus_lost`; UIPI/elevated target → `input_blocked`.
- At most ONE retry, only when the target is provably untouched; toggles/checkboxes/edits are never re-clicked.
- **Only `hit` is success.** Without `until`, a fired click is `fired_unverified` (move/hover prove themselves by cursor position).
- `dry_run=true` resolves and reports the point without firing. `debug=true` returns an annotated image.
- Every result carries `timing_ms` per stage; `action="stats"` returns p50/p95 per stage, live locks, cache state.
- Target locks live 10 s and die on window move/focus change/pixel change. Spatial memory (window-relative priors for
  control labels only; no text content, e-mails or numbers) is stored in `.xerospatial_memory.json`
  (never committed; disable with `XEROSPATIAL_MEMORY=0`).

### Behaviour changes in v2.4 (all tools)
- `point` without `until` now returns `fired_unverified` (was `hit`). `until="X gone"` no longer fails on a stale pre-click OCR.
- `point` drag no longer clicks before dragging; right/double clicks are never replaced by UIA Invoke; retries never re-click toggles.
- `chrome_session` `type`/`keys`/`urlbar` refuse (`focus_failed`) when Chrome did not provably take the foreground.
  (Keyboard input was silently dropped before: the `INPUT` struct was 32 bytes instead of 40, so `SendInput` rejected every event.)
- `pc` and `chrome_session` no longer block the server's event loop; all input (point / spatial_point / see click_text / chrome typing) is serialized on one thread.
- Per-monitor DPI v2, multi-monitor captures in memory (no more `region_*.jpg` per click), bearer token masked in the console.

### Tests / benchmarks
- `python -m pytest tests -q` — XeroSpatial geometry/protocol/locks/memory, engine on a fake desktop, server auth + tools/list (any OS).
- `python tests/test_mouse.py` — live Windows click tests (spawns its own tkinter target).
- `python tests/bench_spatial.py --live -n 20` — raw vs `point` vs `spatial_point` (cold / lock / cell), per-stage p50/p95.

---

## XeroRuntime — `xero` (v2.5): one shared, continuously observed computer

`see`, `point`, `spatial_point`, `chrome_session` and `xero` all read/write ONE
`ComputerRuntime` (`modules/runtime/`): state, event buffer, target tracker,
capability cache, sessions. Physical input is still ONLY on the single input
thread; the runtime never injects.

| ops (`xero`) | what |
|---|---|
| `observe` | compact state (frame_id, age, window, observed Chrome profile, change class + cells, tracked targets with bbox/cells/confidence, text, grid + `space_id`, active action, stale flag, capabilities) and only the events NEW since your last observe. No screenshot unless `image=true`. `refresh`/`deep` force a fresh tick. |
| `events` | WINDOW_CHANGED, FOCUS_CHANGED, PROFILE_CHANGED, URL_CHANGED, MODAL_APPEARED, SCREEN_CHANGED, TARGET_APPEARED/MOVED/DISAPPEARED, ACTION_STARTED/COMPLETED/INTERRUPTED, VERIFICATION_PASSED/FAILED, CAPABILITY_CHANGED, STATE_INVALIDATED (bounded ring, 256) |
| `stream` | `steps:[{type,target|cell|x,y,until?,expect?}]` in ONE round trip. Each step EXPECT→ACT→VERIFY. Before each step the world is compared with what the previous step verified; modal/focus/profile change ⇒ `interrupted` (`fired:false`), soft change (screen/navigation) ⇒ re-acquire once. `motion:"trajectory"` walks the cursor in waypoints with focus/target checks between them. Steps without `until` are accepted only with *weak, labelled* state evidence, else `unverified`. |
| `profiles` / `profile` | Chrome profile registry (from Chrome `Local State`) + the profile of the FOREGROUND Chrome window with evidence |
| `caps` / `status` / `cancel` / `reset` | capability cache, counters, cancel, invalidate |

**Perception**: background thread only while a session is active (300 s TTL).
Fast loop 4 Hz (cursor, foreground window, geometry) → medium loop 1 Hz (display-signature
change class, Chrome profile/title) → deep (OCR frame + target tracking) only on uncertainty:
window/navigation/major layout change or a tracked target's region changed (≥1.5 s apart).
Cursor-only/animation changes keep locks and never trigger OCR.

**Coordinates**: `see(grid=true)` now registers its frame through the same engine path as
`spatial_point` (same surface/capture) and returns `coordinate_space` (`space_id`, monitor,
dpi/scale, capture→physical mapping, `same_as_see_capture`). `space_id` changes when monitors or DPI change and
invalidates targets/locks.

**Chrome `go`** returns `requested_profile` vs `observed_profile`, `stages` (profile_verified,
launch_requested, launch_started, window_found, tab_found, navigation_started,
navigation_verified, destination_visible) and status `ok | profile_mismatch |
navigation_unverified | focus_failed | launch_failed | profile_unknown | launched_unverified`.
`already_open:true` ⇒ that profile's own window is foreground AND its active tab matches
(`destination_evidence` says `window_title` or `url`; real Chrome has no CDP, so title is the evidence).
Profile purposes live in `xero_profiles.json` (gitignored, see the `.example.json`); never guessed.

**Verification**: nothing claims verification it didn't perform: no `until` ⇒ `until_ok:null, how:"not_requested"`.
CDP is probed once per endpoint and cached (`caps`); when unavailable, url proofs fall back to
window title → UIA → OCR immediately (weaker evidence is labelled).

Windows caveat: the runtime, stream, trajectory and Chrome state machine are unit-tested on a
simulated desktop (`python -m pytest tests -q`, `python tests/bench_runtime.py`). Real-input
behaviour must be validated on Windows: `python tests/test_mouse.py`,
`python tests/bench_spatial.py --live -n 20`.

## Hard never-do list

- `taskkill` / `Stop-Process` chrome.exe
- Commands touching port 8000 (Tailscale funnel) unless `allow_port_8000=true`
- Agent-side `pyautogui` via `pc run`
- `--profile-picker` or debug `user-data-dir` for identity work
- Fetching `inspection_image_url` / localhost screenshot URLs
- Treating `clicked:true` / `fired_unverified` as success
- Restarting the hub in a way that drops the tunnel unless the user asked
- Opening a GitHub pull request (push `main` directly)
- Printing `.mcp_token`, `.mcp_status.json` bearer, or any GitHub PAT

---

## Git

- Remote: `https://github.com/Xero-cooks/XeroMCP.git`
- Work on `main`. Push `origin main`. **No PR.**
- Never commit `.mcp_token`, `.mcp_status.json`, `server.log`, `.mcp_inspections/`, `.xerospatial_memory.json`, `__pycache__/`, `_patch_sniper.py`, `*.bak`, `.tmp_mouse_b64.txt`

---

## After a pull / code change

1. Restart the hub (`start_server.bat` in the hub folder) — **only when the user asked** or after they pull these changes.
2. In Notion: disconnect XeroPC, then reconnect (tools/list is cached until a full reconnect).
3. You should then see: `web_task`, `see`, `point`, `spatial_point`, `chrome_session`, `pc`.
4. `chrome_session` should accept `op=go` with `profile` / `until` / `text` / `keys`.
