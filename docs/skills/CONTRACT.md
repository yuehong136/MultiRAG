# 技能资产库合同 v1

本合同约束 Python/FastAPI 与 Go 后端，以及共用 Web、CLI 消费者。
当前状态：合同v1已落地；已验能力与剩余限制见[交付状态](PROGRESS.md)。

## 范围与所有权

首期提供空间 CRUD、不可变版本包、目录/文件读取和下载、本地目录/ZIP 安装、
显式活动版本、模型配置、索引/检索、重建、卸载及空间删除。不执行技能代码，
不把技能自动注入 Agent。GitHub 固定 revision、其他外部来源为后续独立阶段。

Python 继续是当前生产入口。两端使用相同 PostgreSQL `usr_ai` schema；
schema 只由 Python 模型引导和 Alembic 管理，Go 不 AutoMigrate 技能表。
每个空间的 `backend_owner` 固定为创建端 `python` 或 `go`，客户端不可指定。
两端可以读同一空间；只有 owner 能写入和认领该空间操作。非 owner 写请求返回
409/`BACKEND_OWNER_MISMATCH`，不做跨后端 HTTP 转发。不支持在线转移 owner。
同一部署多个 worker 通过数据库租约竞争；两种后端不竞争同一任务。

首期空间仅属于鉴权得到的 tenant；不提供跨 tenant 分享、公共库或 KB 权限继承。
Python 复用 `async_current_tenant_id`；Go 必须保持 JWT/API-key 对 tenant 的等价映射，
不得信任请求体 tenant_id 或任意 X-tenant-id。不存在及越权资源统一 404。

## 身份、版本和包

资源 ID 为服务端生成的 32 位小写十六进制字符串；模型 ID 是数据库 BIGINT 的十进制
字符串，避免 JavaScript 精度丢失。名称不承担索引身份，重命名不改变 ID 或对象地址。
空间名 trim 后为 1–128 字符，name_key 为 Unicode NFC + casefold 后值。
技能名为 1–64 位 `[a-z0-9]+(?:-[a-z0-9]+)*`；名称和 SKILL.md frontmatter name 必须相同。
版本为完整 SemVer（包括合法 prerelease/build）；版本字符串精确唯一。
同版本同内容摘要且activate相同的重试返回已有结果，不同摘要返回409/`VERSION_CONFLICT`。
相同版本但activate不同返回409/`VERSION_ALREADY_INSTALLED`；改用显式活动版本端点。
已发布版本不可原位编辑；内容变化创建新版本。不存在破坏性 force 或跳过服务端校验。

根目录必须有 UTF-8 `SKILL.md`，包含 YAML mapping frontmatter 的 name、description；
不执行 YAML 标签或模板，description 最长 4096 字符，tags 为最多 32 个字符串。
文件路径为 NFC POSIX 相对路径（最多512个Unicode字符，每段最多255字符），禁止绝对路径、反斜线、空段、`.`、
`..`、NUL、控制字符、重复规范路径、软链及特殊文件。ZIP 不自动剥离任意顶层目录，
拒绝加密包。最多 1000 个文件、单文件 5 MiB、总展开字节 50 MiB；压缩上传也受
50 MiB 限制。边读边检查上限，不能仅信任 ZIP header、Content-Length 或客户端 manifest。
二进制资源可以保存和下载，但只提取有效 UTF-8 文本参与索引；响应告知被跳过的二进制数量。

规范 manifest 的 files 按 path 的 UTF-8 字节排序，每项为 path/sha256/size；
content_digest = SHA256(每项 `path + NUL + sha256 + NUL + decimal(size) + LF` 的连接)。
摘要和大小均由服务端验证。来源只记录 `source_kind=local`，不保存访问凭证。
索引从持久 manifest 读取真实内容；不接受客户端拼接的任意索引正文。

## HTTP 约定

