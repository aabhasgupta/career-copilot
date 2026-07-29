# Career Copilot

A job-search copilot agent that does the legwork while you keep the final say:

- **Discovers jobs** matching your resume and preferences across aggregator APIs (Adzuna) and company ATS boards (Greenhouse, Lever, Ashby) - no hand-curated company list; the watchlist builds itself
- **Scores fit** with an explainable LLM rubric, including visa/sponsorship feasibility backed by public H1B filing data
- **Prepares applications**: tailored resume bullets and paste-ready answers per job - you review and click submit yourself, always
- **Tracks everything** in a local SQLite database: pipeline states, salary, location, and visa status as first-class fields
- **Monitors your inbox** (Outlook/Hotmail via Microsoft Graph), correlates replies to applications, files them into folders, and keeps you posted via desktop notifications and a daily digest email
- **Preps you for interviews** with briefs built from the JD, company research, and your own gap areas

Design principle: assist, never impersonate. No LinkedIn scraping, no auto-submission, no ToS-violating automation - quality of match and tailoring over volume.

## Status

Phases 0-3 are complete: foundation, discovery, fit scoring/sponsorship research/email digests, and the base application packet with queue/mark-applied tracking. Phase 4 - inbox monitoring with hourly scheduling - is built and unit-tested; live verification against the real inbox is pending. See `docs/PLAN.md` for the roadmap, `docs/DECISIONS.md` for design decisions, and `docs/APIS.md` for the external APIs in use.

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
uv run copilot jobs list --max-age 7         # only postings from the last week
uv run copilot jobs list --all               # ignore the salary and posting-age floors
```

Re-running `discover` is always safe: postings are deduped by company+title+location, so nothing is stored twice. Each source's own posting date is captured too (shown as "posted Nd ago" in `jobs list`/`jobs show`/the dashboard) - separate from when *you* discovered it, and stored as unknown rather than guessed when a source doesn't report one.

Listings come back ordered by fit, freshness, and your preferences. In `profile.yaml`:

- `search.location_preference` - ordered list that *sorts* jobs (never hides them): `remote`, `within 30 miles of <place>` (real distances - jobs are geocoded once and cached), or plain location text
- `search.industry_preference` - ordered industries you fit best (e.g. banking, fintech, tech, consulting); each company is classified once by Claude and the label stored, so ordering stays instant and free
- `search.company_preference` - companies you would love to work for: their listings rank higher, and their public ATS boards are watched directly so postings arrive with first-party apply links
- `search.min_salary` - hard floor: jobs whose known salary is below it are dropped; jobs that don't state a salary are always kept
- `search.max_posting_age_days` (default 30) - hard floor on posting age, re-checked live against today's date every time you list jobs (not just at discovery time), so a listing quietly ages out the day it crosses the threshold; jobs with an unknown posting date are always kept. Postings within 14 days also rank as a group ahead of older ones, not just as a last-resort tiebreak - see `docs/DECISIONS.md`
- `search.dealbreakers` - hard drops, written in plain English ("no clearance jobs", "don't give me jobs based in Alabama", "nothing at Meta"); Claude compiles them once into precise field-level filters, cached until the list changes

Changed your rules? `uv run copilot jobs prune` re-applies them to everything already stored - no API calls, since the database holds everything ever discovered. Jobs you've applied to are never pruned.

Prefer forms over YAML? `uv run copilot dashboard` serves a local web page
(localhost only) for editing all of the above, with validation before every
save and the file's comments preserved. The same dashboard also has a **Jobs**
tab - a browsable, filterable table of every job (best fit first, or sorted
by newest/salary/**response odds** - a separate score from fit, see "Scoring
fit" below) with a click-through detail view for the full fit breakdown and
sponsorship evidence, so you don't have to piece it together one
`jobs show <id>` at a time, plus Queue/Mark-applied/Set-state buttons on each
job's detail page - answering "what should I apply to?" A separate
**Applications** tab answers "where does
everything stand?": a "Needs attention" strip surfaces interview/OA emails you
haven't acted on yet and applications stuck in "applied" for 21+ days with no
activity, above the pipeline grouped into Active/Waiting/Closed (most recent
activity first within each group, Closed collapsed by default). Click through
to an application's own page for a header card (state, applied date, an
editable note, a link back to the original posting) and a full timeline
merging state history with inbox emails - each email expandable to its full
body - so "why does this say interviewing?" always has an answer, plus the
same Set-state/Untrack actions inline. The **Integrations** tab is the control
panel for everything Career Copilot connects to - upload/replace your resume,
set or update each API key (written to `.env` and applied immediately, no
restart), and install/pause/resume the background sync, all as compact status
rows that expand inline when there's something to edit. **API usage** is the
read-only companion - what each external service is for, whether it's
connected, and known quota (a real "N of 200 left" for JSearch, captured
opportunistically from its own response headers; an honest "no fixed quota"
for the rest rather than a fabricated number).

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

The same call also produces a `response_likelihood_score` - a deliberately different question
from fit ("will this get a reply" vs "is this a good fit"): a great-fit posting at a famous
company can be a worse bet than a decent-fit niche direct-ATS one. It's judged from posting age,
application channel (direct employer ATS vs an aggregator repost), and how generic/prominent the
role and company read, since no job source reports actual applicant counts. It's shown in
`jobs show` and as its own dashboard sort option, never blended into the default fit-based
ranking. Your own reply history isn't factored in yet - there isn't remotely enough tracked-application
data to calibrate that honestly, so it's left out rather than faked.

## Sponsorship research

```sh
uv run copilot sponsorship-sync             # match your companies against public H1B filing data
uv run copilot sponsorship-sync --refresh   # force re-download of the USCIS data
```

Matches companies by name against the public USCIS H-1B Employer Data Hub - free, no API key,
zero LLM calls. This is historical, company-wide evidence (a company can change policy, or
sponsor for some roles and not others), so it's deliberately never folded into `fit_score` -
see it as separate context in `copilot jobs show <id>`.

## Email digest

```sh
uv run copilot email login   # one-time device-code sign-in to your Hotmail/Outlook.com account
uv run copilot email test    # sends yourself a test email to confirm sending works
uv run copilot digest        # emails a summary of jobs discovered since the last digest
uv run copilot digest --dry-run   # prints the digest without sending or marking it sent
```

Sending goes through Microsoft Graph (MSAL device-code flow - no password or client secret
stored). Requires a one-time, free Azure app registration and `AZURE_CLIENT_ID` in `.env` -
**[docs/EMAIL_SETUP.md](docs/EMAIL_SETUP.md)** is the full step-by-step guide (portal clicks,
permissions, troubleshooting, and the planned Gmail equivalent). `copilot email login` caches
the resulting token locally and renews it silently after that - you only do the device-code
prompt once.

The digest reports jobs discovered since the last digest was sent (tracked in
`data/digest_state.json`, not a fixed 24h window, so a missed scheduled run never silently drops
jobs), calling out anything scored 70+ in a table and summarizing the rest as a count - a mailbox
isn't the place to browse the full list, `copilot jobs list` and the dashboard's Jobs tab are.
Nothing is sent when there's nothing new since the last send.

## Applying

```sh
uv run copilot packet base           # (re)generate data/packet_base.md
uv run copilot jobs queue 42         # declare intent to apply
uv run copilot jobs mark-applied 42  # record that you actually applied
uv run copilot jobs set-state 42 interviewing  # record any state, any direction
uv run copilot jobs untrack 42       # undo tracking something by mistake
```

Career Copilot prepares reusable application content - it never fills out or submits a form
itself (docs/DECISIONS.md D1, D22). `copilot packet base` writes `data/packet_base.md`: your
contact details, H1B-transfer work-authorization answers, and a resume summary, all template-filled
from `profile.yaml` and your resume - the same for every job, so it's generated once and reused,
not regenerated per posting (D23). Re-run it yourself after editing your resume or profile.yaml;
it never happens automatically. The dashboard has the same button on the Preferences page.

`jobs queue`/`jobs mark-applied` (also buttons on a job's detail page in the dashboard) just
record what you did - queuing never gets auto-created, and marking applied never fires until you
click it. `jobs untrack` is the undo: it deletes the application with its state history and
email records (after a confirmation prompt); a job that only exists because you confirmed an
email suggestion disappears entirely, while discovery-found jobs stay in the jobs list as
untracked listings. See **[docs/APPLYING.md](docs/APPLYING.md)** for how to actually get the packet's
content into an application: by hand, or via a free autofill tool like Simplify.

## Inbox monitoring

```sh
uv run copilot inbox sync --dry-run  # see what sync would do, without writing anything
uv run copilot inbox sync            # classify replies, update states, file emails, alert
uv run copilot inbox review          # anything needing your decision
uv run copilot inbox confirm 3       # yes - track it / adopt the email's state
uv run copilot inbox decline 4       # no - never ask about this email again
```

Inbox monitoring needs the same one-time email setup as the digest - see
**[docs/EMAIL_SETUP.md](docs/EMAIL_SETUP.md)** (the four Graph permissions it lists cover both).

**The idea in one sentence:** companies reply to your applications by email, so Copilot reads
your inbox every hour and does the bookkeeping - states stay current without you updating
anything, and the two decisions that are genuinely yours get queued for you instead of guessed.

### Your mailbox after setup

Sync creates two folders on first use; the color categories are Outlook tags, not folders:

```
Inbox                    <- read hourly; only NEW mail since the last run is looked at
Career Copilot           <- processed job mail lands here, color-tagged by type
└── Process              <- YOUR drop zone: drag any email here and the next
                            sync will process it, no matter how old it is
