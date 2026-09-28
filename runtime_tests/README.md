# Real Home Assistant diagnostics harness

Black-box tests against a real Home Assistant runtime (entity registry, device
registry, state machine). Isolated from `tests/` (stub unittest). Fakes only
the Hermes HTTP boundary with an aiohttp loopback on `127.0.0.1`.

## Environment (exercised)

- Python 3.12.13
- homeassistant 2024.12.5 (declared minimum is 2024.12.0 in `hacs.json`)
- pytest-homeassistant-custom-component 0.13.195
- pytest 8.3.3

Current HA (2026.9.x) needs Python ≥3.14.2; this harness pins the declared
minimum line instead.

## Commands

From the integration repo root:

```bash
uv python install 3.12
uv venv /tmp/hermes-ha-runtime-2024.12 --python 3.12
uv pip install --python /tmp/hermes-ha-runtime-2024.12/bin/python -r requirements-test-ha.txt
cd runtime_tests
PYTHONPATH=.. /tmp/hermes-ha-runtime-2024.12/bin/python -m pytest -c pytest.ini
```

Do not combine with `tests/`. Do not import `tests.test_support`.
