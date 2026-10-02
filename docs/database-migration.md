# 数据库 schema、升级与恢复

本指南说明 MultiRAG Python 后端的 PostgreSQL 升级入口和恢复边界。2026-10-02
核对基于已提交代码 `2f46a4407e04862555ba0fb20914761fc3fb6497`；并行开发中的新模型能力
不作为这里的已交付能力。实际操作应重新核对目标 checkout、镜像和迁移文件。
本文提供演练和操作准备依据；生产停写、迁移或恢复需要针对具体环境另行安排。

## 三类不同的工作

| 工作 | 范围 | 本仓入口与限制 |
|---|---|---|
| schema 升级 | 表、列、类型、索引、约束及迁移版本 | [模型与升级函数](../api/db/db_models.py)、[Alembic 配置](../alembic.ini)、[迁移目录](../configs/alembic/versions/)；只执行已编写的迁移 |
| 业务数据迁移 | ID、模型引用、默认值及历史业务记录转换 | 部分 Alembic revision 和 [数据初始化](../api/db/init_data.py) 包含数据写入；没有覆盖全部历史版本和业务数据的统一迁移工具 |
| 备份与恢复 | SQL、文件对象、索引、队列及解密所需材料 | [卷归档脚本](../docker/migration.sh) 只覆盖六个固定卷；其他存储及配置、密钥需要另行纳入恢复方案 |

软件版本、Git SHA、Alembic revision 和实际 schema 应分别记录。达到迁移 `head`
不代表数据已全部转换、索引与对象一致或生产业务已经验收。

## 启动会自动做什么

应用表使用 `usr_ai` schema，版本记录为 `usr_ai.alembic_version`。
[schema 引导](../api/db/schema_bootstrap.py) 根据该 schema 是否存在表区分两条路径：

1. **空 schema**：`init_database_tables()` 按当前模型和外键依赖顺序建表，随后
   `upgrade_database_tables(is_fresh_install=True)` 在没有 revision 时 stamp 到 head。
   stamp 只登记版本，历史迁移没有执行；根迁移是列补丁，不能把空库直接
   `alembic upgrade head` 当作完整建库流程。
2. **存量 schema**：先 `upgrade_database_tables(is_fresh_install=False)` 执行迁移到
   head，再 `init_database_tables()` 补建缺失的模型表。已存在表由建表函数跳过，
   字段和约束变化依赖迁移文件，不会仅因改了模型而自动同步。

`usr_ai` 必须预先存在；建表函数不负责创建 schema。仓库的
[PostgreSQL 初始化 SQL](../docker/postgres/init.sql) 创建 schema 并授权给 `usr_ai` 角色，
由 Compose 的 PostgreSQL 服务挂载到初始化目录。已有数据目录、外置数据库或不同角色
不能仅靠再次启动容器获得该初始化，需核对真实 schema 和权限。
schema 缺失或连接检查失败时，建表函数可能返回错误字符串；不能只根据进程退出码
或“初始化完成”日志判断建表成功。

[API 进程入口](../api/multirag_server.py) 在开始 serving 前调用 schema 引导，再调用
`init_web_data()`；后者刷新模型厂商和模型目录，并包含历史名称、模型引用等修复。
其中若干 service 自行提交事务，部分模型目录保存失败会 rollback 后记录警告，
不能把整个启动看作一个原子数据迁移事务。应同时核对日志和实际业务记录。
`uv run python -m api.db.init_data` 也会引导 schema 并写入默认数据，不是只读诊断命令。
直接使用其他 ASGI 启动方式时，应检查是否经过上述进程入口，不能假定导入 `app`
就完成了同样的迁移。

容器还有一层行为：[entrypoint](../docker/entrypoint.sh) 在拉起服务前无条件调用
`init_database_tables()`，API 进程随后才运行上述完整引导。因此容器入口存在先尝试
model-first 建表的步骤，不能将其顺序概括为“所有存量环境都先迁移”。
`--disable-server` 不跳过这一步；当前没有专门的跳过数据库初始化/升级参数。
`SKIP_CONFIG_GENERATE` 只控制配置生成，`MULTIRAG_RENDER_CONFIG_ONLY` 只生成配置后退出，
均不能用作正常运行服务时的迁移开关。

初始化耗时可能延后监听和健康检查。应在演练中测量耗时、锁等待与失败行为，再调整部署
启动预算；本仓没有统一的“十分钟内完成”保证，也不能通过单纯延长健康检查掩盖迁移失败。

## 升级前的只读核对

先确认源版本、目标版本、数据库主机/库名/角色、当前 schema、实际存储后端和所有写入者，
记录脱敏结果。不要打印数据库 URL、密码、API key 或密钥。

在目标仓库根目录，以下命令只读取 Git 和迁移文件，不连接业务数据库：

```bash
git status --short
git rev-parse HEAD
git describe --tags --match='v*' --first-parent --always
uv run --no-sync alembic heads
uv run --no-sync alembic history
```

