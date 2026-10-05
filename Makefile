# MultiRAG 验证/测试统一入口（人类与 AI 通用）
# 分层验证体系说明见 AGENTS.md。日常门禁：make verify
.DEFAULT_GOAL := help
UV := uv run --no-sync
TESTS ?=
PYTEST_ARGS ?= -q
INTEGRATION_SUITE ?= core
INTEGRATION_WORKERS ?= 2

.PHONY: help install fix lint typecheck test test-all coverage integration integration-db integration-system integration-infinity integration-consumer integration-all smoke mcp-compat verify acceptance acceptance-api

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

test-all: ## Tier 2+3：单元和核心集成在独立进程运行
	$(UV) pytest tests/unit -q
	$(MAKE) integration

coverage: ## 单元测试 + 覆盖率报告
	$(UV) pytest tests/unit -q --cov --cov-branch --cov-report=term-missing --cov-report=xml

integration: ## Tier 3：核心集成；按 TESTS 依赖准备服务并保存证据
	$(UV) python scripts/run_integration.py --suite $(INTEGRATION_SUITE) --workers $(INTEGRATION_WORKERS) -- $(TESTS) $(PYTEST_ARGS)

integration-db: ## 仅 PostgreSQL 契约（不收集 HTTP/模型/向量库测试）
	$(MAKE) integration INTEGRATION_SUITE=db

integration-system: ## 独立进程退出与恢复契约
	$(MAKE) integration INTEGRATION_SUITE=system

integration-infinity: ## Infinity 后端契约（所选服务缺失必须失败）
	$(MAKE) integration INTEGRATION_SUITE=infinity

integration-consumer: ## 独立 Web 客户端验收（需要 WEB_DATASET_CHECKOUT）
	$(MAKE) integration INTEGRATION_SUITE=consumer

integration-all: ## 核心 + Infinity + 独立 Web 客户端；不允许意外 skip
	$(MAKE) integration INTEGRATION_SUITE=all

smoke: ## Tier 4：冒烟测试（对运行中的服务器打健康端点；启动：uv run python -m api.multirag_server）
	$(UV) python scripts/smoke.py

acceptance: ## 隔离产品验收：上传、解析、分块、检索、保存读回及明暗主题截图
	$(UV) python scripts/run_product_acceptance.py

acceptance-api: ## 仅隔离 API 产品验收（不代表页面或视觉检查通过）
	$(UV) python scripts/run_product_acceptance.py --api-only

mcp-compat: ## EIM-F2：隔离 MCP 1/2 解释器，运行双方向真实协议兼容矩阵
	uv lock --check --script tests/compat/mcp/legacy_server.py
	uv lock --check --script tests/compat/mcp/modern_server.py
	$(UV) python scripts/check_mcp_compat.py

verify: lint typecheck test ## Python 编码交付门禁（Tier 0+1+2；适用范围见 AGENTS.md）

.PHONY: eval eval-assets eval-generation
# Install the optional eval group explicitly; normal integration never downloads models.
eval-assets: ## 显式准备并校验固定版本 OCR 与中文向量模型
	$(UV) python scripts/provision_eval_assets.py

eval: ## 独立本地 OCR / 中文检索质量回归（需安装 eval 依赖组）
	$(UV) python scripts/run_integration.py --suite eval -- $(PYTEST_ARGS)

eval-generation: ## 显式真实模型评测（需专用模型配置，缺失即失败）
	$(UV) python scripts/run_integration.py --suite eval-generation -- $(PYTEST_ARGS)
