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

This pass supports:

- `.txt`
- `.md`
- `.json`
- `.pdf`
- `.docx`
- `.html`
- `.csv`
- `.tsv`

PDF and DOCX files are converted to extracted text before they are sent to the memory curator.
Scanned image-only PDFs will fail until OCR support is added. PPTX and richer extraction will
come later; for now, export slide decks to PDF before dropping them into an inbox.

## Processing Flow

1. You drop a file into a domain `inbox`.
2. The processor records a `seed_packages` row and a raw-file `artifacts` row.
3. The LLM-backed curator extracts structured memory candidates.
4. A debug preview is written to the domain `previews` folder.
5. Candidates are routed through `MemoryService`.
6. Canonical memory is written or very-high-impact proposals are queued for approval.
7. The raw file moves to `processed`.
8. If processing fails, the raw file moves to `failed` with an `.error.json` file.

## Candidate Evaluation

The current pipeline routes all candidates through the memory manager before anything becomes
canonical memory. The manager:

- skips exact normalized duplicates deterministically
- can use semantic LLM evaluation against nearby existing memories
- can write new memory, reinforce existing memory, supersede old memory, flag conflicts for
  approval, or reject low-value candidates
- preserves provenance and evaluator rationale in preview results and memory metadata

Low-impact candidates write directly when accepted. Medium/high candidates become approved audit
proposals and canonical memory. Very-high-impact candidates, conflicts, and authority-changing
updates wait for user approval.

During seed ingestion, use the debug preview and approval queue as the calibration loop: add
representative files, inspect candidate outcomes, then increase volume.

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

You can run the same flow from the web app once the API and frontend are running:

```bash
uvicorn app.api.main:app --host 0.0.0.0 --port 8000
```

In another terminal:

```bash
cd frontend
npm run dev -- --host 0.0.0.0
```

Open `http://localhost:5173`, select **Memory** in the sidebar, choose a domain, pick a
supported file, and select **Upload**. The file lands in that domain's `inbox`. Select
**Process inbox** to invoke the LLM curator and memory manager. After processing, the Memory
tab shows:

- folder counts for the selected domain
- the latest debug preview from `previews`
- recent canonical memory writes
- very-high-impact memories waiting for approval or rejection

When a preview status is `written`, the processor has already sent the extracted candidates to
the memory manager. Candidates labeled as written have canonical `memory_items` rows. Candidates
that need approval have `memory_proposals` rows and must be approved from the Memory tab before
they become canonical memory.

The Memory tab also includes **Recent ingests** under Source Review. Use it when a file was
staged into the wrong domain or a seed package needs source-level cleanup. Reclassifying a source
updates the generated memories, generated proposals, seed package metadata, and keeps a
reclassification history instead of deleting rows.

Approving a queued proposal writes it to canonical memory. Rejecting it leaves the proposal
record with a rejection reason for debugging.

If the Memory tab shows an API connection error, confirm the app on `localhost:5173` is the
Maestro frontend and that `localhost:8000` is serving this backend:

```bash
curl http://localhost:8000/memory/dropbox/status
```

That command should return the domain list. A `404` usually means an older backend is still
occupying port `8000`.

The command-line path is still useful when you want to test the processor directly.

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
