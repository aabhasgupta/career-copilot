"""Local web dashboard (D11). First slice: a preferences editor for
profile.yaml's search section, so tuning titles/locations/salary/preferences
is a form on localhost instead of a YAML edit.

Design constraints:
- Same source of truth: reads and writes profile.yaml through the same
  comment-preserving ruamel helpers the CLI uses. No second config store.
- Validate before write: the merged result must pass Profile.model_validate
  or nothing is saved and the error is shown.
- Localhost only; single user; no auth (D4).
"""

from __future__ import annotations

import html
from pathlib import Path

import yaml as pyyaml
from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse

from copilot.config import Profile, RemotePreference
from copilot.industry import Industry
from copilot.profile_fill import update_search_preferences

_PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Career Copilot - Preferences</title>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 720px;
         margin: 2rem auto; padding: 0 1rem; color: #1a1a2e; }}
  h1 {{ font-size: 1.4rem; }}
  fieldset {{ border: 1px solid #ddd; border-radius: 8px; margin-bottom: 1rem;
              padding: 1rem; }}
  legend {{ font-weight: 600; padding: 0 .5rem; }}
  label {{ display: block; font-weight: 600; margin: .8rem 0 .2rem; }}
  .hint {{ font-weight: 400; color: #666; font-size: .85rem; margin: 0 0 .3rem; }}
  textarea, input, select {{ width: 100%; box-sizing: border-box; padding: .5rem;
    border: 1px solid #ccc; border-radius: 6px; font: inherit; }}
  textarea {{ min-height: 5.5rem; }}
  button {{ margin-top: 1rem; padding: .6rem 1.6rem; border: 0; border-radius: 6px;
    background: #2b6cb0; color: white; font: inherit; font-weight: 600;
    cursor: pointer; }}
  .flash {{ padding: .6rem 1rem; border-radius: 6px; margin-bottom: 1rem; }}
  .ok {{ background: #e6f6e6; color: #1e5e1e; }}
  .err {{ background: #fbe9e9; color: #8a1f1f; white-space: pre-wrap; }}
</style>
</head>
<body>
<h1>Career Copilot - Search Preferences</h1>
<p class="hint">Edits profile.yaml in place (comments preserved). Changes are
validated before saving. Ordering preferences re-sort listings instantly;
stricter filters take effect on stored jobs via <code>copilot jobs prune</code>.</p>
{flash}
<form method="post" action="/save">
<fieldset>
<legend>What to search for</legend>
<label>Target titles <span class="hint">one per line - what discovery queries for</span></label>
<textarea name="titles">{titles}</textarea>
<label>Search locations <span class="hint">one per line, e.g. "United States"</span></label>
<textarea name="locations">{locations}</textarea>
<label>Remote preference</label>
<select name="remote">{remote_options}</select>
</fieldset>
<fieldset>
<legend>Filters (drop jobs)</legend>
<label>Salary floor (USD) <span class="hint">jobs with known salary below this are
dropped; jobs that don't state a salary are always kept. Blank = no floor.</span></label>
<input name="min_salary" type="number" value="{min_salary}">
<label>Dealbreakers <span class="hint">one per line; jobs whose title/description
contains any of these are never stored</span></label>
<textarea name="dealbreakers">{dealbreakers}</textarea>
</fieldset>
<fieldset>
<legend>Preferences (reorder, never hide)</legend>
<label>Location preference <span class="hint">one per line, most preferred first:
"remote", "within 30 miles of Place, ST", or plain location text</span></label>
<textarea name="location_preference">{location_preference}</textarea>
<label>Industry preference <span class="hint">one per line, most preferred first.
Valid values: {industry_vocab}</span></label>
<textarea name="industry_preference">{industry_preference}</textarea>
</fieldset>
<button type="submit">Save</button>
</form>
</body>
</html>"""


def _lines(values: list[str]) -> str:
    return html.escape("\n".join(values))


def _parse_lines(raw: str) -> list[str]:
    return [line.strip() for line in raw.splitlines() if line.strip()]


def create_app(profile_path: Path = Path("profile.yaml")) -> FastAPI:
    app = FastAPI(title="Career Copilot")

    def render(flash: str = "") -> str:
        with open(profile_path) as f:
            raw = pyyaml.safe_load(f)
        profile = Profile.model_validate(raw)
        search = profile.search
        remote_options = "".join(
            f'<option value="{r.value}"{" selected" if r == search.remote else ""}>{r.value}</option>'
            for r in RemotePreference
        )
        return _PAGE.format(
            flash=flash,
            titles=_lines(search.titles),
            locations=_lines(search.locations),
            remote_options=remote_options,
            min_salary=search.min_salary if search.min_salary is not None else "",
            dealbreakers=_lines(search.dealbreakers),
            location_preference=_lines(search.location_preference),
            industry_preference=_lines(search.industry_preference),
            industry_vocab=", ".join(i.value for i in Industry),
        )

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return render()

    @app.get("/saved", response_class=HTMLResponse)
    def saved() -> str:
        return render(flash='<div class="flash ok">Saved.</div>')

    @app.post("/save")
    def save(
        titles: str = Form(""),
        locations: str = Form(""),
        remote: str = Form("any"),
        min_salary: str = Form(""),
        dealbreakers: str = Form(""),
        location_preference: str = Form(""),
        industry_preference: str = Form(""),
    ):
        updates = {
            "titles": _parse_lines(titles),
            "locations": _parse_lines(locations),
            "remote": remote,
            "min_salary": int(min_salary) if min_salary.strip() else None,
            "dealbreakers": _parse_lines(dealbreakers),
            "location_preference": _parse_lines(location_preference),
            "industry_preference": _parse_lines(industry_preference),
        }

        # Validate the merged profile before touching the file.
        with open(profile_path) as f:
            raw = pyyaml.safe_load(f)
        raw["search"] = {**raw.get("search", {}), **updates}
        try:
            Profile.model_validate(raw)
        except Exception as exc:  # noqa: BLE001 - shown to the user, nothing written
            flash = f'<div class="flash err">Not saved:\n{html.escape(str(exc))}</div>'
            return HTMLResponse(render(flash))

        update_search_preferences(profile_path, updates)
        return RedirectResponse("/saved", status_code=303)

    return app
