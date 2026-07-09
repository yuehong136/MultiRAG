"""类型化应用配置（配置重构 Phase 1，方案见 internal/config_bootstrap_refactor_plan.md）。

设计要点：
- 来源优先级：环境变量（``MULTIRAG_<SECTION>__<FIELD>``，双下划线逐层深入）
  > configs/local.service_conf.yaml（顶层 section **整体替换**，与 read_config 语义一致）
  > configs/service_conf.yaml > 模型默认值；
- section 与字段名和 service_conf.yaml（即 ragflow 上游）1:1 镜像——上游新增配置项时
  只需在对应模型加同名字段，移植映射见 internal/ragflow_settings_porting_map.md；
- 已建模字段类型错误 → 加载即抛 AppConfigError（fail-fast，错误信息含字段路径）；
  未建模的 section/字段原样保留（extra="allow" + get_section 兜底）；
- **纯数据零副作用**：本模块不建立任何连接、不触碰 Redis/DB。
  有状态资源（docStoreConn、STORAGE_IMPL、SECRET_KEY 等）见 common/resources.py（Phase 3）。

用法：
    from common.app_config import get_app_config
    cfg = get_app_config()
    cfg.multirag.http_port          # 类型化访问
    cfg.get_section("tcadp_config")  # 未建模 section 的原样 dict
"""

import copy
import json
import os
from functools import lru_cache
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, PrivateAttr, ValidationError

from common.config_utils import decrypt_database_password, read_config
from common.constants import SERVICE_CONF
from common.file_utils import get_project_base_directory

ENV_PREFIX = "MULTIRAG_"


class AppConfigError(RuntimeError):
    """配置加载/校验失败（信息中包含出错字段路径）。"""


class _Section(BaseModel):
    """所有 section 模型的基类：未建模字段一律保留，镜像上游新增键。"""

    model_config = ConfigDict(extra="allow")


# ---------------------------------------------------------------------------
# 核心 section（字段名严格镜像 service_conf.yaml）
# ---------------------------------------------------------------------------


class ServerConfig(_Section):
    """multirag section。"""

    host: str = "127.0.0.1"
    http_port: int = 9380
    secret_key: str | None = None  # 注意：实际 SECRET_KEY 来自 Redis（见 resources），此字段历史遗留
    admin_require_superuser: bool = False


class PostgresConfig(_Section):
    name: str = "postgresql"
    dbname: str = "postgres"
    user: str = ""
    password: str = ""
    host: str = "127.0.0.1"
    port: int = 5432
    max_connections: int = 100
    stale_timeout: int = 30


class RedisConfig(_Section):
    """redis / datav_redis section（host 字段形如 'host:port'）。"""

    db: int = 1
    username: str = ""
    password: str = ""
    host: str = "127.0.0.1:6379"


class MinioConfig(_Section):
    user: str = ""
    password: str = ""
    host: str = "127.0.0.1:9000"
    region: str = ""
    bucket: str = ""
    prefix_path: str = ""
    workflow_bucket: str = ""


class S3LikeConfig(_Section):
    """s3 / oss section（结构相同）。"""

    access_key: str = ""
    secret_key: str = ""
    bucket: str = ""
    endpoint_url: str = ""
    region: str = ""
    prefix_path: str = ""
    addressing_style: str = ""
    signature_version: str = ""


class AzureConfig(_Section):
    auth_type: str = ""
    account_url: str = ""
    client_id: str = ""
    secret: str = ""
    tenant_id: str = ""
    container_name: str = ""
    cloud: str = ""


class GcsConfig(_Section):
    bucket: str = ""


class EsLikeConfig(_Section):
    """es / os section（结构相同）。"""

    hosts: str = ""
    username: str = ""
    password: str = ""


class MilvusConfig(_Section):
    hosts: str = ""
    username: str = ""
    password: str = ""
    db_name: str = ""
    token: str = ""
    timeout: float | None = None
    kwargs: dict[str, Any] | None = None


class InfinityConfig(_Section):
    uri: str = "infinity:23817"
    postgres_port: int = 5432
    db_name: str = "default_db"


class SchemeConfig(_Section):
    """oceanbase / seekdb / opendal 类 section：scheme + 自由 config。"""

    scheme: str = ""
    config: dict[str, Any] | None = None