根路径 `/api/v1/skills`。成功 JSON 为 `{code:0,message:"success",data:...}`。
同步成功 HTTP 200；异步受理 HTTP 202。错误 HTTP 400/401/404/409/413/422/503，
body 为 `{code:<相同HTTP整数>,message:<安全说明>,data:{error_code:<稳定字符串>}}`。
日志与对外错误不得包含 API key、DSN、源凭据或包正文。所有列表 page 从 1 开始、
page_size 默认 20、最大 100；排序有稳定 ID 次序兜底。时间字段 create_time/update_time
均为 UTC Unix 毫秒。`Idempotency-Key` 为 1–128 ASCII 可见字符；长操作要求提供并保证
幂等重放。同步创建/更新不维护幂等账本；更新使用 revision，创建重名返回冲突并由客户端
读回确认。同 tenant+kind+key 的长操作不同规范请求摘要返回 409/`IDEMPOTENCY_CONFLICT`。
终态失败不会因重复同键请求偷偷重新执行；使用 retry 端点。

同内容去重使用新幂等键时，将键与规范请求摘要绑定至原operation的
`payload.idempotency_aliases`，不增设第八张表。主键与别名均在事务级advisory lock
`hashtextextended('skills-request:'+tenant+':'+kind+':'+key,0)`下查验；同键其他请求仍409。
写别名与worker更新payload必须合并，不能覆盖彼此字段。需要重新锁定operation时先释放
space锁，统一operation→space顺序，避免上传去重与worker死锁。

| 方法与路径 | 输入 | data |
|---|---|---|
| GET /capabilities | — | backend、schema_version=1、sources=[local]、search_modes、search_available、storage_available |
| GET /models | — | models: `{id,name,provider,type,max_tokens,available,reason}`，仅当前 tenant 已启用 embedding/rerank；不含凭证；reason为安全能力码或null |
| GET /spaces | page/page_size/keywords | spaces,total,page,page_size |
| POST /spaces | name,description(默认空) | space |
| GET /spaces/{space_id} | — | space |
| PATCH /spaces/{space_id} | name?,description?,revision | space；过期 revision 冲突 |
| DELETE /spaces/{space_id} | — | accepted operation |
| POST /spaces/delete | ids（1–100个） | accepted operation，逐空间结果 |
| GET /spaces/{space_id}/skills | page/page_size/keywords/sort=name或create_time/desc | skills,total,page,page_size；SQL资产事实 |
| GET /spaces/{space_id}/skills/{skill_id} | — | skill,versions（完整版本元数据，不含字节） |
| POST /spaces/{space_id}/versions | multipart，见下文 | accepted operation |
| PUT /spaces/{space_id}/skills/{skill_id}/active-version | version_id或null,revision | accepted operation；null显式撤销活动版本 |
| DELETE /spaces/{space_id}/skills/{skill_id} | — | accepted operation，卸载全部版本 |
| POST /spaces/{space_id}/skills/delete | ids（1–100个） | accepted operation，逐技能结果 |
| DELETE /spaces/{space_id}/versions/{version_id} | — | accepted operation；活动版本返回409/ACTIVE_VERSION，须先切换或撤销 |
| GET /spaces/{space_id}/versions/{version_id}/files | — | files: `{path,size,sha256,media_type}` |
| GET /spaces/{space_id}/versions/{version_id}/file | path | 原始字节，安全Content-Type、nosniff、鉴权后下载 |
| GET /spaces/{space_id}/versions/{version_id}/download | — | ZIP；精确manifest路径与内容，Content-Disposition |
| GET /spaces/{space_id}/config | — | config |
| PATCH /spaces/{space_id}/config | 配置字段及revision | config,requires_reindex；不隐式破坏旧索引 |
| POST /spaces/{space_id}/reindex | — | accepted operation |
| POST /spaces/{space_id}/search | query,mode=keyword/vector/hybrid,page,page_size | skills,total,total_relation,mode,generation_id |
| GET /operations/{operation_id} | — | operation |
| POST /operations/{operation_id}/retry | — | accepted operation，失败项重试，已完成步骤不重复计数 |

`space`：id,name,description,backend_owner,state,revision,root_folder_id,
active_generation_id（可null）,create_time,update_time。
`skill`：id,space_id,name,description,tags,active_version_id（可null）,state,revision,
create_time,update_time。`version`：id,skill_id,version,content_digest,state,index_state,
file_count,total_size,create_time,update_time。
`accepted operation`：operation_id,state,resource_id（可null）。
`operation`：id,kind,state,phase,attempts,resource_id,progress,result,error,create_time,update_time。
state 为 pending/running/succeeded/partial/failed；result含逐项
`{id,state,error_code?,retryable}`。非终态 GET 不具副作用；客户端可轮询，终态停止。

