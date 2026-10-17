# Azure 对象存储路径

SAS 和 SPN 均在所配置的物理 container/filesystem 内，使用
`{bucket}/{location}` 作为对象路径。`bucket` 是调用方的逻辑命名空间：
文档通常为知识库 ID，文件管理为文件夹 ID，运行时附件为
`{user_id}-downloads`。`location` 可以包含子目录；读、写、删除和存在性
检查必须传入同一对参数。不要把物理容器名当作这些逻辑 bucket 的替代值。

同一 bucket/location 的再次写入更新内容；不同 bucket 的同名文件独立。
SPN 在创建文件前按层创建父目录，已有目录的元数据与 ACL 不重置。
健康检查通过相同写入链操作 `txtxtxtxt1/txtxtxtxt1`；重复检查覆盖该探针。
读写重试耗尽或删除失败会抛异常，不以空结果报告成功。
`obj_exist` 仍是尽力探测，错误记录后返回 False，不能作为删除成功的唯一证据。
可选的 `tenant_id` 仅用于兼容透明加密包装器，命名空间仍由 bucket 决定。

## 下载签名

SPN 的 `get_presigned_url(bucket, location, expires, tenant_id=None)` 使用
`DataLakeServiceClient.get_user_delegation_key` 与 `generate_file_sas`，签发
只读、仅 HTTPS、绑定 `{bucket}/{location}` 的单文件 URL。`expires` 为秒，
范围 1–604500（七天上限扣除五分钟时钟偏差窗口）。SDK 负责路径的 URL 编码；
签名使用原始对象路径。委托密钥客户端在请求后关闭，错误向调用方传播。

SPN 需在存储账户、资源组或订阅层级具有 `generateUserDelegationKey` 权限，
并有目标对象读取权限；仅在容器层级授予读写权限未必足够。本代码不修改云上权限。
依据：[Azure user delegation SAS](https://learn.microsoft.com/en-us/rest/api/storageservices/create-user-delegation-sas)。

SAS-only 凭据不能重新签发更窄范围或更短有效期的 token。因此该模式明确抛出
`NotImplementedError`；不将容器 token 拼入下载 URL，也不承诺无法兑现的
`expires`。需要独立签名的消费者应使用 SPN；若需要为 SAS-only 增加签名凭据，
须另行审查配置与凭据保管方案。原实现调用的 `get_presigned_url` 并非 Azure
SDK 方法，无法生成有效 URL。

## 已有平铺对象的升级

旧版本忽略 bucket，将对象写在 `{location}`。新路径不会自动读取或删除
这些平铺对象；按文件名回退无法证明归属，会使一个知识库读到另一个
知识库的内容。若曾使用 Azure 后端，应在部署前审查存量；本次代码变更
不连接云资源、不迁移对象，也不能证明某个部署没有旧数据。

迁移应先生成只读清单，再经审查执行：

1. 固定数据库与对象快照，暂停该部署的上传、解析写入与删除，记录回退版本。
2. 从实际调用链和数据库关系解析每个逻辑 bucket/location：知识库文档及缩略图、
   文件管理的所属文件夹、附件、分块图片和其他派生对象分别核对。文件和文档有
   关联时按实际读取服务解析存储地址，不单凭名称或 File.parent_id 推断。
   对清单记录归属证据、源/目标、长度、内容摘要和对象版本/ETag，不记录凭据。
3. 对同一平铺源被多个 bucket 引用、源缺失、已有目标与源内容不同的项停止迁移，
   交由拥有者确定正确内容。旧版本已覆盖的文件须从版本历史、备份或原件恢复，
   不能用最后一份平铺内容复制到多个知识库来伪造恢复。
4. 仅对已确认归属的项执行带源 ETag 和目标不存在条件的复制，保留源。
   SPN 先创建目标父目录。通过独立客户端比较摘要/长度，并经应用读取、存在性
   和删除隔离验收后切换新版本。保存清单和所有失败，不能以请求成功代替读回。
5. 回退窗口保留旧对象与快照；若切换后已有新写入，回退前先按同一清单解决新旧
   内容分歧，不直接降级成平铺写入。删除旧对象另行审批，并确认没有剩余消费者。

## 验证边界

`make integration TESTS=tests/integration/test_azure_storage_protocol.py`
使用已安装的真实 Azure SDK，在 HTTP transport 边界提供受控协议状态。
它检查同名文件隔离、嵌套路径和特殊字符、更新、跨 bucket 不回退、
删除后的独立状态读回、重复健康探针与加密包装器参数契约。
SPN 下载专项还通过真实 SDK 的受控委托密钥交换及独立 HMAC 核对签名、
只读范围、有效期和修改 dataset 后的签名差异。SAS-only 拒绝独立签名。
此专项不访问共享基础设施，不代表真实 Azure 认证、RBAC、HNS、SAS 权限
或生产迁移已通过。真实环境验收必须使用自有容器及对象，完成独立读回和清理。
