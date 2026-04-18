---
name: maestro-comfysql
description: "Generate and edit images via comfysql — a SQL-style client that drives a remote ComfyUI server. PRIMARY skill for Maestro's image pipeline. Use for any image generation, character staging, scene composition, camera adjustments, and expression control. Covers four curated Maestro workflows: qwen_image_edit (img-to-img, product-on-person, location swap), qwen_character_scene (close-ups with premium face/skin detail), qwen_next_scene (camera angle + framing changes for video sequences), expression_editor (facial expression retargeting without identity loss). Do NOT use for text-only answers, pure code tasks, or video rendering (not yet wired)."
homepage: https://github.com/zehra-rgb/comfysql
metadata:
  {
    "openclaw":
      {
        "emoji": "🎬",
        "os": ["linux", "darwin"],
        "requires": { "bins": ["python3"] },
        "install":
          [
            {
              "id": "pip-comfysql",
              "kind": "script",
              "label": "Install comfysql from Maestro's fork",
              "command": "pip install --user git+https://github.com/zehra-rgb/comfysql.git",
              "bins": ["comfysql"]
            }
          ],
        "tags": ["image-generation", "image-edit", "comfyui", "comfysql", "qwen", "maestro"],
        "category": "media-generation",
        "priority": 100
      }
  }
---

# maestro-comfysql

Drive Maestro's curated ComfyUI workflows through `comfysql` — a SQL-style CLI that compiles `SELECT image FROM <workflow> ... WHERE ...` into a ComfyUI prompt graph, submits it, and downloads the result.

## What comfysql is

`comfysql` is a thin Python client that wraps ComfyUI's HTTP + WebSocket API. It does **not** run the model itself — it sends workflow JSON to a remote ComfyUI server, streams progress over WebSocket, and downloads outputs. The ComfyUI server is elsewhere (configured via a server **alias**, not a raw URL).

- Workflow catalog lives in the client (one JSON per workflow under `input/workflows/`).
- Input assets (PNGs, JPEGs, reference images) are uploaded to the server before submission via `copy-assets` or referenced by filename if already present.
- Outputs land in `./output/` by default; use `--download-output` on submit to pull images down locally.

## Setup (one-time per container)

**Required env var:**
- `COMFY_SERVER` — server alias from `comfy-agent.json` (not a URL). For Maestro, this is `laptop` in dev or `prod` in production.

**Verify comfysql is available:**
```bash
comfysql --version
comfysql doctor "$COMFY_SERVER"
```

A healthy doctor output looks like:
```
doctor health=ok object_info=ok models=ok websocket=ok
doctor_summary status=ok
```

If `comfysql` isn't on PATH, install from Maestro's fork:
```bash
pip install git+https://github.com/zehra-rgb/comfysql.git
```

**One-time sync** (pulls node/model catalog from the server; speeds up SQL compilation):
```bash
comfysql sync "$COMFY_SERVER"
```

## The CLI — what you'll actually run

All commands accept the server alias positionally after the subcommand.

| Command | Purpose |
|---|---|
| `comfysql doctor "$COMFY_SERVER"` | Health + auth + WebSocket check |
| `comfysql sync "$COMFY_SERVER"` | Refresh node/model catalog cache |
| `comfysql sql "$COMFY_SERVER" --sql "<SQL>;"` | Run a SQL statement (compile + submit + optionally download) |
| `comfysql sql "$COMFY_SERVER" --sql "SHOW TABLES workflows;"` | List available workflows |
| `comfysql sql "$COMFY_SERVER" --sql "DESCRIBE WORKFLOW qwen_image_edit;"` | Show a workflow's bindable fields |
| `comfysql copy-assets "$COMFY_SERVER"` | Upload everything from `input/assets/` to the server |
| `comfysql submit "$COMFY_SERVER" <workflow.json>` | Submit a raw workflow JSON (skip SQL layer) |
| `comfysql validate <workflow.json>` | Static validation (no submit) |

**Useful flags for `sql`:**
- `--dry-run` — compile the SQL to API-prompt JSON without submitting. Great for debugging before burning a GPU run.
- `--output-mode download` (or `--download-output`) — after successful submit, download the generated image to `./output/`.
- `--upload-mode strict|warn|off` — how strictly to auto-upload referenced asset files before submit.
- `-y` — skip confirmation prompts.
- `--no-cache` — force a fresh run (randomizes seed).

## SQL shape you'll use most

```sql
SELECT image
FROM <workflow_name>
USING <preset_name>          -- optional; preset fills in defaults
PROFILE <profile_name>       -- optional; cross-workflow style overrides
WHERE <field>=<value>
  AND <field>=<value>;
```

- `<workflow_name>` — one of the four Maestro workflows below (or anything from `SHOW TABLES workflows;`).
- `<preset_name>` — a saved parameter bundle; `default_run` is the safe default.
- `WHERE` clauses — inputs the workflow expects (prompt text, image filenames, numeric knobs). Use `DESCRIBE WORKFLOW <name>;` to see exactly what a given workflow accepts.

