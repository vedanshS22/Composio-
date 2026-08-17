# Scout100 — evidence-first integration research agent

Scout100 turns the supplied 100-app Composio take-home list into a static,
machine-readable case-study page. It follows the requested reading/decision
order: `README.md` → `TASK.md` → `CONTEXT.md` → `IMPLEMENTATION_PLAN.md`, then
the supporting architecture documents.

## What is implemented

- A fixed 100-app seed (`data/apps_seed.csv`), never mutated by the agent.
- An evidence-first research agent: fetches the app's supplied official-docs
  URL and performs schema-constrained extraction. It returns `unknown` plus a
  blocker if the source/model cannot establish a fact—never a guessed value.
- SQLite append-only findings, so pass 1 remains auditable after pass 2.
- Stratified (two per category) independent verification with field-level
  hit/miss records and automatic re-research for mismatches. The verifier uses
  Playwright's rendered-browser fetch in a fresh context (not pass-1 content).
- A standalone `report/dist/index.html` with embedded JSON for agents, a
  readable 100-row table for humans, pattern summaries, pipeline explanation,
  and mismatch disclosure.
- An MCP server over stdio that exposes the completed dataset as callable
  lookup, filter, patterns, and verification tools.

## Setup

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
Copy-Item .env.example .env
# Set the selected provider's key in .env (OPENROUTER_API_KEY for the default GLM 5.2 configuration), then load it into the shell.
Get-Content .env | ForEach-Object { if ($_ -match '^([^#=]+)=(.*)$') { Set-Item -Path "Env:$($matches[1])" -Value $matches[2] } }
```

The extraction client supports OpenRouter and Groq's OpenAI-compatible JSON-mode
Chat Completions APIs. The default is OpenRouter `z-ai/glm-5.2`; configure
`LLM_PROVIDER`, `LLM_MODEL`, and the matching provider key to change it.
No target-app
credentials or paid target-app accounts are required.

## Run

```powershell
py scripts/seed_db.py
py scripts/run_research.py --apps "Stripe,Slack"  # smoke test first
py scripts/run_research.py                         # full pass 1 (concurrency 5)
py scripts/check_completeness.py                    # must say 100/100
py scripts/run_verification.py --sample-size 20
py scripts/build_report.py
```

Open `report/dist/index.html` directly; it has no runtime server dependency.
Record the required ten manual spot checks in
`data/human_verification_notes.md` before submitting. Do not make an accuracy
claim until verification rows exist.

## Query as MCP

```powershell
py agents/mcp_server.py
```

The server exposes `get_app_record`, `list_findings`, `get_pattern_summary`,
and `get_verification_report`. Point an MCP-compatible client at the command
after the database has findings.

## Test

```powershell
pytest
```

## Generic non-persisting quality traces

Use the same evidence-first research and field-quality recovery contract for
any valid seed selection without writing findings or rebuilding the report:

```powershell
py scripts/run_quality_research.py --apps "Salesforce,HubSpot,Slack,Twilio"
py scripts/run_quality_research.py --sample verification20
py scripts/run_quality_research.py --all
```

Each invocation appends per-app JSON traces under `data/logs/quality_traces/`.

Tests contain no network calls. The actual research run does: every record is
therefore tied to returned evidence and should be inspected before submission.
