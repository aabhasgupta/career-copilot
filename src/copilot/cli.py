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
app.add_typer(profile_app, name="profile")
app.add_typer(jobs_app, name="jobs")

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
        f"remote: {profile.search.remote.value}[/]"
    )


@app.command()
def discover() -> None:
    """Search Adzuna + JSearch for every title/location in your profile and
    save new postings to the database. Safe to re-run - duplicates are
    skipped, not re-added."""
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
    from copilot.discovery.pipeline import run_discovery

    engine = get_engine()
    init_db(engine)

    titles = ", ".join(profile.search.titles)
    locations = ", ".join(profile.search.locations)
    with console.status(f"Searching for {titles} in {locations}..."):
        with get_session(engine) as session:
            summary = run_discovery(
                profile,
                session,
                adzuna_app_id=adzuna_app_id,
                adzuna_app_key=adzuna_app_key,
                jsearch_api_key=jsearch_api_key,
            )

    console.print(
        f"[green]Found {summary.found}[/] postings - "
        f"[green]{summary.added} new[/], "
        f"[dim]{summary.duplicates} already known[/], "
        f"[yellow]{summary.dealbreakers_dropped} dropped (dealbreaker)[/]"
    )
    for err in summary.errors:
        console.print(f"[red]Error:[/] {err}")

    console.print("[dim]Run 'copilot jobs list' to see them.[/]")


@jobs_app.command("list")
def jobs_list(
    min_salary: int | None = typer.Option(None, "--min-salary", help="Filter by salary_min."),
    location: str | None = typer.Option(None, "--location", help="Substring match on location."),
    limit: int = typer.Option(25, "--limit", help="Max rows to show."),
) -> None:
    """List discovered jobs, newest first."""
    from sqlalchemy import select

    from copilot.db import get_engine, get_session
    from copilot.db.models import Job

    engine = get_engine()
    with get_session(engine) as session:
        stmt = select(Job).order_by(Job.created_at.desc()).limit(limit)
        if min_salary is not None:
            stmt = stmt.where(Job.salary_min >= min_salary)
        if location:
            stmt = stmt.where(Job.location.ilike(f"%{location}%"))
        jobs = session.scalars(stmt).all()

        if not jobs:
            console.print("[yellow]No jobs found.[/] Run 'copilot discover' first.")
            return

        table = Table(title=f"Jobs ({len(jobs)} shown)")
        table.add_column("ID", width=4)
        table.add_column("Title")
        table.add_column("Company")
        table.add_column("Location")
        table.add_column("Salary")
        table.add_column("Source")
        for job in jobs:
            company_name = job.company.name if job.company else "?"
            if job.salary_min or job.salary_max:
                lo = f"{job.salary_min:,}" if job.salary_min else "?"
                hi = f"{job.salary_max:,}" if job.salary_max else "?"
                salary = f"${lo}-${hi}"
            else:
                salary = "unknown"
            table.add_row(
                str(job.id), job.title, company_name, job.location or "unknown", salary, job.source
            )
        console.print(table)


if __name__ == "__main__":
    app()
