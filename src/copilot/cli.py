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
    from copilot.scoring.rubric import JobToScore, score_jobs

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

        to_score = [
            JobToScore(
                id=j.id,
                title=j.title,
                company=j.company.name if j.company else "?",
                location=j.location,
                remote=j.remote,
                salary_min=j.salary_min,
                salary_max=j.salary_max,
                jd_text=j.jd_text,
            )
            for j in jobs
        ]

        with console.status(f"Scoring {len(to_score)} jobs against your resume..."):
            scores = score_jobs(profile, resume, to_score, batch_size=batch_size)

        by_id = {j.id: j for j in jobs}
        for job_id, result in scores.items():
            job = by_id[job_id]
            job.fit_score = result.fit_score
            job.fit_reasoning = result.reasoning
            job.visa_signal = result.visa_signal
            job.skill_match_score = result.skill_match_score
            job.experience_level_score = result.experience_level_score
            job.domain_fit_score = result.domain_fit_score
            job.location_fit_score = result.location_fit_score
            job.visa_feasibility_score = result.visa_feasibility_score
        session.commit()

    missing = len(to_score) - len(scores)
    console.print(f"[green]Scored {len(scores)}[/] jobs.")
    if missing:
        console.print(
            f"[yellow]{missing} jobs got no score back[/] - left unscored, "
            "re-run 'copilot score' to retry them."
        )
    console.print("[dim]Run 'copilot jobs list' to see them ranked by fit.[/]")


@app.command("sponsorship-sync")
def sponsorship_sync(
    refresh: bool = typer.Option(
        False, "--refresh", help="Re-download the USCIS data even if already cached."
    ),
) -> None:
    """Match your companies against the public USCIS H-1B Employer Data Hub to
    populate each company's H1B filing history. Deterministic, no LLM calls -
    safe to re-run any time new companies are discovered. This is historical,
    company-wide evidence (not a live policy) and is never folded into
    fit_score - see it via 'copilot jobs show <id>'."""
    from copilot.db import get_engine, get_session
    from copilot.scoring.sponsorship import FISCAL_YEAR, download_data, sync_sponsorship_data

    with console.status("Downloading USCIS H-1B Employer Data Hub (cached after first run)..."):
        cache_path = download_data(force=refresh)

    with get_session(get_engine()) as session:
        with console.status("Matching companies against filing data..."):
            summary = sync_sponsorship_data(session, cache_path=cache_path)

    console.print(
        f"[green]Matched {summary.matched}[/] of {summary.companies_checked} companies "
        f"against FY{FISCAL_YEAR} H1B filing data."
    )
    console.print(
        "[dim]Historical, company-wide evidence, not a live policy - see "
        "'copilot jobs show <id>' for a company's filing history alongside its fit score.[/]"
    )


@app.command()
def dashboard(
    port: int = typer.Option(8765, "--port", help="Port to serve on."),
) -> None:
    """Open the local web dashboard (currently: search preferences editor).
    Localhost only - nothing is exposed to the network."""
    profile_path = Path(PROFILE_FILENAME)
    if not profile_path.exists():
        console.print(f"[red]{PROFILE_FILENAME} not found.[/] Run 'copilot init' first.")
        raise typer.Exit(1)

    import uvicorn

    from copilot.dashboard import create_app

    console.print(f"[green]Dashboard:[/] http://127.0.0.1:{port}  (Ctrl+C to stop)")
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
    location: str | None = typer.Option(None, "--location", help="Substring match on location."),
    limit: int = typer.Option(25, "--limit", help="Max rows to show."),
    show_all: bool = typer.Option(
        False, "--all", help="Ignore the profile salary floor and show everything."
    ),
) -> None:
    """List discovered jobs, best first: scored jobs rank by fit_score (highest
    first), with your location_preference etc. as a tiebreaker; unscored jobs
    follow, ranked the old way, until 'copilot score' reaches them. Jobs with
    unknown salary/fit are always kept - floors only drop known-and-below
    values."""
    from sqlalchemy import select

    from copilot.db import get_engine, get_session
    from copilot.db.models import Job
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
        table.add_column("Source")
        for job in ranked:
            company_name = job.company.name if job.company else "?"
            if job.salary_min or job.salary_max:
                lo = f"{job.salary_min:,}" if job.salary_min else "?"
                hi = f"{job.salary_max:,}" if job.salary_max else "?"
                salary = f"${lo}-${hi}"
            else:
                salary = "unknown"
            row = [str(job.id), f"{job.fit_score:.0f}" if job.fit_score is not None else "-"]
            if rules:
                row.append(tier_label(preference_tier(job, rules), rules))
            if industries or downrank_staffing:
                row.append(industry_label(ind_tier(job), industries))
            row += [job.title, company_name, job.location or "unknown", salary, job.source]
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


@jobs_app.command("show")
def jobs_show(job_id: int) -> None:
    """Full detail for one job: the written fit reasoning and the five
    per-dimension scores behind the overall fit_score shown in 'jobs list'."""
    from sqlalchemy import select

    from copilot.db import get_engine, get_session
    from copilot.db.models import Job

    with get_session(get_engine()) as session:
        job = session.scalar(select(Job).where(Job.id == job_id))
        if job is None:
            console.print(f"[red]No job with id {job_id}.[/]")
            raise typer.Exit(1)

        company = job.company.name if job.company else "?"
        salary = "unknown"
        if job.salary_min or job.salary_max:
            lo = f"{job.salary_min:,}" if job.salary_min else "?"
            hi = f"{job.salary_max:,}" if job.salary_max else "?"
            salary = f"${lo}-${hi}"

        console.print(
            Panel(
                f"[bold]{job.title}[/] at [bold]{company}[/]\n"
                f"{job.location or 'unknown location'}"
                f"{' (remote)' if job.remote else ''} | {salary} | via {job.source}\n"
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


if __name__ == "__main__":
    app()
