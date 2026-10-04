# 数据连接器

## 删除同步

`config.sync_deleted_files` 默认关闭。当前支持 GitHub、Confluence、Notion、Jira、Box、
S3、R2、Google Cloud Storage、OCI Storage、Airtable、Google Drive、Bitbucket、Gmail、GitLab。
首次导入与重建不执行删除核对；后续同步启用
开关时，调度器先收集完整源清单，成功入库本轮增量后再删除过期文档。

## 清单合同

`retrieve_all_slim_docs_perm_sync(callback=None)` 枚举整个配置范围，不接收时间窗口。
文档 ID 必须与内容导入使用的源 ID 一致，包含该连接器导入的附件。仅在生成器正常耗尽
全部分页后，清单才可用于删除核对。

| 结果 | 同步行为 |
|---|---|
| 完整非空清单 | 保留清单内文档，删除该知识库、该连接器来源下缺失的文档 |
| 成功的完整空清单 | 删除该来源的全部本地文档，包括最后一份文件 |
| 权限、分页、网络或枚举错误 | 本轮失败；部分清单不进入删除服务，也不调度成功的下一轮 |
| 内容入库或删除核对失败 | 本轮失败；入库失败不执行删除，删除失败不确认完成 |
| 开关关闭、首次导入、重建 | 沿用内容导入流程，不执行删除核对 |

清单表示配置范围内的源对象存在性。Blob 保留扩展名、图片及路径范围约束，尺寸增长或
暂时下载失败不会让存在对象从清单消失。Jira 清单使用项目、自定义 JQL、标签与附件配置，
不额外加入增量时间条件；Box、Notion、Confluence 包含其范围内的递归内容与附件。

