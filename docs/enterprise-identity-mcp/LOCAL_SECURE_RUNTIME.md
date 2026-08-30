# MultiRAG + of_mcp 本机标准启动

这份文档描述日常源码启动，不是 EIM-O5 验收编排器。正常运行时：

- MultiRAG 的可提交默认配置在 `configs/service_conf.yaml`；本机覆盖写入已忽略的
  `configs/local.service_conf.yaml`；
- Channel 进程密钥位于 `~/.config/multirag/secrets/`；P3/JWKS/policy 制品位于
  `~/.config/multirag/mcp/`；
- of_mcp 的服务拓扑位于 `deploy/profiles/secure.toml`，本机环境位于其仓库根 `.env`，私钥位于
  `~/.config/ofmcp/`；
- `scripts/secure_leave_e2e.py` 只用于可重复验收、故障回滚和证据包，不是产品启动器。

## 启动顺序

分别打开终端，按以下顺序启动。所有命令都从对应仓库根目录执行。

1. MultiRAG API：

```bash
sh scripts/run_api.example.sh
```

2. 只读 TLS JWKS publisher：

```bash
uv run python -m api.identity.jwks_publisher \
  --host 127.0.0.1 \
  --port 9277 \
  --public-key-manifest "$HOME/.config/multirag/mcp/jwks-public-keys.json" \
  --cert-file "$HOME/.config/multirag/mcp/pki/issuer-cert.pem" \
  --key-file "$HOME/.config/multirag/mcp/pki/issuer-key.pem"
```

3. 需要本机 OA 模拟器时，在 of_mcp 仓库启动：

```bash
uv run ofmcp-ecology-mock serve \
  --private-key "$HOME/.config/ofmcp/ecology-mock/private.pem" \
  --host 127.0.0.1 \
  --port 18765
```

4. secure of_mcp Gateway，在 of_mcp 仓库启动：

```bash
uv run ofmcp ops doctor --profile secure --format human
uv run ofmcp serve --profile secure
```

5. MultiRAG Channel supervisor：

```bash
sh scripts/run_channel_supervisor.example.sh
```

API 应先于 supervisor 启动；supervisor 会派生并管理 Channel workers，不要单独手工启动 worker。
本机固定端点为 API `http://127.0.0.1:8123`、JWKS `https://127.0.0.1:9277`、Gateway
`https://127.0.0.1:8765/mcp`、Ecology mock `http://127.0.0.1:18765`。

of_mcp 的首次密钥、TLS、A6 migration、源码/容器差异见其
`deploy/secure-gateway.md`。真实公司网络内 OA 只需替换 of_mcp `.env` 的 `OFMCP_LEAVE_*` adapter
配置并保持严格 TLS 校验；无需更换 MultiRAG 启动器或新增 leave 专用脚本。请假仍为 preview-only，
不得启用 create/draft/submit。

## 停止顺序

先停止 Channel supervisor（由它收掉 workers），再停止 of_mcp Gateway、Ecology mock、JWKS
publisher，最后停止 MultiRAG API。数据库、P3 policy/grant、A6 ledger 和密钥文件都保留。
