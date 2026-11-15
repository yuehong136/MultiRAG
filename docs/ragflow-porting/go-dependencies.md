# Go 依赖版本对照

比较主仓 `ca78eb334dee130dadbd82ccec2994c05369021b`、Go 对齐分支 `5097f2897cfa972ca8b80901acd86f485109f93a` 和冻结 RAGFlow `fcebcebe1e0e798b42c18e5593747cd20eb4c757`。全部读取固定提交，未使用上游脏工作树或 HEAD。

主仓到 Go 对齐分支有 **23 项版本下降、18 项上升、18 项新增、75 项删除、110 项版本不变**。新增/删除单列，不计作版本升降；直接/间接分类变化另计 9 项。主仓有 226 个显式 require（46 直接、180 间接），对齐分支为 169 个（50 直接、119 间接），冻结目标为 168 个（50 直接、118 间接）。

**23 项下降中，22 项精确采用冻结 go.mod 的版本；余下 x/image 精确采用冻结 go.sum 及冻结 Excelize 的传递依赖版本。** 对齐分支 go.mod 与冻结文件只有 module 名和额外显式记录 x/image 的差异；go.sum 与冻结文件逐字节相同。

## 全部降级

| 模块 | 主仓版本 | 对齐版本 | 分类变化 | 与冻结目标的关系 |
| --- | --- | --- | --- | --- |
| `cloud.google.com/go` | `v0.118.3` | `v0.116.0` | 间接 → 间接 | 精确等于冻结 go.mod |
| `cloud.google.com/go/auth` | `v0.15.0` | `v0.9.3` | 间接 → 间接 | 精确等于冻结 go.mod |
| `github.com/aws/aws-sdk-go-v2` | `v1.41.5` | `v1.41.3` | 直接 → 直接 | 精确等于冻结 go.mod |
| `github.com/aws/aws-sdk-go-v2/aws/protocol/eventstream` | `v1.7.8` | `v1.7.6` | 直接 → 直接 | 精确等于冻结 go.mod |
| `github.com/aws/aws-sdk-go-v2/internal/configsources` | `v1.4.21` | `v1.4.19` | 间接 → 间接 | 精确等于冻结 go.mod |
| `github.com/aws/aws-sdk-go-v2/internal/endpoints/v2` | `v2.7.21` | `v2.7.19` | 间接 → 间接 | 精确等于冻结 go.mod |
| `github.com/aws/aws-sdk-go-v2/internal/v4a` | `v1.4.22` | `v1.4.20` | 间接 → 间接 | 精确等于冻结 go.mod |
| `github.com/aws/aws-sdk-go-v2/service/internal/accept-encoding` | `v1.13.7` | `v1.13.6` | 间接 → 间接 | 精确等于冻结 go.mod |
| `github.com/aws/aws-sdk-go-v2/service/internal/checksum` | `v1.9.13` | `v1.9.11` | 间接 → 间接 | 精确等于冻结 go.mod |
| `github.com/aws/aws-sdk-go-v2/service/internal/presigned-url` | `v1.13.21` | `v1.13.19` | 间接 → 间接 | 精确等于冻结 go.mod |
| `github.com/aws/aws-sdk-go-v2/service/internal/s3shared` | `v1.19.21` | `v1.19.19` | 间接 → 间接 | 精确等于冻结 go.mod |
| `github.com/aws/aws-sdk-go-v2/service/s3` | `v1.97.3` | `v1.96.4` | 直接 → 直接 | 精确等于冻结 go.mod |
| `github.com/cloudwego/eino` | `v0.9.8` | `v0.9.5` | 直接 → 直接 | 精确等于冻结 go.mod |
| `github.com/goccy/go-json` | `v0.10.3` | `v0.10.2` | 间接 → 直接 | 精确等于冻结 go.mod |
| `github.com/google/s2a-go` | `v0.1.9` | `v0.1.8` | 间接 → 间接 | 精确等于冻结 go.mod |
| `github.com/googleapis/enterprise-certificate-proxy` | `v0.3.5` | `v0.3.4` | 间接 → 间接 | 精确等于冻结 go.mod |
| `github.com/json-iterator/go` | `v1.1.13-0.20220915233716-71ac16282d12` | `v1.1.12` | 间接 → 直接 | 精确等于冻结 go.mod |
| `github.com/mattn/go-runewidth` | `v0.0.8` | `v0.0.3` | 间接 → 间接 | 精确等于冻结 go.mod |
| `go.uber.org/multierr` | `v1.11.0` | `v1.10.0` | 间接 → 间接 | 精确等于冻结 go.mod |
| `golang.org/x/crypto` | `v0.52.0` | `v0.51.0` | 直接 → 直接 | 精确等于冻结 go.mod |
| `golang.org/x/exp` | `v0.0.0-20240506185415-9bf2ced13842` | `v0.0.0-20231226003508-02704c960a9b` | 间接 → 间接 | 精确等于冻结 go.mod |
| `golang.org/x/image` | `v0.38.0` | `v0.25.0` | 直接 → 间接 | 冻结 go.sum/Excelize 同版本；go.mod 额外补记 |
| `gorm.io/gorm` | `v1.25.10` | `v1.25.7` | 直接 → 直接 | 精确等于冻结 go.mod |

