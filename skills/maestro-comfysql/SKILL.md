---
name: maestro-comfysql
description: "Generate and edit images by writing small SQL queries against pre-built ComfyUI workflow templates. PRIMARY skill for Maestro's image pipeline. Use when the user wants to create, edit, composite, re-light, re-pose, or transform images — including putting a person in a new location, changing outfit, camera-angle changes for storyboard shots, expression adjustments, virtual try-on, color-matching between images. Do NOT use for text-only responses, video rendering (not wired up), or tasks that don't involve producing an image."
homepage: https://github.com/steliosot/comfysql
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

1. Maestro ships a handful of workflow templates at `/home/node/comfysql/input/workflows/` (symlinked to read-only `/opt/comfysql/input/workflows/`) — each is a JSON file that encodes a specific ComfyUI pipeline (image-to-image with N references, character render, camera change, virtual try-on, etc).
2. You write a SQL statement like `SELECT image FROM qwen_1ref WHERE 5.prompt='…' AND 4.image='subject.png'`.
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

## The workflow templates

The qwen image-edit family is split by **how many reference images you have**. Count the user's inputs, pick the workflow with that number, done. No semantic routing — no "outfit vs product vs location" guesswork. The prompt text is where semantics live.

| Template | What it does | Use when |
|---|---|---|
| `qwen_1ref` | Qwen image edit with **one** reference image. | User gave you a single reference. "Put this person in Paris", "add sunglasses to her", "re-light this shot". |
| `qwen_2ref` | Qwen image edit with **two** reference images. | User gave you two references. The prompt describes their roles ("wearing the outfit from image2", "in the location from image2"). |
| `qwen_3ref` | Qwen image edit with **three** reference images. | User gave you three references. Prompt describes each in words. |
| `qwen_character_scene` | Higher-fidelity character render (premium face/skin/lighting). Slower than the qwen_Nref family. | Close-ups and hero shots where the face is the subject. Key art. |
| `qwen_next_scene` | Take an existing shot, produce the same moment from a different camera (pull to full body, swing to side angle, reverse shot) | Video storyboarding. Given shot N, produce shot N+1 with a different framing but consistent subject. |
| `expression_editor` | Change *only* the emotion on a face while preserving identity | Smile, frown, raised brows, closed eyes. Operates on a single face image; does not reframe. |
| `color_match` | Color-grade one image to match the palette of a reference | Harmonizing composited elements, shot-to-shot continuity. |
| `txt2img_empty_latent` | Pure text-to-image, no reference | Concept art, abstract moodboards, anything where no input image is provided. |

**Pick rule of thumb:**
- User gave **1** image + wants an edit → `qwen_1ref`
- User gave **2** images (any pairing: subject+outfit, subject+location, two characters, …) → `qwen_2ref`
- User gave **3** images → `qwen_3ref`
- Tight on a face, premium quality → `qwen_character_scene`
- Different camera on an existing shot → `qwen_next_scene`
- Change only the facial expression → `expression_editor`
- No inputs, just text → `txt2img_empty_latent`

For virtual try-on (put a garment on a person), prefer `qwen_2ref` with a clear prompt describing image1 as the model and image2 as the garment — the dedicated try-on workflow isn't reliable right now and is deliberately not exposed as a skill target.

The qwen_Nref workflows share the same model stack (Qwen-Image-Edit GGUF + Lightning-4step + Boreal LoRAs). They differ only in how many `LoadImage` nodes they expose and how many `imageN` inputs the prompt encoder takes. This keeps each workflow's graph minimal — no wasted LoadImage nodes for unused references.

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
cd /home/node/comfysql && comfysql sql maestro --sql "DESCRIBE WORKFLOW qwen_1ref;"

# Dry-run: compile SQL to workflow JSON without submitting. Useful to catch
# missing fields before burning GPU time.
cd /home/node/comfysql && comfysql sql maestro --dry-run --sql "SELECT image FROM qwen_1ref WHERE 5.prompt='…' AND 4.image='person.png';"

# Real run, downloads the output to a writable dir
cd /home/node/comfysql && comfysql sql maestro -y \
  --download-output --download-dir /home/node/.openclaw/workspace/generated/ \
  --sql "SELECT image FROM qwen_1ref WHERE 5.prompt='…' AND 4.image='person.png';"
```

## The qwen_Nref family — slot layout

All three share the same field names; you just use more slots as reference count goes up.

| Workflow | `4.image` | `30.image` | `31.image` | Prompt field |
|---|---|---|---|---|
| `qwen_1ref` | subject (image1) | — | — | `5.prompt` |
| `qwen_2ref` | image1 | image2 | — | `5.prompt` |
| `qwen_3ref` | image1 | image2 | image3 | `5.prompt` |

Refer to the images in your prompt by their role in **the scene you're describing**, not by slot name. "Keep the woman from image1" / "wearing the outfit from image2" / "holding the bag from image3" etc. — the encoder wires `imageN` to the corresponding slot automatically.

### End-to-end: single-reference edit (`qwen_1ref`)

```bash
# Stage the user's image
cd /home/node/comfysql && comfysql copy-assets maestro /tmp/subject.jpg

cd /home/node/comfysql && comfysql sql maestro -y \
  --timeout 600 \
  --download-output --download-dir /home/node/.openclaw/workspace/generated/ \
  --sql "SELECT image FROM qwen_1ref
         WHERE 4.image='subject.jpg'
           AND 5.prompt='In front of the Eiffel Tower, Paris, overcast afternoon, 35mm, natural light, realistic skin. Keep the person from image1.';"
```

### End-to-end: two-reference edit (`qwen_2ref`)

```bash
cd /home/node/comfysql && comfysql copy-assets maestro /tmp/person.png
cd /home/node/comfysql && comfysql copy-assets maestro /tmp/outfit.jpg

cd /home/node/comfysql && comfysql sql maestro -y \
  --timeout 600 \
  --download-output --download-dir /home/node/.openclaw/workspace/generated/ \
  --sql "SELECT image FROM qwen_2ref
         WHERE 4.image='person.png'
           AND 30.image='outfit.jpg'
           AND 5.prompt='Keep the character from image1. She is wearing the outfit from image2. Walking a wide Paris boulevard at golden hour, cinematic.';"
```

### End-to-end: three-reference composite (`qwen_3ref`)

```bash
cd /home/node/comfysql && comfysql copy-assets maestro /tmp/person.png
cd /home/node/comfysql && comfysql copy-assets maestro /tmp/outfit.jpg
cd /home/node/comfysql && comfysql copy-assets maestro /tmp/bag.jpg

cd /home/node/comfysql && comfysql sql maestro -y \
  --timeout 600 \
  --download-output --download-dir /home/node/.openclaw/workspace/generated/ \
  --sql "SELECT image FROM qwen_3ref
         WHERE 4.image='person.png'
           AND 30.image='outfit.jpg'
           AND 31.image='bag.jpg'
           AND 5.prompt='Keep the character from image1. She is wearing the outfit from image2 and carrying the bag from image3. Walking a wide Paris boulevard at golden hour, cinematic.';"
```

If any image isn't on the ComfyUI server, the run errors with an "invalid value" for that field listing the allowed filenames. Use `comfysql copy-assets maestro` to stage it.

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
