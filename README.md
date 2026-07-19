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

Phase 0 (foundation) in progress. See `docs/PLAN.md` for the roadmap and `docs/DECISIONS.md` for design decisions.

## Setup

Requires [uv](https://docs.astral.sh/uv/) and an [Anthropic API key](https://console.anthropic.com/).

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

## Development

```sh
uv run pytest
```
