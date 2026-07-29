"""Career Copilot CLI. Run 'copilot --help' for commands."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from copilot.config import DATA_DIR, PROFILE_FILENAME, load_profile

load_dotenv()

app = typer.Typer(help="Job-search copilot. You always click submit.")
profile_app = typer.Typer(help="Your profile and resume.")
jobs_app = typer.Typer(help="Discovered jobs.")
email_app = typer.Typer(help="Email login and sending (Microsoft Graph).")
packet_app = typer.Typer(help="Application packet (reusable content, not per-job).")
inbox_app = typer.Typer(help="Inbox monitoring: sync, review suggestions, confirm/decline.")
schedule_app = typer.Typer(help="Hourly background sync via launchd (fires missed runs on wake).")
app.add_typer(profile_app, name="profile")
app.add_typer(jobs_app, name="jobs")
app.add_typer(email_app, name="email")
app.add_typer(packet_app, name="packet")
app.add_typer(inbox_app, name="inbox")
app.add_typer(schedule_app, name="schedule")

console = Console()


@app.command()
def init() -> None:
    """Set up the project: data directory, profile.yaml, and database."""
    DATA_DIR.mkdir(exist_ok=True)

    profile_path = Path(PROFILE_FILENAME)
    if profile_path.exists():
        console.print(f"[yellow]{PROFILE_FILENAME} already exists, leaving it alone.[/]")
    else:
        shutil.copy("profile.yaml.example", profile_path)
        console.print(f"[green]Created {PROFILE_FILENAME}[/] - edit it with your details.")

    from copilot.db import get_engine, init_db

    init_db(get_engine())
    console.print("[green]Database ready[/] at data/copilot.db")
    console.print(
        "\nNext steps:\n"
        "  1. Put your resume at data/resume.pdf\n"
        "  2. Set ANTHROPIC_API_KEY (e.g. in a .env file)\n"
        "  3. Run: copilot profile fill            (auto-fills identity from your resume)\n"
        "  4. Run: copilot profile suggest-titles  (optional: propose search.titles)\n"
        f"  5. Edit {PROFILE_FILENAME}: fill in search, visa, and email_integration by hand\n"
        "  6. Run: copilot profile show"
    )


@profile_app.command("fill")
def profile_fill_cmd() -> None:
    """Auto-fill identity (name, contact, links) in profile.yaml from your resume.

    search/visa/email_integration are your own preferences and are left alone -
    edit those by hand.
    """
    profile_path = Path(PROFILE_FILENAME)
    if not profile_path.exists():
        console.print(f"[red]{PROFILE_FILENAME} not found.[/] Run 'copilot init' first.")
        raise typer.Exit(1)

    profile = load_profile()

    from copilot.profile_fill import fill_identity
    from copilot.resume import extract_resume_profile

    with console.status("Reading resume..."):
        resume = extract_resume_profile(profile)
        changed = fill_identity(profile_path, resume)

    if changed:
        console.print(f"[green]Filled from resume:[/] {', '.join(changed)}")
    else:
        console.print("[yellow]Nothing new to fill[/] - identity already matches the resume.")
    console.print(
        "[dim]search, visa, and email_integration are your preferences, not resume "
        f"facts - edit those by hand in {PROFILE_FILENAME}.[/]"
    )


@profile_app.command("suggest-titles")
def profile_suggest_titles_cmd(
    apply_: bool | None = typer.Option(
        None,
        "--apply/--no-apply",
        help="Skip the confirmation prompt: apply automatically, or just show and exit.",
    ),
) -> None:
    """Suggest target job titles from your resume plus a live search for what's
    currently in demand. Always shown first - profile.yaml is only touched if
    you say yes (or pass --apply)."""
    profile_path = Path(PROFILE_FILENAME)
    if not profile_path.exists():
        console.print(f"[red]{PROFILE_FILENAME} not found.[/] Run 'copilot init' first.")
        raise typer.Exit(1)

    profile = load_profile()

    from copilot.resume import extract_resume_profile
    from copilot.title_suggestions import suggest_target_titles

    with console.status("Reading resume and searching the current job market..."):
        resume = extract_resume_profile(profile)
        suggestions = suggest_target_titles(profile, resume)

    table = Table(title="Suggested target titles")
    table.add_column("#", width=3)
    table.add_column("Title", style="bold")
    table.add_column("Why it fits + demand")
    for i, s in enumerate(suggestions, 1):
        why = s.reasoning + (f" [dim]({s.demand_signal})[/]" if s.demand_signal else "")
        table.add_row(str(i), s.title, why)
    console.print(table)

    console.print(
        f"[dim]Current search.titles in {PROFILE_FILENAME}: "
        f"{', '.join(profile.search.titles)}[/]"
    )

    should_apply = apply_
    if should_apply is None:
        should_apply = typer.confirm(
            f"Replace search.titles in {PROFILE_FILENAME} with these "
            f"{len(suggestions)} titles?",
            default=False,
        )

    if not should_apply:
        console.print(f"[yellow]Not applied.[/] {PROFILE_FILENAME} unchanged.")
        return

    from copilot.profile_fill import set_search_titles

    set_search_titles(profile_path, [s.title for s in suggestions])
    console.print(f"[green]Updated search.titles[/] in {PROFILE_FILENAME}.")


@profile_app.command("show")
def profile_show(
    refresh: bool = typer.Option(
        False, "--refresh", help="Re-extract the resume even if cached."
    ),
) -> None:
    """Show the structured profile Claude extracted from your resume."""
    profile = load_profile()

    from copilot.resume import extract_resume_profile

    with console.status("Reading resume..."):
        resume = extract_resume_profile(profile, force=refresh)

    console.print(
        Panel(
            f"[bold]{resume.full_name}[/] - {resume.seniority} "
            f"({resume.years_of_experience:g} yrs)\n{resume.summary}",
            title="Resume Profile",
        )
    )

    console.print(f"[bold]Skills:[/] {', '.join(resume.skills)}")
    console.print(f"[bold]Domains:[/] {', '.join(resume.domains)}")

    table = Table(title="Experience")
    table.add_column("Title")
    table.add_column("Company")
    table.add_column("When")
    for role in resume.work_experience:
        when = f"{role.start or '?'} to {role.end or 'present'}"
        table.add_row(role.title, role.company, when)
    console.print(table)

    if resume.education:
        edu = ", ".join(
            f"{e.degree}, {e.institution}"
            + (f" ({e.graduation_year})" if e.graduation_year else "")
            for e in resume.education
        )
        console.print(f"[bold]Education:[/] {edu}")
    if resume.certifications:
        console.print(f"[bold]Certifications:[/] {', '.join(resume.certifications)}")

    console.print(
        f"\n[dim]Search targets: {', '.join(profile.search.titles)} | "
        f"visa: {profile.visa.status.value} | "
        f"location prefs: {', '.join(profile.search.location_preference) or 'none'}[/]"
    )


@app.command()
def discover(
    since: str = typer.Option(
        "week",
        "--since",
        help="JSearch posting-age window: today | 3days | week | month | all. "
        "Use 'month' or 'all' for a one-time backfill; 'week' (default) for "
        "regular runs.",
    ),
) -> None:
    """Search all sources for every title/location in your profile and save
    new postings to the database. Safe to re-run - duplicates are skipped,
    not re-added."""
    profile_path = Path(PROFILE_FILENAME)
    if not profile_path.exists():
        console.print(f"[red]{PROFILE_FILENAME} not found.[/] Run 'copilot init' first.")
        raise typer.Exit(1)

    profile = load_profile()

    adzuna_app_id = os.environ.get("ADZUNA_APP_ID")
    adzuna_app_key = os.environ.get("ADZUNA_APP_KEY")
    jsearch_api_key = os.environ.get("JSEARCH_API_KEY")

    if not (adzuna_app_id and adzuna_app_key) and not jsearch_api_key:
        console.print(
            "[red]No discovery source configured.[/] Set ADZUNA_APP_ID + ADZUNA_APP_KEY "
            "and/or JSEARCH_API_KEY in .env."
        )
        raise typer.Exit(1)

    from copilot.db import get_engine, get_session, init_db
    from copilot.discovery.pipeline import resolve_and_poll_ats, run_discovery

    engine = get_engine()
    init_db(engine)

    titles = ", ".join(profile.search.titles)
    locations = ", ".join(profile.search.locations)
    with get_session(engine) as session:
        with console.status(f"Searching for {titles} in {locations}..."):
            summary = run_discovery(
                profile,
                session,
                adzuna_app_id=adzuna_app_id,
                adzuna_app_key=adzuna_app_key,
                jsearch_api_key=jsearch_api_key,
                jsearch_date_posted=since,
            )

        console.print(
            f"[green]Found {summary.found}[/] postings - "
            f"[green]{summary.added} new[/], "
            f"[dim]{summary.duplicates} already known[/], "
            f"[yellow]{summary.dealbreakers_dropped} dropped (dealbreaker)[/]"
        )
        for source in summary.quota_exhausted:
            console.print(
                f"[yellow]{source} has reached its usage limit[/] - no jobs pulled "
                "from it this run. The other sources were still searched, and "
                f"{source} resumes automatically when its limit resets."
            )
        for err in summary.errors:
            console.print(f"[red]Error:[/] {err}")

        with console.status("Checking companies for public ATS job boards..."):
            ats = resolve_and_poll_ats(profile, session)

        from copilot.discovery.pipeline import (
            classify_new_companies,
            geocode_missing_coordinates,
        )

        with console.status("Geocoding job locations (cached after first run)..."):
            geocoded = geocode_missing_coordinates(session)
        if geocoded:
            console.print(f"[dim]Geocoded {geocoded} job locations.[/]")

        with console.status("Classifying new companies by industry (one batched call)..."):
            try:
                classified = classify_new_companies(profile, session)
            except Exception as exc:  # noqa: BLE001 - classification is enrichment, not critical path
                classified = 0
                console.print(f"[red]Industry classification failed:[/] {exc}")
        if classified:
            console.print(f"[dim]Classified {classified} new companies by industry.[/]")

    if ats.companies_probed or ats.boards_polled:
        console.print(
            f"[green]ATS boards:[/] probed {ats.companies_probed} new companies, "
            f"found {ats.boards_found} boards, polled {ats.boards_polled} - "
            f"[green]{ats.links_upgraded} jobs upgraded to direct links[/], "
            f"[green]{ats.board_jobs_added} added from boards[/]"
        )
    for err in ats.errors:
        console.print(f"[red]ATS error:[/] {err}")

    console.print("[dim]Run 'copilot jobs list' to see them.[/]")


@app.command()
def score(
    limit: int = typer.Option(25, "--limit", help="Max unscored jobs to score this run."),
    batch_size: int = typer.Option(8, "--batch-size", help="Jobs per Claude call."),
) -> None:
    """Score unscored jobs (fit_score is null) against your resume: a 0-100 fit
    score with written reasoning, plus each JD's explicit visa/sponsorship signal.
    Costs one batched Claude call per --batch-size jobs. Safe to re-run - already
    -scored jobs are skipped, so this only ever scores what's new."""
    profile_path = Path(PROFILE_FILENAME)
    if not profile_path.exists():
        console.print(f"[red]{PROFILE_FILENAME} not found.[/] Run 'copilot init' first.")
        raise typer.Exit(1)

    profile = load_profile()

    from sqlalchemy import select

    from copilot.db import get_engine, get_session
    from copilot.db.models import Job
    from copilot.resume import extract_resume_profile
    from copilot.scoring.rubric import score_and_apply

    with console.status("Reading resume..."):
        resume = extract_resume_profile(profile)

    engine = get_engine()
    with get_session(engine) as session:
        stmt = (
            select(Job)
            .where(Job.fit_score.is_(None))
            .order_by(Job.created_at.desc())
            .limit(limit)
        )
        jobs = session.scalars(stmt).all()

        if not jobs:
            console.print(
                "[yellow]Nothing to score.[/] All discovered jobs already have a fit score."
            )
            return

        with console.status(f"Scoring {len(jobs)} jobs against your resume..."):
            result = score_and_apply(session, profile, resume, jobs, batch_size=batch_size)

    console.print(f"[green]Scored {result.scored}[/] jobs.")
    if result.missing:
        console.print(
            f"[yellow]{result.missing} jobs got no score back[/] - left unscored, "
            "re-run 'copilot score' to retry them."
        )
    console.print("[dim]Run 'copilot jobs list' to see them ranked by fit.[/]")


