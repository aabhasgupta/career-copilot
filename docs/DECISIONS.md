# Decision Log

What we chose, why, and what we rejected. New sessions: read this before proposing alternatives to any of these.

## D1: Assisted apply, never auto-submit
**Chosen**: the agent discovers, scores, and prepares everything; the human reviews and clicks submit.
**Rejected**: zero-touch auto-submission (AIHawk/LazyApply style).
**Why**: LinkedIn and similar platforms detect and ban automation on accounts; mass-generated applications have measurably worse response rates; and quality-of-tailoring beats volume. Also a portfolio consideration: "scores fit and tailors applications" is a strong story, "evades anti-bot detection" is a liability. The local Playwright prefill in Phase 3 stays inside this line: public ATS forms, user's own machine, human always submits.

## D2: No LinkedIn/Glassdoor/Indeed scraping
**Chosen**: aggregator APIs (Adzuna, optionally JSearch which indexes LinkedIn/Indeed/Glassdoor postings) plus public ATS APIs.
**Rejected**: logging into job boards and scraping listings.
**Why**: ToS/ban risk on the user's real accounts, high maintenance (DOM changes, CAPTCHAs), and coverage overlap is high anyway since most LinkedIn postings are cross-posts of company ATS boards. LinkedIn-exclusive feed posts are a known, accepted gap; the user can still Easy Apply manually and track it here.

## D3: No hand-curated company list; watchlist builds itself
**Chosen**: broad aggregator search is the front door; when a result's apply URL reveals a Greenhouse/Lever/Ashby slug, the company auto-joins a watchlist whose boards are polled directly thereafter.
**Why**: the user explicitly does not want to be restricted to a fixed company list. ATS boards give cleaner JDs and earlier postings than aggregators, so the system gets faster and richer the longer it runs.
**Update (2026-07-19, Phase 1 build)**: the detection code is implemented (`discovery/ats.py`) and correct, but in practice it finds nothing - Adzuna's apply link always proxies through `adzuna.com` rather than the employer's own posting, and live JSearch sampling never returned a direct apply link either (`job_apply_is_direct` was `false` on every result checked). The self-building watchlist as originally scoped needs real ATS URLs to key off of, which these two aggregators don't expose.
**Resolution (same day)**: the mechanism was inverted rather than dropped - instead of sniffing apply URLs for an ATS domain, each newly discovered company's *name* is probed against the three public ATS APIs (`discovery/ats_resolver.py`; free, unauthenticated). Matches are watchlisted and their boards polled on every discover run (`discovery/ats_boards.py`): postings matching a known job upgrade it in place to the direct employer apply link and full JD text; postings matching the profile's search titles are added as new jobs. Guardrails learned from live false positives: loose first-word slug guesses are only trusted on Greenhouse (the one ATS that echoes the company name back for verification - Capital One matched an unrelated Lever board named "capital" without this), title matching for upgrades is exact-only, and same-title-multiple-location postings are only linked when the location disambiguates. Direct-link coverage will never be 100% - Workday/iCIMS/in-house ATSes have no public API - and the aggregator link remains the fallback.

## D4: Single-user architecture, generic through configuration
**Chosen**: all personal data in `profile.yaml` + resume file; code reads config, never hardcodes the user.
**Rejected**: multi-user product infra (auth, hosting, multi-tenant DB).
**Why**: multi-tenancy roughly doubles build cost while teaching little about agents. Clone-and-configure is the norm for strong open-source projects of this kind. The profile-config boundary is the exact seam to refactor at if this ever becomes a product.

## D5: SQLite as source of truth from day one
**Chosen**: every phase writes to `copilot.db`; CLI, digest emails, and any future dashboard are read layers.
**Why**: no migration/backfill when the dashboard phase arrives; months of history make the visualizations worth having; timestamps (`posted_at`, `applied_at`, `received_at`) let the Phase 5 scorer learn from the user's own outcomes.

