# tests/manual —— 手动性能/压测脚本

本目录**不被 pytest 收集**（见 pyproject `addopts --ignore=tests/manual`）。

这些脚本依赖运行中的服务器/真实 LLM/数据库，用于手动性能验证与压测：

```bash
uv run python -m pytest tests/manual/test_db_resilience.py -q          # 需要真实 DB
uv run python -m pytest tests/manual/test_llm_async_performance.py -q  # 需要 LLM 配置
uv run python -m pytest tests/manual/test_upload_concurrent_performance.py -q  # 需要运行中的服务器
```

- 常规单元测试请写到 `tests/unit/`（纯净、无网络、外部依赖全部 monkeypatch）
- 需要真实服务的自动化测试写到 `tests/integration/` 并打 `@pytest.mark.integration`
