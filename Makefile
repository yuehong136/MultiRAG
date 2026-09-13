# MultiRAG 验证/测试统一入口（人类与 AI 通用）
# 分层验证体系说明见 AGENTS.md。日常门禁：make verify
.DEFAULT_GOAL := help
UV := uv run --no-sync

.PHONY: help install fix lint typecheck test test-all coverage integration smoke mcp-compat verify

help: ## 列出全部可用目标
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install: ## 同步依赖（dev 组，按锁文件）
	uv sync --group dev --frozen

fix: ## 全库修复 lint 并格式化（局部改动请限定路径）
	$(UV) ruff check --fix .
	$(UV) ruff format .

lint: ## Tier 0：格式检查 + lint + 分层依赖契约 + async/Session 门禁（秒级）
	$(UV) ruff format --check .
	$(UV) ruff check .
	$(UV) lint-imports
	$(UV) python scripts/check_async_sync_db.py

typecheck: ## Tier 1：mypy 渐进式类型检查（范围见 pyproject [tool.mypy]）
	$(UV) mypy

test: ## Tier 2：单元测试（无需外部服务）
	$(UV) pytest tests/unit -q

test-all: ## Tier 2+3：单元 + 集成（服务缺失时集成测试自动跳过）
	$(UV) pytest tests -q

coverage: ## 单元测试 + 覆盖率报告
	$(UV) pytest tests/unit -q --cov --cov-branch --cov-report=term-missing --cov-report=xml

integration: ## Tier 3：集成测试（需要 docker compose base 服务）
	@$(UV) python scripts/check_services.py || (echo "" && echo "服务未就绪。启动方式：" && echo "  docker compose -f docker/docker-compose-base.yml up -d" && exit 1)
	REQUIRE_SERVICES=1 $(UV) pytest tests/integration -q

smoke: ## Tier 4：冒烟测试（对运行中的服务器打健康端点；启动：uv run python -m api.multirag_server）
	$(UV) python scripts/smoke.py

mcp-compat: ## EIM-F2：隔离 MCP 1/2 解释器，运行双方向真实协议兼容矩阵
	uv lock --check --script tests/compat/mcp/legacy_server.py
	uv lock --check --script tests/compat/mcp/modern_server.py
	$(UV) python scripts/check_mcp_compat.py

verify: lint typecheck test ## Python 编码交付门禁（Tier 0+1+2；适用范围见 AGENTS.md）
