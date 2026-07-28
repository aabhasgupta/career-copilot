"""Local web dashboard (D11) - the primary human surface for Career Copilot.

Rendered with Jinja2 templates (`templates/`) styled by a vendored Pico.css
plus a small custom layer (`static/app.css`) - a left-sidebar layout, not the
old hand-rolled HTML f-strings. Pages:

- Home: what Career Copilot does + an at-a-glance overview of the pipeline.
- Jobs: browse/sort/filter scored jobs and drill into the full fit +
  sponsorship breakdown for one - the "unified view" a CLI table can't give
  without becoming unreadable (docs/DECISIONS.md D19). The detail page has
  write actions - Queue / Mark applied / Set state / Untrack - explicit clicks
  that only record what the user already did (D1).
- Applications: the pipeline view - every tracked application, most active
  state first (D31).
- Suggestions: the review queue for inbox findings needing a human decision
  (untracked applications and state conflicts, D20/D24/D28/D31).
- Preferences: edits profile.yaml's search section and the base application
  packet (D22/D23).
- Integrations: the setup control panel - resume upload, API key entry
  (written to .env and applied live, no restart), and schedule
  install/pause/resume, as compact status rows (D32).
- API usage: read-only companion to Integrations - what each external API
  is for, connection status, and known quota (real numbers only where a
  provider reports them, e.g. JSearch's captured rate-limit headers; honest
  text elsewhere rather than a fabricated figure, D33).

Design constraints:
- Same source of truth: reads profile.yaml/the SQLite DB directly - no second
  store. Preferences writes go through the same comment-preserving ruamel
  helpers the CLI uses, and validate before saving.
- Localhost only; single user; no auth (D4). Fully self-contained - Pico.css is
  vendored, no CDN fonts/scripts.
"""

from __future__ import annotations

import html
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml as pyyaml
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup
from sqlalchemy import func, select

from copilot.config import Profile
from copilot.db import get_engine, get_session, init_db
from copilot.db.models import Application, ApplicationState, Company, Job
from copilot.formatting import format_posted, format_salary
from copilot.geocode import Geocoder
from copilot.industry import Industry
from copilot.profile_fill import update_search_preferences
from copilot.ranking import rank_jobs

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"

# An "applied" application with no activity for this long is a nudge candidate
# on the Home command center (deterministic staleness, zero-LLM).
_STALE_APPLIED_DAYS = 21


def _age_days(dt: datetime | None, now: datetime) -> int | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (now - dt).days


