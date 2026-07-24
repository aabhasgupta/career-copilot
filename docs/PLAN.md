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

### [x] Phase 1 - Discovery - DONE, verified against real postings (2026-07-19)
JSearch promoted from optional to core (alongside Adzuna) - Adzuna alone doesn't reliably cover LinkedIn/Indeed; JSearch closes that gap via its `/search-v2` endpoint (see docs/APIS.md for the confirmed-working endpoint shape, since the commonly-documented `/search` path 404s - it's been renamed).
Adzuna client (`discovery/adzuna.py`) + JSearch client (`discovery/jsearch.py`, `/search-v2`). Both normalize into a common `DiscoveredJob` shape (`discovery/models.py`). Dedupe by company+title+location hash (`discovery/dedupe.py`) so re-running never creates duplicates. Dealbreakers from `profile.yaml` are a hard filter applied at discovery time - matching jobs are dropped before they're stored, never just downranked. Salary and location are never filtered at discovery, only surfaced, per D10's "unknown, never dropped" principle. CLI: `copilot discover`, `copilot jobs list` (`--min-salary`, `--location`, `--limit`).
Direct employer links (added same day, user priority): aggregator apply links always proxy through the aggregator, so `discovery/ats_resolver.py` probes each new company's name against the free public Greenhouse/Lever/Ashby APIs, watchlists matches, and `discovery/ats_boards.py` polls those boards on every discover run - upgrading known jobs in place to the direct apply URL + full JD text and adding board postings that match the search titles. See D3's resolution note for the false-positive guardrails this required. Verified live: 5 boards found among 76 real companies, direct Greenhouse links on Accenture Federal Services / Affirm / Valspec jobs, idempotent across consecutive runs.
**Verify**: real discovery run for the user's target roles (8 GenAI/ML titles) - 109 real postings landed in the DB including NVIDIA, AWS, and other genuinely relevant roles; re-running found 108 postings with only 7 new (101 correctly recognized as duplicates), confirming dedupe works. One transient JSearch timeout was caught and logged without killing the run, confirming per-source error isolation.

