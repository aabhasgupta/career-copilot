"""Local web dashboard (D11). Two pages so far:

- Preferences: edits profile.yaml's search section, so tuning
  titles/locations/salary/preferences is a form instead of a YAML edit. Also
  has the base application packet (D22/D23): view/regenerate the one
  reusable content doc, not per-job.
- Jobs: browse/sort/filter scored jobs and drill into the full fit +
  sponsorship breakdown for one - the "unified view" a CLI table can't give
  without becoming unreadable (docs/DECISIONS.md D19). The detail page has
  the dashboard's first write actions - Queue / Mark applied - both explicit
  clicks that only record what the user already did (D1).

Design constraints:
- Same source of truth: reads profile.yaml/the SQLite DB directly - no
  second store. Preferences writes go through the same comment-preserving
  ruamel helpers the CLI uses, and validate before saving.
- Localhost only; single user; no auth (D4). Fully self-contained page -
  no CDN fonts/scripts.
"""

from __future__ import annotations

import html
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml as pyyaml
from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from copilot.config import Profile
from copilot.db import get_engine, get_session, init_db
from copilot.db.models import Application, Job
from copilot.formatting import format_posted, format_salary
from copilot.geocode import Geocoder
from copilot.industry import Industry
from copilot.profile_fill import update_search_preferences
from copilot.ranking import rank_jobs

