# S3 兼容对象存储

对象存储后端与[数据源连接器](../common/data_source/README.md)是两条配置链。
Python 服务选用 `STORAGE_IMPL=AWS_S3` 时，`common.settings.S3` 将生效配置中的 `s3`
段交给 `core/utils/s3_conn.py`，由 boto3 创建 S3 client。

## 配置

以下为 Tigris 的 S3 兼容配置示例。凭据使用本地私有配置，不提交真实值：

```yaml
s3:
  access_key: 'tid_YOUR_ACCESS_KEY'
  secret_key: 'tsec_YOUR_SECRET_KEY'
  region_name: 'auto'
  endpoint_url: 'https://t3.storage.dev'
  bucket: 'multirag'
  prefix_path: 'production'
  signature_version: 'v4'
  addressing_style: 'virtual'
```

AWS S3 使用对应区域的 `region_name` 和 endpoint；Fly.io 的 Tigris endpoint 可使用
`https://fly.storage.tigris.dev`。账号、区域、endpoint、签名与寻址方式需与实际服务匹配。
`region` 不是 AWS_S3 的区域字段；该名称仍供独立的 OSS 后端使用。Docker 模板保留
`S3_REGION` 环境变量名，但渲染为 `region_name`；类型化环境覆盖使用
`MULTIRAG_S3__REGION_NAME`。

`signature_version` 交给 botocore Config，`addressing_style` 交给它的 `s3` 配置；
临时凭据还可配置 `session_token`。类型模型保留额外字段，但 SDK 只读取适配器已支持的
字段，保留额外字段不表示任意 boto3 参数都会自动透传。

配置优先级和文件覆盖规则见[配置与资源](development.md#配置与资源)。文件覆盖按整个
section 替换，切换时需填写完整 `s3` 段。选用外部对象存储不自动迁移历史对象，也不会
删除 Compose 中的 MinIO；共享 MinIO 或 Milvus 使用的对象存储仍须保留。

## 桶与路径

未配置 `bucket` 时沿用调用方的逻辑桶名；配置后所有对象写入该固定桶。
设置 `prefix_path` 后，键变为 `{prefix_path}/{逻辑桶}/{原始键}`，逻辑桶仍参与隔离。
从多桶切换到固定桶、修改前缀或切换服务时，需先备份 SQL 与对象存储，再按该映射复制
历史对象；只改配置会让旧对象无法读取。切换前应在隔离桶验证写入、读取、删除和旧对象
映射，再按部署流程切换并保留原配置与对象以便回退。

本配置链的自动化验证覆盖配置加载与 SDK 参数，不代表真实 Tigris 账号、网络、权限或
迁移数据已验收。
