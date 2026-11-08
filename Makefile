.PHONY: test lint format bench

test:
	.venv/bin/python -m unittest discover -s tests -t .

lint:
	.venv/bin/ruff check llm_engine tests

format:
	.venv/bin/ruff format llm_engine tests

bench:
	.venv/bin/python -m llm_engine.bench --mode normal \
		--workers 2 --rate 4 --duration 10 --max-tokens 16 \
		--output /tmp/llm-normal