search.skills每项为 `{skill_id,version_id,name,description,tags,version,score}`。
progress固定为`{completed:int,total:int}`；error为null或`{error_code,message,retryable}`。
result为`{items:[{id,state,error_code?,retryable}],skill_id?,version_id?,index_state?,skipped_binary_count?}`。
resource_id：install/delete_version为version ID，activate/delete_skill为skill ID，
reindex/delete_space为space ID，批量删除为null。kind枚举install/activate/reindex/
delete_version/delete_skill/delete_space/delete_skills/delete_spaces。
phase枚举staging/sealed/indexing/cleaning/done；没有字节输入的任务从sealed开始。
所有长操作端点（包含已成功的幂等重放）固定HTTP202和accepted形状；revision为JSON整数。
Web专用严格envelope校验应对成功状态200/202分别校验；非2xx解析上述错误envelope，
以data.error_code构造可消费错误，不能套用只允许HTTP200的旧rest200校验。

上传 multipart：字段 `manifest` 为 JSON `{name,version,activate:false,files:[{path,sha256,size}]}`；
目录模式使用重复 `file` part，顺序与 manifest.files 一致（不依赖 filename）；
ZIP 模式使用一个 `archive` part，files 可省略，由服务端生成并校验；两种模式互斥。
activate=true 是明确请求发布此版本，配置embedding时只有文件与索引准备成功才切换；
未配置embedding是显式例外：文件installed即可设为active并发布/下载，操作结果明确
index_state=unindexed，不能报告可检索；非空搜索返回503/INDEX_NOT_READY。
后续 config + reindex 将索引活动版本。无活动版本的技能不出现在检索结果中。

config：revision,embedding_model_id（可null）,rerank_model_id（可null）,top_k(1–100),
vector_weight(0–1),similarity_threshold(0–1),fields。
fields 为 name/tags/description/content，每项 `{enabled:boolean,weight:number}`；
权重0–10，至少一项enabled且正权重；默认3/2/1/0.5，content默认禁用，其余启用。
未提供字段不变，null仅用于明确清空模型引用。引用同tenant启用且类型匹配的
`t_ai_tenant_llms.id`，API不允许用名称选择同名的另一套provider实例。
模型、维度、字段配置变化增加config revision，新generation完成前继续使用其快照旧配置。
所有配置字段均进入generation快照；变更后requires_reindex=true。模型ID在所有JSON
（含payload、generation快照）均保持十进制字符串；仅SQL列使用BIGINT。
rerank在配置后必须实际执行；故障返回明确错误，不能悄悄伪装为已rerank结果。

搜索总数为候选数，`total_relation=eq`或`gte`说明截断；不将top100当资产总量。
空query只允许keyword，返回SQL活动版本列表；非空搜索按模式真正执行。资产删除状态
在SQL读回中过滤，即使索引物理删除失败也不能泄漏已删除版本。索引不可用返回503，
不能回退成全部资产或伪装为空结果。

## 共享 schema

所有新表包含 create_time/update_time BIGINT 毫秒、create_date/update_date TIMESTAMP，
沿用本地 BaseModel；租约、删除时间为 TIMESTAMPTZ。IDs为VARCHAR(32)，revision为BIGINT。
以下是双方实现的字段真值，新增字段必须同步此表及消费者测试。