## x/image 的本地补记

`golang.org/x/image v0.25.0 // indirect` 是唯一额外显式 require。冻结目标使用的 `github.com/xuri/excelize/v2 v2.10.1` 自身 go.mod 要求 x/image v0.25.0；本机缓存中的该 go.mod 已按冻结 go.sum 的 `/go.mod h1` 校验通过。冻结 go.sum 本来就包含 x/image v0.25.0 的模块及 go.mod 校验和，对齐分支没有更改这份 go.sum。这里把主仓的较高直接锁回到冻结依赖链，并将间接版本显式补记；不能写成冻结 go.mod 原本已有该行。

## SDK 和原生依赖方向

| 依赖 | 主仓 → 对齐分支 | 冻结关系 |
| --- | --- | --- |
| AWS 核心、eventstream、S3 及相关内部模块 | 版本下降，逐项见上表；config/credentials/STS/Smithy 不变 | 均精确等于冻结版本 |
| Eino | `v0.9.8` → `v0.9.5` | 精确等于冻结版本 |
| Google GenAI | `v1.54.0` 不变 | 精确等于冻结版本 |
| OpenTelemetry 主模块/SDK/trace/metric/OTLP HTTP | `v1.41.0` → `v1.44.0` | 升级到冻结版本 |
| gRPC | `v1.79.3` → `v1.81.1` | 升级到冻结版本 |
| grpc-gateway/v2 | `v2.28.0` → `v2.29.0` | 升级到冻结版本 |
| `golang.org/x/net` | `v0.54.0` → `v0.55.0` | 升级到冻结版本 |
| Infinity SDK replace | `github.com/infiniflow/infinity/go v0.0.0-20260424025959-72028e662929` 不变 | 三份完全一致 |
| Office Oxide Go 模块 | `v0.1.2` 不变 | 精确等于冻结版本 |
| PDF Oxide Go 模块 | 主仓未列 → `v0.3.63` | 新增冻结原有依赖 |
| gopdf、NATS、SQLite | 主仓未列 → 冻结版本 | 新增冻结原有依赖；不恢复已删实现的旧依赖 |

本地 CI 的 Go patch 仍为 `1.25.14`、CMake 仍为 `4.0.3`，三份 CMakeLists 的最低版本均为 `4.0`。PCRE2/SIMDe 继续作为系统原生依赖；这些脚本没有为其声明可比较的具体包版本，不能根据 Go require 推断它们降级。对齐脚本新增 Office Oxide `v0.1.2` 原生库准备，与冻结 build.sh 的版本一致。Go 模块声明和 CI 准备代码不能代替实际原生二进制或部署验收。

## 其他模块指令与核对范围

三份均声明 `go 1.25.0`；均无 `toolchain`、`exclude`、`retract`、`tool` 或 `godebug` 指令。唯一 replace 的旧模块、目标模块及版本完全一致。module 名称保留本仓 `multirag`，冻结为 `ragflow`。

完整逐模块版本、直接/间接标记、增删及分类变化已单独记录。三份 go.mod 均用本机 Go 官方解析器读取并核对，`GOPROXY=off`、`GOSUMDB=off`、`GOTOOLCHAIN=local`；快照读取前后不变。这份依赖对照的只读审计未运行 tidy、下载或升级，也未更改源码和索引；合并后的实际构建与回归证据见 [Go 对齐记录](go-alignment.md)。
