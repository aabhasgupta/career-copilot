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
- **Quota math for scheduling (Phase 4):** one discover run costs one JSearch call per title x location combination (currently 7 x 2 = 14), so 200/month supports ~14 full runs. Options when scheduling lands: `--since today` on scheduled runs, rotating titles across days, or letting JSearch skip runs when exhausted (the other sources still run; per-source error isolation already handles this).
- **Depth limitation:** each query returns only the top ~10 results for its search terms, so any single run is a sample, not a census - a specific posting visible in Google's Jobs panel may rank below the cutoff. Fresh postings enter the top results when new, so regular runs accumulate coverage over time. `copilot discover --since month|all` widens the posting-age window for one-time backfills (default `week`).
- **Quota exhaustion behavior:** a 402/429 from any source marks it exhausted for the rest of the run (no further calls burned on it), and `copilot discover` reports it as a friendly notice - "JSearch has reached its usage limit" - rather than an error, since the other sources still ran and the source resumes automatically when its window resets.
- **Endpoint naming caveat:** the search endpoint was `/search` in older documentation/tutorials and returns 404 there now - the current path is `/search-v2`. Verified via the RapidAPI dashboard's live "Test Endpoint" feature, not docs, since this had drifted.

### Adzuna
- **Used for:** broad job discovery aggregator, free tier, no card required
- **Auth:** `ADZUNA_APP_ID` + `ADZUNA_APP_KEY` in `.env`
- **Endpoint in use:** `GET /v1/api/jobs/{country}/search/{page}` (`country="us"`). Params: `app_id`, `app_key`, `what` (title), `where` (location - omitted for countrywide searches like "United States" so the search isn't scoped to a literal place called that), `results_per_page`, `content-type=application/json`.
- **Response shape:** `{count, results: [...]}`. Each result's `salary_is_predicted` ("1"/"0") tells you whether `salary_min`/`salary_max` are Adzuna's own estimate or the posting's stated figure - mapped to `salary_source` (`aggregator_estimate` vs `posted`).
- **Known limitation (confirmed live, 2026-07-19):** `redirect_url` (the apply link) always proxies through `adzuna.com/land/ad/...` or `adzuna.com/details/...` - never the employer's own posting or ATS URL. This means Adzuna results give zero signal for ATS auto-detection (see "ATS auto-watchlist" note below). `description` is also a truncated snippet, not the full JD.
- **Status:** integrated, Phase 1 discovery.

### Greenhouse / Lever / Ashby job-board APIs
- **Used for:** resolving discovered companies to their public ATS job board (`discovery/ats_resolver.py`) and polling those boards for direct employer apply links + full JD text (`discovery/ats_boards.py`). This replaced the original apply-URL-sniffing plan, which had no signal - aggregator apply links always proxy through the aggregator (see docs/DECISIONS.md D3).
- **Auth:** none - all three are public and unauthenticated. No known rate limits; usage is modest (a few probe requests per newly discovered company, one poll per watchlisted board per discover run).
- **Endpoints in use** (field names verified live 2026-07-19):
  - Greenhouse: `GET boards-api.greenhouse.io/v1/boards/{slug}` (board metadata - echoes the company's display name, used to verify slug guesses) and `GET .../boards/{slug}/jobs?content=true` (postings with `absolute_url`, `location.name`, `first_published`, HTML-escaped `content`).
  - Lever: `GET api.lever.co/v0/postings/{slug}?mode=json` (postings with `text` = title, `hostedUrl`, `categories.location`, `createdAt` in epoch ms, `descriptionPlain`).
  - Ashby: `GET api.ashbyhq.com/posting-api/job-board/{slug}` (postings with `title`, `jobUrl`, `location`, `isRemote`, `isListed`, `publishedAt`, `descriptionPlain`).
- **Slug-collision caveat:** only Greenhouse returns the company name, so only Greenhouse hits can be verified. Loose slug guesses (first word of a multi-word company name) are therefore Greenhouse-only; Lever/Ashby accept full-name slugs only. This is load-bearing: loose Lever probing matched Capital One to an unrelated board named "capital" in live testing.

### Remotive
- **Used for:** remote-first tech job board, supplements the aggregators for the user's top preference (remote)
- **Auth:** none, no key
- **Endpoint:** `GET remotive.com/api/remote-jobs?search=<title>&limit=20` - one call per search title per discover run, spaced 0.6s apart per their light-usage request
- **Caveat (found live 2026-07-20):** `search` matches job descriptions too, not just titles - a GenAI query returned "Freelance Writer". Results are therefore re-filtered locally by title match before storage.
- **Fields:** `title`, `company_name`, `candidate_required_location` ("USA", "Worldwide", "Europe" - non-US-eligible postings are skipped), free-text `salary` ("$120k-$150k", parsed best-effort), `publication_date`, HTML `description`, `url`

### RemoteOK
- **Used for:** same as Remotive - remote tech board, free
- **Auth:** none; requires a descriptive `User-Agent` header (sent), and their terms ask for a link back when listings are republished (we only store locally)
- **Endpoint:** `GET remoteok.com/api` - a single call returns the whole active board (~100 jobs; first array element is a legal notice, skipped). Filtered locally against the profile's titles and US eligibility.
- **Fields:** `position`, `company`, `location`, numeric `salary_min`/`salary_max` (0 = unknown), `epoch`, HTML `description`, `apply_url`/`url`

### Nominatim (OpenStreetMap geocoding)
- **Used for:** the `within <N> miles of <place>` location preference (docs/DECISIONS.md D12) - resolving preference anchors and job locations without coordinates to lat/long for distance sorting. Adzuna already ships coordinates; this covers JSearch and ATS-board jobs.
- **Auth:** none. Courtesy requirements: a descriptive `User-Agent` header (sent) and roughly 1 request/second (throttled in `geocode.py`).
- **Endpoint:** `GET nominatim.openstreetmap.org/search?q=<place>&format=json&limit=1&countrycodes=us`
- **Caching (load-bearing):** every result - including failed lookups - is cached forever in `data/geocode_cache.json`, so each distinct place name costs exactly one request ever. The backfill runs inside `copilot discover`; a first run over ~100 jobs took about a minute, subsequent runs are near-instant.

## Planned, not yet integrated

- **Microsoft Graph API** - Outlook/Hotmail inbox monitoring and sending (Phase 2 for sending, Phase 4 for monitoring). Auth via MSAL device-code flow, not a static key.
- **USCIS H-1B Employer Data Hub / DOL LCA disclosure data** - downloadable datasets, not a live rate-limited API, for the sponsorship-research feature (Phase 2).

## Planned tooling

`copilot apis status` (not yet built - see docs/PLAN.md "Later / optional") will list every API above with its rate limit, used, and remaining, sourced from whatever the most recent real call's response headers reported - never a dedicated call made just to check status.
