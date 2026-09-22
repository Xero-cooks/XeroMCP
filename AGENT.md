# XeroMCP / XeroPC — agent playbook

This hub is **5 fat tools**. Complexity lives inside them. Do not invent a 6th MCP.

| Tool | Use for | Never use for |
|---|---|---|
| `chrome_session` `op=go` | Real Chrome identity (Kartik, Gmail, NotebookLM) | Debug profile / CDP toys |
| `web_task` | Chat composer on the **dedicated debug** Chrome | Kartik / real Google account |
| `see` | Unknown native UI, proof screenshots | Driving the profile picker |
| `point` | Click a **named** control with `until` proof | Raw x,y as the normal path |
| `pc` | Shell, files, processes | `taskkill chrome`, probing port 8000 |

## Identity tasks (the fast path)

When the user wants a **real Chrome profile** (Kartik Raghav, a Gmail, NotebookLM):

```
chrome_session(
  op="go",
  profile="Kartik",                          # gaia name, email, or "Profile 11"
  url="https://notebook.google.com",
  until="url contains notebook.google",
)
```

That launches `chrome.exe --profile-directory=<resolved> <url>` (~1s). It:

- never kills Chrome
- never opens the profile picker
- never uses `.chrome_debug_profile`
- proves success from the **window title** (real Chrome has no CDP)

If `status=ok`, you are done. Do **not** `see` the picker. Do **not** `point` at avatars.

List identities: `chrome_session(op="profiles")`.
List Chrome windows: `chrome_session(op="windows")`.

## Debug-profile chat (nokeep / anonymous sites)

`web_task(url=..., message=...)` attaches CDP on the dedicated debug profile.
Sessions persist there. This is the **wrong** tool for Kartik's Google account.

## Vision + mouse

```
see(target="window:Chrome", want=["ocr"], fast=true)   # skip 800px JPEG + landmarks
point(target="Ask anything", window="Chrome", until="Ask anything gone", role="")
```

- Default `point` motion is `sniper` (warp). Do not ask for `human` flick.
- `role="avatar"` clicks the colored circle **above** the caption.
- Ambiguous matches are refused — pass `near=`.
- `until` is required for anything that must actually change the UI.
- `see` warns if the foreground is Edge/Instagram/Notion — lock with `window=`.

## Hard never-do list

- `taskkill` / `Stop-Process` chrome.exe (hub rejects it)
- Commands touching port 8000 (Tailscale funnel) unless `allow_port_8000=true`
- Agent-side `pyautogui` via `pc run`
- Opening `--profile-picker` or `--user-data-dir=.chrome_debug_profile` for identity work
- Fetching `inspection_image_url` / localhost screenshot URLs
- Treating `clicked:true` / `fired_unverified` as success — only `hit` / `until_ok` counts

## After pulling this repo

Restart the hub, then **fully reconnect** the Notion MCP (tools/list is cached).
You should see: `web_task`, `see`, `point`, `chrome_session`, `pc`.
