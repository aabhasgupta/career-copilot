"""Auto-fill profile.yaml's identity section from the parsed resume.

Only `identity` is resume-derivable (name, contact, links). search/visa/
email_integration are the user's own preferences, not facts about them, so
they are never touched here. Uses ruamel.yaml (round-trip mode) instead of
PyYAML so the file's comments and layout survive the rewrite.
"""

from __future__ import annotations

from pathlib import Path

from ruamel.yaml import YAML

from copilot.resume import ResumeProfile

yaml = YAML()
yaml.preserve_quotes = True
yaml.indent(mapping=2, sequence=2, offset=0)


def fill_identity(profile_yaml_path: Path, resume: ResumeProfile) -> list[str]:
    """Merge extracted contact info into profile.yaml's identity block in place.

    Only overwrites a field when the resume actually stated a value - never
    clobbers an existing entry with a blank. Returns the list of fields changed.
    """
    with open(profile_yaml_path) as f:
        data = yaml.load(f)

    identity = data.setdefault("identity", {})
    changed: list[str] = []

    def set_if_present(key: str, value: str | None, target: dict = identity) -> None:
        if value and target.get(key) != value:
            target[key] = value
            changed.append(key)

    set_if_present("full_name", resume.full_name)
    set_if_present("email", resume.contact.email)
    set_if_present("phone", resume.contact.phone)
    set_if_present("location", resume.contact.location)

    links = identity.setdefault("links", {})
    set_if_present("linkedin", resume.contact.linkedin_url, target=links)
    set_if_present("github", resume.contact.github_url, target=links)
    set_if_present("portfolio", resume.contact.portfolio_url, target=links)

    with open(profile_yaml_path, "w") as f:
        yaml.dump(data, f)

    return changed


def set_search_titles(profile_yaml_path: Path, titles: list[str]) -> None:
    """Replace search.titles in profile.yaml, preserving comments/layout.

    Unlike fill_identity, this always overwrites - it's only called after the
    CLI has explicitly confirmed the user wants to apply suggested titles.
    """
    with open(profile_yaml_path) as f:
        data = yaml.load(f)

    search = data.setdefault("search", {})
    search["titles"] = titles

    with open(profile_yaml_path, "w") as f:
        yaml.dump(data, f)
