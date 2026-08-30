# P3 ES256 签名密钥生成、配置与轮换

这份 runbook 面向部署和运维 MultiRAG P3 issuer 的人员。它生成的是 JWT 签名密钥，不是 HTTPS
证书：不需要 CSR，也不需要 CA。HTTPS 证书和 DNS/TLS 发布仍是独立的部署工作。

当前 file provider 的固定契约是：

| 项目 | 要求 |
|---|---|
| JWT 算法 | `ES256` |
| 椭圆曲线 | NIST P-256；OpenSSL 名称 `prime256v1` |
| 文件格式 | 未加密 PKCS#8 private PEM + SubjectPublicKeyInfo public PEM |
| `kid` | 1～64 个 ASCII 字母、数字、`_` 或 `-` |
| 私钥归属 | 只给 MultiRAG issuer；不得提交 Git、发送给 of_mcp 或写入配置正文 |
| 公钥发布 | 优先由 MultiRAG `GET /.well-known/jwks.json` 发布 |

运行时以 `password=None` 加载私钥，所以不接受加密 PEM。安全边界依赖主机文件权限、运行账号和部署
环境；生产环境最终应迁移到满足 `SigningKeyProvider` 契约的 KMS/HSM provider。

## 1. 生成密钥

生成前安装 OpenSSL，并先决定环境专属的 `kid`。测试和生产必须使用不同密钥，例如：

```text
p3-test-2026-08
p3-prod-2026-08
```

脚本将 `kid` 放入文件名：`<kid>-private.pem` 和 `<kid>-public.pem`。这样轮换时可以生成新 `kid`，
预发布新公钥，而不覆盖当前 active key。脚本默认拒绝覆盖；`--force`/`-Force` 只应用于可丢弃的测试
密钥，不应用于正在签发 token 的 key。两个脚本都拒绝把 key 写进 MultiRAG 仓库，也拒绝对文件系统
root、`ProgramData`、`HOME` 等宽目录直接改权限；`KeyDir` 必须是专用子目录。POSIX 已有目录必须先为
`0700`；Windows 已有目录只能包含脚本生成的 `*-private.pem`/`*-public.pem`，避免误改普通目录 ACL。

### Windows PowerShell

以将要运行 MultiRAG 的 Windows 服务账号作为 `-PrivateKeyReader`：

```powershell
.\scripts\init_mcp_signing_key.example.ps1 `
  -Kid p3-prod-2026-08 `
  -KeyDir C:\ProgramData\MultiRAG\secrets\p3 `
  -PrivateKeyReader 'CONTOSO\svc_multirag'
```

开发机省略 `-PrivateKeyReader` 时，默认为当前 Windows 用户：

```powershell
.\scripts\init_mcp_signing_key.example.ps1 `
  -Kid p3-test-2026-08 `
  -KeyDir C:\ProgramData\MultiRAG\secrets\p3
```

脚本会移除目录和 PEM 的继承 ACL，保留 `SYSTEM`，并只授予指定运行账号读取两个 PEM 所需的权限。
运行账号必须能读取 private 和 active public PEM，因为 MultiRAG 启动时会验证两者匹配。正式服务换账号
后必须重新执行 ACL 配置，不能继续依赖开发人员账号。

### macOS

```bash
sh scripts/init_mcp_signing_key.example.sh \
  --kid p3-test-2026-08 \
  --key-dir "$HOME/.config/multirag/secrets/p3"
```

### Linux

在实际运行 MultiRAG 的账号下生成，例如：

```bash
sudo install -d -o multirag -g multirag -m 700 /var/lib/multirag/secrets/p3
sudo -u multirag sh scripts/init_mcp_signing_key.example.sh \
  --kid p3-prod-2026-08 \
  --key-dir /var/lib/multirag/secrets/p3
