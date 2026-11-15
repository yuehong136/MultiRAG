# Go Skills

Go Skills 使用 `internal/service/skill_space.go`、`skill_indexer.go`、`skill_search.go`
和 `internal/handler/skill_search.go` 的原实现；Go 尚未部署。生产 Python 的
[核心合同](CORE_CONTRACT.md)和[资产合同](CONTRACT.md)独立维护。

## 当前接口

原 router 在鉴权后注册 `/api/v1/skills`：空间列表/创建、空间读取/PUT/删除、
按 folder 查空间、GET/POST config、POST search、POST/DELETE index 和 POST reindex。
请求、默认值及返回以这些原 DTO 为准；没有 `/skill-core`、`/skill-assets`、
`SKILLS_API_PROTOCOL` 别名切换、operation worker 或私有 Milvus 适配层。

空间与配置使用原 Go MySQL 表，正文和版本来自原 File 目录和对象存储。
`SkillIndexerService` 负责版本选择、读取正文、生成向量和写入原 DocEngine；
`SkillSearchService` 使用原 keyword/vector/hybrid 查询路径。
模型通过原 ModelProviderService 绑定。CLI 使用对应的原命令和 `filesystem/`。
索引保留实际最新版本，源文件读取失败不会先删除旧索引；删除失败保留可重试状态和文件地址，
仅明确的索引 HEAD 404 可按已删除处理。SQL、对象与索引间不承诺分布式原子提交。
原对象 location 使用文件名；实际存储名称限制可能先于 SQL 字符数限制生效，
255 个汉字不能据本地 rune 校验推定为可上传。

Go 的数据库、对象和索引与 Python 业务资源保持独立；Go 初始化不执行 Python
Alembic 或共享旧资产协议。数据转移和生产切换不属于本次源码收敛。

## 验证

使用[Go 本地验证](../development.md#go-并行实现的本地验证)准备登记的 MySQL、Redis、
NATS、MinIO、ES、Infinity、native 及实际 CLI，然后运行对应原 Go 包的 race。
`internal/service/skill_core_test.go` 覆盖当前 Skills 消费者；
`internal/cli/canonical_skill_http_test.go` 检验 CLI 实际请求和响应处理，受控 HTTP
夹具不能代替真实索引服务的运行验收。

旧 Python 启动私有 Go live-server 的验收脚本已随旧协议移除。
实际组合 SHA、范围、首败和清理证据见[Go 对齐记录](../ragflow-porting/go-alignment.md)。
本地隔离测试不代表真实供应商或生产验收。