_STYLE = """
  :root {
    --ink: #16182d; --muted: #6b7186; --line: #e4e6ef;
    --accent: #4f5bd5; --accent-2: #7a5cd6; --bg: #f3f4fa;
  }
  * { box-sizing: border-box; }
  body { font-family: -apple-system, "SF Pro Text", "Segoe UI", system-ui,
          sans-serif; margin: 0; background: var(--bg); color: var(--ink); }
  header {
    background: linear-gradient(120deg, #171a38 0%, #2c2a6b 55%, #4f3d8f 100%);
    color: #fff; padding: 2.6rem 1.5rem 2.9rem;
  }
  .shell { max-width: 960px; margin: 0 auto; }
  .brand { font-size: 2rem; font-weight: 750; letter-spacing: -.02em; }
  .brand .mark { display: inline-block; margin-right: .55rem; transform: translateY(2px); }
  .brand em { font-style: normal; color: #b9c0ff; }
  .tagline { margin-top: .45rem; color: #c9cdea; font-size: .95rem; letter-spacing: .01em; }
  .tagline b { color: #fff; font-weight: 600; }
  .nav { margin-top: 1.1rem; display: flex; gap: .4rem; }
  .navlink { color: #c9cdea; text-decoration: none; padding: .4rem .95rem;
             border-radius: 999px; font-size: .85rem; font-weight: 600; }
  .navlink:hover { color: #fff; background: rgba(255,255,255,.08); }
  .navlink.active { background: rgba(255,255,255,.16); color: #fff; }
  main { max-width: 960px; margin: -1.4rem auto 3rem; padding: 0 1.5rem; }
  .card {
    background: #fff; border: 1px solid var(--line); border-radius: 14px;
    padding: 1.4rem 1.5rem 1.5rem; margin-bottom: 1.1rem;
    box-shadow: 0 6px 18px rgba(26, 31, 71, .06);
  }
  .card h2 { margin: 0 0 .2rem; font-size: 1.02rem; letter-spacing: .01em; }
  .card .sub { margin: 0 0 .9rem; color: var(--muted); font-size: .86rem; }
  label { display: block; font-weight: 600; font-size: .9rem; margin: 1rem 0 .3rem; }
  label:first-of-type { margin-top: 0; }
  .hint { font-weight: 400; color: var(--muted); font-size: .82rem; }
  textarea, input[type=number], input[type=text], select {
    padding: .55rem .7rem; border: 1px solid var(--line);
    border-radius: 8px; font: inherit; font-size: .92rem; background: #fbfbfe;
  }
  textarea, select { width: 100%; }
  textarea { min-height: 5.2rem; resize: vertical; line-height: 1.5; }
  textarea:focus, input:focus, select:focus {
    outline: none; border-color: var(--accent);
    box-shadow: 0 0 0 3px rgba(79, 91, 213, .15); background: #fff;
  }
  .check { display: flex; align-items: center; gap: .55rem; margin-top: 1rem;
           font-weight: 600; font-size: .9rem; }
  .check input { width: 1.05rem; height: 1.05rem; accent-color: var(--accent); }
  .actions { display: flex; align-items: center; gap: 1rem; }
  button {
    padding: .65rem 2rem; border: 0; border-radius: 9px; color: #fff;
    background: linear-gradient(120deg, var(--accent), var(--accent-2));
    font: inherit; font-weight: 650; cursor: pointer;
    box-shadow: 0 4px 12px rgba(79, 91, 213, .35);
  }
  button:hover { filter: brightness(1.07); }
  .footnote { color: var(--muted); font-size: .8rem; }
  .flash { padding: .7rem 1rem; border-radius: 10px; margin-bottom: 1.1rem; font-size: .9rem; }
  .ok { background: #e7f6ec; color: #1d5e33; border: 1px solid #bfe5cb; }
  .err { background: #fdecec; color: #8a2323; border: 1px solid #f2c7c7; white-space: pre-wrap; }

  .filterbar { display: flex; gap: .6rem; margin-bottom: 1.1rem; flex-wrap: wrap; align-items: center; }
  .filterbar input { width: auto; }
  .filterbar input[name=location] { flex: 1 1 180px; }
  .filterbar button { padding: .5rem 1.2rem; }
  .filterbar .clear { color: var(--muted); font-size: .85rem; text-decoration: none; }

  .tablewrap { overflow-x: auto; }
  table.jobs { width: 100%; border-collapse: collapse; font-size: .86rem; }
  table.jobs th {
    text-align: left; padding: .55rem .6rem; border-bottom: 2px solid var(--line);
    color: var(--muted); font-weight: 600; font-size: .74rem;
    text-transform: uppercase; letter-spacing: .04em; white-space: nowrap;
  }
  table.jobs td { padding: .6rem; border-bottom: 1px solid var(--line); vertical-align: top; }
  table.jobs tr:hover td { background: #fbfbff; }
  table.jobs a { color: var(--accent); text-decoration: none; font-weight: 600; }
  table.jobs a:hover { text-decoration: underline; }

  .fit { font-weight: 700; padding: .15rem .55rem; border-radius: 999px; font-size: .82rem; }
  .fit.good { background: #e7f6ec; color: #1d5e33; }
  .fit.mid { background: #fff6e0; color: #8a6100; }
  .fit.low { background: #fdecec; color: #8a2323; }
  .fit.unscored { color: var(--muted); font-weight: 500; }

  .visa { font-size: .82rem; }
  .visa.yes { color: #1d5e33; font-weight: 600; }
  .visa.no { color: #8a2323; font-weight: 600; }
  .visa.evidence { color: var(--accent); font-weight: 600; }
  .visa.none { color: var(--muted); }

  .dims { width: 100%; border-collapse: collapse; margin-top: .8rem; font-size: .9rem; }
  .dims td { padding: .45rem .2rem; border-bottom: 1px solid var(--line); }
  .dims td:last-child { text-align: right; font-weight: 600; }
  .backlink { display: inline-block; margin-bottom: 1rem; color: var(--muted);
              text-decoration: none; font-size: .85rem; }
  .backlink:hover { color: var(--ink); }

  .packetbox { white-space: pre-wrap; background: #f7f7fb; border: 1px solid var(--line);
               border-radius: 10px; padding: 1rem; font-family: ui-monospace, monospace;
               font-size: .82rem; max-height: 320px; overflow-y: auto; }
  .state { font-weight: 700; padding: .15rem .55rem; border-radius: 999px; font-size: .82rem;
           background: #eef0fb; color: var(--accent); }
  .btn-row { display: flex; gap: .6rem; flex-wrap: wrap; }
  .btn-row button { padding: .5rem 1.2rem; }
  button.secondary { background: #eef0fb; color: var(--ink); box-shadow: none; }
  .badge { background: #e8590c; color: #fff; border-radius: 999px; font-size: .72rem;
           font-weight: 700; padding: .1rem .5rem; margin-left: .35rem; }
  .evidence { background: #f6f7fb; border-left: 3px solid var(--accent); padding: .7rem .9rem;
              white-space: pre-wrap; font-size: .85rem; max-height: 260px; overflow-y: auto; }
"""


def _shell(active: str, body: str, pending_suggestions: int = 0) -> str:
    def link(path: str, label: str) -> str:
        cls = "navlink active" if path == active else "navlink"
        return f'<a class="{cls}" href="{path}">{label}</a>'

    suggestions_label = "Suggestions"
    if pending_suggestions:
        suggestions_label += f'<span class="badge">{pending_suggestions}</span>'
    nav = link("/", "Preferences") + link("/jobs", "Jobs") + link("/suggestions", suggestions_label)
    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Career Copilot</title>