### [x] Phase 2 - Fit scoring, sponsorship research, email sending - DONE, verified live (2026-07-24)
LLM rubric scoring (`scoring/rubric.py`) each job against the structured resume, in one batched Claude call per `--batch-size` (default 8) unscored jobs (resume is shared context across the batch, not repeated per job-per-call). Produces five independent 0-100 dimension scores - skill match, experience level, domain fit, location fit, visa feasibility - plus a holistic `fit_score` that is explicitly *not* an average of them (docs/DECISIONS.md D17), written reasoning, and the job's `visa_signal` (explicit_yes/explicit_no/unknown) extracted from the same JD text in the same call. CLI: `copilot score --limit --batch-size`, `copilot jobs show <id>` (full breakdown - dimensions are deliberately not columns in `jobs list`, to avoid clutter), `copilot jobs list --min-fit` (scored jobs now rank first by `fit_score` descending; unscored jobs are always kept, never treated as low-scoring, and fall back to the pre-Phase-2 preference ranking until scored).
Sponsorship module (`scoring/sponsorship.py`, `copilot sponsorship-sync`): matches companies against the public USCIS H-1B Employer Data Hub by normalized name - deterministic, zero LLM cost - to populate `h1b_filing_count`/`sponsorship_evidence`/`sponsorship_status`. Deliberately never blended into `fit_score` or `visa_feasibility_score` (D18): the filing data is a year-plus-old, whole-company aggregate that says nothing about a specific role's current policy, so it's shown as separate context in `copilot jobs show <id>` rather than a fused number implying false precision.
Email sending: `email_agent/provider.py` (interface + factory) and `email_agent/outlook.py` (Microsoft Graph, MSAL device-code auth, token cached in `data/email_token.json`) are built. `notify/digest.py` composes the daily digest ("N new jobs, M scored 70+", with apply links, salary, location) from jobs discovered since the last digest (tracked in `data/digest_state.json`, not a fixed 24h window, so a missed scheduled run doesn't drop jobs). CLI: `copilot email login`, `copilot email test`, `copilot digest --dry-run`.
Posting date: every discovery source (Adzuna, JSearch, Greenhouse/Lever/Ashby, remote boards) already parsed a `posted_at` into the DB, but it wasn't surfaced anywhere - added a shared `formatting.format_posted()` ("Nd ago", falling back to `unknown` per D10) and wired it into `jobs list`, `jobs show`, and the dashboard's Jobs list/detail views, alongside `format_salary()`.
Recency as a real ranking factor (user feedback 2026-07-24: "there is no point applying to a job posted 60 days ago"): `search.max_posting_age_days` (default 30) is a live floor re-evaluated against "now" on every `jobs list`/dashboard visit, not a one-time drop at discovery time - a job's age keeps increasing whether or not `discover` re-runs. Unknown `posted_at` is always kept (same "unknown, never dropped" rule as the salary/fit floors). `ranking.rank_jobs` also gained a coarse `freshness_tier` (postings within `FRESH_WINDOW_DAYS`=14 group ahead of older ones) placed after the staffing downrank and before the location/industry/company blend - a real ranking factor, not just the pre-existing last-resort `posted_at` tiebreak (which now uses the posting's own date instead of our discovery `created_at`). CLI: `copilot jobs list --max-age`; dashboard gained a matching "Max age" filter input.
**Verify**: scored 25 real jobs twice (once pre-, once post-dimension-breakdown) - scores spread widely (14-78), reasoning cited concrete resume/JD specifics, and real visa blockers (PwC's "no H-1B lottery" language, EisnerAmper, Vizient) were caught correctly from JD text alone. Sponsorship sync verified against real data: Amazon (6,126 approved), JPMorgan Chase (1,525), Stripe (64), Capital One (50) all matched correctly; MITRE Corporation correctly stayed `unknown` (genuinely zero filings, not a matching bug). Email verified live 2026-07-24: real Azure app registration (personal-account app, public client flows, `Mail.Send`/`Mail.Read`/`Mail.ReadWrite`/`MailboxSettings.ReadWrite` scopes), `copilot email login` device-code flow completed, `copilot email test` and `copilot digest` both landed real emails in the user's Hotmail inbox - including the "nothing new, skip send" path (state-tracked, confirmed empty digests are never sent) and a real populated digest after a fresh `copilot discover` + `copilot score --limit 20` run (173 new jobs, top fit 78/100).

### [ ] Phase 3 - Application assist + tracking (web dashboard as the human surface - D11)
**Update (2026-07-23, D19)**: the read-only half of the dashboard table landed early, same pattern as the preferences editor (D11's update) - `copilot dashboard`'s **Jobs** tab shows every job sorted best-fit-first (`ranking.rank_jobs`, shared with `copilot jobs list` so the two can't drift) with fit score, salary, location, visa/sponsorship signal, and apply link, plus a click-through detail page for the full 5-dimension breakdown and sponsorship evidence. What's still Phase 3: queueing, mark-applied, the packet, and the state machine below - the table only reads today, it doesn't write.
Per-job packet (markdown), paste-ready not advisory: personal details, correctly-worded work-authorization answers for H1B transfer, tailored resume bullets, drafted answers to the posting's custom questions, direct apply link. State machine on `applications`.
Primary interface is a local web dashboard (localhost, same SQLite DB per D5): table of scored jobs with fit, salary, location, visa signal, and apply link; queue jobs, open the packet, and mark applied with a click instead of per-ID CLI commands. CLI keeps the machine-facing commands (`copilot status` for a quick pipeline view incl. visa breakdown; `copilot dashboard` to serve the UI). Manual marking becomes the fallback once Phase 4's email agent auto-marks applications from ATS confirmation emails.
Second half: local browser prefill for Greenhouse/Lever/Ashby forms - Playwright on the user's machine fills standard fields (contact info, resume upload, links, visa questions), then stops and hands control to the user to review, complete custom questions from the packet, and click submit. Packet-only remains the fallback for non-ATS jobs.
**Verify**: user applies to 2-3 real jobs end to end through the dashboard and confirms the flow beats manual.

### [ ] Phase 4 - Inbox monitoring + schedule management
`copilot inbox sync` (scheduled): pre-filters recent Hotmail messages to job/ATS-like sender and subject patterns (not the whole inbox), then tries to correlate each against `applications` (company domain, subject, ATS sender addresses like no-reply@greenhouse.io) and classify with Claude (rejection / OA / interview invite / recruiter screen). Matched emails: file into a single "Career Copilot" folder tagged with an Outlook category per type (not per-type subfolders - see D20 for why), update application state, send an immediate alert for actionable events plus the daily digest (new high-fit jobs, state changes, response stats).
Emails that are confidently job-related but correlate to nothing tracked (you applied outside Career Copilot entirely) become a pending suggestion instead of being auto-tracked: the email is left untouched until you confirm, at which point a stub `Company`/`Job` (`source="email"`) and `Application` get created and the email is filed like any other (D20). Declining leaves everything as-is. Review-queue surface (dashboard vs. CLI) not yet decided.
Also: `copilot schedule` command group (install / status / pause / resume / uninstall) wrapping launchd plists.
**Verify**: seed with existing application-related emails in the real inbox; correct correlation, foldering, categorization, state updates; a manually-applied job's confirmation email correctly surfaces as a suggestion rather than auto-tracking; digest arrives; schedule commands install and fire.

**Idea, not yet scoped (requested 2026-07-24)**: for classified emails that call for a response (interview scheduling asks, recruiter availability checks - not rejections or plain confirmations, which need no reply), draft a reply with Claude and save it for review, same "assist never impersonate" principle as D1 and the LinkedIn referral drafts - never auto-sent, always shown for the user to edit and send themselves. Would need: (1) which classified types actually warrant a drafted reply (a subset of the Phase 4 categories above), (2) where the draft lives (likely a field on `email_events`, alongside the classification), (3) a review surface - the dashboard's Jobs tab pattern (D19) suggests a similar read/copy view rather than a new CLI flow. User flagged this as low priority; revisit once Phase 4's classification is real and there's a sense of how often replies are actually warranted.

### [ ] Phase 5 - Response-likelihood scoring
Rubric combining fit score, visa feasibility, posting age, competition proxies, application channel (direct ATS vs aggregator), and, once data accumulates, the user's own response history. Shown in `copilot status`, prioritizes the queue.
**Verify**: sanity-check against actual outcomes as replies arrive; score visibly reorders the queue.

### [ ] Phase 6 - Interview prep copilot
On `interviewing`: prep brief from the JD, company research (web search for interview-process reports), the user's gap areas from fit reasoning, likely question areas per round type. CLI: `copilot prep <application-id>`.
**Verify**: brief for a real upcoming interview; user judges usefulness.

### [ ] Later / optional
Push notifications (Telegram/Slack); off-machine scheduling (GitHub Actions or small VM) if laptop-closed latency becomes a problem. (The local web dashboard moved into Phase 3 per D11.)

`copilot apis status` - lists every API in docs/APIS.md with rate limit / used / remaining. Design constraint: never spend quota making a dedicated call just to check status - JSearch (and most rate-limited APIs) report current usage via response headers (`x-ratelimit-requests-limit` etc.) on every real call already being made for actual work, so capture and persist those opportunistically (e.g. a small table or JSON file updated after each real API call) and have the command just read the latest known values.

LinkedIn referral finder - for a shortlisted job, find who could refer the user: match their LinkedIn connections against the job's company, or failing that surface the likely hiring manager / people with a similar title, then draft a personalized referral-request message. Feasibility constraint (D2 stands - no login automation or scraping): LinkedIn's API does not expose a member's connections to third-party apps, but LinkedIn's official data export (Settings -> Get a copy of your data -> Connections) produces a Connections.csv with name/company/position that the user can drop into data/ and re-export whenever their network grows. Matching that CSV against the jobs table is then pure local work; hiring-manager/similar-title discovery can use Claude's web search; message drafting reuses the tailoring machinery from Phase 3. Drafts are saved in the DB attached to the application and shown for copy-paste - never sent automatically, same principle as D1: a referral request is a personal favor, and it stays human-sent. Storing drafts also lets Phase 4/5 correlate which referral messages led to replies. Design confirmed with the user 2026-07-20; implement after Phase 3.

## Build approach

Roughly one phase per session, each ending with working, tested, committed code and a README update. Explain agent-engineering concepts (prompt design, structured output, rubric evaluation, correlation heuristics) as they arise. API keys needed: Adzuna (free, Phase 1), Anthropic (Phase 2), Azure app registration (free, Phase 2), optional RapidAPI/JSearch (Phase 1).