@app.command("sponsorship-sync")
def sponsorship_sync(
    refresh: bool = typer.Option(
        False, "--refresh",
        help="Probe USCIS for a newer fiscal year than what's cached and re-download.",
    ),
) -> None:
    """Match your companies against the public USCIS H-1B Employer Data Hub to
    populate each company's H1B filing history. Deterministic, no LLM calls -
    safe to re-run any time new companies are discovered. This is historical,
    company-wide evidence (not a live policy) and is never folded into
    fit_score - see it via 'copilot jobs show <id>'."""
    from copilot.db import get_engine, get_session
    from copilot.scoring.sponsorship import (
        current_cached_year, download_data, h1b_cache_path, refresh_h1b_data, sync_sponsorship_data,
    )

    if refresh:
        with console.status("Checking USCIS for a newer H-1B Employer Data Hub report..."):
            year, found_newer = refresh_h1b_data()
        if found_newer:
            console.print(f"[green]Found and switched to FY{year} data.[/]")
        else:
            console.print(f"[dim]No newer report published yet - re-verified FY{year} data.[/]")
    else:
        year = current_cached_year()
        with console.status("Downloading USCIS H-1B Employer Data Hub (cached after first run)..."):
            download_data(year=year)

    cache_path = h1b_cache_path(year)
    with get_session(get_engine()) as session:
        with console.status("Matching companies against filing data..."):
            summary = sync_sponsorship_data(session, cache_path=cache_path, year=year)

    console.print(
        f"[green]Matched {summary.matched}[/] of {summary.companies_checked} companies "
        f"against FY{year} H1B filing data."
    )
    console.print(
        "[dim]Historical, company-wide evidence, not a live policy - see "
        "'copilot jobs show <id>' for a company's filing history alongside its fit score.[/]"
    )


