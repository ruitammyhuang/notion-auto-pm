# Repository Guidelines

## Project Structure & Module Organization
This repository is a local Python 3.12 + Flask app for syncing project-specific Notion WBS databases into a shared faculty workload hub. `main.py` starts the web app, and `sync_work_type_options.py` pushes shared Notion select options. Core application code lives in `focal/`: `app.py` wires blueprints, `routes/` exposes UI and API endpoints, `sync_engine.py` handles cross-database sync, and `notion_client.py` centralizes Notion API access. HTML templates live in `focal/templates/`. Operational scripts are in `scripts/`, with one-off repair tools under `scripts/hotfixes/`. Historical snapshots and generated artifacts live in `backups/`, `health_check_reports/`, and `archive/` and should usually not be edited.

## Build, Test, and Development Commands
Install dependencies with `python3 -m pip install -r requirements.txt`.

- `python3 main.py` starts the app at `http://localhost:8765`.
- `python3 main.py --debug` runs Flask in debug mode.
- `python3 main.py --port 9000` uses a custom port.
- `python3 -m focal` runs the packaged entry point from `pyproject.toml`.
- `python3 sync_work_type_options.py` updates the shared Work Type taxonomy in Notion.
- `python3 -m pip install -e ".[dev]"` installs development tools, including `pylint`.
- `python3 -m pylint main.py focal sync_work_type_options.py` runs the configured linter.

Because this repo touches a live Notion workspace, avoid running write operations unless you understand their effect.

## Coding Style & Naming Conventions
Use 4-space indentation and standard Python naming: `snake_case` for functions and variables, `PascalCase` for classes, and short descriptive module names like `task_routes.py`. Keep route handlers thin and place Notion API calls in `focal/notion_client.py` rather than calling `requests` from routes or sync logic. Follow the existing style in nearby files. `pylint` is configured in `pyproject.toml`; generated and archived directories are excluded from lint runs.

## Testing Guidelines
There is no committed `pytest` or `unittest` suite yet, so each change should include both static and manual checks:

- Run `python3 -m pylint main.py focal sync_work_type_options.py` before opening a PR.
- Run `python3 main.py --debug` and confirm affected pages or endpoints load.
- Exercise the specific sync or utility script you changed.
- For Notion-writing flows, test on the smallest safe scope possible and verify generated JSON reports if applicable.

When adding automated tests later, place them in a top-level `tests/` directory, name files `test_<feature>.py`, and prefer small unit tests around `focal/` modules before adding end-to-end sync coverage.

## Commit & Pull Request Guidelines
Recent history follows Conventional Commit-style prefixes such as `fix:`, `feat:`, `chore:`, and `docs:`. Keep commits scoped and imperative, for example `fix: filter archived tasks from focus cache`. Pull requests should explain user-visible behavior changes, call out any Notion data impact, list manual verification steps, and include screenshots for template or dashboard UI changes.