<style>{_STYLE}</style>
</head>
<body>
<header>
  <div class="shell">
    <div class="brand"><span class="mark">&#x1F9ED;</span>Career <em>Copilot</em></div>
    <div class="tagline">discover &middot; rank &middot; prepare &mdash; <b>you always click submit</b></div>
    <nav class="nav">{nav}</nav>
  </div>
</header>
<main>
{body}
</main>
</body>
</html>"""


_PREFS_BODY = """{flash}
<form method="post" action="/save">

<div class="card">
<h2>What to search for</h2>
<p class="sub">Discovery queries every title in every location on each
<code>copilot discover</code> run.</p>
<label>Target titles <span class="hint">one per line</span></label>
<textarea name="titles">{titles}</textarea>
<label>Search locations <span class="hint">one per line, e.g. "United States"</span></label>
<textarea name="locations">{locations}</textarea>
</div>

<div class="card">
<h2>Filters &mdash; drop jobs entirely</h2>
<p class="sub">Applied at discovery; run <code>copilot jobs prune</code> after
tightening to re-apply to stored jobs.</p>
<label>Salary floor (USD) <span class="hint">jobs with a known salary below this
are dropped; jobs that don't state pay are always kept. Blank = no floor.</span></label>
<input name="min_salary" type="number" value="{min_salary}">
<label>Dealbreakers <span class="hint">plain English, one per line - e.g.
"don't give me jobs based in Alabama", "no clearance jobs", "nothing at Meta".
Claude compiles these into precise filters on the next discover/prune.</span></label>
<textarea name="dealbreakers">{dealbreakers}</textarea>
</div>

<div class="card">
<h2>Preferences &mdash; reorder, never hide</h2>
<p class="sub">Listings sort by these instantly; unmatched jobs go last but
stay visible.</p>
<label>Location preference <span class="hint">one per line, most preferred first:
"remote", "within 30 miles of Place, ST", or plain location text</span></label>
<textarea name="location_preference">{location_preference}</textarea>
<label>Industry preference <span class="hint">one per line, most preferred first.
Vocabulary: {industry_vocab}</span></label>
<textarea name="industry_preference">{industry_preference}</textarea>
<label>Company preference <span class="hint">one per line, most preferred first -
their listings rank higher and their public job boards (Greenhouse/Lever/Ashby)
are watched directly</span></label>
<textarea name="company_preference">{company_preference}</textarea>
<label class="check"><input type="checkbox" name="deprioritize_staffing"
{staffing_checked}> Rank direct employers above staffing agencies</label>
</div>

<div class="actions">
  <button type="submit">Save</button>
  <span class="footnote">writes profile.yaml &middot; comments preserved &middot;
  validated before saving</span>
</div>
</form>

<div class="card">
<h2>Application packet</h2>
<p class="sub">Reusable content - contact details, H1B work-authorization Q&amp;A,
and a resume summary - the same for every job, so it's generated once and
reused rather than regenerated per posting (docs/DECISIONS.md D22, D23).
Regenerate it yourself after editing your resume or profile.yaml; it never
happens automatically.</p>
{packet_section}
<form method="post" action="/packet/base">
  <div class="actions"><button type="submit" class="secondary">Regenerate base packet</button></div>
</form>
</div>

<div class="card">
<h2>Inbox sync</h2>
<p class="sub">The hourly background job that reads new inbox mail, updates
application states, and files emails. Pausing stops the schedule; nothing
touches your inbox until you resume (manual <code>copilot inbox sync</code>
still works). Missed time is always caught up on the next run - never
skipped.</p>
{schedule_section}
</div>"""


def _lines(values: list[str]) -> str:
    return html.escape("\n".join(values))


def _parse_lines(raw: str) -> list[str]:
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _fit_badge(fit_score: float | None) -> str:
    if fit_score is None:
        return '<span class="fit unscored">-</span>'
    cls = "good" if fit_score >= 70 else "mid" if fit_score >= 40 else "low"
    return f'<span class="fit {cls}">{fit_score:.0f}</span>'


def _visa_cell(job: Job) -> str:
    """One column, strongest signal wins: an explicit JD statement (specific,
    current) outranks company-wide filing history (dated, aggregate) -
    D18/D19. The detail page shows both separately in full."""
    if job.visa_signal.value == "explicit_no":
        return '<span class="visa no">No sponsorship (JD)</span>'
    if job.visa_signal.value == "explicit_yes":
        return '<span class="visa yes">Sponsors (JD)</span>'
    if job.company and job.company.sponsorship_status.value == "sponsors":
        return f'<span class="visa evidence">H1B history: {job.company.h1b_filing_count}</span>'
    return '<span class="visa none">-</span>'


def _job_row(job: Job) -> str:
    company = html.escape(job.company.name) if job.company else "?"
    title = html.escape(job.title)
    location = html.escape(job.location or "unknown")
    apply_url = html.escape(job.apply_url)
    return f"""<tr>
