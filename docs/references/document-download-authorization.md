# SDK 文档下载权限

实际注册的 `GET /api/v1/documents/{document_id}` 使用 beta API token。
`Authorization` 必须为两段 Bearer 格式；`APIToken.beta` 必须恰好匹配一行，
关联租户必须存在且有效。重复 token 配置整体拒绝，不取第一行，也不记录 token。

下载仅允许该 token 租户拥有的有效知识库及有效文档。知识库的 `me`/`team` 标签、
用户加入其他团队，以及知道 document ID，均不能把 token 扩展到其他租户。
`GET /api/v1/datasets/{dataset_id}/documents/{document_id}` 使用现有普通 API token
依赖，另绑定 URL 中的 dataset 与 document；两个入口共用授权存储地址解析。

存储读取前再次检查 tenant、dataset 和 document：

- 无文件映射时，使用文档自身的知识库 namespace 与登记的 location。
- 有映射时，要求唯一且源 File 存在；本地文件的 namespace 必须是源文件所属租户的
  File 目录。错误租户目录、缺失目录、缺失文件和重复映射拒绝。
- 文件关联 API 允许把源文件共享到已授权知识库。该映射允许读取共享源文件的原文，
  不要求源文件租户等于知识库租户；存储目录仍必须属于源文件自身租户，避免错误
  parent_id 重定向到另一租户对象。非本地文件仍使用文档知识库 namespace。
- namespace 和对象 key 必须非空，拒绝绝对路径、反斜杠、点/上级目录段及控制字符。
  正常相对目录、空格、Unicode 和百分号保留。

未授权、资源失效或地址不可信均在调用存储 `get` 前返回业务错误；合法空对象仍返回
现有 empty-file 业务错误。成功返回原始文件字节与 UTF-8 下载文件名。
内部可信 worker 等调用的旧 `get_storage_address` 保留，本次未扩大其调用契约。

`tests/integration/test_beta_document_download.py` 验证注册路由选择、真实 token 数据库查询、
scratch PostgreSQL 和自有 MinIO 原文字节。测试记录真实存储读取次数，拒绝时为零，
并独立读取 SQL 和对象内容确认不变；所有者的 private/team 下载、团队成员自身 token
拒绝其他租户、唯一身份、失效/孤立资源、dataset/document 错配和地址错误均覆盖。
测试资源用后逐项清理检查。它不证明生产部署或其他存储适配器的实际运行。
