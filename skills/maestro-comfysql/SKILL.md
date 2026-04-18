---
name: maestro-comfysql
description: "Generate and edit images by writing small SQL queries against pre-built ComfyUI workflow templates. PRIMARY skill for Maestro's image pipeline. Use when the user wants to create, edit, composite, re-light, re-pose, or transform images — including putting a person in a new location, changing outfit, camera-angle changes for storyboard shots, expression adjustments, virtual try-on, color-matching between images. Do NOT use for text-only responses, video rendering (not wired up), or tasks that don't involve producing an image."
homepage: https://github.com/steliosot/comfysql/tree/maestro-fixes
metadata:
  {
    "openclaw":
      {
        "emoji": "🎬",
        "os": ["linux"],
        "requires": { "bins": ["python3", "comfysql"] },
        "tags": ["image-generation", "image-edit", "comfyui", "comfysql", "qwen", "maestro"],
        "category": "media-generation",
        "priority": 100
      }
  }
---

# maestro-comfysql

You do **not** write ComfyUI workflow JSON. You do **not** run Python scripts. Your only job is to pick a pre-built workflow template, write a one-line SQL query against it, and run `comfysql`.

## How it actually works

1. Maestro ships ~7 workflow templates at `/home/node/comfysql/input/workflows/` (symlinked to read-only `/opt/comfysql/input/workflows/`) — each is a JSON file that encodes a specific ComfyUI pipeline (image-to-image, character render, camera change, etc).
2. You write a SQL statement like `SELECT image FROM qwen_image_edit WHERE prompt='…' AND input_image='…'`.
3. `comfysql` takes that SQL, finds the matching template, substitutes your WHERE clause values into the right nodes of the template, and POSTs the resulting workflow JSON to the Maestro ComfyUI server.
4. The server runs the workflow on GPU and returns an image.

You never touch the workflow JSON. You just describe the user's intent in SQL WHERE clauses.

## Environment — always do this first

Every invocation runs from `/home/node/comfysql` (writable workdir, pre-wired at container creation) so that `comfysql` auto-discovers the workflow templates and the `comfy-agent.json` server config:

```bash
cd /home/node/comfysql
export PATH="$HOME/.local/bin:$PATH"
```

Outputs should go to a writable dir (the bind-mount is read-only), so pass `--download-output --download-dir /home/node/.openclaw/workspace/generated/` on any query that should return a file.

**Server alias** is always `maestro` (points at the Maestro ComfyUI tunnel).

## The seven workflow templates

| Template | What it does | Use when |
|---|---|---|
| `qwen_image_edit` | General image-to-image edit driven by a text instruction + one or more reference images | Default for editing an image. "Put this dress on her", "have him hold this object", "place her in this location", "add sunglasses". |
| `qwen_character_scene` | Higher-fidelity character render (premium face/skin/lighting). Slower than qwen_image_edit. | Close-ups and hero shots where the face is the subject. Key art. |
| `qwen_next_scene` | Take an existing shot, produce the same moment from a different camera (pull to full body, swing to side angle, reverse shot) | Video storyboarding. Given shot N, produce shot N+1 with a different framing but consistent subject. |
| `expression_editor` | Change *only* the emotion on a face while preserving identity | Smile, frown, raised brows, closed eyes. Operates on a single face image; does not reframe. |
| `color_match` | Color-grade one image to match the palette of a reference | Harmonizing composited elements, shot-to-shot continuity. |
| `fashn_vton` | Virtual try-on — put a garment on a person | "How would this shirt look on the model?" Garment can be flat-lay or on another model. |
| `txt2img_empty_latent` | Pure text-to-image, no reference | Concept art, abstract moodboards, anything where no input image is provided. |

**Pick rule of thumb:**
- User provides an image of a subject + asks for something done to them → `qwen_image_edit`
- Focus is tight on a face → `qwen_character_scene`
- User wants a different camera on an existing shot → `qwen_next_scene`
- User wants to change emotion/expression only → `expression_editor`
- Pure try-on → `fashn_vton`
- Just text, no inputs → `txt2img_empty_latent`

## SQL shape

```sql
SELECT image
FROM <workflow_name>
USING <preset_name>         -- optional; `default_run` is always safe
WHERE <field>=<value>
  AND <field>=<value>;
```

