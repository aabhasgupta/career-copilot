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

Phases 0 (foundation) and 1 (discovery) complete. Next: fit scoring and sponsorship research. See `docs/PLAN.md` for the roadmap, `docs/DECISIONS.md` for design decisions, and `docs/APIS.md` for the external APIs in use.

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
uv run copilot discover      # searches Adzuna + JSearch for every title/location in your
                             # profile, then probes each company for a public Greenhouse/
                             # Lever/Ashby board - matched jobs get direct employer apply
                             # links and full JD text instead of aggregator redirects
uv run copilot jobs list                     # newest first
uv run copilot jobs list --min-salary 150000 --location remote
```

Re-running `discover` is always safe: postings are deduped by company+title+location, so nothing is stored twice.

## Development

```sh
uv run pytest
```
