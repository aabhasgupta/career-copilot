# Career Copilot - Phased Build Plan

Living roadmap. Check off phases as they complete. Reasoning behind decisions lives in DECISIONS.md; short summary of the system in /CLAUDE.md.

## What this is

A job-search copilot agent for a single user (configured via `profile.yaml`, so anyone can clone and use it):

1. Discovers jobs matching the user's resume and preferences across many companies, with no hand-curated company list
2. Scores each job for fit with an explainable LLM rubric, including visa/sponsorship feasibility (user needs H1B transfer)
3. Prepares tailored, paste-ready application packets; pre-fills ATS forms locally via Playwright; the human always reviews and clicks submit
4. Tracks every application in SQLite (states: found, queued, applied, replied, interviewing, offer, rejected, ghosted) with salary, location, and visa status as first-class fields
5. Monitors the user's Hotmail inbox via Microsoft Graph, correlates replies to applications, files them into a Career Copilot folder with categories, and alerts by email
6. Scores response likelihood to prioritize the queue
7. Generates interview prep briefs once an application reaches interviewing

## Stack

- Python 3.12 + uv (pyproject-managed)
- SQLite via SQLAlchemy - single-file DB, source of truth for everything; CLI/emails/dashboard are read layers
- Claude API (claude-sonnet-5) for resume understanding, fit scoring, JD analysis, tailoring, email classification
- Typer CLI (`copilot ...`) as the v1 interface; local web dashboard is a later optional phase
- Microsoft Graph API (MSAL device-code flow, free Azure app registration on a personal Microsoft account) behind an `EmailProvider` interface chosen in `profile.yaml`; Gmail as second implementation for genericity
- Playwright for local browser prefill of ATS application forms (Phase 3)
- launchd for scheduling (not cron: launchd fires missed runs on wake from sleep; laptop does not need to stay on)

## Repo layout

```
career-copilot/
  pyproject.toml
  profile.yaml.example        # roles, locations, remote pref, visa status, salary floor, dealbreakers, email provider
  data/                       # gitignored: resume.pdf, copilot.db, email token
  src/copilot/
    config.py                 # load/validate profile.yaml
    db/                       # SQLAlchemy models + migrations
    discovery/                # adzuna.py, jsearch.py, ats/{greenhouse,lever,ashby}.py, dedupe.py
    scoring/                  # rubric.py (LLM fit score), sponsorship.py (H1B data)
    apply_assist/             # tailoring.py, packet.py, prefill.py (Playwright)
    email_agent/              # provider.py (interface), outlook.py, gmail.py, classifier.py, correlator.py
    notify/                   # digest.py, alerts.py
    schedule/                 # launchd plist management
    cli.py
  tests/
  docs/
```

## DB schema (core tables)

- `companies`: name, ats_type, ats_slug, watchlist flag, sponsorship_status (transfers_h1b / sponsors / no_sponsor / unknown), h1b_filing_count, sponsorship_evidence
- `jobs`: company_id, title, location, remote, employment_type, seniority_level, salary_min, salary_max, salary_currency, salary_source (posted / aggregator_estimate / unknown), source, jd_text, apply_url, posted_at, dedupe_hash, fit_score, fit_reasoning, visa_signal (explicit_yes / explicit_no / unknown), response_likelihood_score
  - Location and salary are decision-making fields: surfaced in `copilot jobs list`, the digest email, and the apply packet; usable as filters (`--min-salary`, `--location`). Salary comes from the posting when stated, aggregator estimates otherwise (Adzuna provides these), and is `unknown` rather than dropped when absent.
- `applications`: job_id, state, applied_at, tailored_materials_path, notes
- `email_events`: application_id, provider_thread_id, classified_type (rejection / interview_invite / OA / recruiter_screen / other), received_at

## Phases

### [x] Phase 0 - Foundation - DONE, verified against the user's real resume and API key (2026-07-19)
Project scaffolding, `profile.yaml` schema + loader, DB models, resume ingestion: parse PDF, Claude extracts a structured profile (skills, experience, seniority, contact info) cached as JSON for downstream prompts. `copilot profile fill` auto-populates the `identity` block of `profile.yaml` from the extracted resume (comment-preserving merge via ruamel.yaml) - applied automatically since it only overwrites with facts the resume actually states. `copilot profile suggest-titles` proposes `search.titles` by combining the resume with a live Claude web-search of current market demand; this is a judgment call, not a fact, so it always shows suggestions and asks before writing (`--apply`/`--no-apply` to skip the prompt). `visa`/`email_integration` stay fully manual. CLI: `copilot init`, `copilot profile fill`, `copilot profile suggest-titles`, `copilot profile show`.
**Verify**: `copilot profile show` prints an accurate structured summary of the real resume. Done - extracted profile matched the user's real background exactly (seniority, GenAI/ML skill set, full work history).

