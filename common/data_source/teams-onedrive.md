# Microsoft Teams / OneDrive

这两个源使用 Microsoft Graph app-only 凭据：`config.credentials` 包含非空
`tenant_id`、`client_id` 和 `client_secret`。构造和保存连接器不访问供应商；首次读取时
才创建 MSAL 客户端并获取 token，同一实例复用 token 缓存。连接测试使用既有
`POST /api/v1/connectors/{connector_id}/test`，先校验租户所有权，然后仅探测已保存配置，
不写 SQL、对象、解析队列或同步状态。REST API、Teams 和 OneDrive 支持此入口。

Teams 需要管理员同意的 `Team.ReadBasic.All`、`ChannelMessage.Read.All` 权限。
每个频道父帖及所有回复组成一个文档；HTML 保留，混合的纯文本会转义，附件名称、链接和
内联文本保留在正文，附件远程二进制内容不另行下载。团队、频道、消息及回复均遍历分页；
即使父帖较旧，也读取回复，以整个讨论串的最新修改时间判断增量窗口。

OneDrive 需要管理员同意的 `Files.Read.All`。`config.drive_ids` 必须明确给出 Drive ID
字符串数组，不允许省略、空字符串或重复项；`[]` 表示没有云盘，绝不扩大为全部租户。
空 Drive 范围的同步可成功结束；连接测试会明确拒绝空范围，避免将无请求误报为凭据验证成功。
Graph 的清单接口属于用户、组或站点，不使用上游的无范围 `/drives` 请求。
可选 `config.folder_path` 规范化为以 `/` 开始的子目录路径，拒绝 `.`/`..`，按路径段编码，
解析为文件夹 ID 后读取该子树的 delta。空路径表示每个指定云盘的根。

OneDrive 完整读取 delta 分页至最终 deltaLink，同一 ID 多次出现以最后状态为准，含删除标记。
文件 ID 为 `drive_id:item_id`；下载实际文件字节，使用实际字节数，保留 JSON MIME 文件与
合法的零字节内容。目前索引 PDF、Word、Excel、PowerPoint、TXT、Markdown、CSV。
文件夹和删除项不入库。权限、分页、下载、格式错误一律上抛，不能变成空成功。
单次 Graph 响应最多 100 MiB，单个云盘的完整 delta 最多 100,000 个不同 ID，超限失败。

两者的 `batch_size` / `sync_batch_size` 为正整数。worker 的完整导入和重建从时间零开始，
增量使用 `(poll_range_start, 本轮结束时间]`。OneDrive worker 每轮重新遍历 delta 并按时间过滤，
当前 SQL checkpoint 不保存不透明 deltaLink，因此不宣称跨任务的游标优化。直接使用
OneDrive connector checkpoint 的调用方可以持有严格校验且绑定范围的 delta links；
生成器返回新的 checkpoint，失败不修改输入。调用方必须在自己的入库成功后才保存返回值。
超过本轮结束时间的 OneDrive 文件拒绝推进游标，以免时间过滤后永久漏项。

Graph 和 MSAL 均使用有独立请求截止时间的 HTTP 客户端：每个请求只追踪自己的连接与 socket，
守护线程在取消/截止时间到达时关闭这些 socket，覆盖响应头、正文和 token 请求阻塞；返回前关闭
Session 并等待守护线程退出。正文使用单次 `read1`，不等待填满大块，禁用响应压缩并有大小上限。
分页、分块读和最后 SDK 响应后都检查取消/截止时间；
错误不返回供应商 token 错误正文。Graph continuation 限定为 HTTPS Graph 源和已配置云盘；
内容跳转只允许微软下载域且不转发 Graph bearer。源 ACL 不映射为本地知识库 ACL。
两者**不开放删除同步**：可见清单不代表租户的权威库存，未声明的来源不能因空结果触发删除。
`retrieve_all_slim_docs_perm_sync` 仅提供同范围清单，不能绕过 worker 的删除能力注册表。

本地测试使用已安装 Teams SDK、受控 Graph HTTP、真实 worker、隔离 SQL、对象存储和
队列读回。微软真实租户、管理员同意及供应商 ACL 行为需要另行使用授权环境验收。
