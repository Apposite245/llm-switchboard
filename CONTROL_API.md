# LLM Switchboard — control API

A small HTTP API Switchboard serves so local tools can start, stop, suspend and resume its
engines **through Switchboard** instead of launching or killing `ninfer-serve.exe` /
`llama-server.exe` directly.

Why go through Switchboard: it builds each engine's command line from saved per-artifact and
per-model settings (context size, KV type, MTP, vision, chat template…) and owns the child
process. A tool that runs the exe itself has to duplicate all of that, and leaves the
Switchboard's view of the process — buttons, consoles, the `local-llama.json` manifest — out of
date.

- **Base URL:** `http://127.0.0.1:8090` (change with `"control_port"` in
  `%APPDATA%\llm-switchboard\settings.json`)
- Localhost only, **no authentication**, JSON in and out.
- Requests from web pages are refused: any request with an `Origin` header, or with a
  `Host` other than `127.0.0.1` / `localhost`, answers **403**. Local tools (curl,
  PowerShell, Python, an MCP server) send neither and are unaffected.
- Available whenever Switchboard window is open. If the port is taken (e.g. a second
  Switchboard), that instance runs without the API and says so in its status bar.
- Every action is echoed into the relevant console tab, e.g.
  `[switchboard] suspended via control API (released 18450 MB)`.

Engines are named `ninfer` (port 8082 by default), `llama` (the llama.cpp single server
or router, port 8080) and `strata` (Strata's engine with one of its `strata-*.json` configs,
port 8097).

`strata` cannot run beside either of the others: it takes nearly the whole GPU and most of
system RAM. Starting it while `llama` or `ninfer` runs (or either of them while `strata`
runs) answers **409** with `"running": [...]`; suspend or stop the other engine first.

## Freeing the GPU for a ComfyUI generation

This is what the API is for. The sequence an MCP tool should run:

```text
1. POST /suspend        {"engines": ["ninfer"]}     # stop the LLM, wait until VRAM is released
2. queue the ComfyUI workflow and wait for it to finish
3. POST <comfy>/free    {"unload_models": true, "free_memory": true}
4. POST /resume         {}                           # restart it; returns once it can serve
5. return the tool result
```

Step 3 is required. ComfyUI keeps models resident after a job. `/resume` checks that enough
VRAM is free before it starts the engine and answers **409** if it is not, rather than
starting anyway. On Windows, starting short of VRAM does not fail: the driver silently spills
to system memory and decode runs at roughly half speed.

Things to know when this runs inside the model's own tool call:

- It works because nothing is generating while a tool runs. The model has already emitted
  the tool call and is waiting for the result.
- `/resume` returns only when the server is healthy **and** `/v1/models` lists the model, so
  the harness's next request will not 404.
- The restarted server has an empty prompt cache. The next turn re-reads the whole
  conversation, which can take several seconds per 10k tokens of context.
- The tool's timeout in the MCP client must be longer than the slowest ComfyUI job plus about
  30 s for suspend and resume.
- **Pick the engine that serves the calling model.** `engines` defaults to *every running
  engine*. If the calling agent runs on the llama.cpp router, suspending `llama` is correct;
  if it runs on NInfer, pass `["ninfer"]` so the router — and anything else using it — stays
  up.

## Endpoints

All responses carry `"ok": true|false`. Errors add `"error": "<reason>"`.

### `GET /status`

```json
{
  "ok": true,
  "engines": {
    "ninfer": {"state": "ready", "pid": 32148, "url": "http://127.0.0.1:8082",
               "artifact": "D:\\ninfer\\models\\my_model.ninfer",
               "model_id": "my-model"},
    "llama":  {"state": "stopped", "pid": null, "url": "http://127.0.0.1:8080", "mode": "router"},
    "strata": {"state": "stopped", "pid": null, "url": "http://127.0.0.1:8097",
               "artifact": "D:\\Strata\\strata-my-model.json", "model_id": null}
  },
  "gpu": {"name": "NVIDIA GeForce RTX 4090", "total_mb": 24564, "used_mb": 18480, "free_mb": 6084},
  "suspended": {}
}
```