### [ ] Phase 1 - Discovery
UPDATE: JSearch promoted from optional to core (alongside Adzuna) - Adzuna alone doesn't reliably cover LinkedIn/Indeed; JSearch closes that gap via its `/search-v2` endpoint (see docs/APIS.md for the confirmed-working endpoint shape, since the commonly-documented `/search` path 404s - it's been renamed). ATS pollers (Greenhouse/Lever/Ashby) are deferred, not dropped: ship Adzuna + JSearch discovery first, evaluate real JD text quality from the aggregators, and only add ATS enrichment if that quality genuinely needs supplementing for Phase 2 fit-scoring.
Adzuna client (free key) + JSearch client (`/search-v2`, RapidAPI). Normalize postings, dedupe (company+title+location hash). Auto-watchlist discovered companies for future ATS polling if that layer gets added later. CLI: `copilot discover`, `copilot jobs list`.
**Verify**: real discovery run for the user's target roles; real postings land in the DB; re-running creates no duplicates.

### [ ] Phase 2 - Fit scoring, sponsorship research, email sending
LLM rubric scoring each job against the structured resume: skill match, experience level, domain, location/remote, visa feasibility. Produces a 0-100 score plus written reasoning stored on the job. Sponsorship module: detect explicit visa language in JDs; per company, look up public H1B data (USCIS H-1B Employer Data Hub / DOL LCA disclosure CSVs) for filing counts as transfer-likelihood evidence.
Email sending goes live here (not Phase 4): one-time Graph auth setup and `sendMail`, so the daily digest ("N new jobs, M scored 80+", with apply links, salary, location) and alerts flow as soon as scoring works. CLI: `copilot score`, `copilot jobs list --min-fit 70`.
**Verify**: score 20+ real jobs; user spot-checks rankings and reasoning; a known big H1B sponsor and a known non-sponsor classify correctly; digest email arrives in the Hotmail inbox.

### [ ] Phase 3 - Application assist + tracking
Per-job packet (markdown), paste-ready not advisory: personal details, correctly-worded work-authorization answers for H1B transfer, tailored resume bullets, drafted answers to the posting's custom questions, direct apply link. State machine on `applications`. CLI: `copilot queue`, `copilot apply next` (opens packet + URL), `copilot mark applied <id>`, `copilot status` (pipeline incl. visa breakdown).
Second half: local browser prefill for Greenhouse/Lever/Ashby forms - Playwright on the user's machine fills standard fields (contact info, resume upload, links, visa questions), then stops and hands control to the user to review, complete custom questions from the packet, and click submit. Packet-only remains the fallback for non-ATS jobs.
**Verify**: user applies to 2-3 real jobs end to end and confirms the flow beats manual.

### [ ] Phase 4 - Inbox monitoring + schedule management
`copilot inbox sync` (scheduled): search recent Hotmail messages via Graph, correlate to applications (company domain, subject, ATS sender addresses like no-reply@greenhouse.io), classify with Claude (rejection / OA / interview invite / recruiter screen), file into a "Career Copilot" folder with categories, update application state, send immediate alert email for actionable events plus the daily digest (new high-fit jobs, state changes, response stats).
Also: `copilot schedule` command group (install / status / pause / resume / uninstall) wrapping launchd plists.
**Verify**: seed with existing application-related emails in the real inbox; correct correlation, foldering, categorization, state updates; digest arrives; schedule commands install and fire.

### [ ] Phase 5 - Response-likelihood scoring
Rubric combining fit score, visa feasibility, posting age, competition proxies, application channel (direct ATS vs aggregator), and, once data accumulates, the user's own response history. Shown in `copilot status`, prioritizes the queue.
**Verify**: sanity-check against actual outcomes as replies arrive; score visibly reorders the queue.

### [ ] Phase 6 - Interview prep copilot
On `interviewing`: prep brief from the JD, company research (web search for interview-process reports), the user's gap areas from fit reasoning, likely question areas per round type. CLI: `copilot prep <application-id>`.
**Verify**: brief for a real upcoming interview; user judges usefulness.

### [ ] Later / optional
Local web dashboard (`copilot dashboard`, localhost) reading the same DB; push notifications (Telegram/Slack); off-machine scheduling (GitHub Actions or small VM) if laptop-closed latency becomes a problem.

`copilot apis status` - lists every API in docs/APIS.md with rate limit / used / remaining. Design constraint: never spend quota making a dedicated call just to check status - JSearch (and most rate-limited APIs) report current usage via response headers (`x-ratelimit-requests-limit` etc.) on every real call already being made for actual work, so capture and persist those opportunistically (e.g. a small table or JSON file updated after each real API call) and have the command just read the latest known values.

## Build approach

Roughly one phase per session, each ending with working, tested, committed code and a README update. Explain agent-engineering concepts (prompt design, structured output, rubric evaluation, correlation heuristics) as they arise. API keys needed: Adzuna (free, Phase 1), Anthropic (Phase 2), Azure app registration (free, Phase 2), optional RapidAPI/JSearch (Phase 1).
