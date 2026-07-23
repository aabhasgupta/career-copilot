# Career Copilot

A job-search copilot agent that does the legwork while you keep the final say:

- **Discovers jobs** matching your resume and preferences across aggregator APIs (Adzuna) and company ATS boards (Greenhouse, Lever, Ashby) - no hand-curated company list; the watchlist builds itself
- **Scores fit** with an explainable LLM rubric, including visa/sponsorship feasibility backed by public H1B filing data
- **Prepares applications**: tailored resume bullets and paste-ready answers per job - you review and click submit yourself, always
- **Tracks everything** in a local SQLite database: pipeline states, salary, location, and visa status as first-class fields
- **Monitors your inbox** (Outlook/Hotmail via Microsoft Graph), correlates replies to applications, files them into folders, and alerts you by email
- **Preps you for interviews** with briefs built from the JD, company research, and your own gap areas

Design principle: assist, never impersonate. No LinkedIn scraping, no auto-submission, no ToS-violating automation - quality of match and tailoring over volume.

## Status

Phases 0 (foundation) and 1 (discovery) complete. Phase 2's fit scoring and sponsorship research are done; email sending is next. See `docs/PLAN.md` for the roadmap, `docs/DECISIONS.md` for design decisions, and `docs/APIS.md` for the external APIs in use.

## Setup

Requires [uv](https://docs.astral.sh/uv/) and an [Anthropic API key](https://console.anthropic.com/). Discovery needs at least one of: [Adzuna](https://developer.adzuna.com/) (`ADZUNA_APP_ID` + `ADZUNA_APP_KEY`) or [JSearch on RapidAPI](https://rapidapi.com/letscrape-6bRBa3QguO5/api/jsearch) (`JSEARCH_API_KEY`), both with free tiers.

```sh
uv sync
uv run copilot init          # creates profile.yaml, data/, and the database
# drop your resume at data/resume.pdf, put ANTHROPIC_API_KEY=sk-... in .env
uv run copilot profile fill            # auto-fills identity (name/contact/links) from your resume
uv run copilot profile suggest-titles  # optional: proposes search.titles from your resume + a live
                                        # web search for in-demand titles; asks before writing anything
# edit profile.yaml: fill in visa and email_integration by hand - these are
# your preferences, not facts the resume states
uv run copilot profile show  # Claude extracts and displays your structured profile
```

## Discovering jobs

```sh
uv run copilot discover      # searches Adzuna + JSearch (which indexes LinkedIn, Indeed,
                             # Glassdoor via Google for Jobs) plus the free remote boards
                             # Remotive and RemoteOK, then probes each company for a public
                             # Greenhouse/Lever/Ashby board - matched jobs get direct
                             # employer apply links and full JD text
uv run copilot jobs list                     # best first (see below)
uv run copilot jobs list --location chicago  # plus ad-hoc filters
uv run copilot jobs list --all               # ignore the salary floor
```

Re-running `discover` is always safe: postings are deduped by company+title+location, so nothing is stored twice.

Listings come back ordered by your preferences, not just recency. In `profile.yaml`:

- `search.location_preference` - ordered list that *sorts* jobs (never hides them): `remote`, `within 30 miles of <place>` (real distances - jobs are geocoded once and cached), or plain location text
- `search.industry_preference` - ordered industries you fit best (e.g. banking, fintech, tech, consulting); each company is classified once by Claude and the label stored, so ordering stays instant and free
- `search.company_preference` - companies you would love to work for: their listings rank higher, and their public ATS boards are watched directly so postings arrive with first-party apply links
- `search.min_salary` - hard floor: jobs whose known salary is below it are dropped; jobs that don't state a salary are always kept
- `search.dealbreakers` - hard drops, written in plain English ("no clearance jobs", "don't give me jobs based in Alabama", "nothing at Meta"); Claude compiles them once into precise field-level filters, cached until the list changes

Changed your rules? `uv run copilot jobs prune` re-applies them to everything already stored - no API calls, since the database holds everything ever discovered. Jobs you've applied to are never pruned.

Prefer forms over YAML? `uv run copilot dashboard` serves a local web page
(localhost only) for editing all of the above, with validation before every
save and the file's comments preserved.

## Scoring fit

```sh
uv run copilot score              # scores up to 25 unscored jobs against your resume
uv run copilot score --limit 50   # keep going through the backlog
uv run copilot jobs list --min-fit 70   # unscored jobs are always kept, never treated as low fit
uv run copilot jobs show 42       # full breakdown: reasoning + the 5 dimension scores behind fit_score
```

Each job gets a 0-100 `fit_score` with written reasoning, plus five independent dimension
scores (skill match, experience level, domain fit, location fit, visa feasibility) - `jobs list`
only shows the overall score to stay readable; the breakdown lives in `jobs show`. `fit_score`
is the model's own holistic judgment, not an average of the five dimensions. Scoring is batched
(several jobs share one Claude call, since the resume is common context) and only ever touches
unscored jobs, so re-running is always cheap and safe.

## Sponsorship research

```sh
uv run copilot sponsorship-sync             # match your companies against public H1B filing data
uv run copilot sponsorship-sync --refresh   # force re-download of the USCIS data
```

Matches companies by name against the public USCIS H-1B Employer Data Hub - free, no API key,
zero LLM calls. This is historical, company-wide evidence (a company can change policy, or
sponsor for some roles and not others), so it's deliberately never folded into `fit_score` -
see it as separate context in `copilot jobs show <id>`.

## Development

```sh
uv run pytest
```