def _posting_sort_key(job: Job) -> datetime:
    """Newest-first sort key by the posting's own date. Unknown posting dates
    sort last (not by discovery time - a just-discovered old posting isn't
    'new')."""
    dt = job.posted_at
    if dt is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _clean_jd(raw: str | None) -> str | None:
    """Stored JD text is HTML from some sources (Greenhouse, remote boards) and
    plain from others (Lever/Ashby descriptionPlain). Render it readably: strip
    tags to newlines/bullets and unescape entities when it looks like HTML."""
    if not raw:
        return None
    if "<" in raw and ">" in raw:
        text = re.sub(r"<\s*(br|/p|/div|/li|/tr|/h[1-6])\s*/?>", "\n", raw, flags=re.I)
        text = re.sub(r"<\s*li[^>]*>", "\n- ", text, flags=re.I)
        text = re.sub(r"<[^>]+>", "", text)
        text = html.unescape(text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()
    return raw.strip()


# --- Domain badges: small HTML fragments reused across templates. Returned as
# Markup so Jinja's autoescaping leaves them intact. ---

def _fit_badge(fit_score: float | None) -> Markup:
    if fit_score is None:
        return Markup('<span class="fit unscored">-</span>')
    cls = "good" if fit_score >= 70 else "mid" if fit_score >= 40 else "low"
    return Markup(f'<span class="fit {cls}">{fit_score:.0f}</span>')


def _visa_cell(job: Job) -> Markup:
    """One column, strongest signal wins: an explicit JD statement (specific,
    current) outranks company-wide filing history (dated, aggregate) -
    D18/D19. The detail page shows both separately in full."""
    if job.visa_signal.value == "explicit_no":
        return Markup('<span class="visa no">No sponsor (JD)</span>')
    if job.visa_signal.value == "explicit_yes":
        return Markup('<span class="visa yes">Sponsors (JD)</span>')
    if job.company and job.company.sponsorship_status.value == "sponsors":
        return Markup(f'<span class="visa evidence">H1B: {job.company.h1b_filing_count}</span>')
    return Markup('<span class="visa none">-</span>')


def _state_badge(state: ApplicationState) -> Markup:
    cls = "state"
    if state in (ApplicationState.rejected, ApplicationState.ghosted):
        cls += " terminal"
    elif state == ApplicationState.offer:
        cls += " won"
    return Markup(f'<span class="{cls}">{state.value}</span>')


def _parse_lines(raw: str) -> list[str]:
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _link_options(session, suggestion) -> Markup:
    """<option> list of tracked applications for the suggestion link dropdown -
    a picker instead of a free-typed job id, so linking can't hit a typo'd or
    wrong job (user feedback, live verification 2026-07-25).

    Ordered by likelihood, deterministically - zero LLM (a review page must
    render instantly and free): name overlap with the suggestion's evidence
    first, then most recent state activity. Ordering only promotes; every
    tracked application stays in the list, because a suggestion exists precisely
    when automatic matching failed - the human may know better."""
    from copilot.db.models import ApplicationStateEvent
    from copilot.scoring.sponsorship import normalize_company_name

    rows = session.execute(
        select(Application, Job, Company)
        .join(Job, Application.job_id == Job.id)
        .join(Company, Job.company_id == Company.id)
    ).all()
    # Monotonic event ids as the recency proxy: higher = more recent activity.
    last_event = dict(
        session.execute(
            select(ApplicationStateEvent.application_id, func.max(ApplicationStateEvent.id))
            .group_by(ApplicationStateEvent.application_id)
        ).all()
    )
    evidence = normalize_company_name(
        f"{suggestion.company_name or ''} {suggestion.subject or ''} {suggestion.sender or ''}"
    )

    def name_score(company: Company) -> int:
        name = normalize_company_name(company.name)
        if not name:
            return 0
        if f" {name} " in f" {evidence} ":
            return 2  # whole company name appears in the email's evidence
        return 1 if any(t in evidence.split() for t in name.split()) else 0

    rows.sort(key=lambda r: (-name_score(r[2]), -last_event.get(r[0].id, 0), r[2].name))
    return Markup(
        "".join(
            f'<option value="{job.id}">'
            f"#{job.id} {html.escape(company.name)} - {html.escape(job.title)}"
            f" ({application.state.value})</option>"
            for application, job, company in rows
        )
    )


def _schedule_status() -> object:
    """Best-effort launchd status; a broken launchctl must not kill the page."""
    from types import SimpleNamespace

    try:
        from copilot.schedule.launchd import status

        return status()
    except Exception as exc:  # noqa: BLE001
        return SimpleNamespace(
            installed=False, loaded=False, detail=f"could not read schedule: {exc}"
        )


# Integrations page: one-line "what it powers" per setup check, shown as a row
# tooltip (not inline) so the compact status list stays scannable.
_INTEGRATION_PURPOSES = {
    "profile": "Your identity, search titles, locations, and visa status - the config every feature reads.",
    "resume": "The resume Career Copilot reads to score fit and build tailored packets.",
    "anthropic": "Powers fit scoring, packet tailoring, and inbox email classification.",
    "discovery": "The aggregator search (Adzuna / JSearch) that finds jobs to rank.",
    "email": "Reply tracking and digest emails via Microsoft Graph - the inbox-sync backbone.",
    "schedule": "The hourly background job that reads new inbox mail and updates application states.",
}


def _integration_row(check, sched) -> dict:
    """One compact status row. `note` is a terse trailing status (green rows) or
    the one-step fix (rows needing action) - never the full purpose, which lives
    in the row tooltip. `dot`: on = ready, warn = required-and-missing, off =
    optional-and-off."""
    key, ok = check.key, check.ok
    if key == "profile":
        note = "" if ok else "run copilot init"
    elif key == "resume":
        note = check.detail  # the path when present, "... is missing" otherwise
    elif key == "anthropic":
        note = "key detected" if ok else "add ANTHROPIC_API_KEY to .env"
    elif key == "discovery":
        note = check.detail.replace(" configured", "") if ok else "add Adzuna or JSearch keys to .env"
    elif key == "email":
        note = "signed in" if ok else "add AZURE_CLIENT_ID, then copilot email login"
    elif key == "schedule":
        # Friendly UI text, not the raw launchctl detail (which leaks jargon).
        note = "running hourly" if sched.loaded else ("paused" if sched.installed else "not installed")
    else:
        note = check.detail
    return {
        "key": key,
        "label": check.label,
        "required": check.required,
        "ok": ok,
        "dot": "on" if ok else ("warn" if check.required else "off"),
        "note": note,
        "purpose": _INTEGRATION_PURPOSES.get(key, ""),
    }


def _format_reset(reset_at: datetime | None, now: datetime) -> str:
    if reset_at is None:
        return ""
    delta = reset_at - now
    if delta.total_seconds() <= 0:
        return "resets soon"
    if delta.days >= 1:
        return f"resets in {delta.days}d"
    hours = int(delta.total_seconds() // 3600)
    return f"resets in {hours}h" if hours >= 1 else "resets soon"


def _usage_rows(jsearch_quota, graph_connected: bool) -> tuple[list[dict], list[dict]]:
    """Rows for the API usage page: what each service is for, whether it's
    connected, and whatever quota was last opportunistically captured (never
    a dedicated call spent just to check status - D33). Read-only and purely
    informational, so unlike Integrations there's no amber "needs you" dot -
    only on (connected/available) or off (optional and not connected)."""
    now = datetime.now(timezone.utc)
    anthropic_on = bool(os.environ.get("ANTHROPIC_API_KEY"))
    adzuna_on = bool(os.environ.get("ADZUNA_APP_ID") and os.environ.get("ADZUNA_APP_KEY"))
    jsearch_on = bool(os.environ.get("JSEARCH_API_KEY"))

    def jsearch_note() -> str:
        if not jsearch_on:
            return "not connected"
        if jsearch_quota is None:
            return "connected - no calls recorded yet"
        parts = []
        if jsearch_quota.remaining is not None and jsearch_quota.limit is not None:
            parts.append(f"{jsearch_quota.remaining} of {jsearch_quota.limit} left")
        reset = _format_reset(jsearch_quota.reset_at, now)
        if reset:
            parts.append(reset)
        return " · ".join(parts) if parts else "connected"

    metered = [
        {"key": "anthropic", "label": "Anthropic API", "dot": "on" if anthropic_on else "off",
         "note": "connected - pay-as-you-go, no fixed quota" if anthropic_on else "not connected",
         "purpose": "Powers fit scoring, packet tailoring, resume extraction, and inbox classification."},
        {"key": "jsearch", "label": "JSearch (RapidAPI)", "dot": "on" if jsearch_on else "off",
         "note": jsearch_note(),
         "purpose": "Job discovery - LinkedIn/Indeed/Glassdoor via Google for Jobs' index. "
                    "200 requests/month on the free tier."},
        {"key": "adzuna", "label": "Adzuna", "dot": "on" if adzuna_on else "off",
         "note": "connected - free tier, no published limit" if adzuna_on else "not connected",
         "purpose": "Broad job discovery aggregator, free tier."},
        {"key": "graph", "label": "Microsoft Graph", "dot": "on" if graph_connected else "off",
         "note": "connected - no published limit" if graph_connected else "optional, not connected",
         "purpose": "Sends the digest and syncs your inbox for reply tracking."},
    ]
    free = [
        {"key": "ats", "label": "Greenhouse / Lever / Ashby", "dot": "on",
         "note": "always on - no key needed",
         "purpose": "Public ATS job boards - direct employer postings and full JD text."},
        {"key": "remotive", "label": "Remotive", "dot": "on",
         "note": "always on - no key needed",
         "purpose": "Remote-first tech job board."},
        {"key": "remoteok", "label": "RemoteOK", "dot": "on",
         "note": "always on - no key needed",
         "purpose": "Remote-first tech job board."},
        {"key": "nominatim", "label": "Nominatim (OpenStreetMap)", "dot": "on",
         "note": "always on - throttled to ~1 req/sec, results cached forever",
         "purpose": "Geocodes locations for the \"within N miles of\" preference."},
        {"key": "uscis", "label": "USCIS H-1B Data Hub", "dot": "on",
         "note": "always on - downloaded once, refresh manually",
         "purpose": "Company-level H1B sponsorship filing history."},
    ]
    return metered, free


def create_app(
    profile_path: Path = Path("profile.yaml"),
    db_path: Path | None = None,
    packet_base_path: Path | None = None,
) -> FastAPI:
    from copilot.packet import PACKET_BASE_PATH

    app = FastAPI(title="Career Copilot")
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
    packet_path = packet_base_path if packet_base_path is not None else PACKET_BASE_PATH
    # Every page needs the DB now (the nav's pending-suggestions badge), so make
    # sure the schema exists even if the dashboard is the first thing run.
    init_db(get_engine(db_path))

    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    # Bust the browser cache when app.css changes (its mtime is the version), so
    # a styling edit is never masked by a stale cached stylesheet.
    try:
        static_v = str(int((_STATIC_DIR / "app.css").stat().st_mtime))
    except OSError:
        static_v = "0"
    env.globals.update(
        fit_badge=_fit_badge,
        visa_cell=_visa_cell,
        state_badge=_state_badge,
        format_salary=format_salary,
        format_posted=format_posted,
        static_v=static_v,
    )

    def render(active: str, template_name: str, **ctx) -> str:
        from copilot.suggestions import pending_count

        with get_session(get_engine(db_path)) as session:
            count = pending_count(session)
        return env.get_template(template_name).render(
            active=active, pending_suggestions=count, **ctx
        )

    def _profile_ready() -> bool:
        """True when profile.yaml exists and validates - the minimum for the
        profile-dependent pages to render. When false those pages funnel to
        Home, which shows the setup landing (crash-prevention until Phase C's
        full onboarding gate)."""
        from copilot.setup_status import get_setup_status

        check = get_setup_status(profile_path).get("profile")
        return bool(check and check.ok)

    def render_integrations(flash: str = "") -> str:
        from copilot.setup_status import get_setup_status

        status = get_setup_status(profile_path)
        sched = _schedule_status()
        rows = [_integration_row(c, sched) for c in status.checks]
        return render(
            "/integrations",
            "integrations.html",
            flash=flash,
            core=[r for r in rows if r["required"]],
            optional=[r for r in rows if not r["required"]],
            configured=sum(1 for c in status.checks if c.ok),
            total=len(status.checks),
            missing_required=len(status.missing_required),
            required_complete=status.required_complete,
            schedule=sched,
        )

    def render_prefs(flash: str = "") -> str:
        with open(profile_path) as f:
            raw = pyyaml.safe_load(f)
        profile = Profile.model_validate(raw)
        search = profile.search
        packet_text = packet_path.read_text() if packet_path.exists() else None
        return render(
            "/preferences",
            "preferences.html",
            flash=flash,
            titles=search.titles,
            locations=search.locations,
            min_salary=search.min_salary if search.min_salary is not None else "",
            dealbreakers=search.dealbreakers,
            location_preference=search.location_preference,
            industry_preference=search.industry_preference,
            company_preference=search.company_preference,
            industry_vocab=", ".join(i.value for i in Industry),
            staffing_checked=search.deprioritize_staffing,
            packet_text=packet_text,
        )

    @app.get("/", response_class=HTMLResponse)
    def home() -> str:
        """Home adapts to where the user is:
        - not set up yet -> a landing/explainer with a setup checklist;
        - set up but no data -> a getting-started guide;
        - active -> the command center (needs-attention, pipeline, high-fit).
        All deterministic, zero-LLM."""
        from copilot.db.models import ApplicationStateEvent
        from copilot.setup_status import get_setup_status

        status = get_setup_status(profile_path)
        if not status.required_complete:
            return render("/", "home.html", needs_setup=True, setup=status)

        now = datetime.now(timezone.utc)
        active_states = (
            ApplicationState.interviewing,
            ApplicationState.offer,
            ApplicationState.replied,
        )
        active_order = {ApplicationState.offer: 0, ApplicationState.interviewing: 1,
                        ApplicationState.replied: 2}
        ladder = [
            ApplicationState.found, ApplicationState.queued, ApplicationState.applied,
            ApplicationState.interviewing, ApplicationState.offer, ApplicationState.replied,
            ApplicationState.rejected, ApplicationState.ghosted,
        ]
        with get_session(get_engine(db_path)) as session:
            rows = session.execute(
                select(Application, Job, Company)
                .join(Job, Application.job_id == Job.id)
                .join(Company, Job.company_id == Company.id)
            ).all()
            last_event = dict(
                session.execute(
                    select(
                        ApplicationStateEvent.application_id,
                        func.max(ApplicationStateEvent.created_at),
                    ).group_by(ApplicationStateEvent.application_id)
                ).all()
            )
            counts = {s: 0 for s in ladder}
            active_apps, stale_apps = [], []
            for application, job, company in rows:
                counts[application.state] = counts.get(application.state, 0) + 1
                if application.state in active_states:
                    active_apps.append((application, job, company))
                elif application.state == ApplicationState.applied:
                    la = last_event.get(application.id) or application.applied_at
                    days = _age_days(la, now)
                    if days is not None and days >= _STALE_APPLIED_DAYS:
                        stale_apps.append((application, job, company, days))
            active_apps.sort(key=lambda r: active_order.get(r[0].state, 9))
            stale_apps.sort(key=lambda r: -r[3])

            top_fit = session.execute(
                select(Job, Company)
                .join(Company, Job.company_id == Company.id)
                .outerjoin(Application, Application.job_id == Job.id)
                .where(Job.fit_score >= 70, Application.id.is_(None))
                .order_by(Job.fit_score.desc())
                .limit(5)
            ).all()
            last_discovered = session.scalar(select(func.max(Job.created_at)))

            total_jobs = session.scalar(select(func.count(Job.id))) or 0
            total_apps = sum(counts.values())
            pipeline = [(s.value, counts.get(s, 0)) for s in ladder]
            body = render(
                "/",
                "home.html",
                needs_setup=False,
                has_data=(total_jobs > 0 or total_apps > 0),
                has_apps=(total_apps > 0),
                active_apps=active_apps,
                stale_apps=stale_apps,
                pipeline=pipeline,
                top_fit=top_fit,
                last_discovered=last_discovered.strftime("%Y-%m-%d") if last_discovered else "never",
                schedule_detail=_schedule_status().detail,
            )
        return body

    @app.get("/preferences", response_class=HTMLResponse)
    def preferences(saved: str = ""):
        if not _profile_ready():
            return RedirectResponse("/", status_code=303)
        flash = ""
        if saved == "prefs":
            flash = (
                '<div class="flash ok">Saved. Stricter filters take effect on stored '
                "jobs after <code>copilot jobs prune</code>.</div>"
            )
        elif saved == "packet":
            flash = '<div class="flash ok">Base packet regenerated.</div>'
        return render_prefs(flash)

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
        if not _profile_ready():
            return RedirectResponse("/", status_code=303)
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
        return RedirectResponse("/preferences?saved=prefs", status_code=303)

    @app.post("/packet/base")
    def packet_base():
        if not _profile_ready():
            return RedirectResponse("/", status_code=303)
        from copilot.packet import write_base_packet

        with open(profile_path) as f:
            raw = pyyaml.safe_load(f)
        profile = Profile.model_validate(raw)
        write_base_packet(profile, path=packet_path)
        return RedirectResponse("/preferences?saved=packet", status_code=303)

    @app.get("/jobs", response_class=HTMLResponse)
    def jobs_list(
        min_fit: str = "", location: str = "", max_age: str | None = None,
        sort: str = "fit", hide_tracked: bool = False, limit: int = 50,
    ):
        if not _profile_ready():
            return RedirectResponse("/", status_code=303)
        with open(profile_path) as f:
            raw = pyyaml.safe_load(f)
        profile = Profile.model_validate(raw)
        if sort not in ("fit", "new", "salary"):
            sort = "fit"

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
            if hide_tracked:
                # Focus on what you haven't acted on yet - drop jobs that
                # already have an application row.
                stmt = stmt.where(~Job.application.has())
            jobs = session.scalars(stmt).all()
            total = len(jobs)
            scored = sum(1 for j in jobs if j.fit_score is not None)

            if sort == "new":
                ranked = sorted(jobs, key=_posting_sort_key, reverse=True)
            elif sort == "salary":
                ranked = sorted(jobs, key=lambda j: (j.salary_max or j.salary_min or 0), reverse=True)
            else:
                ranked = rank_jobs(jobs, profile, Geocoder())
            ranked = ranked[:limit]
            max_age_value = str(age_ceiling) if age_ceiling is not None else ""

            show_more_url = ""
            if len(ranked) >= limit and total > len(ranked):
                show_more_url = (
                    f"/jobs?limit={limit + 50}&min_fit={html.escape(min_fit)}"
                    f"&location={html.escape(location)}&max_age={html.escape(max_age_value)}"
                    f"&sort={sort}&hide_tracked={'true' if hide_tracked else 'false'}"
                )
            body = render(
                "/jobs",
                "jobs.html",
                jobs=ranked,
                total=total,
                scored=scored,
                min_fit=min_fit,
                location=location,
                max_age=max_age_value,
                sort=sort,
                hide_tracked=hide_tracked,
                show_more_url=show_more_url,
            )
        return body

    @app.get("/applications", response_class=HTMLResponse)
    def applications_list() -> str:
        from copilot.db.models import ApplicationStateEvent

        state_order = {
            ApplicationState.interviewing: 0,
            ApplicationState.offer: 1,
            ApplicationState.replied: 2,
            ApplicationState.applied: 3,
            ApplicationState.queued: 4,
            ApplicationState.found: 5,
            ApplicationState.rejected: 6,
            ApplicationState.ghosted: 7,
        }
        with get_session(get_engine(db_path)) as session:
            rows = session.execute(
                select(Application, Job, Company)
                .join(Job, Application.job_id == Job.id)
                .join(Company, Job.company_id == Company.id)
            ).all()
            last_activity = dict(
                session.execute(
                    select(
                        ApplicationStateEvent.application_id,
                        func.max(ApplicationStateEvent.created_at),
                    ).group_by(ApplicationStateEvent.application_id)
                ).all()
            )
            enriched = [
                (application, job, company, last_activity.get(application.id))
                for application, job, company in rows
            ]
            enriched.sort(
                key=lambda r: (
                    state_order.get(r[0].state, 9),
                    -(r[3].timestamp() if r[3] is not None else 0),
                )
            )
            body = render("/applications", "applications.html", rows=enriched)
        return body

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_detail(job_id: int):
        with get_session(get_engine(db_path)) as session:
            job = session.scalar(select(Job).where(Job.id == job_id))
            if job is None:
                return HTMLResponse(
                    render(
                        "/jobs",
                        "placeholder.html",
                        heading="Job not found",
                        blurb=f"No job with id {job_id}.",
                        detail="",
                    ),
                    status_code=404,
                )
            application = session.scalar(select(Application).where(Application.job_id == job_id))
            dimensions = [
                ("Skill match", job.skill_match_score),
                ("Experience level", job.experience_level_score),
                ("Domain fit", job.domain_fit_score),
                ("Location fit", job.location_fit_score),
                ("Visa feasibility", job.visa_feasibility_score),
            ]
            sponsorship = None
            if job.company and (
                job.company.sponsorship_status.value != "unknown"
                or job.company.sponsorship_evidence
            ):
                sponsorship = {
                    "status": job.company.sponsorship_status.value,
                    "evidence": job.company.sponsorship_evidence,
                }
            body = render(
                "/jobs",
                "job_detail.html",
                job=job,
                application=application,
                state_value=application.state.value if application else "found",
                states=[s.value for s in ApplicationState],
                dimensions=dimensions,
                sponsorship=sponsorship,
                jd_text=_clean_jd(job.jd_text),
            )
        return HTMLResponse(body)

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

    @app.post("/jobs/{job_id}/untrack")
    def job_untrack(job_id: int):
        from copilot.applications import untrack

        with get_session(get_engine(db_path)) as session:
            try:
                untrack(session, job_id)
                session.commit()
            except LookupError as exc:
                return HTMLResponse(str(exc), status_code=404)
        # The job page may no longer exist (email-created jobs are deleted with
        # their tracking), so land on the list either way.
        return RedirectResponse("/jobs", status_code=303)

    @app.post("/jobs/{job_id}/set-state")
    def job_set_state(job_id: int, state: str = Form(...)):
        from copilot.applications import set_state

        try:
            to_state = ApplicationState(state)
        except ValueError:
            return HTMLResponse(f"'{html.escape(state)}' is not a state.", status_code=400)
        with get_session(get_engine(db_path)) as session:
            if session.scalar(select(Job).where(Job.id == job_id)) is None:
                return HTMLResponse(f"No job with id {job_id}.", status_code=404)
            set_state(session, job_id, to_state)
            session.commit()
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.post("/schedule/install")
    def schedule_install():
        from copilot.schedule.launchd import install

        try:
            install(Path.cwd())
        except Exception as exc:  # noqa: BLE001 - shown on the page
            return HTMLResponse(
                render_integrations(f'<div class="flash err">Install failed: {html.escape(str(exc))}</div>')
            )
        return RedirectResponse("/integrations", status_code=303)

    @app.post("/schedule/pause")
    def schedule_pause():
        from copilot.schedule.launchd import pause

        try:
            pause()
        except Exception as exc:  # noqa: BLE001 - shown on the page
            return HTMLResponse(
                render_integrations(f'<div class="flash err">Pause failed: {html.escape(str(exc))}</div>')
            )
        return RedirectResponse("/integrations", status_code=303)

    @app.post("/schedule/resume")
    def schedule_resume():
        from copilot.schedule.launchd import resume

        try:
            resume()
        except Exception as exc:  # noqa: BLE001 - shown on the page
            return HTMLResponse(
                render_integrations(f'<div class="flash err">Resume failed: {html.escape(str(exc))}</div>')
            )
        return RedirectResponse("/integrations", status_code=303)

    @app.post("/config/env")
    def config_env(
        ANTHROPIC_API_KEY: str = Form(""),
        ADZUNA_APP_ID: str = Form(""),
        ADZUNA_APP_KEY: str = Form(""),
        JSEARCH_API_KEY: str = Form(""),
        AZURE_CLIENT_ID: str = Form(""),
    ):
        """Write the filled-in keys to .env and apply them live (D4 localhost).
        Blank fields are left untouched, so one form can update a subset."""
        from copilot.envfile import set_env

        submitted = {
            "ANTHROPIC_API_KEY": ANTHROPIC_API_KEY,
            "ADZUNA_APP_ID": ADZUNA_APP_ID,
            "ADZUNA_APP_KEY": ADZUNA_APP_KEY,
            "JSEARCH_API_KEY": JSEARCH_API_KEY,
            "AZURE_CLIENT_ID": AZURE_CLIENT_ID,
        }
        changed = [k for k, v in submitted.items() if v.strip()]
        for key in changed:
            set_env(key, submitted[key])
        if not changed:
            return HTMLResponse(
                render_integrations('<div class="flash err">Nothing entered - no keys changed.</div>')
            )
        return RedirectResponse("/integrations", status_code=303)

    @app.post("/config/resume")
    async def config_resume(file: UploadFile = File(...)):
        """Replace the resume file at profile.yaml's resume_path. The upload's
        extension must match so the parser (pdf vs docx) can't be fed a mismatch."""
        if not _profile_ready():
            return RedirectResponse("/", status_code=303)
        with open(profile_path) as f:
            profile = Profile.model_validate(pyyaml.safe_load(f))
        target = Path(profile.resume_path)
        up_suffix = Path(file.filename or "").suffix.lower()
        if not up_suffix:
            return HTMLResponse(
                render_integrations('<div class="flash err">That file has no extension - upload a .pdf or .docx.</div>')
            )
        if up_suffix != target.suffix.lower():
            return HTMLResponse(render_integrations(
                f'<div class="flash err">Your profile expects a <code>{html.escape(target.suffix)}</code> '
                f"resume at <code>{html.escape(str(target))}</code>. Upload a "
                f"<code>{html.escape(target.suffix)}</code> file, or change "
                "<code>resume_path</code> in Preferences first.</div>"
            ))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(await file.read())
        return RedirectResponse("/integrations", status_code=303)

    @app.get("/suggestions", response_class=HTMLResponse)
    def suggestions_list() -> str:
        from copilot.suggestions import list_pending

        with get_session(get_engine(db_path)) as session:
            cards = [(s, _link_options(session, s)) for s in list_pending(session)]
            body = render("/suggestions", "suggestions.html", cards=cards)
        return body

    def _try_provider():
        """Filing the email is a courtesy mirror of the DB write (D5) - if Graph
        isn't reachable (no client id, expired login), the confirm still
        proceeds and only the mailbox filing is skipped."""
        try:
            with open(profile_path) as f:
                raw = pyyaml.safe_load(f)
            from copilot.email_agent.provider import get_provider

            return get_provider(Profile.model_validate(raw))
        except Exception:  # noqa: BLE001
            return None

    # Serializes suggestion writes: FastAPI handles requests on a thread pool, so
    # a double-click/browser re-POST runs two confirms concurrently - both would
    # pass the check-before-insert dedupe and the loser 500s on the UNIQUE
    # constraint (seen live 2026-07-26). Behind the lock, the repeat sees
    # AlreadyResolved instead and becomes a harmless redirect.
    suggestion_write_lock = threading.Lock()

    @app.post("/suggestions/{suggestion_id}/confirm")
    def suggestion_confirm(suggestion_id: int, job_id: str = Form("")):
        from copilot.suggestions import AlreadyResolved, confirm

        # "" = the kind's default action; "new" = create from the email
        # (conflict's third option, D31); a number = link to that job.
        value = job_id.strip()
        as_new = value == "new"
        linked_job_id = int(value) if value and not as_new else None
        with suggestion_write_lock, get_session(get_engine(db_path)) as session:
            try:
                confirm(
                    session, suggestion_id, _try_provider(),
                    job_id=linked_job_id, as_new=as_new,
                )
                session.commit()
            except AlreadyResolved:
                pass  # repeat of a click that already succeeded - no-op
            except LookupError as exc:
                return HTMLResponse(str(exc), status_code=404)
            except ValueError as exc:
                return HTMLResponse(str(exc), status_code=400)
        return RedirectResponse("/suggestions", status_code=303)

    @app.post("/suggestions/{suggestion_id}/decline")
    def suggestion_decline(suggestion_id: int):
        from copilot.suggestions import AlreadyResolved, decline

        with suggestion_write_lock, get_session(get_engine(db_path)) as session:
            try:
                decline(session, suggestion_id)
                session.commit()
            except AlreadyResolved:
                pass  # repeat click - no-op
            except LookupError as exc:
                return HTMLResponse(str(exc), status_code=404)
        return RedirectResponse("/suggestions", status_code=303)

    @app.get("/integrations", response_class=HTMLResponse)
    def integrations() -> str:
        return render_integrations()

    @app.get("/usage", response_class=HTMLResponse)
    def usage() -> str:
        from copilot.api_usage import get_jsearch_quota
        from copilot.setup_status import get_setup_status

        graph_check = get_setup_status(profile_path).get("email")
        metered, free = _usage_rows(get_jsearch_quota(), bool(graph_check and graph_check.ok))
        return render("/usage", "usage.html", metered=metered, free=free)

    return app
