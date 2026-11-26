# Notes for contributors and coding assistants

* **Version:** every pull request that changes anything under `src/`,
  `packaging/` or `pyproject.toml` must raise `__version__` in
  `src/rtpl/__init__.py` (the only place it is set): PATCH for fixes, MINOR
  for features. The *version-bump* check fails otherwise. A merge to `main`
  with a new version publishes a GitHub Release only while the repository
  variable `AUTO_RELEASE` is `true`. See `docs/packaging.md`.
* Run the tests with `QT_QPA_PLATFORM=offscreen uv run --extra dev pytest -q`.
* **Tooltips** are plain text and Qt doesn't wrap them. Break each line
  with `\n` at 70 characters or fewer (`TIP_WIDTH` in
  `src/rtpl/app/__init__.py`), so they fit the smallest supported screen
  (1024 pixels wide). Wrap text from data files or the user's own names
  with `textwrap.fill(..., TIP_WIDTH)`. A test in `tests/test_app_smoke.py`
  fails on longer lines.
