.PHONY: install run desktop refresh train backtest teasers test lint demo exe update clean

VENV ?= .venv
PY   := $(VENV)/bin/python
PIP  := $(VENV)/bin/pip

$(VENV):
	python3 -m venv $(VENV)
	$(PIP) install -q --upgrade pip

install: $(VENV)
	$(PIP) install -q -e ".[dev]"
	@echo "installed. copy .env.example to .env and add your ODDS_API_KEY (optional)"

run: install
	$(PY) -m nflpicker.cli serve

demo: install
	NFLPICKER_DEMO=1 $(PY) -m nflpicker.cli serve

desktop: install
	$(PIP) install -q pywebview
	$(PY) -m nflpicker.cli desktop

# Single-file executable. PyInstaller cannot cross-compile: run this on the
# platform you want the build for. `make exe PROFILE=lite` drops pyarrow
# (~40MB smaller) at the cost of training and play-by-play.
PROFILE ?= full
exe: install
	$(PIP) install -q pywebview pyinstaller
	$(VENV)/bin/pyinstaller nflpicker.spec --noconfirm -- --profile $(PROFILE)
	@echo "built dist/NFLPicker (profile: $(PROFILE))"

# Update in place. Your database is never touched: it lives in data/ (or in
# your application-data directory for a packaged build), the schema migrates
# forward additively, and a copy is written beside it before anything changes.
update:
	git pull
	$(PIP) install -q -e ".[dev]"
	$(PY) -c "from nflpicker import db; db.connect(); print('database ready')"
	@echo "updated. restart the app to pick it up."

refresh: install
	$(PY) -m nflpicker.cli refresh

train: install
	$(PY) -m nflpicker.cli train

backtest: install
	$(PY) -m nflpicker.cli backtest

teasers: install
	$(PY) -m nflpicker.cli teasers --sweep

test: install
	$(VENV)/bin/pytest -q

lint: install
	$(VENV)/bin/ruff check nflpicker tests

clean:
	rm -rf $(VENV) .pytest_cache .ruff_cache **/__pycache__
