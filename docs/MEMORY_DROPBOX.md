# Memory Dropbox

The memory dropbox is the first end-to-end raw staging path for Maestro memory. You can drag
supported files into a domain inbox, run the processor, inspect a preview JSON file, and then
see the raw file moved to `processed` or `failed`.

## Folder Layout

The processor creates folders under `MEMORY_DROPBOX_ROOT`, which defaults to
`maestro_dropbox`.

```text
maestro_dropbox/
  global/
    inbox/
    processed/
    failed/
    previews/
  personal/
    inbox/
    processed/
    failed/
    previews/
  maestro-development/
    inbox/
    processed/
    failed/
    previews/
  praxis/
    inbox/
    processed/
    failed/
    previews/
  ophi/
    inbox/
    processed/
    failed/
    previews/
  usma/
    inbox/
    processed/
    failed/
    previews/
  personal-irad-projects/
    inbox/
    processed/
    failed/
    previews/
  l3/
    inbox/
    processed/
    failed/
    previews/
```

Domain folder names match the domain keys in Postgres. Use `global/inbox` for material that
is not domain-specific.

## Supported Files

This first pass supports:

- `.txt`
- `.md`
- `.json`

PDF, DOCX, PPTX, and richer extraction will come later. For now, convert those files to text or
Markdown before dropping them into an inbox.

## Processing Flow

1. You drop a file into a domain `inbox`.
2. The processor records a `seed_packages` row and a raw-file `artifacts` row.
3. The LLM-backed curator extracts structured memory candidates.
4. A debug preview is written to the domain `previews` folder.
5. Candidates are routed through `MemoryService`.
6. Canonical memory is written or very-high-impact proposals are queued for approval.
7. The raw file moves to `processed`.
8. If processing fails, the raw file moves to `failed` with an `.error.json` file.

## Current Curator Prompt

For the first seed ingestion pass, the LLM Memory Curator prompt is intentionally hardened
inside the extraction service. It includes:

- Maestro system context
- source-instruction isolation
- scope selection rules
- impact-level rules
- seed-ingestion guidance for old notes, docs, and AI conversations
- lightweight domain context based on the dropbox folder

Issue #19 will replace this temporary embedded prompt context with a reusable prompt hierarchy
and domain prompt registry for all agents.

## Setup

Add your OpenRouter API key to `.env`. Do not commit it.

```bash
LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=...
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
OPENROUTER_HTTP_REFERER=http://localhost:5173
OPENROUTER_APP_TITLE=Maestro
LLM_MODEL=openai/gpt-5.5
MEMORY_DROPBOX_ROOT=maestro_dropbox
```

Install dependencies after pulling this change:

```bash
source .venv/bin/activate
pip install -e ".[dev]"
```

Start Postgres and apply migrations:

```bash
docker compose up -d postgres
alembic upgrade head
```

## Create Folders

The first processor run creates the folder tree automatically:

```bash
python -m app.memory.dropbox
```

If no files are waiting, it prints:

```text
No supported files found in domain inboxes.
```

## Manual End-To-End Test

Create a small file in the Ophi inbox:

```bash
mkdir -p maestro_dropbox/ophi/inbox
cat > maestro_dropbox/ophi/inbox/ophi-memory-test.md <<'EOF'
# Ophi memory test

Ophi should prioritize a lightweight product research loop before adding complex CRM
automation. Chris prefers concise memory previews before writes so he can debug the pipeline.
Do not allow Maestro to make external commitments without explicit approval.
EOF
```

Run the processor once:

```bash
python -m app.memory.dropbox
```

Expected result:

- the command prints one JSON result with `status` set to `processed`
- `maestro_dropbox/ophi/processed/ophi-memory-test.md` exists
- `maestro_dropbox/ophi/previews/ophi-memory-test.preview.json` exists
- low/medium/high candidates are written to `memory_items`
- very-high-impact candidates are queued in `memory_proposals`

Inspect the preview:

```bash
cat maestro_dropbox/ophi/previews/ophi-memory-test.preview.json
```

Inspect memory rows:

```bash
docker compose exec -T postgres psql -U maestro -d maestro -c \
  "select scope, memory_type, title, impact_level
   from memory_items order by created_at desc limit 10;"
```

Inspect pending approvals:

```bash
docker compose exec -T postgres psql -U maestro -d maestro -c \
  "select status, title, impact_level from memory_proposals order by created_at desc limit 10;"
```

## Watch Mode

For a simple polling loop:

```bash
python -m app.memory.dropbox --watch --interval 5
```

This scans domain inboxes every five seconds until interrupted.
