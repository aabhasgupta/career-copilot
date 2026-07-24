"""Base application packet (docs/DECISIONS.md D22, D23): contact details,
work-authorization Q&A, and a resume summary/skills snippet - the content
that's genuinely identical across every job application, so it's generated
once and reused rather than regenerated per posting.

No per-job content, no per-job LLM call. The one Claude call this makes
(resume extraction) is only paid when the resume file actually changed -
extract_resume_profile's own content-hash cache makes that call a no-op
otherwise. Generation only ever happens on explicit request (`copilot packet
base` or the dashboard button) - never automatically, even though the
underlying cache would already detect a changed resume on its own.
"""

from __future__ import annotations

from pathlib import Path

from copilot.config import DATA_DIR, RESUME_CACHE_PATH, Identity, Profile, VisaPreferences, VisaStatus
from copilot.resume import ResumeProfile, extract_resume_profile

PACKET_BASE_PATH = DATA_DIR / "packet_base.md"


def _contact_section(identity: Identity) -> str:
    lines = [
        f"- Name: {identity.full_name}",
        f"- Email: {identity.email}",
        f"- Phone: {identity.phone or 'not set in profile.yaml'}",
    ]
    if identity.location:
        lines.append(f"- Location: {identity.location}")
    if identity.links.linkedin:
        lines.append(f"- LinkedIn: {identity.links.linkedin}")
    if identity.links.github:
        lines.append(f"- GitHub: {identity.links.github}")
    if identity.links.portfolio:
        lines.append(f"- Portfolio: {identity.links.portfolio}")
    return "\n".join(lines)


def _work_authorization_qa(visa: VisaPreferences) -> str:
    q1 = "**Q: Are you legally authorized to work in the United States?**"
    q2 = "**Q: Will you now or in the future require sponsorship for employment visa status?**"

    if visa.status == VisaStatus.h1b_transfer:
        a1 = "A: Yes, I am currently authorized to work in the U.S. under an H-1B visa."
        a2 = (
            "A: I currently hold H-1B status and would require the new employer to "
            "file an H-1B transfer petition, which does not require a new lottery entry."
        )
    elif visa.status in (VisaStatus.f1_opt, VisaStatus.stem_opt) and visa.needs_sponsorship:
        opt_label = "F-1 OPT" if visa.status == VisaStatus.f1_opt else "F-1 STEM OPT"
        a1 = f"A: Yes, I am currently authorized to work in the U.S. under {opt_label}."
        a2 = "A: Yes, I will require H-1B sponsorship to continue working after my current authorization ends."
    elif not visa.needs_sponsorship:
        a1 = "A: Yes, I am authorized to work in the U.S. without needing employer sponsorship."
        a2 = "A: No."
    else:
        a1 = "A: [visa.status in profile.yaml didn't match a known template - edit this by hand.]"
        a2 = "A: Yes, sponsorship will be required."

    return f"{q1}\n{a1}\n\n{q2}\n{a2}"


def _summary_section(resume: ResumeProfile) -> str:
    skills = ", ".join(resume.skills[:12])
    domains = ", ".join(resume.domains)
    return (
        f"{resume.summary}\n\n"
        f"- Seniority: {resume.seniority} ({resume.years_of_experience:g} years)\n"
        f"- Key skills: {skills}\n"
        f"- Domains: {domains}"
    )


def build_base_packet(profile: Profile, cache_path: Path = RESUME_CACHE_PATH) -> str:
    resume = extract_resume_profile(profile, cache_path=cache_path)
    return f"""# Career Copilot - Base Application Packet

Reusable across every application - not tailored to any specific job.
Regenerate with `copilot packet base` (or the dashboard button) after
editing your resume or profile.yaml; it won't happen automatically.

## Contact details
{_contact_section(profile.identity)}

## Work authorization
{_work_authorization_qa(profile.visa)}

## Professional summary
{_summary_section(resume)}
"""


def write_base_packet(
    profile: Profile, path: Path = PACKET_BASE_PATH, cache_path: Path = RESUME_CACHE_PATH
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_base_packet(profile, cache_path=cache_path))
    return path
