# 模型配置诊断

`LLMBundle` 通过 `LLM4Tenant` 初始化模型。模型类型沿用配置读取合同：
优先读取 `mdl_type`，为空时使用 `model_type`；这与
`TenantLLMService.model_instance` 的 provider 选择一致。

配置有效但 provider 不支持该类型时，初始化报错包含租户、已解析模型类型与模型名。
错误消息不序列化配置，不包含 API key 或 endpoint。配置缺少模型类型时，
继续由 `model_instance` 报告 `Model config is missing mdl_type/model_type`。

对应回归位于 `tests/unit/test_tenant_llm_api_base.py`，覆盖本地字段、备用字段及
两字段同时存在时的优先级，并检查凭据不进入错误消息与日志。
