# 技能资产库交付状态

2026-10-04：已授权Python与Go双后端、共用Web/CLI。当前已完成评估和合同v1冻结，
任何模块都尚未标记实现完成。实现范围与行为真值见 [CONTRACT.md](CONTRACT.md)。

| 单元 | owner | 状态/依赖 |
|---|---|---|
| 共同合同、集成、CLI | root | 合同v1冻结；等待删除正式交接 |
| Python/schema/migration | backend_space | 只读准备完成 |
| Go backend/Milvus | search_models | 只读准备完成；不改模型工作线拥有文件 |
| Web | web_cli | 只读准备完成；等待合同冻结 |
| 文件批删前置 | 独立批删工作线 | 已收WIP合同；正式SHA与验证待交接 |
| Go模型前置 | 独立模型工作线 | 已交接4ba652f3，GetEmbeddingModel/GetRerankModel保持签名；本功能精确按tenant_llm ID绑定 |

## 范围

首期：本地目录/ZIP、不可变版本、显式活动版本、空间CRUD、目录/下载、安装卸载、
配置/索引/检索、Web/CLI。外部技能源后续独立处理；不接Agent自动执行。
每个可交付单元独立验证和提交，不push。其他工作线WIP不暂存、不回滚。

## 证据

尚无本功能的实现提交、迁移验收、真实HTTP、对象或索引读回证据。
上游代码审计及依赖编译不能计为业务完成。
