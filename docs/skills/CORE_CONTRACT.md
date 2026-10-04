# Skills 核心合同

协议为 `ragflow-skills-v1`，固定入口 `/api/v1/skill-core`。Python 和 Go 分别原生实现，
消费者只共享此处的行为合同，不共享数据库或物理索引。实现、实测和剩余限制见
[PROGRESS](PROGRESS.md)；Python 资产增强见 [CONTRACT](CONTRACT.md)。

## 协议与部署

- `SKILLS_API_PROTOCOL` 选择 `/api/v1/skills` 的别名：`multirag-assets-v1`（默认）或
  `ragflow-skills-v1`；非法值阻止启动。固定两个 namespace 的语义不随别名变化。
- Web 用 `VITE_SKILLS_API_PROTOCOL` 明确选择消费协议；CLI `skills` 使用资产扩展，
  `skill-core` 与 `ls/search/cat skills/...`、`install-skill/uninstall-skill` 使用核心。
- `GET /api/v1/skill-protocols` 描述部署能力。没有启用的引擎必须声明不可用并拒绝写入，
  不允许持久化一个假成功状态。旧 Go assets 在核心模式只读并排空既存任务。
- 两后端必须使用独立数据库，包括 File、租户和模型绑定。可共用基础设施集群，但对象
  prefix 和索引命名空间独立。模型 ID 由目标部署分配，切换时不能照抄为同一模型。
- 本期必须显式指定已创建且授权的空间；缺少或空白 `space_id` 为 HTTP400，
  `data.error_code=SPACE_REQUIRED`；不存在或越权空间为404。没有隐式 `default` 空间。
  这是已确认的本地入口约束；Go 内部服务仍保持来源结构。

## HTTP 与消费者

JSON 成功为 `{code:0,message:...,data:...}`；消费者同时检查HTTP与业务码。
空间删除受理为HTTP202，其余核心成功为200。`202` 不表示清理已完成。
上游同步结果保持同步；不生成伪 operation ID、version ID 或 active-version。

| 方法与路径 | 输入 | data |
|---|---|---|
| GET /spaces | page/page_size/keywords | spaces,total |
| POST /spaces | name,description?,embd_id?,rerank_id? | space |
| GET /spaces/{id} | — | space |
| PUT /spaces/{id} | name?,description?,embd_id?,rerank_id?,top_k? | space |
| DELETE /spaces/{id} | — | `{deleting:true,space_id}` |
| GET /space/by-folder | folder_id | space |
| GET /config | space_id,embd_id? | 配置 |
| POST /config | space_id,embd_id,vector_similarity_weight,similarity_threshold,field_config,rerank_id,top_k | 配置 |
| POST /search | space_id,query,page,page_size,sort_by,sort_order | skills,total,query,search_type |
| POST /index | space_id,embd_id?,skills | indexed_count |
| DELETE /index | space_id,skill_id | true |
| POST /reindex | space_id,embd_id? | indexed_count,total_skills,version,failed_count |
| GET /models | — | models（本地模型身份适配，额外端点） |

`space` 为 id,tenant_id,name,folder_id,top_k,status，以及可选description、embd_id、
rerank_id、时间字段。**HTTP status 是 active/deleting/deleted**；Go表内1/2/0不是wire值。
删除失败保持对外deleting并给可消费错误；不把失败标成deleted。成功后GET为404。
客户端轮询真实空间状态，失败停止并显示可重试操作，不构造持久任务。

搜索命中为skill_id,name,folder_id,description,tags,version,score等字段。一个技能的
索引身份是技能名；目录ID用于真实文件定位。分页total不能用当前页长度替代。
空查询列出已索引条目，无索引时返回空；未索引目录由 Files 列表展示，Web/CLI无需先索引
才能看到已上传内容。不得把 Files 目录存在等同于索引已完成。
模型ID保持十进制字符串（包括超过JavaScript安全整数的ID）；不用显示名任取实例。
核心rerank配置保存/返回，当前不宣称执行；Python扩展协议另提供严格实际rerank。

## 文件、版本与一致性

空间目录下为技能目录、版本目录和文件；支持已有无版本目录。重建选择最高三段数字
版本；这不等于显式活动版本或不可变发布。多个版本的真实字节均可读取。

- `POST /api/v1/files` JSON `{name,parent_id,type:"folder"}` 建目录；multipart
  `parent_id` + 重复 `file` 上传，合法相对filename保留嵌套层级。
- `GET /files?parent_id&page&page_size` 返回files,total；`GET /files/{id}`返回实际字节。
  普通根目录隐藏Skills树，但授权的空间目录仍能通过Files读写。
- `DELETE /files` 支持`{file_ids:[...]}`和已有`{ids:[...]}`；同时给两字段拒绝歧义。
  Go 成功 data 为 `true`，Python 为 `{success_count,errors}`；消费者显式接受这两种
  已声明形态，不从 `true` 伪造逐项计数。非零业务码或部分失败始终显示失败明细。
  核心技能目录、版本、附件的删除由服务端负责索引联动。普通资产扩展的不可变目录
  仍禁止绕过领域服务修改。HTTP200的部分失败业务码不能呈现为删除完成。
- 核心目录的普通 Files move/rename 本期明确拒绝，不允许绕过领域索引与对象定位。
  Python 遇到已关联 dataset document 的核心 File 返回409/LINKED_DOCUMENT，
  避免绕过文档删除合同；普通文件批删仍沿用既有处理。
- `DELETE /index`只移除指定技能索引，不删除文件；重新索引可从持久文件恢复。
  Files删除必须阻止检索继续引用已删字节，保留失败清理地址供重试。
- 索引从已授权持久目录读取；客户端传来的正文不能成为越权或任意索引注入入口。
  构建失败保留旧的可查询索引；删除/上传并发不能发布过期索引或复活已删资源。
- 进程重启恢复空间与文件清理。SQL、对象和索引不是跨存储事务，持久状态记录未完成
  副作用；对象与File地址不可在实际清理成功前丢弃。

## 持久化边界

Go使用两张私有Skills表（空间、检索配置）和现有File目录；必要可靠性字段用于恢复清理
与安全切换，不暴露成新的公共领域模型。服务/实体/DAO/handler保持来源对应关系。
Python使用自己的两张core表与File；七张资产表继续作为增强实现。两端均不直读对方表。
索引重建在各自引擎完成；逻辑合同不要求相同物理集合、评分数值或内部任务格式。

Python 当前保留退役索引与异常上传对象地址：候选索引600秒后、旧索引发布后、异常上传
恢复后继续由每2秒 sweep 严格清理，以捕获迟到IO重新写入。成本随历史记录增长；后续
垃圾回收必须建立IO已结束的可靠边界，不能为减少记录而丢弃未完成副作用的地址。

迁移使用原始包、内容摘要、版本和用户可见元数据，目标重新绑定租户/模型并重建索引。
不迁移任务租约或物理集合。Python显式active不能冒充Go最高版本；不满足相同选择时必须
报告差异。当前无存量技能/版本，未执行生产切换或数据清理。

## 验收

共同HTTP/真实CLI脚本为`tests/support/skill_core_contract.py`，分别运行于独立后端数据库；
覆盖空间、目录、倒序上传与最高版本、正文/中文附件、索引删除、Files卸载联动和202到404。
两端另测租户、模型身份、大列表、失败不丢旧索引、迟到IO与重启恢复。Web需真实浏览器
分别验证Python资产增强、Python核心与Go核心。具体命令和证据以PROGRESS为准。
