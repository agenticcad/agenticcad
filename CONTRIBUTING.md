# Contributing to AgenticCAD

Thanks for your interest. Issues and pull requests are welcome.

## Issues
Use the issue templates (bug report / feature request). For bugs, include the design script (Code tab →
Download .py), what you asked the agent or which tool you used, and what you expected. Screenshots help.

## Pull requests
- Fork the repository, create a branch in your fork, and open a pull request against `main`. Direct
  pushes to `main` are not possible; all changes go through review.
- Run the tests before opening a PR: `.venv/bin/python -m pytest -q` (no Claude calls needed). For
  changes to the agent prompt or tools, also run the relevant evals: `.venv/bin/python evals/run.py
  --filter <case>` (these call Claude and cost money; say in the PR which you ran).
- Keep the design-as-script principle: manual UI operations must be written into the script
  (`script_edit.py`), never applied only to the viewer.
- Add a line to `CHANGELOG.md` under the next version. Don't bump `version.py` in a PR; releases are
  cut by the maintainer.
- One topic per PR, with a short description of what changed and why.

## Licence
By contributing you agree that your contributions are licensed under the project's licence
(PolyForm Noncommercial 1.0.0) and that the maintainer may offer them under a commercial licence to
commercial users of AgenticCAD.