运行镜像还需记录镜像标识及其 `VERSION` 文件；[版本函数](../common/versions.py) 优先读取
`VERSION`，缺失时才使用 Git 描述。版本标签不能代替逐 revision 审核。检查当前 revision
到目标 head 之间的升级和降级函数，特别留意删除、重命名、类型转换、唯一约束、回填和
不可逆的数据变更。多个 head、未知 revision、已有表却无版本记录都应先调查，不能用
`stamp head` 消除不确定性。

数据库连接来源必须分清：

- 应用引擎由 `api/db/db_models.py` 经 `DB_TYPE`（默认 `postgresql`）和
  `common/config_utils.py:decrypt_database_config()` 读取对应 section；文件覆盖来自
  `service_conf.yaml`、`local.service_conf.yaml` 和 `MULTIRAG_CONFIG_OVERLAY_FILE`，按顶层
  section 整体替换。该遗留连接路径不能仅凭类型化配置的
  `MULTIRAG_<SECTION>__<FIELD>` 环境覆盖就认定数据库目标已变更。
- Alembic CLI 的 [env.py](../configs/alembic/env.py) 优先使用
  `ALEMBIC_DATABASE_URL`；未设置时，当前 ini 的占位 URL 会使 CLI 回用应用引擎。
  调用方传入 SQLAlchemy connection 时以该 connection 为准。**启动引导使用应用引擎，
  不受 `ALEMBIC_DATABASE_URL` 重定向。**

确认连接目标并通过受控渠道注入 URL，且将其导出给子进程后，才运行以下数据库检查；本文不要求连接生产。
没有显式 URL 时不要直接复制执行：

```bash
: "${ALEMBIC_DATABASE_URL:?请先注入已核对目标的数据库 URL}"
export ALEMBIC_DATABASE_URL
uv run --no-sync alembic current
```

也可在已核对目标的 PostgreSQL SQL 会话中使用只读事务记录环境：

```sql
BEGIN READ ONLY;
SELECT current_database(), current_user, current_setting('server_version'),
       current_setting('search_path');
SELECT to_regnamespace('usr_ai'), to_regclass('usr_ai.alembic_version');
SELECT table_name FROM information_schema.tables
 WHERE table_schema = 'usr_ai' ORDER BY table_name;
COMMIT;
```

只有版本表存在时，另在只读事务中查询
`SELECT version_num FROM usr_ai.alembic_version;`。结合目标迁移逐表核对列、类型、默认值、
索引和约束；必要时保存受影响表的精确计数、ID/引用关系以及对象和索引的对应清单。
SQL 会话与 Alembic 必须确认是同一库，不能用另一实例的检查结果代替。

## 备份对象与当前恢复能力

备份清单应同时覆盖：

- PostgreSQL 业务记录、schema、revision，以及恢复所需角色/权限；
- 当前对象存储的文件字节及其 SQL 登记，文档索引的切片、向量和父子关系；
- Redis 中实际需要恢复的任务流/队列与运行状态；恢复旧队列前判断是否会重复执行；
- 生效配置、镜像/代码版本、模型配置，以及独立保管的解密密钥和历史 key ring。
  Channel 主密钥的分发及丢失后果见 [Docker 文档](../docker/README.md)；其他密钥按实际
  加密配置另行核对。凭据材料不能入库或写入公开报告。

当前 `docker/migration.sh` 按 `-p` 拼接卷名，默认项目名为 `docker`，不读取 Compose
profile、实际挂载或应用配置。仅归档以下对象：

| 卷名后缀 | 归档文件 |
|---|---|
| `postgres_data` | `postgres_backup.tar.gz` |
| `minio_data` | `minio_backup.tar.gz` |
| `redis_data` | `redis_backup.tar.gz` |
| `milvus_etcd_data` | `milvus_etcd_backup.tar.gz` |
| `milvus_minio_data` | `milvus_minio_backup.tar.gz` |
| `milvus_data` | `milvus_backup.tar.gz` |

它检查正在运行且挂载这些卷的 Docker 容器并拒绝继续，但不会主动停写、停止容器或阻止
外部写入者。tar 归档不提供跨库、对象、索引和队列的在线一致性快照。使用前必须将相关
数据服务和所有写入者停到一致的状态，或采用另行验证的备份协调机制。

脚本对缺失卷只告警并跳过，仍可能输出成功；restore 则要求六个归档全部存在。
因此未启用 Milvus 的部署也不能假定能用此脚本直接完整恢复，不要制造空归档绕过检查。
[Compose 定义](../docker/docker-compose-base.yml) 中的 `esdata01`、`osdata01`、
`infinity_data`、`ob_data`，以及 SeekDB 实际使用的 `./seekdb` bind mount，均不在这六卷
覆盖范围内；`seekdb_data` 仅声明为卷，并非 SeekDB 当前实际挂载。外部数据库、远程对象
存储、其他 bind mount 和配置/密钥也需独立备份。

restore 在现有卷上直接解压，会覆盖同名文件却保留归档外的旧文件；它不清空目标、
校验归档完整性或执行业务对账。应先在干净且独立的目标卷演练。PostgreSQL 卷是物理
数据目录，跨数据库服务版本的升级兼容性需单独验证；此脚本不提供 PostgreSQL 版本转换，
也不是 `pg_dump`/`pg_restore` 逻辑备份工具。

