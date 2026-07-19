"""Career Copilot CLI. Run 'copilot --help' for commands."""

from __future__ import annotations

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
app.add_typer(profile_app, name="profile")

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
        "  3. Run: copilot profile fill   (auto-fills identity from your resume)\n"
        f"  4. Edit {PROFILE_FILENAME}: fill in search, visa, and email_integration by hand\n"
        "  5. Run: copilot profile show"
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


if __name__ == "__main__":
    app()