<td>{_fit_badge(job.fit_score)}</td>
<td><a href="/jobs/{job.id}">{title}</a></td>
<td>{company}</td>
<td>{location}</td>
<td>{format_salary(job)}</td>
<td>{format_posted(job)}</td>
<td>{_visa_cell(job)}</td>
<td><a href="{apply_url}" target="_blank" rel="noopener">Apply &rarr;</a></td>
</tr>"""


def _jobs_list_body(
    jobs: list[Job], total: int, min_fit: str, location: str, max_age: str
) -> str:
    rows = "".join(_job_row(j) for j in jobs) or (
        '<tr><td colspan="8" class="sub">No jobs match these filters.</td></tr>'
    )
    clear = '<a class="clear" href="/jobs">Reset filters</a>' if (min_fit or location) else ""
    return f"""<div class="card">
<h2>Jobs</h2>
<p class="sub">{len(jobs)} of {total} shown, most recent and best fit first. Unscored jobs are
never treated as low fit - they just haven't run through <code>copilot score</code>
yet. Click a title for the full fit and sponsorship breakdown.</p>
<form method="get" action="/jobs" class="filterbar">
  <input type="number" name="min_fit" placeholder="Min fit" value="{html.escape(min_fit)}">
  <input type="text" name="location" placeholder="Location contains..." value="{html.escape(location)}">
  <input type="number" name="max_age" placeholder="Max age (days)" value="{html.escape(max_age)}">
  <button type="submit">Filter</button>
  {clear}
</form>
<div class="tablewrap">
<table class="jobs">
<thead><tr><th>Fit</th><th>Title</th><th>Company</th><th>Location</th>
<th>Salary</th><th>Posted</th><th>Visa</th><th></th></tr></thead>
<tbody>{rows}</tbody>
</table>
</div>
</div>"""


def _dimension_rows(job: Job) -> str:
    dims = [
        ("Skill match", job.skill_match_score),
        ("Experience level", job.experience_level_score),
        ("Domain fit", job.domain_fit_score),
        ("Location fit", job.location_fit_score),
        ("Visa feasibility", job.visa_feasibility_score),
    ]
    return "".join(
        f"<tr><td>{name}</td><td>{value:.0f}/100</td></tr>" if value is not None
        else f"<tr><td>{name}</td><td>-</td></tr>"
        for name, value in dims
    )


def _job_detail_body(job: Job, application: Application | None) -> str:
    company = html.escape(job.company.name) if job.company else "?"
    title = html.escape(job.title)
    location = html.escape(job.location or "unknown location")
    apply_url = html.escape(job.apply_url)

    header_card = f"""<div class="card">
<h2>{title}</h2>
<p class="sub">{company} &middot; {location}{' (remote)' if job.remote else ''} &middot;
{format_salary(job)} &middot; posted {format_posted(job)} &middot; via {job.source}</p>
<p><a href="{apply_url}" target="_blank" rel="noopener">Open application &rarr;</a></p>
</div>"""

    if job.fit_score is None:
        fit_card = """<div class="card">
<p class="sub">Not scored yet. Run <code>copilot score</code> to add this job.</p>
</div>"""
    else:
        fit_card = f"""<div class="card">
<h2>Fit: {job.fit_score:.0f}/100</h2>
<p>{html.escape(job.fit_reasoning or "")}</p>
<table class="dims">{_dimension_rows(job)}</table>
<p class="sub">Visa signal from this JD: {job.visa_signal.value}</p>
</div>"""

    sponsorship_card = ""
    if job.company and (
        job.company.sponsorship_status.value != "unknown" or job.company.sponsorship_evidence
    ):
        sponsorship_card = f"""<div class="card">
