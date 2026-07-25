# Regression Alerts

No product regression is open.

Validation:

- 95 focused source tests passed.
- 10/10 audit regression scripts passed.
- Ruff on all changed Python files passed.
- `uvx ty check` passed.
- `bash -n scripts/install.sh` passed.
- Full suite: 1,485 passed, 7 skipped, 2 order/timing failures.
- Both full-suite failures passed immediately when rerun together (2/2):
  - JWKS mock authentication test.
  - SQLite WAL diagnostic-backup race test.

The full-suite failures did not touch modified paths and are classified as non-reproducible suite-order flakes.
