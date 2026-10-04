# Go Skills 核心运行边界

Go 核心是独立后端，数据通过显式导入导出迁移。不得把 Go 指向 Python 的业务数据库、
对象前缀或索引命名空间。可共用基础设施实例，但应配置独立数据库和 MinIO bucket/prefix。
启动会检查 Python 核心资产和旧 Python owner 资产，发现存量时拒绝初始化 Go 核心。
此检查不能代替部署层的数据隔离。

## 协议选择

- `/api/v1/skill-core`：空间、Files 目录/版本、索引与检索核心。
- `/api/v1/skill-assets`：保留原资产协议及旧数据。
- `SKILLS_API_PROTOCOL=multirag-assets-v1`（默认）：`/api/v1/skills` 指向资产协议。
- `SKILLS_API_PROTOCOL=ragflow-skills-v1`：`/api/v1/skills` 指向核心协议；资产入口只读，
  旧 worker 继续排空已存在任务，新资产写入返回 `503 PROTOCOL_READ_ONLY`。
- 显式空值及其他环境变量值导致启动失败；仅未设置时使用默认值。`GET /api/v1/skill-protocols` 返回实际别名及能力。

消费者应使用固定 namespace，不根据 payload 猜协议。核心 config/search/index/reindex 必须
显式提供 `space_id`，缺失或空白返回 `400 SPACE_REQUIRED`；`default` 不创建隐式空间。

## 配置与存储

核心初始化仅管理 `t_ai_go_skill_spaces`、`t_ai_go_skill_search_configs` 两张 Go 私表，
不降级、删除或迁移旧七表。空间表的 `core_state` 是文件清理地址的最小恢复记录，
不提供 operation API。File 树是技能与版本的事实来源；空间目录 marker 为 `skill_space_core`。
Python core 目录和旧资产受管树继续在 Go 通用 Files API 隐藏并保护。

当前注册并验收的是独立 Milvus SDK 适配和严格 MinIO 存储。现有通用 Milvus 写入桩不会被调用。
Elasticsearch、Infinity 尚未注册核心适配，不能宣称支持；默认资产模式下核心返回明确 503，
显式选择核心模式则对不支持的引擎拒绝启动，已有普通功能不通过假写入开启核心。

索引对外为稳定 `skill_<tenant>_<space>` 名称。重建先写私有集合并读回验证，再切换 alias；
物理集合带空间摘要，删除失败仍可定位清理。上传成功不等于索引可用。
一个技能仍为一个逻辑索引文档和一个模型向量；Milvus 适配器内部将正文与元数据分成
不超过 8192 UTF-8 字节的片段，全部读回验证后再发布单条 head。查询只让已发布 head
指向的片段参与候选，失败准备的片段不能挤掉仍有效的旧文档。删除逻辑文档会删除全部片段。
BM25 使用文档最佳正文片得分，这是本地 Milvus 排序适配，不等同于 ES/Infinity 整文得分。
向量保留核心模型最大输入长度规则；大正文的完整关键词覆盖不意味着模型编码了超出其限制的全文。
单文件约 5 MiB 的 UTF-8 正文和大元数据属于专项验收；不据此承诺任意 50 MiB 包均可索引。

## HTTP 消费

核心保持 12 个业务端点：空间列表/创建、空间读取/PUT/删除、按 folder 查空间、
GET/POST config、POST search、POST/DELETE index、POST reindex。成功响应使用
`{code:0,message,data}`。业务错误必须检查 code，不能仅凭 HTTP 200 判断成功。
空间 wire status 为 `active`、`deleting`、`deleted`；删除返回 202 的
`{deleting:true,space_id}`，完成后读取为 404。清理失败保持 deleting，后台重试。

目录创建使用 `POST /api/v1/files` JSON `{name,parent_id,type:"folder"}`；上传同一路径使用
multipart 重复 `file` 字段及 `parent_id`，filename 可携带合法嵌套相对路径。
删除接受 `{file_ids:[...]}` 或兼容 `{ids:[...]}`，两者同时出现会拒绝。删除技能、版本或附件
由服务端撤下相关索引、清理对象和 SQL，再重建剩余目录；失败保留地址，重启继续。
核心目录的 move/rename 当前明确拒绝；同空间移动需要后续独立实现。

上传在 Put 前持久记录唯一对象地址，File 提交后才认领。失败上传的地址保留为 tombstone，
后台反复清理，覆盖存储请求超时后迟到完成的写入；已提交 File 引用的对象不会被清理。
这些 tombstone 目前不会自动过期，空间删除后也继续重试，有持续存储删除调用和 SQL 状态成本。
后续 GC 需依据存储提供方的迟到写入边界另行设计，不能在首次删除成功后丢弃地址。

空查询列已索引文档，没有索引则为空；未索引目录通过 Files 展示。索引列表使用完整分页，
不受 top_k 或 100 项总量截断。默认版本为最高的
合法三段数字目录；显式索引可以指定其他版本目录。正文和元数据从持久文件读取，
不会相信任意客户端正文或跨空间 folder_id。没有版本目录时兼容技能目录直属 SKILL.md。

`GET /skill-core/models` 返回 `{models:[...]}`，优先用十进制字符串 id 配置 `embd_id`。
支持精确旧模型行、唯一 `model@provider`，以及现有 Go resolver 的
`model@instance@provider`；歧义、禁用和未实现驱动均明确拒绝。查询通过 Query=true 的
bound wrapper；目前开放已验证的对称 embedding 驱动，其查询和文档编码协议相同。
核心保存并读回 rerank 配置，能力声明 `rerank:false`，不会伪装为执行过 rerank。

## 验证

本地独立 Go HTTP/SQL/MinIO/Milvus 与 CLI 合同：

```sh
MULTIRAG_TEST_GO=go make integration TESTS=tests/integration/skill_core_go_acceptance.py INTEGRATION_WORKERS=0
```

测试创建另一 scratch 数据库，仅复制隔离测试身份及模型 seed；不复用 Python 服务数据库、
File 树、索引、operation。模型 provider 使用受控 HTTP 替身；这不证明真实外部 provider
或生产切换已验证。构建与 vet 按仓库 Go 约定分别覆盖 internal 和各独立 main。

本机浏览器验收采用同源 Vite 反向代理连接独立 Go HTTP 服务；跨源 CORS 部署未验收。

本轮证据分为不同代码时点：完整七项隔离验收通过后，删除请求隔离与配置 SQL 故障修复又完成
三项定向回归（包含原重建/文件清理用例）。前者覆盖共同 HTTP/CLI、Files 与索引各 101 项分页、
显式空间、5 MiB UTF-8 正文、未发布片段候选隔离，以及上传 SQL 失败后的进程重启与迟到写清理。
后者确认恢复旧删除计划不会把另一请求报告成功、配置查询失败不会丢弃恢复计划。
这不是生产部署验收，也不是 Elasticsearch/Infinity 或外部模型服务验收。