Notion 删除同步要求有效的 `config.root_page_id`，并使用该根页面的递归遍历。配置根页面
会自动启用递归。无根的 workspace Search 模式仍可导入内容，但不能作为删除清单；
启用删除同步后该模式明确失败。[Notion Search 的官方限制](https://developers.notion.com/reference/search-optimizations-and-limitations)
说明搜索不保证穷尽可访问文档，正常分页结束也不能证明清单完整。

源服务若返回成功但静默隐藏失去权限的对象，清单接口无法区分该对象已删除还是不再
可见。启用开关意味着按当前凭证和配置范围核对；缩小范围或权限也可能导致本地删除。
显式的权限错误会阻断整份清单。

Airtable 按所选 base/table 的全部记录分页枚举附件，使用与入库相同的
`airtable:{record_id}:{attachment_id}`。多选、关联记录和协作者字段不作为附件。
清单不下载附件，不因尺寸阈值或下载链接暂时不可用而漏掉已存在的附件；缺失附件身份、
缺失记录集合、失效分页或内容下载失败会中断同步。增量内容仍沿用记录创建时间窗口。

Google Drive 按现有 OAuth/服务账号配置遍历个人 Drive、Shared Drive 和指定文件夹，
清单只保存文档 ID，不下载正文、不读取权限。文件身份沿用入库 URL/回退 ID，
所有清单查询不加增量时间窗口。完整空 Drive/文件夹可核对删除；`incompleteSearch`、
权限拒绝、失效分页或未遍历完指定范围均失败。文件夹访问先验证根存在，再递归子文件夹。
清单的遍历状态不影响后续内容检查点；内容窗口终点在清单前捕获，刷新凭据仍按原服务持久化。
OAuth 的 `my_drive_emails` 模式当前内容链不支持，删除同步明确拒绝；该模式应使用服务账号。

Gmail 清单按当前可读邮箱完整分页枚举线程 ID，不读取正文、不加入增量时间窗口。
开启删除同步时 OAuth 只枚举配置的自身邮箱；Workspace 服务账号必须完整读取域用户，
且包含配置的主邮箱，再完整读取每个邮箱。目录/邮箱拒绝访问、禁用邮箱、异常响应、
失效分页、循环 token 或任一正文读取失败都使本轮失败，不能降级成单邮箱或跳过后删除。
开关关闭时保留原内容读取和错误跳过行为；首次导入与重建仍不执行删除核对。

Gmail 不返回 Drive 的 `kind`，清单请求显式读取 `resultSizeEstimate` 并校验其非负整数形状；
缺少线程集合或集合为空的终页须明确报告零结果，否则拒绝作为完整空清单。
该字段是估算值，不用于判断收集的线程总数；完整性由正常耗尽全部分页确认。
入库身份继续使用原线程 ID。清单与正文沿用默认排除 Spam/Trash 的范围；清单保留已存在
但没有落在内容增量窗口的旧线程。[Gmail threads.list 合同](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.threads/list)
说明分页与 Spam/Trash 范围。

Gmail 当前内容增量使用时间查询，检查点来自邮件 `Date` 头，不是邮箱变更历史。
因此旧邮件移入 Trash 后被本地核对删除，随后还原时，仅出现在完整清单并不会自动补回
正文；应全量重建。需要可靠追踪还原、标签变化及线程内邮件删除时，建议另行设计
[History API 同步](https://developers.google.com/workspace/gmail/api/guides/sync)，包括游标过期
404 后全量同步。它涉及检查点和历史身份兼容，不在当前删除清单接线中自动迁移。
服务账号切换为 OAuth 或缩小域权限也可能改变可见范围；应先关闭开关，确认邮箱范围
并重建再开启。Workspace 含不可读/禁用邮箱时按安全合同阻断整轮；若需跳过，应先设计
显式邮箱范围及范围迁移，不能把读取失败当成删除。

Google 服务构建显式设置 `cache_discovery=False`，OAuth 与服务账号（含主体代理）均适用。
Drive 的 Shared Drive ID 缓存、防御性副本、加载新凭据及清单前后失效继续复用现有实现。
刷新后的 Google 凭据仍由现有同步 driver 保存，不新增第二条缓存或凭据持久化链。

Bitbucket 清单覆盖配置 workspace、repository 或 project 内的全部 OPEN、MERGED、DECLINED
Pull Request，使用与入库相同的来源 ID，无 `updated_on` 时间窗口。现有轻量 PR 枚举被复用；
缺失仓库/PR 身份、异常集合、分页循环及显式权限错误均失败。PR 映射失败不会确认检查点
或继续删除；正常内容同步保留原检查点和更新时间窗口。

GitLab 清单覆盖配置的代码文件、MR 和 issue；代码按默认分支递归分页，沿用内容导入的
路径排除规则与文件 URL，MR/issue 沿用 `web_url` 和 `state_filter`（默认 `all`）。
清单不下载代码正文、不读取提交历史、不加入增量时间过滤。正文或提交时间读取失败、
目录/对象分页失败都会阻断本轮；旧 MR 不会截断后续 MR/issue 的增量读取。
空仓库须由项目明确标记 `empty_repo`，缺失默认分支不能被默认为空清单。
默认分支、项目路径或状态过滤变化会改变可见身份/范围；应关闭删除同步并确认范围后重建。
GitLab 的嵌套目录与分页依赖 [Repository Tree API](https://docs.gitlab.com/api/repositories/#list-repository-tree)。

## 文档身份与删除链

新文档 ID 同时限定知识库和连接器。已经由当前知识库、当前连接器持有的历史 ID 原位
复用；删除核对识别原始源 ID 哈希、连接器限定哈希与知识库限定哈希，避免升级重复入库。
其他来源和其他知识库不参与删除候选计算。本轮清单生成后才出现、随后成功导入的源对象
也保留，避免源端在两次枚举之间新增对象而被本轮误删。枚举后、导入批次前、删除批次前
及最终调度提交时检查取消状态；取消不会被成功完成或失败处理覆盖，已提交的删除仍按读回
结果记录数量。失败恢复保留本轮开始的增量边界，不让批次进度越过未成功导入的源文件。

删除复用 `FileService.delete_docs` 和 `DocumentService.remove_document`。文档、任务、
文件引用及知识库计数在短事务内更新；仍有引用的文件保留，孤立对象才清理。事务通过
`DELETE … RETURNING` 保存被删任务 ID，提交后发出取消信号，不再查询已经删除的任务。
Milvus 确认索引不存在时跳过索引依赖的图片、chunk 与图谱操作；探针异常保留清理尝试。
其他后端保留原有清理行为。

现有文档删除服务仍将提交后的存储清理视作尽力清理：图片、对象、索引或图谱故障会记录
日志，不能用 SQL 行已删除推断存储全部已清理。该边界与枚举失败禁止删除的合同分别验证。

## Google Web OAuth

Gmail 和 Google Drive 的 `/api/v1/connectors/...` 与 `/v1/connector/...` OAuth
入口共用同一服务。启动和结果领取需要登录；回调使用发起流程的随机 state，并按来源
读取缓存。结果仅允许发起用户领取，领取后清除。state 与结果的缓存有效期均为 15 分钟。
现有 scope、离线访问、强制 consent 和已注册的旧回调路径继续使用。

启动流程显式生成 PKCE verifier，在授权 URL 中发送 S256 challenge，将 verifier
保存在服务端 state 缓存。回调重建 Flow 后将同一个 verifier 传入 token exchange；
不将 verifier 返回浏览器或写入最终凭证。Google Flow 工厂的生成开关需要显式启用，
不能依赖构造器默认值。升级前的缓存可能没有 verifier，仍按原方式尝试交换；若授权服务
拒绝，则清除该会话并要求重新授权。

取消授权或 token exchange 失败会清除 state，不生成结果；缺少授权码时保留 state，
便于在有效期内完成回调。正常完成后清除 state，顺序重复回调不再交换 token。
回调不要求登录，结果领取仍检查发起用户；PKCE 不替代现有 state 和用户归属检查。

隔离测试覆盖真实 Flow/OAuthLib 编码、HTTP 回调、Redis 缓存及凭证加载，但测试 token
服务运行在本机。真实 Google 登录、consent、客户端及 redirect URI 注册、Workspace
授权策略、refresh token 发放与 Gmail/Drive API 访问仍需使用部署环境的 Google 应用和
测试账号验证；本机模拟成功不能证明这些外部条件已满足。
