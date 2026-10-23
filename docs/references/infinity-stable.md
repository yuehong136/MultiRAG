# Infinity 0.7.0 与当前兼容范围

当前 `pyproject.toml` / `uv.lock` 的 Python SDK 和 Compose 可选 `infinity` profile
都固定正式 `0.7.0`；镜像为 `infiniflow/infinity:v0.7.0`。本仓没有 Helm 入口。
这次对齐上游 `9d94527b1d98f18b8c09a32e24f0721c9a2cc5b1`，保留现有存储接口、
表和索引命名、元数据 JSON、严格失败语义及[有限竞争重试](infinity-metadata-retries.md)。
[dev7](infinity-dev7.md) 和 [dev6](infinity-dev6.md) 页面只保存各自历史验收。

稳定版仍使用 Thrift 协议 36。SDK 相对 dev7 的实际执行差异包含 SQL 表达式的
`LIKE` / `NOT LIKE` 与显式 `ESCAPE` 解析；新增 7 项真实服务回归，覆盖匹配、
取反、转义和非法表达式，不以导入或字符串版本比较代替存储操作。

Python 继续支持 `>=3.12,<3.14`。移除未使用的 Linux TensorFlow 测试依赖；
NumPy 在 3.12 固定 `1.26.4`，在 3.13 固定 `2.1.3`，避免 3.13 无可用 1.26 wheel
而退到不兼容的源码构建。其他包版本保持；3.13 的实际数值运行覆盖 Leiden、Numba JIT、
scikit-learn、Torch、OpenCV、XGBoost 及 ONNX OCR 推理，不能仅凭锁文件声称兼容。

2026-10-24 在独占 Linux arm64 Infinity 实例完成 Python 3.12.9 / 3.13.14
两版本专项，各 **31 passed，零失败和 skip**；各版本相关单测 **278 passed**。
集成线在最终稳定锁文件上另跑 3.12 专项，仍为 31 passed。覆盖真实 SDK 的标量、
全文、向量、混合检索、分页、身份、更新删除、JSON、连接归还和元数据竞争，
各运行的竞争次数以原始 JUnit 为准，不累加重跑结果。

实际镜像 ID 为 `sha256:992c87a68612ca13e5279b874ca3efc1170c9af42f995047a699cc8c77b7430b`，
server build 为 `v0.7.0-0-544d5b876`。实例只绑定本机端口 55614，使用自有卷，
私有验收配置将 buffer 限为 512MB、memindex 限为 128MB；未改共享服务配置。
创建的 scratch 数据库逐项登记，清理后独立 SDK 连接确认仅剩空 `default_db`；
自有实例、卷和运行配置随后移除，读回见本批交付证据。

本机原始记录在
`/Users/xldu/.local/state/multirag/validation/20261024-batch-after-41072ed4/line-I/`，
包含 SDK 差异、锁文件语义比较、双版本环境/包清单、命令退出码、JUnit、容器身份和资源账本。
集成复核见同根目录 `integration/infinity-final-312/` 与
`integration/infinity-cleanup-readback.json`；批次完整边界见[本批交付](../ragflow-porting/batch-after-41072ed4.md)。

首次严格 5 秒预检在冷启动并行导入时超时，原 exit=1 保留；独立 SDK 读取成功后，
同一预检及 31 项重跑通过，没有调整门禁。上述证据只覆盖 macOS arm64 客户端和新建 Linux
arm64 服务；生产持久卷升级/降级、Linux amd64、Windows、多副本及生产切换未验证。
完整组合和远程 CI 的状态另见批次记录，不能用本专项结果替代。
