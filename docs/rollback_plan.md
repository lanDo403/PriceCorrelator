# Rollback Plan

## Preconditions

- Keep a copy of the previous known-good release branch/tag.
- Keep the previous `requirements.lock` as `requirements.lock.prev`.

## Fast Rollback (same code, known-good dependencies)

1. Stop running strategy processes.
2. Restore pinned dependencies from previous lock file:
   - `Copy-Item .\requirements.lock.prev .\requirements.lock -Force`
   - `.\.venv\Scripts\python.exe -m pip install -r .\requirements.lock --force-reinstall`
3. Reinstall project package:
   - `.\.venv\Scripts\python.exe -m pip install -e . --force-reinstall`
4. Run smoke tests:
   - `.\.venv\Scripts\python.exe -m pytest -q tests/test_cli.py tests/test_strategy.py`
5. Restart strategy with the same run script.

## Full Rollback (code + dependencies)

1. Stop running strategy processes.
2. Checkout previous known-good release branch/tag.
3. Restore previous lock file and reinstall dependencies.
4. Run full test suite.
5. Restart strategy and verify:
   - `logs/price_correlator.log`
   - `logs/alerts.log`

## Rollback Validation

- No startup errors in stdout.
- `summary:` lines are present.
- No new critical warnings in `logs/alerts.log` during first event cycle.