| 表（usr_ai下） | 字段（除共同时间字段） |
|---|---|
| t_ai_skill_spaces | id PK,tenant_id,created_by,name VARCHAR(128),name_key VARCHAR(384),description TEXT,root_folder_id,state VARCHAR(24),backend_owner VARCHAR(8),revision,active_generation_id nullable,deleted_at nullable |
| t_ai_skills | id PK,tenant_id,space_id,folder_id,name VARCHAR(64),description TEXT,tags JSONB,active_version_id nullable,state VARCHAR(24),revision,deleted_at nullable |
| t_ai_skill_versions | id PK,tenant_id,skill_id,folder_id,version VARCHAR(128),content_digest CHAR(64),manifest JSONB,source_kind VARCHAR(24),state VARCHAR(24),index_state VARCHAR(24),file_count INT,total_size BIGINT,deleted_at nullable |
| t_ai_skill_version_files | id PK,tenant_id,version_id,file_id,relative_path VARCHAR(512),content_digest CHAR(64),size BIGINT,media_type VARCHAR(128) |
| t_ai_skill_search_configs | id PK,tenant_id,space_id,embedding_model_id BIGINT nullable,rerank_model_id BIGINT nullable,top_k INT,vector_weight DOUBLE,similarity_threshold DOUBLE,fields JSONB,revision |
| t_ai_skill_index_generations | id PK,tenant_id,space_id,config_revision BIGINT,source_revision BIGINT,config JSONB,dimension INT,index_name VARCHAR(128),state VARCHAR(24),error JSONB nullable |
| t_ai_skill_operations | id PK,tenant_id,space_id nullable,resource_id nullable,backend_owner VARCHAR(8),kind VARCHAR(32),state VARCHAR(24),phase VARCHAR(32),idempotency_key VARCHAR(128),request_hash CHAR(64),payload JSONB,progress JSONB,result JSONB,error JSONB nullable,attempts INT,lease_owner VARCHAR(128) nullable,lease_expires_at TIMESTAMPTZ nullable,next_attempt_at TIMESTAMPTZ nullable,revision BIGINT |

唯一约束：space(tenant_id,name_key) WHERE deleted_at IS NULL；skill(space_id,name)
WHERE deleted_at IS NULL；version(skill_id,version)；version_file(version_id,relative_path)
及file_id；config(space_id)；operation(tenant_id,kind,idempotency_key)。
tenant+resource复合引用约束保证同租户归属；active_version必须属于该skill，generation必须
属于该space。数值范围与状态CHECK均入库，不能只靠HTTP校验。版本删除保留tombstone；
同skill/version不可改变digest或复用成不同内容。

状态枚举：space与skill为active/deleting/delete_failed/deleted；version为staging/
installed/install_failed/deleting/delete_failed/deleted；index_state为unindexed/indexing/
ready/failed；generation为building/active/retired/failed/cleanup_failed/deleted。
未标nullable的列均NOT NULL，JSON默认{}（tags与result.items为[]），计数默认0，revision
默认1；文本description默认空，state无默认值、创建时显式指定。deleted_at仅在deleted
终态赋值，删除中继续占名。循环复合FK命名并DEFERRABLE INITIALLY DEFERRED；
File绑定不使用会销毁版本历史的级联删除。任何包/活动版本/配置/卸载变更递增space.revision。

File source_type统一为skill_space/skill/skill_version/skill_file；对象使用实际File
parent_id与location映射。空间目录/技能目录/版本目录及文件绑定只由领域服务写入。
通用Files API不可上传到、移动出入、重命名或删除这些受管资源。

## 操作、删除和恢复

metadata与operation先在同一短SQL事务登记，再执行外部副作用。租约基于数据库时钟，
`FOR UPDATE SKIP LOCKED`认领到期pending/expired running，递增fencing revision；
完成写回要求lease_owner与revision仍匹配。长动作续租。进程内task只负责调度，
任务真值是SQL，重启可恢复。payload必须足以定位暂存对象，不能引用内存或本机临时文件。
上传写暂存前先登记operation，逐文件记录持久进度；失败保留可精确清理的manifest。
上传的staging不可认领；只有全部字节、manifest验证通过后才能sealed。崩溃半包标记失败
并精确清理，不能假装成完整安装。写回还须lease_expires_at>数据库now；失租停止写入。
每个attempt使用新generation/不可变对象地址；旧worker不能覆盖新attempt输出。
混合owner的批量删除按项返回BACKEND_OWNER_MISMATCH，任务只处理当前owner资源。

索引构建使用全新generation和独立集合，以immutable skill/version ID作pk。
完成逐项计数、维度、hash和检索读回后，以space revision CAS切换active_generation。
源数据在构建期间变化则不发布过期generation，重新调度；不先drop当前索引。
旧集合清理失败需要持久记录并重试，不能让清理空桩表示完成。
activate用当前活动版本快照加候选version构建generation；最终CAS事务同时更新
skill.active_version_id、space.active_generation_id及revision，不能先改active再建索引。