<h2>Company sponsorship history</h2>
<p class="sub">Historical, company-wide evidence from public USCIS filing data -
not a live policy, and never blended into the fit score above (see docs/DECISIONS.md D18).</p>
<p><b>{job.company.sponsorship_status.value}</b>
{" - " + html.escape(job.company.sponsorship_evidence) if job.company.sponsorship_evidence else ""}</p>
</div>"""

    return (
        '<a class="backlink" href="/jobs">&larr; Back to jobs</a>'
        + header_card
        + _application_actions(job.id, application)
        + fit_card
        + sponsorship_card
    )


def _packet_section(packet_base_path: Path) -> str:
    if not packet_base_path.exists():
        return '<p class="sub">Not generated yet.</p>'
    return f'<div class="packetbox">{html.escape(packet_base_path.read_text())}</div>'


def _schedule_section() -> str:
    try:
        from copilot.schedule.launchd import status

        s = status()
    except Exception as exc:  # noqa: BLE001 - a broken launchctl shouldn't kill the page
        return f'<p class="sub">Could not read schedule status: {html.escape(str(exc))}</p>'

    detail = f'<p>Status: <span class="state">{html.escape(s.detail)}</span></p>'
    if s.loaded:
        button = (
            '<form method="post" action="/schedule/pause">'
            '<button type="submit" class="secondary">Pause</button></form>'
        )
    elif s.installed:
        button = (
            '<form method="post" action="/schedule/resume">'
            "<button type=\"submit\">Resume</button></form>"
        )
    else:
        button = (
            '<p class="footnote">Not installed - run <code>copilot schedule install</code> '
            "once to start hourly syncing.</p>"
        )
    return f'{detail}<div class="btn-row">{button}</div>'


def _suggestion_card(s) -> str:
    kind_label = (
        "State conflict" if s.kind.value == "state_conflict" else "Untracked application"
    )
    company = html.escape(s.company_name or "unknown company")
    title = f" - {html.escape(s.job_title)}" if s.job_title else ""
    proposed = s.proposed_state.value if s.proposed_state else "no state change"
    if s.kind.value == "state_conflict":
        explain = (
            f"This email implies <b>{proposed}</b>, which contradicts the state you set. "
            "Confirm to adopt the email's state; decline to keep yours."
        )
    else:
        explain = (
            f"This looks like an application you made outside Career Copilot. Confirm to "
            f"track it (state: <b>{proposed}</b>); decline to ignore this email permanently."
        )
    sender = html.escape(s.sender or "")
    subject = html.escape(s.subject or "")
    snippet = html.escape((s.body_text or "")[:800])
    return f"""<div class="card">
<h2>{kind_label}: {company}{title}</h2>
<p class="sub">{explain}</p>
<div class="evidence">From: {sender}
Subject: {subject}

{snippet}</div>
<div class="btn-row" style="margin-top:.8rem">
<form method="post" action="/suggestions/{s.id}/confirm"><button type="submit">Confirm</button></form>
<form method="post" action="/suggestions/{s.id}/decline"><button type="submit" class="secondary">Decline</button></form>
</div>
</div>"""


def _suggestions_body(pending: list) -> str:
    if not pending:
        return (
            '<div class="card"><h2>Suggestions</h2>'
            '<p class="sub">Nothing pending. When inbox sync finds an email it can\'t act on '
            "alone - an application made outside Career Copilot, or a state conflict - it "
            "shows up here for your decision.</p></div>"
        )
    return "".join(_suggestion_card(s) for s in pending)


def _application_actions(job_id: int, application: Application | None) -> str:
    state = application.state.value if application else "found"
    return f"""<div class="card">
<h2>Application status: <span class="state">{state}</span></h2>
<div class="btn-row">
<form method="post" action="/jobs/{job_id}/queue">
  <button type="submit" class="secondary">Queue</button>
</form>
<form method="post" action="/jobs/{job_id}/mark-applied">
  <button type="submit">Mark applied</button>
