# Salesforce 数据源

数据源 `source` 为 `salesforce`，读取 Connected App 执行用户可查询的 CRM 记录。
Connected App 需启用 OAuth Client Credentials Flow 与 API 权限；对象权限由该执行用户控制。
保存与编辑只做本地配置校验，不发出 OAuth 或 Salesforce 请求；当前测试连接接口不支持此来源。

```json
{
  "credentials": {
    "instance_url": "https://your-org.my.salesforce.com",
    "client_id": "connected-app-consumer-key",
    "client_secret": "connected-app-consumer-secret"
  },
  "objects": ["Account", "Contact"],
  "api_version": "v59.0",
  "batch_size": 100,
  "sync_deleted_files": false
}
```

`instance_url` 必须是规范的 Salesforce HTTPS origin（`*.salesforce.com`），
不含尾斜杠、路径、端口、用户名、查询参数或 fragment。OAuth 返回的 canonical
instance URL 必须与配置的 origin 相同；组织迁移后应先编辑配置。请求不跟随重定向，
分页地址在发送 access token 前验证相同 origin 和 Query More 端点。
凭据只放 credentials，错误不包含响应正文、URL 或底层传输错误文本。

`objects` 省略或为 null 时选择 Account、Contact、Opportunity、Case、Knowledge__kav。
只有默认选择中的 Knowledge__kav 在组织明确没有该对象时可跳过；显式选择的对象不可用、
权限失败或服务错误都会让同步失败。可用字符串 CSV 代替数组；对象名必须是 API 标识符。
显式 `[]` 或空字符串表示空范围，且不进行供应商 IO。`batch_size` 为 1–1000 的整数；
`api_version` 格式为 `v59.0`，省略时默认 v59.0。

每条记录转换成 `.txt`，文档源 ID 为 `Object/RecordId`，更新时间为 SystemModstamp。
字段排序与嵌套 JSON 排序稳定，0 和 false 会保留。元数据含 object、record_id 和 web_url。
worker 为首跑、重建与增量同步固定同一结束时间；增量下界包含上次水位，按稳定 ID 幂等重放
相同时间戳的记录。分页失败、循环游标、记录缺少 ID/有效时间戳或不完整结果会终止任务，
恢复原始 poll 起点；此前已落库的批次会在重试时幂等更新。
SOQL 使用官方支持的整秒时间格式；带小数秒的窗口先向外取整查询，完整核验分页数量后，
仅发布原始精确窗口内的记录。

checkpoint 接口只在完整遍历后发布结果；当前 worker 持久化的是现有全局 poll 水位，
不持久化该类的逐对象 checkpoint。请求有总时限与取消检查，取消会中断本次 socket 并等待
请求线程退出、关闭资源后返回。

删除同步暂不支持，`sync_deleted_files` 只能省略或为 false；true 会在保存或运行前被拒绝。
完整 slim 列表接口不赋予 prune 权限。受控 HTTP、SQL、对象存储与 Web 验收不代表真实
Salesforce 账号权限、组织定制字段或生产环境已经验收。