class VastbaseConfig(_Section):
    database: str = ""
    user: str = ""
    password: str = ""
    host: str = "127.0.0.1"
    port: int = 5432
    max_connections: int = 100
    schema_: str | None = None

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    def __init__(self, **data: Any) -> None:
        # yaml 键名是 schema（与 BaseModel.schema 冲突），入模时改存 schema_
        if "schema" in data:
            data.setdefault("schema_", data.pop("schema"))
        super().__init__(**data)


class SmtpConfig(_Section):
    mail_server: str = ""
    mail_port: int = 0
    mail_use_ssl: bool = True
    mail_use_tls: bool = False
    mail_username: str = ""
    mail_password: str = ""
    mail_default_sender: list[str] = []
    mail_frontend_url: str = ""


class AuthenticationConfig(_Section):
    client: dict[str, Any] = {}
    site: dict[str, Any] = {}
    disable_password_login: bool = False


# ---------------------------------------------------------------------------
# user_default_llm：收编 settings._parse_model_entry / _resolve_per_model_config
# ---------------------------------------------------------------------------

_DEFAULT_PARSERS = "naive:General,qa:Q&A,resume:Resume,manual:Manual,table:Table,paper:Paper,book:Book,laws:Laws,presentation:Presentation,picture:Picture,one:One,audio:Audio,email:Email,tag:Tag"


def _parse_model_entry(entry: Any) -> dict[str, Any]:
    """与 settings._parse_model_entry 语义一致（有 characterization 测试钉板）。"""
    if isinstance(entry, str):
        return {"name": entry, "factory": None, "api_key": None, "base_url": None}
    if isinstance(entry, dict):
        name = entry.get("name") or entry.get("model") or ""
        return {
            "name": name,
            "factory": entry.get("factory"),
            "api_key": entry.get("api_key"),
            "base_url": entry.get("base_url"),
        }
    return {"name": "", "factory": None, "api_key": None, "base_url": None}


class ResolvedModelConfig(BaseModel):
    """default_models 中单个模型解析后的最终形态（等价旧 CHAT_CFG 等 dict）。"""

    model_config = ConfigDict(frozen=True)

    model: str = ""
    factory: str = ""
    api_key: str = ""
    base_url: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"model": self.model, "factory": self.factory, "api_key": self.api_key, "base_url": self.base_url}


class UserDefaultLLMConfig(_Section):
    factory: str = ""
    api_key: str | None = None
    base_url: str = ""
    allowed_factories: list[str] | None = None
    parsers: str = _DEFAULT_PARSERS
    default_models: dict[str, Any] = {}

    def resolved_model(self, kind: str) -> ResolvedModelConfig:
        """解析 default_models 中的一项（kind 如 'chat_model'）。

        规则与旧 settings._resolve_per_model_config 一致：
        entry 自带字段优先，缺失回退 user_default_llm 顶层兜底；
        name 无 '@' 且能确定 factory 时拼接为 'name@factory'。
        """
        entry = _parse_model_entry(self.default_models.get(kind, ""))
        name = (entry.get("name") or "").strip()
        m_factory = entry.get("factory") or self.factory or ""
        m_api_key = entry.get("api_key") or self.api_key or ""
        m_base_url = entry.get("base_url") or self.base_url or ""
        if name and "@" not in name and m_factory:
            name = f"{name}@{m_factory}"
        return ResolvedModelConfig(model=name, factory=m_factory, api_key=m_api_key, base_url=m_base_url)


class ObservabilityConfig(_Section):
    """observability section：OTel 追踪开关与导出目标（初始化单点见 common/observability.py）。"""

    enabled: bool = False
    # OTLP gRPC 端点；本地 Jaeger 见 docker/docker-compose-observability.yml
    otlp_endpoint: str = "http://localhost:4317"
    # 空值时取 OTEL_SERVICE_NAME 环境变量，再退回按入口进程推导
    service_name: str = ""


# ---------------------------------------------------------------------------
# 根模型
# ---------------------------------------------------------------------------