</form>
</div>
<p class="footnote">These only record what you did yourself - Career Copilot never
submits an application.</p>
</div>"""


def create_app(
    profile_path: Path = Path("profile.yaml"),
    db_path: Path | None = None,
    packet_base_path: Path | None = None,
) -> FastAPI:
    from copilot.packet import PACKET_BASE_PATH

    app = FastAPI(title="Career Copilot")
    packet_path = packet_base_path if packet_base_path is not None else PACKET_BASE_PATH
    # Every page needs the DB now (the nav's pending-suggestions badge), so
    # make sure the schema exists even if the dashboard is the first thing run.
    init_db(get_engine(db_path))

    def shell(active: str, body: str) -> str:
        from copilot.suggestions import pending_count

        with get_session(get_engine(db_path)) as session:
            count = pending_count(session)
        return _shell(active, body, pending_suggestions=count)

    def render_prefs(flash: str = "") -> str:
        with open(profile_path) as f:
            raw = pyyaml.safe_load(f)
        profile = Profile.model_validate(raw)
        search = profile.search
        body = _PREFS_BODY.format(
            flash=flash,
            titles=_lines(search.titles),
            locations=_lines(search.locations),
            min_salary=search.min_salary if search.min_salary is not None else "",
            dealbreakers=_lines(search.dealbreakers),
            location_preference=_lines(search.location_preference),
            industry_preference=_lines(search.industry_preference),
            company_preference=_lines(search.company_preference),
            industry_vocab=", ".join(i.value for i in Industry),
            staffing_checked="checked" if search.deprioritize_staffing else "",
            packet_section=_packet_section(packet_path),
            schedule_section=_schedule_section(),
        )
        return shell("/", body)

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return render_prefs()

    @app.get("/saved", response_class=HTMLResponse)
    def saved() -> str:
        return render_prefs(
            flash='<div class="flash ok">Saved. Stricter filters take '
            "effect on stored jobs after <code>copilot jobs prune</code>.</div>"
        )

    @app.post("/save")
    def save(
        titles: str = Form(""),
        locations: str = Form(""),
        min_salary: str = Form(""),
        dealbreakers: str = Form(""),
        location_preference: str = Form(""),
        industry_preference: str = Form(""),
        company_preference: str = Form(""),
        deprioritize_staffing: str | None = Form(None),
    ):
        updates = {
            "titles": _parse_lines(titles),
            "locations": _parse_lines(locations),
            "min_salary": int(min_salary) if min_salary.strip() else None,
            "dealbreakers": _parse_lines(dealbreakers),
            "location_preference": _parse_lines(location_preference),
            "industry_preference": _parse_lines(industry_preference),
            "company_preference": _parse_lines(company_preference),
            "deprioritize_staffing": deprioritize_staffing is not None,
        }

        # Validate the merged profile before touching the file.
        with open(profile_path) as f:
            raw = pyyaml.safe_load(f)
        raw["search"] = {**raw.get("search", {}), **updates}
        try:
            Profile.model_validate(raw)
        except Exception as exc:  # noqa: BLE001 - shown to the user, nothing written
            flash = f'<div class="flash err">Not saved:\n{html.escape(str(exc))}</div>'
            return HTMLResponse(render_prefs(flash))

        update_search_preferences(profile_path, updates)
        return RedirectResponse("/saved", status_code=303)

    @app.get("/packet/saved", response_class=HTMLResponse)
    def packet_saved() -> str:
        return render_prefs(flash='<div class="flash ok">Base packet regenerated.</div>')

    @app.post("/packet/base")
    def packet_base():
        from copilot.packet import write_base_packet

        with open(profile_path) as f:
            raw = pyyaml.safe_load(f)
        profile = Profile.model_validate(raw)
        write_base_packet(profile, path=packet_path)
        return RedirectResponse("/packet/saved", status_code=303)

    @app.get("/jobs", response_class=HTMLResponse)
    def jobs_list(
        min_fit: str = "", location: str = "", max_age: str | None = None, limit: int = 50
    ) -> str:
        with open(profile_path) as f:
            raw = pyyaml.safe_load(f)
        profile = Profile.model_validate(raw)

        # max_age distinguishes "not in the URL at all" (fresh /jobs visit -
        # apply the profile default) from "submitted blank" (user cleared the
        # field on purpose - no floor), unlike min_fit/location which have no
        # profile-level default to fall back to.
        if max_age is None:
            age_ceiling = profile.search.max_posting_age_days
        elif max_age.strip() == "":
            age_ceiling = None
        else:
            age_ceiling = int(max_age)

        engine = get_engine(db_path)
        with get_session(engine) as session:
            stmt = select(Job)
            if location.strip():
                stmt = stmt.where(Job.location.ilike(f"%{location.strip()}%"))
            if min_fit.strip():
                floor = int(min_fit)
                stmt = stmt.where((Job.fit_score.is_(None)) | (Job.fit_score >= floor))
            if age_ceiling is not None:
                cutoff = datetime.now(timezone.utc) - timedelta(days=age_ceiling)
                stmt = stmt.where((Job.posted_at.is_(None)) | (Job.posted_at >= cutoff))
            jobs = session.scalars(stmt).all()
            total = len(jobs)
            ranked = rank_jobs(jobs, profile, Geocoder())[:limit]
            max_age_value = str(age_ceiling) if age_ceiling is not None else ""
            body = _jobs_list_body(ranked, total, min_fit, location, max_age_value)
        return shell("/jobs", body)

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_detail(job_id: int):
        with get_session(get_engine(db_path)) as session:
            job = session.scalar(select(Job).where(Job.id == job_id))
            if job is None:
                return HTMLResponse(
                    shell(
                        "/jobs",
                        '<a class="backlink" href="/jobs">&larr; Back to jobs</a>'
                        f'<div class="card"><p class="sub">No job with id {job_id}.</p></div>',
                    ),
                    status_code=404,
                )
            application = session.scalar(select(Application).where(Application.job_id == job_id))
            body = _job_detail_body(job, application)
        return shell("/jobs", body)

    @app.post("/jobs/{job_id}/queue")
    def job_queue(job_id: int):
        from copilot.applications import queue

        with get_session(get_engine(db_path)) as session:
            if session.scalar(select(Job).where(Job.id == job_id)) is None:
                return HTMLResponse(f"No job with id {job_id}.", status_code=404)
            queue(session, job_id)
            session.commit()
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.post("/jobs/{job_id}/mark-applied")
    def job_mark_applied(job_id: int):
        from copilot.applications import mark_applied

        with get_session(get_engine(db_path)) as session:
            if session.scalar(select(Job).where(Job.id == job_id)) is None:
                return HTMLResponse(f"No job with id {job_id}.", status_code=404)
            mark_applied(session, job_id)
            session.commit()
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.post("/schedule/pause")
    def schedule_pause():
        from copilot.schedule.launchd import pause

        try:
            pause()
        except Exception as exc:  # noqa: BLE001 - shown on the page
            return HTMLResponse(
                render_prefs(f'<div class="flash err">Pause failed: {html.escape(str(exc))}</div>')
            )
        return RedirectResponse("/", status_code=303)

    @app.post("/schedule/resume")
    def schedule_resume():
        from copilot.schedule.launchd import resume

        try:
            resume()
        except Exception as exc:  # noqa: BLE001 - shown on the page
            return HTMLResponse(
                render_prefs(f'<div class="flash err">Resume failed: {html.escape(str(exc))}</div>')
            )
        return RedirectResponse("/", status_code=303)

    @app.get("/suggestions", response_class=HTMLResponse)
    def suggestions_list() -> str:
        from copilot.suggestions import list_pending

        with get_session(get_engine(db_path)) as session:
            body = _suggestions_body(list_pending(session))
        return shell("/suggestions", body)

    def _try_provider():
        """Filing the email is a courtesy mirror of the DB write (D5) - if
        Graph isn't reachable (no client id, expired login), the confirm
        still proceeds and only the mailbox filing is skipped."""
        try:
            with open(profile_path) as f:
                raw = pyyaml.safe_load(f)
            from copilot.email_agent.provider import get_provider

            return get_provider(Profile.model_validate(raw))
        except Exception:  # noqa: BLE001
            return None

    @app.post("/suggestions/{suggestion_id}/confirm")
    def suggestion_confirm(suggestion_id: int):
        from copilot.suggestions import confirm

        with get_session(get_engine(db_path)) as session:
            try:
                confirm(session, suggestion_id, _try_provider())
                session.commit()
            except (LookupError, ValueError) as exc:
                return HTMLResponse(str(exc), status_code=404)
        return RedirectResponse("/suggestions", status_code=303)

    @app.post("/suggestions/{suggestion_id}/decline")
    def suggestion_decline(suggestion_id: int):
        from copilot.suggestions import decline

        with get_session(get_engine(db_path)) as session:
            try:
                decline(session, suggestion_id)
                session.commit()
            except (LookupError, ValueError) as exc:
                return HTMLResponse(str(exc), status_code=404)
        return RedirectResponse("/suggestions", status_code=303)

    return app