## D6: Microsoft Graph for email, behind a provider interface
**Chosen**: user's primary inbox is Hotmail/Outlook.com, so Outlook via Microsoft Graph (MSAL device-code flow) is the primary `EmailProvider`; Gmail is the second implementation for genericity. Provider selected in `profile.yaml`.
**Superseded**: the original plan assumed Gmail. Changed 2026-07-18 when the user clarified their primary account.
**Note**: Outlook folders + categories fulfill the "organize into a folder/tag" requirement.

## D7: Email sending in Phase 2, inbox monitoring in Phase 4
**Chosen**: Graph auth + `sendMail` (digest, alerts) lands with scoring; reading/classification lands after applying starts.
**Why**: email is the only passive channel, so digests are valuable as soon as discovery+scoring work; but there is nothing to monitor until applications exist, and building the classifier before real reply emails exist means building against imagined data.

## D8: launchd, not cron; laptop does not need to stay on
**Chosen**: launchd-scheduled CLI runs, managed by a `copilot schedule` command group.
**Why**: cron silently skips jobs missed during sleep; launchd fires them on wake. The system is poll-based, so missed runs mean delayed data, never lost data. Off-machine scheduling (GitHub Actions / small VM) is the documented escape hatch if closed-laptop latency ever matters.

## D9: Python 3.12 + uv
**Chosen**: Python with uv, SQLAlchemy, Typer, httpx, Anthropic SDK.
**Rejected**: TypeScript/Node (equally capable, better only if this were web-first).
**Why**: first-class libraries for every integration on the roadmap, lingua franca of agent engineering (learning transfers), and the CSV/data work (H1B disclosure files, dedupe, scoring) is bread-and-butter Python.

## D10: Visa status is a first-class field, unknowns are kept
**Chosen**: per-job `visa_signal` (explicit_yes / explicit_no / unknown) and per-company sponsorship evidence from public H1B data (USCIS H-1B Employer Data Hub, DOL LCA disclosures). Jobs with no visa information are still surfaced and tracked as unknown, never filtered out.
**Why**: user needs H1B transfer; sponsorship-unknown jobs are still worth applying to, and no existing market tool (Teal, Simplify, Huntr) tracks this at all - it is a genuine differentiator.

## D11: Local web dashboard is the primary human surface (decided 2026-07-19)
**Chosen**: promote the "later / optional" local web dashboard into Phase 3 as the main way the user reviews jobs and tracks applications: a localhost page over the same SQLite DB showing scored jobs (fit, salary, location, visa signal, apply link) with a mark-applied action. The CLI keeps the machine-facing commands (discover, score, schedule).
**Rejected**: CLI-only application tracking (`copilot mark applied <id>` one row at a time).
**Why**: the review loop is browse-heavy - dozens of jobs, open link, come back, tick off - which a table UI handles far better than per-ID CLI commands. Fits D5 exactly: the DB stays the source of truth and the dashboard is just another read/write layer. Manual marking is expected to become the fallback anyway once Phase 4 lands: ATS confirmation emails ("we received your application") let the email agent auto-mark applications with the correct timestamp, and rejections/interview invites auto-update state the same way.

## D12: Preferences reorder, filters drop (decided 2026-07-19)
**Chosen**: two distinct mechanisms in `profile.yaml`. Filters (`dealbreakers`, `min_salary`) remove jobs from view - and per D10, `min_salary` only drops jobs whose *known* salary is below the floor; unknown-salary jobs always stay. Preferences (`search.location_preference`, an ordered list) never hide anything: they sort listings so the best-matching jobs are at the top, with unmatched jobs last but visible. Entry forms: `remote`, `within <N> miles of <place>` (haversine over job coordinates - Adzuna provides them, others are geocoded once via Nominatim and cached), or plain text matched against the location (with a city-part fallback, since sources format the same place differently).
**Rejected**: folding location preference into the Phase 2 LLM fit score alone.
**Why**: the user's goal is "don't make me spend time choosing what to apply to." Deterministic preference ordering is transparent (the Pref column shows exactly why a job ranked where it did), free (no LLM tokens), and instant. The Phase 2 fit score will layer on top - fit becomes the primary sort with preference tier as input/tiebreak - but location ordering shouldn't cost tokens or be non-reproducible.
