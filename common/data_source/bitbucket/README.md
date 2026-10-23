# 添加 Bitbucket 数据源

Bitbucket 连接器读取 **Bitbucket Cloud** 中的 Pull Request（PR），包括 OPEN、MERGED 和
DECLINED 状态。导入内容是 PR 标题、描述、作者、审阅者、分支及状态等信息，保留 PR 原文链接；
不克隆仓库代码，也不抓取 diff、评论或附件。当前 API 地址固定为 `api.bitbucket.org/2.0`，
不支持自建 Bitbucket Server / Data Center。

## 准备账号和范围

- 一个能够读取目标 workspace、仓库及 PR 的 Atlassian 账号和该账号的邮箱。
- 该账号的 Bitbucket Cloud API token。连接器使用邮箱和 token 进行 HTTP Basic 认证；
  创建方式见 [Atlassian API token 指南](https://support.atlassian.com/bitbucket-cloud/docs/using-api-tokens/)。
  按 [API token 权限说明](https://support.atlassian.com/bitbucket-cloud/docs/api-token-permissions/)
  授予 `read:repository:bitbucket`、`read:pullrequest:bitbucket`，并确保账号本身有目标范围的读取权限。
- Workspace ID：例如仓库地址 `https://bitbucket.org/my-workspace/repo1` 中的 `my-workspace`。
  指定仓库时使用 `repo1` 这样的 slug；指定项目时使用项目 key。

## 创建连接器

在 **设置 → 数据源** 新建数据源，选择 **Bitbucket**，填写名称和以下字段：

| 页面字段 | 配置键 | 填写方式 |
|---|---|---|
| Workspace | `config.workspace` | Workspace ID，不填完整 URL |
| 索引模式 | `config.index_mode` | `workspace`（默认）、`repositories` 或 `projects` |
| Repository Slugs | `config.repository_slugs` | 仓库模式填写，例如 `repo1,repo2` |
| Projects | `config.projects` | 项目模式填写，例如 `PROJ1,PROJ2` |
| Bitbucket 账号邮箱 | `config.credentials.bitbucket_account_email` | Token 所属 Atlassian 账号的邮箱 |
| Bitbucket API Token | `config.credentials.bitbucket_api_token` | 对应账号的 API token |

Workspace 模式会枚举账号可读的 workspace 仓库；Repositories 模式读取列出的仓库；
Projects 模式先枚举指定项目中的仓库，再读取 PR。

切换模式时清空不使用的 Repository Slugs / Projects 字段。当前 worker 按实际非空字段
选择范围：仓库列表优先，其次项目列表，两者均为空时枚举 workspace；`index_mode` 仅用于
表单展示，不能单靠切换它清除旧范围。仓库或项目模式的列表为空会回退到 workspace 范围。

保存仅持久化连接器和调度配置，不会立即验证 Bitbucket 凭据，也不会直接创建知识库索引。

## 关联知识库和同步

1. 打开目标知识库的 **配置 → 关联数据源**，选择已创建的 Bitbucket 连接器。
2. 首次关联会安排全量同步，由同步 worker 拉取内容。当前 Web 初次关联默认关闭自动解析；
   如需自动分块和生成向量，开启该关联的 **自动解析**，或对导入的文档手动启动解析。
3. 在数据源详情查看同步日志和错误，并在知识库文档列表检查导入及解析状态。
   连接器需处于启用状态，且同步 worker、解析 worker 和所需模型可用。

后续按刷新频率同步 `updated_on` 时间窗口内的 PR；重建重新读取所选范围的全部 PR。
解除关联会取消该关联尚未完成的同步任务，已导入文档会保留。

删除同步开关 `config.sync_deleted_files` 默认关闭。开启后，后续增量同步使用完整 PR 清单
核对本知识库、本连接器来源的过期文档；首次导入和重建不做删除核对。权限或分页失败会
阻断该轮核对。切换范围或缩小账号权限前，应先关闭删除同步并确认范围，详见
[连接器删除同步合同](../README.md#删除同步)。

## 排查连接问题

- 401：检查账号邮箱、token 是否匹配或已过期。
- 403：检查账号及 token 的仓库、PR 读取权限。
- 只保存连接器但没有文档：检查知识库关联、连接器启用状态、worker 和同步日志。
- 文档已导入但未生成向量：检查关联的自动解析开关或手动解析结果、嵌入模型配置。

范围与字段实现见 [连接器](connector.py)、[PR 映射](utils.py) 和
[同步 worker](../../../core/svr/sync_data_source.py)。
