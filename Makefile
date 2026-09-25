.PHONY: test smoke doctor demo
test:
	.venv/bin/python -m pytest -q
smoke:
	.venv/bin/tracefix --smoke
doctor:
	.venv/bin/tracefix --doctor
demo:
	./scripts/start.sh --mode repair --spec profiles/persistence.spec.json
