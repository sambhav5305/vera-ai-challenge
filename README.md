# Vera Merchant AI — magicpin AI Challenge

A submission-ready, stateful HTTP bot for the magicpin Vera AI Challenge.

## What it does

- Implements the required `/v1/context`, `/v1/tick`, `/v1/reply`, `/v1/healthz`, and `/v1/metadata` endpoints.
- Stores category, merchant, customer, and trigger contexts with version handling.
- Prioritizes active triggers and suppresses duplicate sends.
- Produces grounded merchant messages using concrete facts from the supplied context.
- Supports customer-facing recall/lapse messages when `CustomerContext` is available.
- Detects common WhatsApp Business canned auto-replies and backs off instead of burning turns.
- Detects explicit action intent and switches immediately from qualification to execution.
- Handles negative replies and off-topic requests gracefully.
- Avoids inventing prices, performance figures, dates, or customer details that were not supplied.
- Includes `/v1/teardown` to wipe in-memory state at the end of a test.

## Architecture

```text
Judge
  |
  +--> POST /v1/context --> Versioned Context Store
  |
  +--> POST /v1/tick -----> Trigger Prioritizer --> Grounded Composer --> Action
  |
  +--> POST /v1/reply ----> Intent Router --> Conversation Manager --> send/wait/end
  |
  +--> GET /v1/healthz
  +--> GET /v1/metadata
```

The implementation is deliberately dependency-light and does not require an LLM API key. This keeps every request comfortably inside the challenge's 30-second timeout. An LLM can be added later as an optional composer, but the deterministic composer is the default so the bot never hallucinates merchant data.

## Run locally

```bash
python -m venv .venv
# Windows
.venv\\Scripts\\activate
# macOS/Linux
# source .venv/bin/activate

pip install -r requirements.txt
python app.py
```

Server: `http://localhost:8080`

Health check:

```bash
curl http://localhost:8080/v1/healthz
```

## Deployment

The challenge requires a public URL. Deploy this folder to any service that exposes an HTTP/HTTPS URL (for example Render, Railway, Fly.io, Replit, or a VM).

Start command:

```bash
uvicorn app:app --host 0.0.0.0 --port $PORT
```

For a local tunnel, the challenge brief also allows ngrok. The submitted URL must expose `/v1/*` directly.

## Expected endpoint contract

### `POST /v1/context`

Stores a context payload. Higher versions replace lower versions atomically; reposting the same version is idempotent.

### `POST /v1/tick`

Receives simulated time and active trigger IDs and returns zero or more proactive actions.

### `POST /v1/reply`

Receives the simulated merchant/customer response and returns one of:

- `send`
- `wait`
- `end`

### `GET /v1/healthz`

Returns liveness plus context counts.

### `GET /v1/metadata`

Returns submission metadata.

### `POST /v1/teardown`

Clears all in-memory state.

## Why this design matches the judging rubric

The challenge scores messages on specificity, category fit, merchant fit, decision quality/trigger relevance, and engagement compulsion. This bot therefore avoids generic blasts and builds messages from the actual context pushed by the judge.

Conversation handling specifically targets the replay cases described in the challenge:

1. Repeated canned auto-replies -> back off/end rather than loop.
2. Explicit intent such as “ok let's do it” -> switch to action immediately.
3. Hostile/off-topic requests -> remain polite and on Vera's merchant-growth mission.

## Important submission note

The bot stores challenge payloads only in process memory. Do not add external persistence containing merchant/customer payloads unless it is explicitly allowed by the challenge rules.

Set these optional environment variables before deployment:

```text
TEAM_NAME=Vera Builders
TEAM_MEMBERS=Sambhav Saxena
CONTACT_EMAIL=your-email@example.com
```
