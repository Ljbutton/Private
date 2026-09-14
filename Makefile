.PHONY: install run refresh train backtest test lint demo clean

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

refresh: install
	$(PY) -m nflpicker.cli refresh

train: install
	$(PY) -m nflpicker.cli train

backtest: install
	$(PY) -m nflpicker.cli backtest

test: install
	$(VENV)/bin/pytest -q

lint: install
	$(VENV)/bin/ruff check nflpicker tests

clean:
	rm -rf $(VENV) .pytest_cache .ruff_cache **/__pycache__