```

Everything else in your mailbox - other folders, Sent, archives - is never read. And the
folders are just a mirror for your convenience: the real record is the local database, so
moving or deleting filed mail never breaks tracking.

### What to expect, scenario by scenario

- **A company replies about a job you're tracking** (confirmation, rejection, recruiter
  outreach): the application's state advances by itself, the email is filed and tagged, and
  the change shows up in your daily digest. This is the default case and needs nothing from
  you - even when the reply is a bare "Re: Next steps" with no company name in it, thread
  matching keeps the conversation connected.
- **An interview invite or online assessment arrives**: same as above, plus an immediate macOS
  desktop notification - these two are time-sensitive, everything else waits for the digest.
  (Requires allowing notifications from "Script Editor" in System Settings once.)
- **An email about a job you applied to outside Copilot**: nothing is touched. It becomes a
  *suggestion* in `copilot inbox review` (or the dashboard's Suggestions tab): **confirm** to
  start tracking that application, **decline** to never be asked about that email again, or pick
  an existing application from the dropdown (`--job <id>` on the CLI) to attach the email there
  - for when you know where it belongs and the agent couldn't tell.
- **An email contradicts a state you set yourself** (say you marked a job rejected and an
  interview invite arrives): Copilot never overrules you. It records the email and flags the
  conflict for you to resolve three ways - adopt the email's state, keep yours (decline), or,
  if it's actually about a *different* application at the same company, link it there / create a
  separate application. The flagged one is never silently overwritten.
- **The filter missed a job email**: drag it into **Career Copilot/Process** and the next sync
  handles it - old mail never triggers an alert, since that clock has already run. (Dropped it
  on the parent folder by mistake? Also fine - that's swept too.) To *check* for misses,
  `inbox sync --dry-run` lists every skipped subject.
- **You made a mistake**: `copilot jobs set-state <id> <state>` records any state in any
  direction (including backward), and `copilot jobs untrack <id>` removes a tracking entirely -
  both also available on the job's dashboard page.

What it will never do: send or reply to anything on its own, change a state you set without
asking, or touch an email it isn't sure about. Cost-wise it's built cheapest-first: newsletters
and receipts are filtered out by patterns before any Claude call, each genuinely new job email
costs one small classification call exactly once, and a quiet inbox costs nothing at all. The
full design reasoning lives in `docs/DECISIONS.md` (D20, D24, D26-D29).

```sh
uv run copilot schedule install    # hourly background sync + digest via launchd
uv run copilot schedule status     # installed? running? last exit code
uv run copilot schedule pause      # stop runs without uninstalling
uv run copilot schedule resume
uv run copilot schedule uninstall
```

Scheduling uses launchd rather than cron so a run missed while the laptop was asleep fires on
wake instead of being skipped. The job runs hourly, but the digest inside it self-gates to at
most one email a day (`copilot digest --if-due`) - sync stays timely without your own digest
turning into hourly notifications. Logs go to `data/logs/`. The dashboard's Integrations page
shows the schedule's status with the same Install/Pause/Resume controls.

## Development

```sh
uv run pytest
```
