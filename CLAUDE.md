# Career Copilot

A single-user, config-driven job-search copilot: discovers jobs matching the user's resume and preferences, scores fit with an LLM rubric, prepares tailored application packets (the human always clicks submit), tracks every application in SQLite including visa/sponsorship status, monitors email for company replies, and helps prep for interviews.

This is also a learning project (first agentic build) and a portfolio piece: explain agent-engineering choices at decision points, and keep the code clean enough to be read by hiring managers.

## Load-bearing decisions (do not re-litigate; see docs/DECISIONS.md for reasoning)

- **Assisted apply, never auto-submit.** No LinkedIn/Glassdoor/Indeed login automation or scraping, ever.
- **Discovery**: aggregator APIs (Adzuna primary, JSearch optional) as the broad net; when an apply URL points at Greenhouse/Lever/Ashby, pull the full JD from that public ATS API and auto-add the company to a polled watchlist. No hand-curated company list.
- **Email**: user's primary inbox is Hotmail/Outlook.com. Use Microsoft Graph (MSAL device-code flow) behind an `EmailProvider` interface selected in `profile.yaml`; Gmail is the second implementation for genericity. Alerts are email-only for now.
- **Single-user, config-driven**: all personal data lives in `profile.yaml` + resume file in `data/` (gitignored). No auth, no multi-tenant infra. Genericity through configuration.
- **SQLite is the source of truth** from day one; CLI, digest emails, and any future dashboard are read layers over it. Salary, location, and visa signal are first-class decision fields; unknowns are stored as `unknown`, never dropped.
- **Visa**: the user needs H1B transfer. Sponsorship research (JD language + public H1B filing data) is a core scoring input.

## Stack

Python 3.12 + uv, SQLAlchemy/SQLite, Typer CLI, Claude API (claude-sonnet-5) for scoring/tailoring/classification, Microsoft Graph for email, Playwright for local browser prefill (Phase 3), launchd for scheduling (not cron; missed runs must fire on wake).

## Status

- Full phased roadmap: docs/PLAN.md (check off phases as completed)
- Decision log: docs/DECISIONS.md
- External API reference (what's integrated, auth, endpoints, known rate limits): docs/APIS.md - update it whenever an integration is added or a limit is discovered
- Current: Phase 0 code complete (scaffold, config, DB models, resume extraction via structured outputs, `copilot init` / `copilot profile show`). End-to-end verification with the user's real resume and API key is the remaining Phase 0 step. Next: Phase 1 discovery. (The cloud Ultraplan session was abandoned; ignore any artifacts from it.)

## Conventions

- Never use the em dash character; use a plain dash.
- Each phase ends with working, tested, committed code and a README update.
- Verify features end-to-end with real data (real postings, the user's real resume and inbox), not just unit tests.
