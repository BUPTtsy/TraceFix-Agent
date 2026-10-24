.PHONY: test web demo
test:
	.venv/bin/python -m pytest -q
web:
	./scripts/bootstrap/start.sh --console-only
# repair 编码只走 Claude Agent SDK：需先在 .env 配置 TRACEFIX_CLAUDE_* 与 TRACEFIX_SDK_*（见 .env.example）。
demo:
	./scripts/bootstrap/start.sh