@packet_app.command("base")
def packet_base_cmd() -> None:
    """(Re)generate data/packet_base.md: contact details, H1B-transfer
    work-authorization Q&A, and a resume summary - the content that's the
    same for every application, so it's written once and reused rather than
    regenerated per job (docs/DECISIONS.md D22, D23). Only ever runs when you
    ask - re-run this yourself after editing your resume or profile.yaml."""
    if not Path(PROFILE_FILENAME).exists():
        console.print(f"[red]{PROFILE_FILENAME} not found.[/] Run 'copilot init' first.")
        raise typer.Exit(1)

    from copilot.packet import PACKET_BASE_PATH, write_base_packet

    profile = load_profile()
    with console.status("Building base packet (re-extracts your resume only if it changed)..."):
        write_base_packet(profile)
    console.print(f"[green]Wrote {PACKET_BASE_PATH}[/]")


@email_app.command("login")
def email_login() -> None:
    """One-time interactive Microsoft sign-in (device-code flow): prints a
    URL and a short code, waits for you to enter it in a browser. The
    resulting token is cached in data/email_token.json and silently
    refreshed after that - you shouldn't need to run this often."""
    import os

    client_id = os.environ.get("AZURE_CLIENT_ID")
    if not client_id:
        console.print(
            "[red]AZURE_CLIENT_ID not set in .env.[/] See README for the Azure app "
            "registration steps."
        )
        raise typer.Exit(1)

    from copilot.email_agent.outlook import login

    try:
        login(client_id, on_prompt=lambda msg: console.print(f"[bold]{msg}[/]"))
    except Exception as exc:  # noqa: BLE001 - shown to the user directly
        console.print(f"[red]Login failed:[/] {exc}")
        raise typer.Exit(1) from exc

    console.print("[green]Signed in.[/] Run 'copilot email test' to confirm sending works.")


