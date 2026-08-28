# Repository Guidelines

## Project Structure & Module Organization

`app.py` is the only executable entry point. Application code lives in `src/`: `app_factory.py` constructs Flask, `routes.py` defines HTTP endpoints, `database.py` owns SQLite access, `security.py` handles request protections, `storage.py` validates and routes images, and `server.py` controls Waitress and the packaged desktop UI. Server-rendered pages are in `src/templates/`; browser assets are in `src/static/`. Security deployment documentation lives in `docs/`. Tests belong in `tests/` and use isolated temporary directories. Runtime state—SQLite data and local uploads—belongs in `data/` and is intentionally ignored by Git.

## Build, Test, and Development Commands

From `Picture_bed/`, create and activate a virtual environment, then install dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

The service listens on `0.0.0.0:9990`; access it locally at `http://127.0.0.1:9990`. `start.bat` is the Windows shortcut for an existing Conda environment named `picture-bed`. Run the automated checks with:

```powershell
python -m unittest discover -s tests -v
```

## Coding Style & Naming Conventions

Use Python 4-space indentation, `snake_case` for functions, variables, and file names, and `PascalCase` for classes. Follow the established Flask pattern: validate request input, keep database work inside `app.app_context()` where needed, and return redirects or JSON explicitly. Use `pathlib.Path` for filesystem paths. No formatter or linter is configured; preserve the surrounding style and keep changes focused. Name tests `test_<behavior>.py`, with test methods such as `test_upload_uses_active_location`.

## Testing Guidelines

Add or update a regression test whenever changing routes, upload rules, or storage failover. Use `unittest.TestCase`, Flask's `test_client()`, and `tempfile.TemporaryDirectory()` so tests never read or write a developer's real image directories. Tests must pass before review; no coverage threshold is currently enforced.

## Commit & Pull Request Guidelines

Recent history uses concise Conventional Commit-style subjects, for example `feat: add image rotation`. Prefer `feat:`, `fix:`, `test:`, `docs:`, or `chore:` followed by an imperative summary. Keep commits small and single-purpose. Pull requests should state the user-visible change, note configuration or migration effects, link the relevant issue when available, include test results, and attach screenshots for template or CSS changes.

## Security & Configuration

Never commit `data/`, uploaded images, API tokens, session secrets, or real passwords. The first-run administrator password is documented in `README.md`; change it immediately. Treat configured storage paths as local, trusted paths and test path-validation changes carefully.
