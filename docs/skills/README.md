# 技能资产库

独立管理本地SKILL.md版本包，提供空间、文件下载、索引和检索。Web入口为`/skills`，
CLI入口为`skills help`。上传内容不会被执行，也不会自动装入Agent。

实现约定见[共同合同](CONTRACT.md)，本次验证及限制见[交付状态](PROGRESS.md)。

## 运行顺序

1. 由Python既有数据库引导/Alembic流程应用`c5e7f9a1b3d5`及其前置迁移；
   七张表位于PostgreSQL `usr_ai`。Go不建表、不管理迁移。
2. Python正常启动FastAPI即启动技能操作worker，关闭时停止；Go独立server也启动自己的worker。
   Python生产入口无需配置或启动Go。两端使用共同数据时需保持数据库、对象存储前缀、
   Milvus数据库、鉴权密钥及存储加密配置一致。
3. Web或CLI连接所选后端创建空间。空间owner由创建端确定，后续写请求继续发给该端。
   另一端允许读取；写入返回409，客户端不自动跨后端转发。
4. 上传本地目录或根含SKILL.md的ZIP，等待operation成功后读回版本。
   无embedding可先保存并设置活动版本；需要检索时选精确模型ID并执行重建。
5. 搜索配置修改不会提前销毁当前索引；只有新generation构建、验证并发布后才切换。

API前缀`/api/v1/skills`，认证沿用JWT/API key。模型ID始终用十进制字符串，不能转换为
JavaScript number。Web与CLI使用相同HTTP合同；[CLI用法](../../internal/cli/README.md#skills-asset-library)。

## 操作与故障恢复

HTTP202只表示已受理。保存operation ID与幂等键，轮询到succeeded后再确认结果；
failed/partial要检查稳定错误码及逐项结果。相同请求重用幂等键可以查回原操作；
显式retry恢复可重试失败，不创建重复资产。同步创建若响应不确定，先列表/读回确认。

持久化租约支持worker重启恢复。半包不能恢复为安装成功，会清理暂存对象；删除先隐藏，
再依次清理对象、索引与File绑定。跨存储操作非原子，清理失败继续隐藏并保留恢复地址。
不要绕过领域服务直接删除SQL行、手动置成功或用普通Files API修改受管目录。

先查`capabilities`与`models[].available/reason`；未支持的存储、检索引擎或模型驱动明确拒绝。
本期搜索适配Milvus，存储适配MinIO；外部真实模型仍需在部署环境做单独验收。
空embedding输出、非法维度/非有限数、rerank缺项、清理读回失败均不能标记成功。

数据库回退只允许七表均空；已有资产或operation时迁移拒绝downgrade，避免丢失恢复记录。
需要停用时先停止入口写入和worker，保留数据及对应版本服务进行恢复，不直接删表。

## 复验

```bash
make verify
make integration INTEGRATION_WORKERS=0 TESTS='tests/integration/test_skill_assets.py tests/integration/test_skill_search_store.py tests/integration/test_skill_http.py tests/integration/test_file_batch_delete.py tests/integration/test_db_bootstrap.py'
make integration INTEGRATION_WORKERS=0 TESTS=tests/integration/skill_go_acceptance.py
```

Go验收需要符合go.mod的工具链，默认调用`go`，也可设置`MULTIRAG_TEST_GO`为其绝对路径。
真实集成仅使用隔离数据库、对象前缀和索引，外部模型请求使用本地HTTP替身。
浏览器验收入口`tests/integration/skill_ui_server.py`须显式运行，并指定新的私有
`SKILL_UI_HANDOFF_PATH`；该文件含临时认证信息，验收后写同路径`.done`触发清理，勿归档凭证。