@email_app.command("test")
def email_test() -> None:
    """Send a one-off test email to your configured address, to verify Graph
    auth and sending both actually work before relying on the daily digest."""
    profile_path = Path(PROFILE_FILENAME)
    if not profile_path.exists():
        console.print(f"[red]{PROFILE_FILENAME} not found.[/] Run 'copilot init' first.")
        raise typer.Exit(1)

    profile = load_profile()

    from copilot.email_agent.provider import get_provider

    try:
        provider = get_provider(profile)
        provider.send_mail(
            to=profile.email_integration.address,
            subject="Career Copilot - test email",
            body_html="<p>If you're reading this, Graph auth and sending both work.</p>",
        )
    except Exception as exc:  # noqa: BLE001 - shown to the user directly
        console.print(f"[red]Send failed:[/] {exc}")
        raise typer.Exit(1) from exc

    console.print(f"[green]Sent.[/] Check {profile.email_integration.address}.")


@inbox_app.command("sync")
def inbox_sync(
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show what would happen without writing state, moving mail, or "
        "alerting. Classification still runs (it IS the answer to 'what "
        "would happen'), so Claude calls are still spent.",
    ),
) -> None:
    """Read recent inbox mail, classify job-related messages, update
    application state (D24 rules), file emails into the Career Copilot
    folder, and queue suggestions for anything needing your decision."""
    profile = load_profile()

    from copilot.db import get_engine, get_session, init_db
    from copilot.email_agent.provider import get_provider
    from copilot.email_agent.sync import run_sync

    engine = get_engine()
    init_db(engine)
    try:
        provider = get_provider(profile)
        with get_session(engine) as session:
            result = run_sync(session, provider, profile, dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001 - shown to the user directly
        console.print(f"[red]Sync failed:[/] {exc}")
        raise typer.Exit(1) from exc

    if dry_run:
        console.print("[dim]--dry-run: nothing was written, moved, or sent.[/]")
    console.print(
        f"Fetched {result.fetched} message{'s' if result.fetched != 1 else ''}: "
        f"{result.skipped_prefilter} not job mail, {result.skipped_seen} already handled."
    )
    for action in result.actions:
        console.print(f"  - {action}")
    if dry_run and result.skipped_subjects:
        # A wrong pre-filter skip leaves no other trace - this list is the
        # only way to audit its recall. Spot a job email here? It's a
        # pattern we should add.
        console.print("[dim]Skipped as not job mail:[/]")
        for line in result.skipped_subjects:
            console.print(f"  [dim]- {line}[/]")
    if result.suggestions or result.conflicts:
        console.print(
            f"[yellow]{result.suggestions + result.conflicts} suggestion(s) awaiting review[/]"
            " - run 'copilot inbox review'."
        )


@inbox_app.command("review")
def inbox_review() -> None:
    """List pending suggestions: untracked applications the email agent
    noticed, and state conflicts it refused to resolve on its own."""
    from copilot.db import get_engine, get_session
    from copilot.suggestions import list_pending

    with get_session(get_engine()) as session:
        pending = list_pending(session)
        if not pending:
            console.print("Nothing pending. All caught up.")
            return
        table = Table(title="Pending suggestions")
        table.add_column("ID", justify="right")
        table.add_column("Kind")
        table.add_column("Company")
        table.add_column("Would set")
        table.add_column("Email")
        for s in pending:
            table.add_row(
                str(s.id),
                "conflict" if s.kind.value == "state_conflict" else "untracked",
                s.company_name or "?",
                s.proposed_state.value if s.proposed_state else "-",
                (s.subject or "")[:60],
            )
        console.print(table)
        console.print("confirm with 'copilot inbox confirm <id>', decline with 'inbox decline <id>'")


@inbox_app.command("confirm")
def inbox_confirm(
    suggestion_id: int,
    job: int | None = typer.Option(
        None,
        "--job",
        help="Link this email to an existing tracked job (see 'copilot jobs "
        "list') instead of creating a new company/job from the email - for "
        "when you know which application it belongs to and the agent didn't.",
    ),
    new: bool = typer.Option(
        False,
        "--new",
        help="For a state conflict: the email is about a different, NEW "
        "application - create it from the email and leave the flagged "
        "application untouched.",
    ),
) -> None:
    """Apply a suggestion: create the tracked application (untracked kind)
    or adopt the email's implied state (conflict kind), then file the email.
    With --job, attach the email to a job you already track; with --new,
    resolve a conflict by creating a separate new application instead."""
    profile = load_profile()

    from copilot.db import get_engine, get_session
    from copilot.email_agent.provider import get_provider
    from copilot.suggestions import confirm

    with get_session(get_engine()) as session:
        try:
            provider = get_provider(profile)
            suggestion = confirm(session, suggestion_id, provider, job_id=job, as_new=new)
            session.commit()
        except (LookupError, ValueError) as exc:
            console.print(f"[red]{exc}[/]")
            raise typer.Exit(1) from exc
    if job is not None:
        console.print(f"[green]Confirmed.[/] Email recorded against job {job}.")
    elif new:
        console.print(
            f"[green]Confirmed.[/] Created a new application for "
            f"{suggestion.company_name or '?'}; the flagged one is untouched."
        )
    else:
        console.print(
            f"[green]Confirmed.[/] {suggestion.company_name or '?'} is now tracked"
            + (f" at state '{suggestion.proposed_state.value}'." if suggestion.proposed_state else ".")
        )


@inbox_app.command("decline")
def inbox_decline(suggestion_id: int) -> None:
    """Decline a suggestion: nothing is created or changed, and this email
    will never be suggested again."""
    from copilot.db import get_engine, get_session
    from copilot.suggestions import decline

    with get_session(get_engine()) as session:
        try:
            decline(session, suggestion_id)
            session.commit()
        except (LookupError, ValueError) as exc:
            console.print(f"[red]{exc}[/]")
            raise typer.Exit(1) from exc
    console.print("[green]Declined.[/] Nothing was changed.")


@schedule_app.command("install")
def schedule_install() -> None:
    """Install the hourly launchd job (inbox sync + digest). Missed runs
    fire when the laptop wakes - no need to stay on."""
    from copilot.schedule.launchd import install

    try:
        plist = install(Path.cwd())
    except Exception as exc:  # noqa: BLE001 - shown to the user directly
        console.print(f"[red]Install failed:[/] {exc}")
        raise typer.Exit(1) from exc
    console.print(f"[green]Installed.[/] {plist}\nRuns hourly; logs in data/logs/.")


@schedule_app.command("status")
def schedule_status() -> None:
    """Show whether the hourly job is installed, loaded, and its last exit."""
    from copilot.schedule.launchd import status

    s = status()
    color = "green" if s.loaded else ("yellow" if s.installed else "red")
    console.print(f"[{color}]{s.detail}[/]")


@schedule_app.command("pause")
def schedule_pause() -> None:
    """Stop scheduled runs without uninstalling (resume brings them back)."""
    from copilot.schedule.launchd import pause

    try:
        pause()
    except Exception as exc:  # noqa: BLE001 - shown to the user directly
        console.print(f"[red]Pause failed:[/] {exc}")
        raise typer.Exit(1) from exc
    console.print("[yellow]Paused.[/] 'copilot schedule resume' to restart.")


@schedule_app.command("resume")
def schedule_resume() -> None:
    """Resume scheduled runs after a pause."""
    from copilot.schedule.launchd import resume

    try:
        resume()
    except Exception as exc:  # noqa: BLE001 - shown to the user directly
        console.print(f"[red]Resume failed:[/] {exc}")
        raise typer.Exit(1) from exc
    console.print("[green]Resumed.[/] Runs hourly again.")


@schedule_app.command("uninstall")
def schedule_uninstall() -> None:
    """Remove the launchd job and its plist entirely."""
    from copilot.schedule.launchd import uninstall

    if uninstall():
        console.print("[green]Uninstalled.[/]")
    else:
        console.print("[yellow]Nothing installed.[/]")


def _html_to_text(html_body: str) -> str:
    """Rough HTML-to-text for the digest dry-run preview - so `--dry-run`
    shows the actual content (State changes, high-fit table) the way inbox
    sync's dry-run does, not just the subject line. Not a general converter:
    it only needs to handle the tags build_digest emits."""
    import re
    from html import unescape

    text = html_body
    text = re.sub(r"</(p|h3|tr|table|ul|li)>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<li>", "  - ", text, flags=re.IGNORECASE)
    text = re.sub(r"<t[dh][^>]*>", " | ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = unescape(text)
    lines = [line.strip(" |") for line in text.splitlines()]
    return "\n".join(line for line in lines if line.strip())


@app.command()
def digest(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Build and print the digest without sending or marking it sent."
    ),
    if_due: bool = typer.Option(
        False,
        "--if-due",
        help="Skip unless the last digest is 20+ hours old. The hourly "
        "scheduled job uses this so the digest stays daily while inbox "
        "sync stays hourly; running 'copilot digest' yourself never skips.",
    ),
) -> None:
    """Email a digest of jobs discovered since the last digest, with fit_score
    80+ jobs called out (apply link, salary, location). Safe to run on any
    schedule - only marks itself sent after a successful send, and does
    nothing (no empty email) if there's nothing new."""
    profile_path = Path(PROFILE_FILENAME)
    if not profile_path.exists():
        console.print(f"[red]{PROFILE_FILENAME} not found.[/] Run 'copilot init' first.")
        raise typer.Exit(1)

    profile = load_profile()

    from copilot.db import get_engine, get_session
    from copilot.notify.digest import build_digest, digest_due, mark_digest_sent

    if if_due and not digest_due():
        console.print("[dim]Digest not due yet (sent within the last 20h). Skipping.[/]")
        return

    with get_session(get_engine()) as session:
        result = build_digest(session)

    if result is None:
        console.print("[yellow]Nothing new since the last digest.[/] No email sent.")
        return

    console.print(f"[bold]{result.subject}[/]")
    if dry_run:
        console.print(_html_to_text(result.body_html) or "[dim](empty body)[/]")
        console.print("[dim]--dry-run: not sending, not marking sent.[/]")
        return

    from copilot.email_agent.provider import get_provider

    try:
        provider = get_provider(profile)
        provider.send_mail(
            to=profile.email_integration.address,
            subject=result.subject,
            body_html=result.body_html,
        )
    except Exception as exc:  # noqa: BLE001 - shown to the user directly
        console.print(f"[red]Send failed:[/] {exc}")
        raise typer.Exit(1) from exc

    mark_digest_sent()
    console.print(f"[green]Sent.[/] Check {profile.email_integration.address}.")


@app.command()
def dashboard(
    port: int = typer.Option(8765, "--port", help="Port to serve on."),
    reload: bool = typer.Option(
        False, "--reload", help="Auto-restart on code changes (for development)."
    ),
) -> None:
    """Open the local web dashboard: home overview, jobs, applications, inbox
    suggestions, and preferences. Localhost only - nothing is exposed to the
    network."""
    profile_path = Path(PROFILE_FILENAME)
    if not profile_path.exists():
        console.print(
            f"[yellow]No {PROFILE_FILENAME} yet[/] - the dashboard will open on a setup "
            "page to guide you."
        )

    import uvicorn

    console.print(f"[green]Dashboard:[/] http://127.0.0.1:{port}  (Ctrl+C to stop)")
    if reload:
        # Reload needs an import string, not an instance; the factory picks up
        # the default profile.yaml in the working directory.
        uvicorn.run(
            "copilot.dashboard:create_app", factory=True, host="127.0.0.1", port=port,
            reload=True, log_level="warning",
        )
    else:
        from copilot.dashboard import create_app

        uvicorn.run(create_app(profile_path), host="127.0.0.1", port=port, log_level="warning")


@jobs_app.command("prune")
def jobs_prune(
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt."),
) -> None:
    """Re-apply your current dealbreakers and salary floor to jobs already in
    the database, deleting violators. No API calls - this reprocesses stored
    data, so it's how rule changes in profile.yaml take effect retroactively.
    Jobs you've applied to are never touched."""
    profile = load_profile()

    from copilot.db import get_engine, get_session
    from copilot.discovery.pipeline import prune_jobs

    floor = f"${profile.search.min_salary:,}" if profile.search.min_salary else "none"
    console.print(
        f"Rules: dealbreakers={profile.search.dealbreakers or 'none'}, salary floor={floor}"
    )
    if not yes and not typer.confirm("Delete stored jobs violating these rules?", default=False):
        console.print("[yellow]Nothing deleted.[/]")
        return

    with get_session(get_engine()) as session:
        summary = prune_jobs(profile, session)

    console.print(
        f"[green]Pruned[/] {summary.dealbreakers} dealbreaker matches, "
        f"{summary.below_salary_floor} below the salary floor."
    )


@jobs_app.command("list")
def jobs_list(
    min_salary: int | None = typer.Option(
        None, "--min-salary", help="Salary floor; defaults to search.min_salary in profile.yaml."
    ),
    min_fit: int | None = typer.Option(
        None, "--min-fit", help="Fit-score floor (0-100). Unscored jobs are always kept."
    ),
    max_age: int | None = typer.Option(
        None,
        "--max-age",
        help="Posting-age ceiling in days; defaults to search.max_posting_age_days in profile.yaml.",
    ),
    location: str | None = typer.Option(None, "--location", help="Substring match on location."),
    limit: int = typer.Option(25, "--limit", help="Max rows to show."),
    show_all: bool = typer.Option(
        False, "--all", help="Ignore the profile salary/posting-age floors and show everything."
    ),
) -> None:
    """List discovered jobs, best first: scored jobs rank by fit_score (highest
    first), with recency and your location_preference etc. as tiebreakers;
    unscored jobs follow, ranked the old way, until 'copilot score' reaches
    them. Jobs with unknown salary/fit/posting-date are always kept - floors
    only drop known-and-below values."""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select

    from copilot.db import get_engine, get_session
    from copilot.db.models import Job
    from copilot.formatting import format_posted, format_salary
    from copilot.geocode import Geocoder
    from copilot.ranking import (
        build_rules,
        industry_label,
        industry_tier,
        preference_tier,
        rank_jobs,
        tier_label,
    )

    profile = load_profile()
    floor = None if show_all else (min_salary if min_salary is not None else profile.search.min_salary)
    age_ceiling = (
        None if show_all else (max_age if max_age is not None else profile.search.max_posting_age_days)
    )
    geocoder = Geocoder()
    rules = build_rules(profile.search.location_preference, geocoder)
    industries = profile.search.industry_preference
    downrank_staffing = profile.search.deprioritize_staffing

    def ind_tier(j):
        return industry_tier(j, industries, downrank_staffing)

    engine = get_engine()
    with get_session(engine) as session:
        stmt = select(Job)
        if location:
            stmt = stmt.where(Job.location.ilike(f"%{location}%"))
        if floor is not None:
            # Drop only when the posting's best case is known to be below the
            # floor; unknown salary is kept, never dropped.
            stmt = stmt.where((Job.salary_max.is_(None)) | (Job.salary_max >= floor))
        if min_fit is not None:
            # Same "unknown, never dropped" rule as the salary floor: a job
            # simply not scored yet is not the same as a job that scored low.
            stmt = stmt.where((Job.fit_score.is_(None)) | (Job.fit_score >= min_fit))
        if age_ceiling is not None:
            # Live floor, not a one-time drop at discovery: a job's age keeps
            # increasing whether or not you re-run discover, so this is
            # re-evaluated against "now" on every list. Unknown posted_at is
            # kept, same "unknown, never dropped" rule as the other floors.
            cutoff = datetime.now(timezone.utc) - timedelta(days=age_ceiling)
            stmt = stmt.where((Job.posted_at.is_(None)) | (Job.posted_at >= cutoff))
        jobs = session.scalars(stmt).all()

        if not jobs:
            console.print("[yellow]No jobs found.[/] Run 'copilot discover' first.")
            return

        ranked = rank_jobs(jobs, profile, geocoder)[:limit]

        table = Table(title=f"Jobs ({len(ranked)} of {len(jobs)} shown)")
        table.add_column("ID", width=4)
        table.add_column("Fit")
        if rules:
            table.add_column("Pref")
        if industries or downrank_staffing:
            table.add_column("Industry")
        table.add_column("Title")
        table.add_column("Company")
        table.add_column("Location")
        table.add_column("Salary")
        table.add_column("Posted")
        table.add_column("Source")
        for job in ranked:
            company_name = job.company.name if job.company else "?"
            salary = format_salary(job)
            row = [str(job.id), f"{job.fit_score:.0f}" if job.fit_score is not None else "-"]
            if rules:
                row.append(tier_label(preference_tier(job, rules), rules))
            if industries or downrank_staffing:
                row.append(industry_label(ind_tier(job), industries))
            row += [
                job.title,
                company_name,
                job.location or "unknown",
                salary,
                format_posted(job),
                job.source,
            ]
            table.add_row(*row)
        console.print(table)
        if floor is not None:
            console.print(
                f"[dim]Salary floor ${floor:,} applied (unknown-salary jobs kept) - "
                "--all to see everything.[/]"
            )
        if min_fit is not None:
            console.print(
                f"[dim]Fit floor {min_fit} applied (unscored jobs kept) - "
                "run 'copilot score' to reduce the unscored pool.[/]"
            )
        if age_ceiling is not None:
            console.print(
                f"[dim]Posting-age ceiling {age_ceiling}d applied (unknown-date jobs kept) - "
                "--all to see everything.[/]"
            )


@jobs_app.command("show")
def jobs_show(job_id: int) -> None:
    """Full detail for one job: the written fit reasoning and the five
    per-dimension scores behind the overall fit_score shown in 'jobs list'."""
    from sqlalchemy import select

    from copilot.db import get_engine, get_session
    from copilot.db.models import Application, Job
    from copilot.formatting import format_posted, format_salary

    with get_session(get_engine()) as session:
        job = session.scalar(select(Job).where(Job.id == job_id))
        if job is None:
            console.print(f"[red]No job with id {job_id}.[/]")
            raise typer.Exit(1)

        company = job.company.name if job.company else "?"
        salary = format_salary(job)
        application = session.scalar(select(Application).where(Application.job_id == job_id))
        state_line = f"State: {application.state.value}" if application else "State: not tracked yet"

        console.print(
            Panel(
                f"[bold]{job.title}[/] at [bold]{company}[/]\n"
                f"{job.location or 'unknown location'}"
                f"{' (remote)' if job.remote else ''} | {salary} | via {job.source}\n"
                f"Posted {format_posted(job)} | {state_line}\n"
                f"{job.apply_url}",
                title=f"Job #{job.id}",
            )
        )

        if job.fit_score is None:
            console.print("[yellow]Not scored yet.[/] Run 'copilot score' to score it.")
            return

        console.print(f"\n[bold]Fit score: {job.fit_score:.0f}/100[/]")
        console.print(job.fit_reasoning or "[dim](no reasoning stored)[/]")

        table = Table(title="Dimension breakdown", show_header=True)
        table.add_column("Dimension")
        table.add_column("Score", justify="right")
        dimensions = [
            ("Skill match", job.skill_match_score),
            ("Experience level", job.experience_level_score),
            ("Domain fit", job.domain_fit_score),
            ("Location fit", job.location_fit_score),
            ("Visa feasibility", job.visa_feasibility_score),
        ]
        for name, value in dimensions:
            table.add_row(name, f"{value:.0f}/100" if value is not None else "-")
        console.print(table)
        console.print(f"[dim]Visa signal from JD: {job.visa_signal.value}[/]")

        if company != "?" and job.company:
            sponsorship = job.company.sponsorship_status.value
            if sponsorship != "unknown" or job.company.sponsorship_evidence:
                console.print(
                    f"[dim]Company sponsorship: {sponsorship}"
                    + (
                        f" - {job.company.sponsorship_evidence}"
                        if job.company.sponsorship_evidence
                        else ""
                    )
                    + "[/]"
                )


@jobs_app.command("queue")
def jobs_queue(job_id: int) -> None:
    """Mark a job as queued - a declaration of intent to apply, not an
    automated action. Creates the application record if one doesn't exist
    yet; never downgrades a job already further along (applied, etc.)."""
    from sqlalchemy import select

    from copilot.applications import queue
    from copilot.db import get_engine, get_session
    from copilot.db.models import Job

    with get_session(get_engine()) as session:
        if session.scalar(select(Job).where(Job.id == job_id)) is None:
            console.print(f"[red]No job with id {job_id}.[/]")
            raise typer.Exit(1)
        application = queue(session, job_id)
        session.commit()
        console.print(f"[green]Job {job_id} is now {application.state.value}.[/]")


@jobs_app.command("mark-applied")
def jobs_mark_applied(job_id: int) -> None:
    """Mark a job as applied. This only records what you already did
    yourself - Career Copilot never submits applications (D1)."""
    from sqlalchemy import select

    from copilot.applications import mark_applied
    from copilot.db import get_engine, get_session
    from copilot.db.models import Job

    with get_session(get_engine()) as session:
        if session.scalar(select(Job).where(Job.id == job_id)) is None:
            console.print(f"[red]No job with id {job_id}.[/]")
            raise typer.Exit(1)
        application = mark_applied(session, job_id)
        session.commit()
        console.print(
            f"[green]Job {job_id} marked applied[/] at {application.applied_at:%Y-%m-%d %H:%M} UTC."
        )


@jobs_app.command("untrack")
def jobs_untrack(
    job_id: int,
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Stop tracking a job - the undo for a wrong confirm or a mistaken
    queue/mark-applied. Deletes the application, its history, and its email
    records; a job created from a confirmed email disappears entirely, while
    discovery-found jobs stay in the jobs list as untracked listings."""
    from copilot.applications import untrack
    from copilot.db import get_engine, get_session

    if not yes and not typer.confirm(
        f"Remove all tracking for job {job_id}? This deletes its application, "
        "state history, and email records."
    ):
        raise typer.Exit(0)

    with get_session(get_engine()) as session:
        try:
            removed = untrack(session, job_id)
            session.commit()
        except LookupError as exc:
            console.print(f"[red]{exc}[/]")
            raise typer.Exit(1) from exc
    console.print(f"[green]Untracked.[/] Removed: {removed}.")


@jobs_app.command("set-state")
def jobs_set_state(job_id: int, state: str) -> None:
    """Set an application to any state, in any direction - the manual
    escape hatch for reality Copilot missed (an email that never arrived, a
    phone call) or for undoing a wrong state. Records who said so, so email
    conflict detection (D24) keeps working."""
    from sqlalchemy import select

    from copilot.applications import set_state
    from copilot.db import get_engine, get_session
    from copilot.db.models import ApplicationState, Job

    try:
        to_state = ApplicationState(state)
    except ValueError:
        valid = ", ".join(s.value for s in ApplicationState)
        console.print(f"[red]'{state}' is not a state.[/] Valid: {valid}")
        raise typer.Exit(1) from None

    with get_session(get_engine()) as session:
        if session.scalar(select(Job).where(Job.id == job_id)) is None:
            console.print(f"[red]No job with id {job_id}.[/]")
            raise typer.Exit(1)
        application = set_state(session, job_id, to_state)
        session.commit()
        console.print(f"[green]Job {job_id} is now {application.state.value}.[/]")


if __name__ == "__main__":
    app()