Always `SELECT image`. Always end with a semicolon. One statement per run.

Quotes: single-quote strings. Double up single quotes inside a string (SQL-style escape).

## Commands you will actually run

```bash
# Verify the tunnel is reachable (do once per session)
cd /home/node/comfysql && comfysql doctor maestro

# See what fields a workflow accepts (before you write your WHERE clause)
cd /home/node/comfysql && comfysql sql maestro --sql "DESCRIBE WORKFLOW qwen_image_edit;"

# Dry-run: compile SQL to workflow JSON without submitting. Useful to catch
# missing fields before burning GPU time.
cd /home/node/comfysql && comfysql sql maestro --dry-run --sql "SELECT image FROM qwen_image_edit WHERE prompt='…' AND input_image='person.png';"

# Real run, downloads the output to a writable dir
cd /home/node/comfysql && comfysql sql maestro -y \
  --download-output --download-dir /home/node/.openclaw/workspace/generated/ \
  --sql "SELECT image FROM qwen_image_edit WHERE prompt='…' AND input_image='person.png';"
```

## End-to-end: "put this man in front of the Eiffel Tower"

Given a photo the user uploaded to the workspace:

```bash
# 1. Check what fields qwen_image_edit accepts
cd /home/node/comfysql && comfysql sql maestro --sql "DESCRIBE WORKFLOW qwen_image_edit;"

# 2. Submit + download
cd /home/node/comfysql && comfysql sql maestro -y \
  --download-output --download-dir /home/node/.openclaw/workspace/generated/ \
  --sql "SELECT image
         FROM qwen_image_edit
         WHERE prompt='In front of the Eiffel Tower, Paris, overcast afternoon, 35mm, natural light, realistic skin'
           AND input_image='ilker.png';"

# 3. The resulting file is in /home/node/.openclaw/workspace/generated/
ls /home/node/.openclaw/workspace/generated/
```

If the input image isn't yet on the ComfyUI server, the first run will error with an "invalid value" for the image field listing the allowed filenames. Use `comfysql copy-assets maestro` after putting the file under a writable `input/assets/` dir, or use the ComfyUI `/upload/image` endpoint directly.

## End-to-end: virtual try-on with `fashn_vton`

Given a person photo and a garment photo (either a flat-lay or a model wearing it):

```bash
# Stage both images into ComfyUI
cd /home/node/comfysql && comfysql copy-assets maestro /tmp/person.png
cd /home/node/comfysql && comfysql copy-assets maestro /tmp/garment.jpg

# Run try-on. `category` is one of tops / bottoms / one-pieces.
# `garment_photo_type` is flat-lay (product shot on white) or model (on another person).
cd /home/node/comfysql && comfysql sql maestro -y \
  --timeout 600 \
  --download-output --download-dir /home/node/.openclaw/workspace/generated/ \
  --sql "SELECT image FROM fashn_vton
         WHERE \`1.image\`='person.png'
           AND \`2.image\`='garment.jpg'
           AND category='tops'
           AND garment_photo_type='flat-lay';"
```

Model weights (`fashn-ai/fashn-vton-1.5`, `fashn-ai/DWPose`) download from Hugging Face on the first run — that first invocation will take several extra minutes, subsequent ones are cached.

## When NOT to use this skill

- User wants text only → answer directly, no skill.
- User wants video → not wired up yet. Say so.
- User wants a workflow that isn't in the table above → say which workflows exist, let them pick.
- You can't generate workflow JSON from scratch. If the user asks for something none of the templates cover, explain that.

## Troubleshooting

- `doctor health=fail` → tunnel down. Ask the user / surface to a human.
- `HTTP 403` → Cloudflare bot rules caught the UA. Should be patched in this build; if it comes back, `COMFYSQL_USER_AGENT` env var overrides.
- `validation_failed missing_models` → the ComfyUI server is missing a checkpoint/LoRA that the workflow references. Not fixable from inside the container; say so.
- `validation_failed unknown class_type` → custom node missing on the server. Same — not fixable here.
- Submits then hangs → WebSocket not reachable. `doctor websocket=ok` should confirm.
- Want to see what SQL compiles to before burning GPU time → add `--dry-run`.
