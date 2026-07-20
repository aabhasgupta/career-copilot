# External APIs

Every third-party service this project talks to, what it's for, how it's authenticated, and what's known about its limits. Update this file whenever an integration is added or a limit is discovered - this is the source `copilot apis status` (planned, see docs/PLAN.md) will eventually read from.

## In use

### Anthropic API
- **Used for:** resume extraction (structured outputs), title suggestions (web search tool), and every later agentic step - fit scoring (Phase 2), tailoring (Phase 3), email classification (Phase 4)
- **Auth:** `ANTHROPIC_API_KEY` in `.env`
- **Model:** `claude-sonnet-5` (set in `profile.yaml` under `llm.model`)
- **Billing:** pay-as-you-go, no fixed monthly quota - cost scales with tokens used
- **Docs:** console.anthropic.com

### JSearch (RapidAPI)
- **Used for:** job discovery - aggregates postings including LinkedIn, Indeed, Glassdoor, ZipRecruiter (via Google for Jobs' index), plus direct company career pages
- **Auth:** `JSEARCH_API_KEY` in `.env`, sent as `X-RapidAPI-Key` header alongside `X-RapidAPI-Host: jsearch.p.rapidapi.com`
- **Host:** `jsearch.p.rapidapi.com`
- **Endpoints in use:**
  - `GET /search-v2` - bulk keyword+location search, used for discovery. Params: `query`, `num_pages`, `country`, `date_posted`. Returns `data.jobs[]` + a `cursor` for pagination.
  - `GET /job-details` - single-job enrichment (`job_id`, `country`). Returns much richer fields than search results: `visa_sponsorship`, `seniority_level`, `work_arrangement`, structured salary, `required_technologies`/`preferred_technologies`.
- **Known limitation:** the enrichment fields above are `null` on `/search-v2` results - they only populate via a separate `/job-details` call per job. Given the tight quota (below), Phase 1 discovery uses `/search-v2` only and does not auto-enrich every discovered job via `/job-details`.
- **Rate limit (confirmed live, 2026-07-19):** 200 requests/month on the free Basic plan. Response headers `x-ratelimit-requests-limit` / `x-ratelimit-requests-remaining` / `x-ratelimit-requests-reset` report current usage on every real call - the plan is to capture these opportunistically from calls already being made for real work, not spend quota on dedicated status checks.
- **Endpoint naming caveat:** the search endpoint was `/search` in older documentation/tutorials and returns 404 there now - the current path is `/search-v2`. Verified via the RapidAPI dashboard's live "Test Endpoint" feature, not docs, since this had drifted.

### Adzuna
- **Used for:** broad job discovery aggregator, free tier, no card required
- **Auth:** `ADZUNA_APP_ID` + `ADZUNA_APP_KEY` in `.env` (pending - not yet obtained)
- **Status:** planned for Phase 1, not yet integrated

## Planned, not yet integrated

- **Microsoft Graph API** - Outlook/Hotmail inbox monitoring and sending (Phase 2 for sending, Phase 4 for monitoring). Auth via MSAL device-code flow, not a static key.
- **Greenhouse / Lever / Ashby job-board APIs** - public, unauthenticated, per-company. Deferred per docs/DECISIONS.md - only added if Adzuna/JSearch JD quality turns out to need supplementing.
- **USCIS H-1B Employer Data Hub / DOL LCA disclosure data** - downloadable datasets, not a live rate-limited API, for the sponsorship-research feature (Phase 2).

## Planned tooling

`copilot apis status` (not yet built - see docs/PLAN.md "Later / optional") will list every API above with its rate limit, used, and remaining, sourced from whatever the most recent real call's response headers reported - never a dedicated call made just to check status.
