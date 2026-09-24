.PHONY: help sync browsers test integration_test lint format type check repository_check
.DEFAULT_GOAL := help

help:
	@echo "make sync    - install the app and development tools"
	@echo "make test    - run network-free business tests"
	@echo "make browsers - install the matching Chromium browser"
	@echo "make integration_test - check browser readiness, then test WebGL2 (fail fast)"
	@echo "make lint    - check code, formatting and types"
	@echo "make format  - format application Python files"
	@echo "make type    - check application types"
	@echo "make repository_check - check local documentation links and import boundaries"
	@echo "make check   - run repository checks, unit tests and lint (no network or browser)"

check:
	$(MAKE) repository_check
	$(MAKE) test
	$(MAKE) lint

repository_check:
	uv run --no-sync python scripts/check_repository.py

sync:
	uv sync --locked --all-groups

browsers:
	PLAYWRIGHT_SKIP_BROWSER_GC=1 uv run --no-sync playwright install chromium --no-shell

test:
	uv run --no-sync python -W error -m unittest discover -s tests/unit_tests -q

integration_test:
	# 先用现有最小渲染测试检查浏览器, 失败即停止, 避免整组测试反复启动失败.
	uv run --no-sync python -W error -m unittest tests.integration_tests.test_renderer.WebGL2RenderTests.test_solid_color_and_exact_dimensions -q
	uv run --no-sync python -W error -m unittest discover -s tests/integration_tests -q -f

lint:
	uv run --no-sync ruff check .
	uv run --no-sync ruff format --check .
	$(MAKE) type

format:
	uv run --no-sync ruff format .

type:
	uv run --no-sync ty check src main.py scripts