class AppConfig(BaseModel):
    """service_conf.yaml（含 local 覆盖与 env 覆盖）的类型化视图。"""

    model_config = ConfigDict(extra="allow")

    multirag: ServerConfig = ServerConfig()
    admin: ServerConfig = ServerConfig()
    postgresql: PostgresConfig = PostgresConfig()
    vastbase: VastbaseConfig = VastbaseConfig()
    redis: RedisConfig = RedisConfig()
    datav_redis: RedisConfig = RedisConfig()
    minio: MinioConfig = MinioConfig()
    s3: S3LikeConfig = S3LikeConfig()
    oss: S3LikeConfig = S3LikeConfig()
    azure: AzureConfig = AzureConfig()
    gcs: GcsConfig = GcsConfig()
    opendal: SchemeConfig = SchemeConfig()
    es: EsLikeConfig = EsLikeConfig()
    os: EsLikeConfig = EsLikeConfig()
    milvus: MilvusConfig = MilvusConfig()
    infinity: InfinityConfig = InfinityConfig()
    oceanbase: SchemeConfig = SchemeConfig()
    seekdb: SchemeConfig = SchemeConfig()
    user_default_llm: UserDefaultLLMConfig = UserDefaultLLMConfig()
    oauth: dict[str, Any] = {}
    authentication: AuthenticationConfig = AuthenticationConfig()
    smtp: SmtpConfig = SmtpConfig()
    observability: ObservabilityConfig = ObservabilityConfig()

    _raw: dict[str, Any] = PrivateAttr(default_factory=dict)

    @property
    def raw(self) -> dict[str, Any]:
        """合并+env 覆盖后的原始 dict（get_base_config 兼容层使用）。"""
        return self._raw

    def get_section(self, name: str, default: Any = None) -> Any:
        """未建模 section 的原样访问（等价 get_base_config，不含其 env 名回退）。"""
        return self._raw.get(name, default)


# ---------------------------------------------------------------------------
# 加载器
# ---------------------------------------------------------------------------


def _coerce_env_value(value: str) -> Any:
    """env 值按 YAML 标量解析（'8123'→int、'true'→bool、'{a: 1}'→dict），失败保留原串。"""
    try:
        return yaml.safe_load(value)
    except yaml.YAMLError:
        return value


def _apply_env_overlay(merged: dict[str, Any]) -> None:
    """把 MULTIRAG_<SECTION>__<FIELD>[__<SUBFIELD>...] 环境变量写入合并配置。

    - 至少要有一层 '__'（裸 MULTIRAG_X 不视为配置覆盖，避免误伤无关变量）；
    - 中间路径不存在时自动建 dict；中间路径撞上非 dict 值则覆盖为 dict。
    """
    for env_key, env_value in os.environ.items():
        if not env_key.startswith(ENV_PREFIX):
            continue
        path = env_key[len(ENV_PREFIX) :].lower().split("__")
        if len(path) < 2 or not all(path):
            continue
        node = merged
        for part in path[:-1]:
            child = node.get(part)
            if not isinstance(child, dict):
                child = {}
                node[part] = child
            node = child
        node[path[-1]] = _coerce_env_value(env_value)


def _apply_decryption(merged: dict[str, Any]) -> None:
    """与现状一致：仅 minio.password 走 decrypt_database_password
    （未启用 encrypt_password 时为恒等变换）。"""
    minio_conf = merged.get("minio")
    if isinstance(minio_conf, dict) and "password" in minio_conf:
        minio_conf["password"] = decrypt_database_password(minio_conf["password"])


def load_app_config(conf_name: str = SERVICE_CONF) -> AppConfig:
    """完整加载一次配置（不走缓存；常规入口用 get_app_config）。"""
    merged: dict[str, Any] = copy.deepcopy(read_config(conf_name))
    _apply_env_overlay(merged)
    _apply_decryption(merged)
    try:
        config = AppConfig.model_validate(merged)
    except ValidationError as exc:
        paths = "; ".join(".".join(str(p) for p in err["loc"]) + f" ← {err['msg']}" for err in exc.errors())
        raise AppConfigError(f"service_conf 配置校验失败（检查 yaml/local 覆盖/MULTIRAG_* 环境变量）: {paths}") from exc
    config._raw = merged
    return config


@lru_cache(maxsize=1)
def get_app_config() -> AppConfig:
    """进程级配置单例（配置视为不可变；测试用 reset_app_config 重置）。"""
    return load_app_config()


@lru_cache(maxsize=1)
def get_factory_llm_infos() -> list[dict[str, Any]]:
    """configs/llm_factories.json 的 factory_llm_infos 列表（读取失败时为空列表，
    与旧 init_settings 的 try/except 语义一致）。"""
    try:
        with open(os.path.join(get_project_base_directory(), "configs", "llm_factories.json")) as f:
            return json.load(f)["factory_llm_infos"]
    except Exception:
        return []


def reset_app_config() -> None:
    """清空配置缓存（仅测试/热重载场景使用）。"""
    get_app_config.cache_clear()
    get_factory_llm_infos.cache_clear()
