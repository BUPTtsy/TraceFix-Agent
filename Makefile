.PHONY: test web demo
test:
	.venv/bin/python -m pytest -q
web:
	./scripts/bootstrap/start.sh --console-only
demo:
	./scripts/bootstrap/start.sh