```

也可以用环境变量设置 POSIX 默认目录：

```bash
export MULTIRAG_P3_KEY_DIR=/var/lib/multirag/secrets/p3
sh scripts/init_mcp_signing_key.example.sh --kid p3-prod-2026-08
```

POSIX 脚本把目录设为 `0700`、private PEM 设为 `0600`。运行时只接受当前进程 owner 持有、权限为
`0400` 或 `0600` 的 private PEM。不要通过放宽 group/world read 来解决服务账号错误；应修正 owner：

```bash
sudo chown multirag:multirag /var/lib/multirag/secrets/p3
sudo chown multirag:multirag /var/lib/multirag/secrets/p3/*-private.pem
sudo chown multirag:multirag /var/lib/multirag/secrets/p3/*-public.pem
sudo chmod 700 /var/lib/multirag/secrets/p3
sudo chmod 600 /var/lib/multirag/secrets/p3/*-private.pem
```

## 2. 独立验证生成结果

两个平台脚本已在落盘前执行下列等价校验。需要人工复核时，可运行：

```bash
openssl pkey -in /absolute/path/p3-prod-2026-08-private.pem -check -noout
openssl pkey -pubin -in /absolute/path/p3-prod-2026-08-public.pem -noout
```

再比较从 private PEM 派生的公钥与 public PEM；两个 SHA-256 必须完全相同：

```bash
openssl pkey -in /absolute/path/p3-prod-2026-08-private.pem -pubout -outform DER |
  openssl dgst -sha256

openssl pkey -pubin -in /absolute/path/p3-prod-2026-08-public.pem -outform DER |
  openssl dgst -sha256
```

脚本只输出公钥指纹和文件路径，不输出 private PEM 内容。公钥指纹用于人工核对，不是 `kid`，也不是
token verifier 的替代品。

## 3. 配置 MultiRAG

仓库的 `configs/service_conf.yaml` 只提供安全的 disabled 默认值。把实际配置写入 gitignored 的
`configs/local.service_conf.yaml` 或由部署系统注入；不要把 PEM 内容写进 YAML。

`local.service_conf.yaml` 的顶层 section 是**整体替换**，不是深合并。若本机已经有 `identity:`，应在
现有完整 section 中合入下面字段，不能只复制 `mcp_issuer` 后丢失其他 identity 配置。

macOS/Linux 示例：

```yaml
identity:
  mcp_issuer:
    enabled: true
    issuer: https://multirag.example.com
    client_id: multirag-channel-host
    ttl_seconds: 300
    clock_skew_seconds: 30
    jwks_cache_ttl_seconds: 300
    resources:
      of_mcp:
        audience: https://of-mcp.example.com/mcp
        registered_scopes:
          - leave:read
        allow_provider_identity: true
    key_provider:
      kind: file
      active_key_id: p3-prod-2026-08
      private_key_file: /var/lib/multirag/secrets/p3/p3-prod-2026-08-private.pem
      public_key_files:
        p3-prod-2026-08: /var/lib/multirag/secrets/p3/p3-prod-2026-08-public.pem

  # Issuer ready does not itself enable outbound P3 delegation. Enable this
  # only after the reviewed policy/grant artifacts exist.
  mcp_delegation:
    enabled: false

  # Native MCP form persistence is a separate switch and key ring.
  mcp_interactions:
    enabled: false
```

Windows 的 path 也必须是绝对路径；YAML 使用单引号，避免反斜杠转义：

```yaml
    key_provider:
      kind: file
      active_key_id: p3-prod-2026-08
      private_key_file: 'C:\ProgramData\MultiRAG\secrets\p3\p3-prod-2026-08-private.pem'
      public_key_files:
        p3-prod-2026-08: 'C:\ProgramData\MultiRAG\secrets\p3\p3-prod-2026-08-public.pem'
```

关键约束：

- `issuer`、resource `audience` 必须是 canonical HTTPS URI；本机 HTTP 地址不满足 secure 配置。
- `audience` 必须与 of_mcp verifier 的 expected audience 精确一致。
- `registered_scopes` 只能登记经过审查的真实 scope；请假只读纵切使用 `leave:read`。
- 请假资源需要当前 Channel 身份时才设置 `allow_provider_identity: true`；其他 resource 默认不携带。
- `active_key_id` 必须出现在 `public_key_files`，其 private/public 必须为同一 keypair。
- 所有 PEM 路径必须是 absolute、regular、非 symlink；active private PEM 不得加密。
- 启用 `mcp_delegation` 还需要绝对路径的 `tool_policy_file` 和 `grant_policy_file`；本页不生成这两个
  authority artifact。

启用并重启 API 后，先检查 health/log 中没有密钥装配错误，再检查公开 JWKS：

```bash
curl --fail --silent https://multirag.example.com/.well-known/jwks.json
```

响应应只含 EC public JWK，且能看到 active `kid`、`alg=ES256`、`crv=P-256`、`use=sig`；不得出现
private PEM、文件路径或错误堆栈。issuer disabled/unready 时该端点返回安全的 HTTP 503。

### 本机独立 TLS JWKS publisher

EIM-O5 的 loopback 联调可以把 JWKS 从 API 进程拆到只持有 public PEM 的独立进程。先在仓库外生成
一份不含 private key、DSN 或 Channel secret 的严格 JSON manifest：

```json
{
  "format": 1,
  "active_key_id": "p3-local-2026-08",
  "jwks_cache_ttl_seconds": 300,
  "public_key_files": {
    "p3-local-2026-08": "/absolute/deployment/secrets/p3-local-2026-08-public.pem"
  }
}
```

manifest 只允许这四个键；public PEM 路径必须绝对。publisher 本身不读取应用配置、P3 private PEM、
数据库或 Channel secret，只在 loopback 上提供 `/.well-known/jwks.json` 与 `/livez`：

```bash
uv run python -m api.identity.jwks_publisher \
  --host 127.0.0.1 --port 9277 \
  --public-key-manifest /absolute/deployment/artifacts/p3-public-keys.json \
  --cert-file /absolute/deployment/pki/jwks-cert.pem \
  --key-file /absolute/deployment/pki/jwks-key.pem
```

TLS key 是独立的 HTTPS server key，不能复用 P3 JWT signing key。POSIX 上 TLS private key 必须由进程
owner 持有且为 `0400`/`0600`；manifest、证书与 public PEM 不可 group/world writable，publisher 使用的
public PEM 还必须由当前进程 owner 或 root 持有。部署 doctor 还应把该进程的
JWKS bytes 与主 API 的 canonical JWKS 做逐字节对账。该 publisher 是 EIM-O5 本机联调面，不替代
生产 DNS、企业 PKI、KMS 或多副本 rotation barrier。

## 4. of_mcp 如何使用

of_mcp 只需要信任以下服务端配置事实：

```text
issuer       = https://multirag.example.com
jwks_uri     = https://multirag.example.com/.well-known/jwks.json
audience     = https://of-mcp.example.com/mcp
token_use    = mcp_access
algorithm    = ES256
```

精确配置字段以 of_mcp 仓库当前 strict verifier runbook 为准。不要把 MultiRAG private PEM 复制到
of_mcp，也不要让 of_mcp 从聊天、表单或工具参数接收 key/JWKS 地址。生产 JWKS 必须通过受信任 HTTPS
访问，并按 of_mcp 的固定 issuer/JWKS/audience 配置 fail closed。

仅配置 `mcp_issuer` 只会让签发服务和 JWKS ready；它不会自动启用 P3 delegation、给 Agent 授权、
打开 MCP 原生表单或开放写操作。

## 5. 无中断轮换

正常轮换不要覆盖旧 key，按以下顺序执行：

1. 用新 `kid` 生成新 keypair，例如 `p3-prod-2026-11`。
2. 保持旧 private/active `kid` 不变，把新 public PEM 加入 `public_key_files`。
3. 滚动重启所有 issuer 副本，确认 JWKS 同时发布 old/new `kid`。
4. 把 `active_key_id` 和 `private_key_file` 切到新 key，旧 public 仍保留。
5. 再滚动重启所有 issuer 副本，确认新 token 使用新 `kid`，of_mcp 能验证新旧 token。
6. 从最后一个副本完成切换时起，至少保留旧 public：

   ```text
   ttl_seconds + clock_skew_seconds + jwks_cache_ttl_seconds
   ```

   默认是 `300 + 30 + 300 = 630` 秒；实际值更大时按实际配置计算。
7. retention 到期且确认没有旧签发副本后，才从 `public_key_files` 删除旧 public，再滚动重启。
8. 安全归档或销毁 retired private PEM；不得把它作为“备份”提交到仓库。

脚本只生成文件，不修改配置、不重启副本，也不能代替多副本发布屏障、监控、回滚或 KMS rotation。

## 6. 备份、丢失与泄露

- private PEM 备份必须进入企业 secret manager/加密备份，访问权限至少等同生产 issuer 主机。
- private PEM 丢失但未泄露：停止用该 `kid` 签发，生成新 key 并按轮换流程切换；无法从 public key
  恢复 private key。
- private PEM 疑似泄露：这是应急轮换。立即阻止旧 key 继续签发，发布并切换新 key；必要时提前从
  JWKS 移除旧 key 以主动使旧 token 失效。该做法会牺牲正常无中断 retention，应按安全事件处理。
- 测试和生产不得复用 keypair、`kid` 命名空间或 secret 目录。

EIM-O1 只有在 KMS/secret 托管、DNS/TLS、网络策略、多副本轮换和回滚演练全部验收后才能完成；本
runbook 与两个 file-key 脚本只是其中的 bootstrap slice。