`state` is one of `stopped`, `starting`, `ready`, `stopping`. When NInfer or Strata is
stopped, `artifact` is what Start would launch; for Strata it is the config file.

### `POST /suspend`

Body (optional): `{"engines": ["ninfer"]}` — default: every running engine.

Stops each engine, waits for the process to exit and for GPU memory to settle, and records
what was stopped and how much VRAM it released (its footprint). Engines that are not running
are skipped.

```json
{"ok": true, "suspended": ["ninfer"],
 "records": {"ninfer": {"artifact": "D:\\ninfer\\models\\my_model.ninfer",
                        "footprint_mb": 18450}},
 "free_mb": 30120}
```

### `POST /resume`

Body (all optional):

| field | default | meaning |
|---|---|---|
| `vram_wait_s` | `60` | how long to wait for VRAM to free up before giving up |
| `margin_mb` | `512` | headroom required on top of the recorded footprint |
| `force` | `false` | start even if VRAM is short (expect the half-speed spill) |

Restarts exactly what `/suspend` stopped, with the same artifact and its saved settings.
Blocks until each engine is ready.

```json
{"ok": true,
 "resumed": {"ninfer": {"started": true, "state": "ready", "url": "http://127.0.0.1:8082",
                        "artifact": "...", "model_id": "my-model"}},
 "free_mb": 11650}
```

- Nothing suspended → `{"ok": true, "resumed": {}}`.
- Engine already running again → reported as `{"already_running": true}`.
- **409** when VRAM stays short:
  `{"ok": false, "error": "not enough free VRAM to resume ninfer", "free_mb": 10000, "needed_mb": 18962}`.
  The suspend record is kept, so free ComfyUI's memory and call `/resume` again.

### `POST /ninfer/start`

Body (optional): `{"artifact": "my_model.ninfer", "wait": true}`

`artifact` is a file name or full path under the NInfer folder's `models\`. It selects that
artifact in Switchboard, exactly as picking it from the list would, including its saved
settings. Omit it to start whatever is selected. With `wait` (the default) it returns once
the server is ready, with its `model_id`.

- Already running the same artifact → `{"started": false, "already_running": true, ...}`.
- Running a different artifact → **409**. Stop it first.
- Unknown artifact → **400** with the reason.

### `POST /llama/start`

Body (optional): `{"wait": true}`. Starts the llama.cpp engine in whatever mode and with
whatever model Switchboard has selected (single or router).

### `POST /strata/start`

Body (optional): `{"config": "strata-my-model.json", "wait": true}`

`config` is a file name or full path of a `strata-*.json` in the Strata folder (the ones
Strata's setup writes). Omit it to start whatever is selected on the Strata page. With `wait`
it returns once the server is ready, with its `model_id`; Strata's load takes minutes, so the
readiness timeout is 900 s instead of 300 s.

### `POST /ninfer/stop`, `POST /llama/stop`, `POST /strata/stop`

Stops the engine and waits until it has exited:
`{"ok": true, "stopped": true, "freed_mb": 18450}`, or
`{"ok": true, "stopped": false, "already_stopped": true}`.

## Status codes

| code | meaning |
|---|---|
| 200 | done |
| 400 | bad request: malformed JSON, unknown engine, unknown artifact, nothing selected |
| 403 | the request came from a web page (`Origin` header) or used an unexpected `Host` |
| 404 | no such route |
| 409 | conflict: engine busy starting or stopping, a different artifact is running, an engine that cannot share the GPU is running, or not enough VRAM to resume |
| 500 | the engine exited during startup (details in its console tab) |
| 504 | timed out waiting to stop (30 s) or become ready (300 s; 900 s for Strata) |

Operations that change state run one at a time; concurrent calls queue behind each other
rather than interleaving.

## Example (PowerShell)

```powershell
$api = "http://127.0.0.1:8090"
Invoke-RestMethod "$api/suspend" -Method Post -ContentType application/json -Body '{"engines":["ninfer"]}'
# ... run the ComfyUI job, then POST http://127.0.0.1:8188/free {"unload_models":true,"free_memory":true}
Invoke-RestMethod "$api/resume" -Method Post -ContentType application/json -Body '{}'
```

Implementation: `core/control.py`.
