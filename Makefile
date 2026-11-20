UV ?= uv
RUN = $(UV) run --locked
PORT ?= 8000

.PHONY: setup demo test check browser eval-fixtures eval-live smoke-live refresh-data
setup:
	$(UV) sync --locked
demo:
	$(RUN) python -m uvicorn recall.app:app_factory --factory --host 127.0.0.1 --port $(PORT)
test:
	$(RUN) python -m pytest -m "not browser"
browser:
	$(RUN) python -m pytest -m browser
check:
	$(RUN) ruff format --check .
	$(RUN) ruff check .
	$(RUN) mypy
	$(RUN) python -m scripts.check_text
eval-fixtures:
	$(RUN) python -m scripts.evaluate --output .runtime/reports/fixture.json
eval-live:
	$(RUN) python -m scripts.evaluate --live --output .runtime/reports/live.json
smoke-live:
	$(RUN) python -m scripts.operations live-smoke
refresh-data:
	$(RUN) python -m scripts.operations refresh
