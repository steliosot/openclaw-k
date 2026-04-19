---
name: maestro-comfysql
description: "Generate and edit images by writing small SQL queries against pre-built ComfyUI workflow templates. PRIMARY skill for Maestro's image pipeline. Use when the user wants to create, edit, composite, re-light, re-pose, or transform images. Do NOT use for text-only responses or video."
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

**HOW to invoke image workflows.** The list of workflows and which one
to pick for a given scenario lives in the platform instruction — this
doc is the mechanics.

## How it actually works

1. Maestro ships workflow templates at `/home/node/comfysql/input/workflows/`
   (symlinked to read-only `/opt/comfysql/input/workflows/`). Each is a
   JSON file encoding a specific ComfyUI pipeline.
2. You write a SQL statement like `SELECT image FROM <workflow_name> WHERE …`.
3. `comfysql` substitutes your WHERE values into the template and POSTs
   the resulting workflow JSON to the Maestro ComfyUI server.
4. The server runs it on GPU and returns an image.

You never touch the workflow JSON. You write SQL.

## Environment — do this first each session

```bash
cd /home/node/comfysql
export PATH="$HOME/.local/bin:$PATH"
```

Outputs must go to a writable dir (the bind-mount is read-only), so
pass `--download-output --download-dir /home/node/.openclaw/workspace/generated/`
on any query that returns a file. Server alias is always `maestro`.

## SQL shape

```sql
SELECT image
FROM <workflow_name>
WHERE <field>=<value>
  AND <field>=<value>;
```

- Always `SELECT image`. Always end with a semicolon. One statement per run.
- Field names can be plain (`prompt`, `width`) or node-qualified (`4.image`, `5.prompt`).
- **No backticks** around dotted field names — the parser treats them as
  advanced expressions and rejects them ("Advanced WHERE expressions are
  currently supported only for models table").
- Quotes: single-quote strings. Double up single quotes to escape.

## Staging user-uploaded images

`/home/node/comfysql/input` is read-only. Before a workflow can
reference a user upload, stage it into the ComfyUI assets dir:

```bash
# 1. Write the file to a writable location (/tmp or workspace/uploads)
curl -sL --fail -o /tmp/subject.jpg "<url>"

# 2. Stage it into the pipeline
cd /home/node/comfysql && comfysql copy-assets maestro /tmp/subject.jpg
```

After staging, reference the file by bare filename in SQL
(e.g. `4.image='subject.jpg'`).

Maestro also auto-stages any inline attachments the user uploads
through the chat UI — those arrive under
`/home/node/.openclaw/workspace/uploads/<filename>`. Still run
`copy-assets` to move them into the read-only assets dir the
workflows read from.

## Discovering a workflow's fields

```bash
cd /home/node/comfysql && comfysql sql maestro --sql "DESCRIBE WORKFLOW <name>;"
```

Lists the WHERE fields that workflow accepts, plus which ones are
ambiguous (qualify those with a node prefix).

## Dry-run vs real run

```bash
# Dry-run — compile SQL to workflow JSON without submitting
comfysql sql maestro --dry-run --sql "SELECT image FROM <name> WHERE …;"

# Real run — submits, downloads output
comfysql sql maestro -y \
  --timeout 600 \
  --download-output --download-dir /home/node/.openclaw/workspace/generated/ \
  --sql "SELECT image FROM <name> WHERE …;"
```

Use dry-run to catch missing-field errors before burning GPU time.

## Error recovery

If a real run errors or the WebSocket times out, the ComfyUI job
often still completed on the server. Before telling the user
"it failed":

1. Check the download dir — `ls -la /home/node/.openclaw/workspace/generated/`.
   If a fresh PNG is there, you're done.
2. Query ComfyUI's history for the last job:
   ```bash
   curl -sSL 'https://maestro-llm.twentyfiftyfilms.com/history' \
     | python3 -c 'import json,sys; h=json.load(sys.stdin); \
                   last=list(h.items())[-1]; print(last[1]["outputs"])'
   ```
   Grab the filename, then fetch it:
   ```bash
   curl -sSL -o /home/node/.openclaw/workspace/generated/<name>.png \
     'https://maestro-llm.twentyfiftyfilms.com/view?filename=<name>.png&type=output'
   ```
3. Only say "it failed" if the download dir is empty AND /history
   shows no recent success.

Each numbered step is a separate bash tool call — read the output
before deciding the next one.

## Heartbeat

Renders take 30–90s. Emit one prose line to the user before each
bash call so the chat doesn't look frozen. Examples:

  Staging your reference…
  Generating — takes about a minute…
  Still rendering, almost done…
  Done.

Never fake progress. Only emit "done" after the file is actually on
disk.

## Troubleshooting

- `doctor health=fail` → tunnel down.
- `HTTP 403` → Cloudflare bot rules; set `COMFYSQL_USER_AGENT` env var.
- `validation_failed missing_models` → ComfyUI server missing a
  checkpoint. Not fixable from inside the container.
- `validation_failed unknown class_type` → custom node missing on the
  server. Same.
- Submits then hangs → WebSocket not reachable; `doctor websocket=ok`
  should confirm.

## When NOT to use this skill

- Text-only responses → answer directly.
- Video → not wired up yet.
- User asks for a workflow that isn't in the platform instruction's
  catalog → say which workflows exist (read the catalog from the
  instruction), let them pick.