以下是仓库根目录下的脚本调用形式。项目名必须对应已核对的真实卷，目录参数使用当前
目录下的相对目录。每次使用全新且为空的备份目录；脚本不会清理旧包，缺卷时可能
混入旧时点的归档。仅在覆盖清单完整、已停写且准备好独立恢复目标的演练中执行：

```bash
bash docker/migration.sh help
# 先通过受控操作设置 migration_project、migration_backup_dir、migration_restore_project。
# migration_restore_project 应是与源环境隔离的演练目标。
: "${migration_project:?请先设置源项目名}"
: "${migration_backup_dir:?请先设置相对备份目录}"
: "${migration_restore_project:?请先设置独立恢复项目名}"
bash docker/migration.sh -p "$migration_project" backup "$migration_backup_dir"
bash docker/migration.sh -p "$migration_restore_project" restore "$migration_backup_dir"
```

`help` 也会先检查 Docker 可访问性。备份后应独立核对每个预期归档是否生成、校验摘要与
可解包性，并保存一致性时点和对应服务版本；恢复演练成功才构成可恢复证据。不同
Compose project 名不自动消除固定 `container_name`、端口或外部存储配置冲突。

## 在隔离副本中演练升级

先恢复源数据到隔离的 SQL、对象、索引和 Redis 资源，核对源 revision 和基线清单。
应用启动还可能写默认数据、连接模型服务并启动后台活动；演练配置应禁止真实业务及
外部副作用。只复制 SQL 库不能证明全套恢复能力。

对于**已有 schema 和业务表的存量副本**，在确认 `ALEMBIC_DATABASE_URL` 指向该副本后，
可以提前执行已审核的迁移，记录退出码、耗时、日志及独立读回：

```bash
: "${ALEMBIC_DATABASE_URL:?请先注入隔离副本的数据库 URL}"
export ALEMBIC_DATABASE_URL
uv run --no-sync alembic upgrade head
uv run --no-sync alembic current
uv run --no-sync alembic check
```

`upgrade` 会写数据库；`check` 检测 Alembic autogenerate 范围内是否仍需 schema 变更，
不会生成迁移文件，不是业务数据核验。它依赖可连接的目标 schema/版本，失败应分析
实际差异，不能靠 stamp、吞异常或放宽检查通过。迁移使用反射查询，不能假定
`upgrade --sql` 支持完整历史链或是可用的 dry-run。

对于**全新隔离环境**，先按部署方案准备 `usr_ai` 及权限，再使用已配置隔离目标的应用
schema 引导。以下命令使用应用配置，必须独立核对其数据库目标，不能只设置
`ALEMBIC_DATABASE_URL`：

```bash
uv run --no-sync python -c 'from api.db.schema_bootstrap import bootstrap_database_schema; bootstrap_database_schema()'
```

schema 准备后，按 [服务与运行](development.md#服务与运行) 在隔离环境启动目标 API，
验证数据初始化及实际受影响业务。提前执行 CLI 升级只完成其迁移链，不替代启动时
补表、默认数据写入或验收；当前 API 正常入口仍会再次检查 schema 并运行数据初始化。

生产切换方案应使用演练得到的停写范围、耗时和恢复步骤，指定单一迁移执行者，避免
多个新旧实例并发迁移或在结构切换期间继续写入。当前 bootstrap 没有部署级迁移互斥锁，
也没有统一的自动重试、数据转换预览或跨存储回退命令。

## 验后对账与回退边界

升级与恢复后分别核对 revision、真实 schema、受影响业务表精确计数和引用关系、文件
字节与登记、索引切片/向量/状态，以及任务队列和执行状态。对允许发生的数据变化列出
预期映射；其余关键记录应保持一致。验证权限、模型选择、上传/解析/检索等受影响流程，
读回实际结果，不能只看 HTTP 200、启动日志或一个总行数。

健康入口为 `GET /api/v1/system/ping` 和 `GET /api/v1/system/healthz`，判读规则见
[服务与运行](development.md#服务与运行)。探活证明组件可访问，不证明每个迁移对象或
业务记录已正确转换。

`alembic downgrade` 仅运行写好的降级函数，可能删除新列、新表及其数据，不能恢复文件、
索引、队列或升级后新写入的业务。[租户模型主键迁移](../configs/alembic/versions/284487da1d89_migrate_tenant_llm_id_to_bigint_pk.py)
的 downgrade 会重新生成字符串 ID，不会恢复原 ID；所以代码切回、schema 降级和业务
数据恢复必须分别判断。fresh-install stamp 过的 schema 也不能当成逐一执行过历史升级
后可任意反向回放的数据库。

迁移失败时先保持停写，记录当前 revision、真实 schema 和已提交的数据变化；迁移事务
失败不代表整个启动或跨存储操作全部回滚。不要盲目重启反复触发初始化。只有验证了
对应源代码/镜像与一致性备份的恢复组合，才可以据此安排回退；新版本已接受写入时，
还需确定增量数据处理和可接受的数据丢失范围。本文未执行生产迁移，也未证明任何
现有生产备份可恢复。