## Which workflow, when

The four curated Maestro workflows — use them based on what the user is asking for.

### `qwen_image_edit` — the default for image-to-image

**Use when:** the user wants to modify or composite an image. "Put this dress on the person", "have her hold this product", "place him in this location", "add sunglasses", "swap the background". Takes one or more reference images and a text instruction.

**Typical:**
```sql
SELECT image
FROM qwen_image_edit
USING default_run
WHERE prompt='Put the green silk dress on the woman, studio lighting, realistic'
  AND input_image='person.png'
  AND reference_image='green_dress.png';
```

### `qwen_character_scene` — higher-fidelity character renders

**Use when:** you need premium face detail, skin texture, and lighting on a character — e.g. close-ups, hero shots, key art. Same kind of instruction as `qwen_image_edit` but slower and more polished for faces. Prefer this over `qwen_image_edit` when the user is pitching a scene that centers on a person's face.

**Typical:**
```sql
SELECT image
FROM qwen_character_scene
USING default_run
WHERE prompt='Close-up portrait, side light, melancholic mood, 85mm'
  AND character_image='hero.png';
```

### `qwen_next_scene` — camera and framing changes for sequences

**Use when:** the user has an existing shot and wants a *different camera* on the same moment — pull to full body, go wide, swing to a side angle, reverse shot. Purpose-built for video storyboarding: given shot N, produce shot N+1 with a different framing but consistent subject.

**Typical:**
```sql
SELECT image
FROM qwen_next_scene
USING default_run
WHERE prompt='Pull back to full body, three-quarter angle from the left'
  AND input_image='shot_01.png';
```

### `expression_editor` — facial expression control

**Use when:** the user wants to change *only* the emotion/expression on a face while preserving identity — a smile, a frown, raised brows, closed eyes. Operates on a single face image; does not reframe or re-render the scene.

**Typical:**
```sql
SELECT image
FROM expression_editor
USING default_run
WHERE input_image='portrait.png'
  AND smile=0.7
  AND eye_opening=0.4
  AND eyebrow_raise=0.3;
```

*(Exact knob names vary — always `DESCRIBE WORKFLOW expression_editor;` to see the current field list.)*

## End-to-end example

User asks: *"Take the picture of Ilker I uploaded, put him in the Paris café wearing the leather jacket."*

1. **Confirm assets** are on the server:
   ```bash
   comfysql copy-assets "$COMFY_SERVER"   # uploads anything new from input/assets/
   ```

2. **Dry-run to sanity-check** compilation:
   ```bash
   comfysql sql "$COMFY_SERVER" --dry-run --sql "
     SELECT image
     FROM qwen_image_edit
     USING default_run
     WHERE prompt='Put Ilker in a Paris café, golden hour, wearing the leather jacket'
       AND input_image='ilker.png'
       AND reference_image='leather_jacket.png'
       AND location_image='paris_cafe.jpg';"
   ```

3. **Submit and download**:
   ```bash
   comfysql sql "$COMFY_SERVER" -y --download-output --sql "
     SELECT image
     FROM qwen_image_edit
     USING default_run
     WHERE prompt='Put Ilker in a Paris café, golden hour, wearing the leather jacket'
       AND input_image='ilker.png'
       AND reference_image='leather_jacket.png'
       AND location_image='paris_cafe.jpg';"
   ```

4. **Result** is in `./output/` with the prompt ID in the filename. Hand it back to the user.

## When NOT to use this skill

- User wants **pure text generation** (use the main chat model).
- User wants **video rendering** (Wan workflows are installed on the server but not wrapped here yet).
- User wants **raw ComfyUI workflow authoring** (use `comfysql submit` with a hand-crafted JSON, or point them at the ComfyUI web UI).
- The image involves **identities or subjects the user hasn't explicitly approved** (Maestro policy — always confirm before generating a recognizable person).

## Troubleshooting

- `doctor health=fail` → server alias wrong, or the tunnel/ComfyUI is down. Check `$COMFY_SERVER` and try the URL from `comfy-agent.json` directly.
- `HTTP 403 Forbidden` on `/object_info` or `/models` → upstream Cloudflare bot rules. The Maestro-vendored comfysql sets a passing User-Agent; verify you're using that build.
- `validation_failed missing_models` → the workflow references a checkpoint/LoRA/VAE not installed on this server. Pick a different workflow or ask the human.
- `validation_failed unknown class_type` → custom node not installed on the server. Same — pick another workflow.
- Workflow submits but hangs forever → WebSocket not reachable; check `doctor websocket`.
- Want to see the compiled prompt before submission → add `--dry-run`.

## What the output looks like

`comfysql sql ... -y --download-output` writes:
- `./output/<timestamp>/<workflow>_<seed>.png` — the generated image(s)
- `./.state/sql_runs/run_<ts>/statement_<n>_api_prompt.json` — the compiled ComfyUI prompt (useful for debugging)
- stdout: `validated nodes=N edges=N ... api_prompt: <path>` on success
