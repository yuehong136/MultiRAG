# Azure Blob 数据源

在 Web 数据源中选择 Azure Blob Storage，保存后关联知识库并启动同步。
这是读取用户 Azure 容器的连接器；应用自身文件的 Azure 存储配置独立维护。

`source` 为 `azure_blob`，`config.auth_mode` 必须显式指定：

| auth_mode | config.credentials 必填字段 |
|---|---|
| `account_key` | `account_name`、`account_key`、`container_name` |
| `connection_string` | `connection_string`、`container_name` |
| `sas_token` | `container_url`、`sas_token` |

选中模式决定认证字段；切换模式后遗留的其他模式字段不会参与认证。
保存和编辑仅作本地配置检查，不连接 Azure；同步时才构造 SDK 客户端。
当前保存配置的“测试连接”接口尚不支持 Azure。

示例（凭据只使用占位内容）：

```json
{
  "auth_mode": "account_key",
  "credentials": {
    "account_name": "myaccount",
    "account_key": "<account key>",
    "container_name": "documents"
  },
  "prefix": "reports/",
  "batch_size": 2,
  "allow_images": false,
  "sync_deleted_files": false
}
```

`batch_size` 是 1–1000 的整数，默认沿用 `INDEX_BATCH_SIZE`，控制每批入库数。
`allow_images` 是布尔值，默认 false。`prefix` 默认为空，按原样匹配 Blob 名称前缀，
保留大小写和前导斜线；`reports` 也会匹配 `reports-old`，选择虚拟目录应填 `reports/`。
文件扩展名复用本地文本、文档和可选图片过滤，并支持 `.doc`、`.xls`、`.ppt`、`.htm`。
超过现有 Blob 尺寸限制的对象不下载正文。
含独立 `.` 或 `..` 路径段的 Blob 名称会使本轮失败，避免 HTTP URL 规范化改变容器或存储前缀。

连接端点仅接受 Azure 公有云、中国云和美国政府云的标准 Blob HTTPS 主机。
`container_url` 必须是无 query 的完整容器 URL，SAS 另填在 `sas_token`；不接受
用户名、端口、fragment、自定义域名或模拟器。连接字符串中的 BlobEndpoint 必须与
AccountName、EndpointSuffix 指定的标准账户一致；不支持额外的非 Blob 服务端点字段。
SDK 不跟随重定向，异常、文档 metadata 和同步日志不包含凭据或带签名的 URL。

每轮同步完整分页枚举配置范围，清单全部成功后才按 ETag 指纹下载新增或变化对象；
未变化对象不下载，重建强制下载。下载使用清单的 If-Match，并验证正文对应的 ETag、
修改时间和大小。分页、权限、消失对象或版本竞争使本轮失败并保留原检查点；恢复后
重新枚举。文档身份使用完整 Blob 名称，再由同步层加入知识库和连接器命名空间；
metadata 保存 container、prefix 和 ETag，不保存凭据。

Azure 删除同步暂不支持：省略 `sync_deleted_files` 或填写 false，true 会被拒绝。
连接器虽提供完整 slim 清单接口，仍不进入 worker 的删除同步 allowlist。
停止或超时后等待有限 SDK 请求收尾并关闭客户端，再返回任务结果。

本地受控 SDK/HTTP、数据库和对象存储验证不代表真实 Azure 账号、RBAC、HNS 或 SAS
服务端权限已验收。