删除先在SQL标记deleting/tombstone并阻止新写，所有列表、下载和检索立即隐藏；
依次清除全部相关generation索引、对象、File及关联、版本绑定，读回确认后到deleted。
失败保持隐藏、记录failed/partial，可重试；父目录只有后代清理成功才删除。
迟到的索引任务在写入及发布前校验revision/tombstone，不能复活资产。

现有文件批删合同：delete_files_async(uid,file_ids)返回(success,{success_count,errors})；
count为完整删除的唯一File行（含目录），不存在ID报错，失败后代保留祖先，合法兄弟继续。
跨存储非原子，errors不意味着零副作用。Skills内部清理适配此领域合同，不调用HTTP回环，
不在外层包事务，不解析errors字符串猜资源结果；结合操作记录按已授权绑定独立读回。
Go实现相同清理保证，不复用假成功engine Delete。正式交接证据记录在PROGRESS。

## 验收

### Milvus共享物理格式

集合为`skill_<generation_id>`，与知识库索引独立。每个启用字段按UTF-8完整字符分块，
每块最多`min(8192, floor(max_tokens*0.8))`字节；不丢弃空白或截断文件尾部。
行主键为`SHA256(version_id + NUL + field + NUL + decimal(从0开始块序号))`前32位hex；
字段为id、version_id、skill_id、field、text、content_digest、vector、sparse。
前三个ID为VARCHAR(32)，field为VARCHAR(16)，text为VARCHAR(32768)，摘要为VARCHAR(64)；
vector为实际维度FLOAT_VECTOR/COSINE/AUTOINDEX，sparse由text的standard analyzer和BM25
函数生成，SPARSE_INVERTED_INDEX的k1=1.2、b=0.75。每块摘要为版本包摘要。
空集合通过所选模型对`skills`编码获取真实维度，不伪造向量。

每个字段按版本取最高块分；keyword归一为`max(0,BM25)/(1+max(0,BM25))`，vector为
`clamp(COSINE,0,1)`，按字段权重加权并除以全部启用正权重总和。hybrid按vector_weight
混合两种分数；先应用similarity_threshold，后对候选实际rerank。rerank使用每版本最高分
代表块，保留provider原始有限分数，不将单候选强制归零。结果分数降序、skill/version ID兜底。
每字段原始候选上限`min(16384,max(limit*4,100))`；原始或唯一候选达到截断条件时返回gte。
top_k为固定候选预算（1–100）；分页只切片同一候选池，不能随page扩大再rerank造成页间漂移。
候选窗口截断时标gte，不能据此请求无限下一页；资产列表分页与总量来自SQL，不受top_k限制。

当前首个适配后端为Milvus；其他引擎返回明确不可用。rerank先适配已严格验证的
OpenAI-API-Compatible/VLLM `/rerank`协议，要求完整唯一的results[index,relevance_score]；
其他provider的available=false，不将旧helper吞错返回零的行为视为支持。

对象存储首期支持具备严格get_bytes读回的MinIO适配器；包装层与底层均须满足此能力。
无此能力时storage_available=false，上传、读取和删除明确503，不用吞错的get判断对象不存在。
两端共读的加密格式支持既有RAGF AES-128-CBC/AES-256-CBC；须配置相同密钥与算法。
Go暂不支持SM4，显式不可用；不得向Python加密空间写入另一种明文字节格式。

### 验收矩阵

两端运行相同黑盒合同：JWT/API-key、租户隔离、CRUD、并发重名、不可变版本、ZIP与
目录摘要、嵌套文件读回/下载、活动版本、模型配置清空及换维、超过100项分页、三种检索
及rerank、部分失败、重试、重启恢复、删除与重建竞争。SQL/对象/索引独立读回。
同库交叉读取测试保证schema/字节一致；非owner写入及任务认领拒绝。
Milvus为必需验收后端；ES/Infinity分别适配验收，未支持后端明确报错。
Python make verify/integration/smoke；Go build/vet及独立main；Web适用测试/build及实际
浏览器明暗主题、窄屏、上传检索删除闭环。外部真实模型验收与fake模型故障注入分别记录。
