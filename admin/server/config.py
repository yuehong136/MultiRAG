import logging
import threading
from enum import Enum
from typing import Any
from pydantic import BaseModel, ConfigDict, Field
from urllib.parse import urlparse

from common.config_utils import read_config

class BaseConfig(BaseModel):
    """基础配置模型"""
    id: int
    name: str
    host: str
    port: int
    service_type: str
    detail_func_name: str

    model_config = ConfigDict(from_attributes=True)

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            'id': self.id,
            'name': self.name,
            'host': self.host,
            'port': self.port,
            'service_type': self.service_type
        }


class ServiceConfigs:
    configs = list[BaseConfig]

    """服务配置管理器"""
    def __init__(self):
        self.configs: list[BaseModel] = []
        self.lock = threading.Lock()


SERVICE_CONFIGS = ServiceConfigs


class ServiceType(Enum):
    """服务类型枚举"""
    METADATA = "metadata"
    RETRIEVAL = "retrieval"
    MESSAGE_QUEUE = "message_queue"
    MULTIRAG_SERVER = "multirag_server"
    TASK_EXECUTOR = "task_executor"
    FILE_STORE = "file_store"


class MetaConfig(BaseConfig):
    """元数据配置"""
    meta_type: str

    def to_dict(self) -> dict[str, Any]:
        result = super().to_dict()
        if 'extra' not in result:
            result['extra'] = {}
        extra_dict = result['extra'].copy()
        extra_dict['meta_type'] = self.meta_type
        result['extra'] = extra_dict
        return result


class MySQLConfig(MetaConfig):
    """MySQL 配置"""
    username: str
    password: str

    def to_dict(self) -> dict[str, Any]:
        result = super().to_dict()
        if 'extra' not in result:
            result['extra'] = {}
        extra_dict = result['extra'].copy()
        extra_dict['username'] = self.username
        extra_dict['password'] = self.password
        result['extra'] = extra_dict
        return result


class PostgreSQLConfig(MetaConfig):
    """PostgreSQL 配置"""
    username: str
    password: str
    dbname: str
    max_connections: int | None = None
    stale_timeout: int | None = None

    def to_dict(self) -> dict[str, Any]:
        result = super().to_dict()
        if 'extra' not in result:
            result['extra'] = {}
        extra_dict = result['extra'].copy()
        extra_dict['username'] = self.username
        extra_dict['password'] = self.password
        extra_dict['dbname'] = self.dbname
        if self.max_connections:
            extra_dict['max_connections'] = self.max_connections
        if self.stale_timeout:
            extra_dict['stale_timeout'] = self.stale_timeout
        result['extra'] = extra_dict
        return result


class RetrievalConfig(BaseConfig):
    """检索服务配置"""
    retrieval_type: str

    def to_dict(self) -> dict[str, Any]:
        result = super().to_dict()
        if 'extra' not in result:
            result['extra'] = {}
        extra_dict = result['extra'].copy()
        extra_dict['retrieval_type'] = self.retrieval_type
        result['extra'] = extra_dict
        return result


class MilvusConfig(RetrievalConfig):
    """Milvus 配置"""
    username: str | None = None
    password: str | None = None
    db_name: str | None = None
    token: str | None = None
    timeout: int | None = None

    def to_dict(self) -> dict[str, Any]:
        result = super().to_dict()
        if 'extra' not in result:
            result['extra'] = {}
        extra_dict = result['extra'].copy()
        if self.username:
            extra_dict['username'] = self.username
        if self.password:
            extra_dict['password'] = self.password
        if self.db_name:
            extra_dict['db_name'] = self.db_name
        if self.token:
            extra_dict['token'] = self.token
        if self.timeout:
            extra_dict['timeout'] = self.timeout
        result['extra'] = extra_dict
        return result


class InfinityConfig(RetrievalConfig):
    """Infinity 配置"""
    db_name: str

    def to_dict(self) -> dict[str, Any]:
        result = super().to_dict()
        if 'extra' not in result:
            result['extra'] = {}
        extra_dict = result['extra'].copy()
        extra_dict['db_name'] = self.db_name
        result['extra'] = extra_dict
        return result


class VastBaseConfig(RetrievalConfig):
    """VastBase 配置"""
    database: str
    user: str
    password: str
    db_schema: str | None = None
    max_connections: int | None = None

    def to_dict(self) -> dict[str, Any]:
        result = super().to_dict()
        if 'extra' not in result:
            result['extra'] = {}
        extra_dict = result['extra'].copy()
        extra_dict['database'] = self.database
        extra_dict['user'] = self.user
        extra_dict['password'] = self.password
        if self.db_schema:
            extra_dict['schema'] = self.db_schema
        if self.max_connections:
            extra_dict['max_connections'] = self.max_connections
        result['extra'] = extra_dict
        return result


class ElasticsearchConfig(RetrievalConfig):
    """Elasticsearch 配置"""
    username: str
    password: str

    def to_dict(self) -> dict[str, Any]:
        result = super().to_dict()
        if 'extra' not in result:
            result['extra'] = {}
        extra_dict = result['extra'].copy()
        extra_dict['username'] = self.username
        extra_dict['password'] = self.password
        result['extra'] = extra_dict
        return result


class MessageQueueConfig(BaseConfig):
    """消息队列配置"""
    mq_type: str

    def to_dict(self) -> dict[str, Any]:
        result = super().to_dict()
        if 'extra' not in result:
            result['extra'] = {}
        extra_dict = result['extra'].copy()
        extra_dict['mq_type'] = self.mq_type
        result['extra'] = extra_dict
        return result


class RedisConfig(MessageQueueConfig):
    """Redis 配置"""
    database: int
    password: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result = super().to_dict()
        if 'extra' not in result:
            result['extra'] = {}
        extra_dict = result['extra'].copy()
        extra_dict['database'] = self.database
        if self.password:
            extra_dict['password'] = self.password
        result['extra'] = extra_dict
        return result


class RabbitMQConfig(MessageQueueConfig):
    """RabbitMQ 配置"""

    def to_dict(self) -> dict[str, Any]:
        result = super().to_dict()
        if 'extra' not in result:
            result['extra'] = {}
        return result


class MultiRAGServerConfig(BaseConfig):
    """MultiRAG 服务器配置"""
    secret_key: str | None = None
    admin_require_superuser: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        result = super().to_dict()
        if 'extra' not in result:
            result['extra'] = {}
        extra_dict = result['extra'].copy()
        if self.secret_key:
            extra_dict['secret_key'] = self.secret_key
        if self.admin_require_superuser is not None:
            extra_dict['admin_require_superuser'] = self.admin_require_superuser
        result['extra'] = extra_dict
        return result


class TaskExecutorConfig(BaseConfig):
    """任务执行器配置"""
    message_queue_type: str

    def to_dict(self) -> dict[str, Any]:
        result = super().to_dict()
        if 'extra' not in result:
            result['extra'] = dict()
        result['extra']['message_queue_type'] = self.message_queue_type
        return result


class FileStoreConfig(BaseConfig):
    """文件存储配置"""
    store_type: str

    def to_dict(self) -> dict[str, Any]:
        result = super().to_dict()
        if 'extra' not in result:
            result['extra'] = {}
        extra_dict = result['extra'].copy()
        extra_dict['store_type'] = self.store_type
        result['extra'] = extra_dict
        return result


class MinioConfig(FileStoreConfig):
    """Minio 配置"""
    user: str
    password: str
    region: str | None = None
    workflow_bucket: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result = super().to_dict()
        if 'extra' not in result:
            result['extra'] = {}
        extra_dict = result['extra'].copy()
        extra_dict['user'] = self.user
        extra_dict['password'] = self.password
        if self.region:
            extra_dict['region'] = self.region
        if self.workflow_bucket:
            extra_dict['workflow_bucket'] = self.workflow_bucket
        result['extra'] = extra_dict
        return result


def load_configurations(config_path: str) -> list[BaseConfig]:
    """加载配置文件"""
    raw_configs = read_config(config_path)
    configurations: list[BaseConfig] = []
    multirag_count = 0
    id_count = 0
    
    for k, v in raw_configs.items():
        match k:
            case "multirag":
                # MultiRAG 主服务配置
                name = f'multirag_{multirag_count}'
                host = v.get('host', 'localhost')
                http_port = v.get('http_port', 8123)
                secret_key = v.get('secret_key')
                admin_require_superuser = v.get('admin_require_superuser')
                
                config = MultiRAGServerConfig(
                    id=id_count,
                    name=name,
                    host=host,
                    port=http_port,
                    service_type="multirag_server",
                    secret_key=secret_key,
                    admin_require_superuser=admin_require_superuser,
                    detail_func_name="check_multirag_server_alive"
                )
                configurations.append(config)
                id_count += 1
                multirag_count += 1
            
            case "postgresql":
                # PostgreSQL 数据库配置
                name = 'postgresql'
                host = v.get('host', 'localhost')
                port = v.get('port', 5432)
                username = v.get('user', '')
                password = v.get('password', '')
                dbname = v.get('dbname', 'postgres')
                max_connections = v.get('max_connections')
                stale_timeout = v.get('stale_timeout')
                
                config = PostgreSQLConfig(
                    id=id_count,
                    name=name,
                    host=host,
                    port=port,
                    username=username,
                    password=password,
                    dbname=dbname,
                    max_connections=max_connections,
                    stale_timeout=stale_timeout,
                    service_type="metadata",
                    meta_type="postgresql",
                    detail_func_name="get_database_status"
                )
                configurations.append(config)
                id_count += 1
                
            case "mysql":
                # MySQL 数据库配置（保留兼容）
                name = 'mysql'
                host = v.get('host', '')
                port = v.get('port', 3306)
                username = v.get('user', '')
                password = v.get('password', '')
                config = MySQLConfig(
                    id=id_count,
                    name=name,
                    host=host,
                    port=port,
                    username=username,
                    password=password,
                    service_type="metadata",
                    meta_type="mysql",
                    detail_func_name="get_database_status"
                )
                configurations.append(config)
                id_count += 1
            
            case "milvus":
                # Milvus 向量数据库配置
                name = 'milvus'
                hosts_url = v.get('hosts', 'http://localhost:19530')
                
                # 解析 URL
                if hosts_url.startswith('http://') or hosts_url.startswith('https://'):
                    parsed = urlparse(hosts_url)
                    host = parsed.hostname or 'localhost'
                    port = parsed.port or 19530
                else:
                    # 如果没有协议，尝试按 host:port 格式解析
                    parts = hosts_url.split(':', 1)
                    host = parts[0]
                    port = int(parts[1]) if len(parts) > 1 else 19530
                
                username = v.get('username')
                password = v.get('password')
                db_name = v.get('db_name')
                token = v.get('token')
                timeout = v.get('timeout')
                
                config = MilvusConfig(
                    id=id_count,
                    name=name,
                    host=host,
                    port=port,
                    service_type="retrieval",
                    retrieval_type="milvus",
                    username=username,
                    password=password,
                    db_name=db_name,
                    token=token,
                    timeout=timeout,
                    detail_func_name="get_milvus_cluster_stats"
                )
                configurations.append(config)
                id_count += 1
                
            case "es":
                # Elasticsearch 配置
                if not v.get('hosts'):
                    # 如果 hosts 为空，跳过
                    logging.info("Elasticsearch configuration is empty, skipping...")
                    continue
                    
                name = 'elasticsearch'
                url = v['hosts']
                parsed = urlparse(url)
                host = parsed.hostname or ''
                port = parsed.port or 9200
                username = v.get('username', '')
                password = v.get('password', '')
                config = ElasticsearchConfig(
                    id=id_count,
                    name=name,
                    host=host,
                    port=port,
                    service_type="retrieval",
                    retrieval_type="elasticsearch",
                    username=username,
                    password=password,
                    detail_func_name="get_es_cluster_stats"
                )
                configurations.append(config)
                id_count += 1

            case "infinity":
                # Infinity 向量数据库配置
                name = 'infinity'
                url = v['uri']
                parts = url.split(':', 1)
                host = parts[0]
                port = int(parts[1])
                database = v.get('db_name', 'default_db')
                config = InfinityConfig(
                    id=id_count,
                    name=name,
                    host=host,
                    port=port,
                    service_type="retrieval",
                    retrieval_type="infinity",
                    db_name=database,
                    detail_func_name="get_infinity_status"
                )
                configurations.append(config)
                id_count += 1

            case "vastbase":
                # VastBase 向量数据库配置
                name = 'vastbase'
                host = v.get('host', '127.0.0.1')
                port = v.get('port', 5433)
                database = v.get('database', 'datav')
                user = v.get('user', 'datav')
                password = v.get('password', '')
                db_schema = v.get('schema', 'public')
                max_connections = v.get('max_connections')

                config = VastBaseConfig(
                    id=id_count,
                    name=name,
                    host=host,
                    port=port,
                    service_type="retrieval",
                    retrieval_type="vastbase",
                    database=database,
                    user=user,
                    password=password,
                    db_schema=db_schema,
                    max_connections=max_connections,
                    detail_func_name="get_vastbase_status"
                )
                configurations.append(config)
                id_count += 1

            case "minio":
                # MinIO 对象存储配置
                name = 'minio'
                url = v['host']
                parts = url.split(':', 1)
                host = parts[0]
                port = int(parts[1])
                user = v.get('user', '')
                password = v.get('password', '')
                region = v.get('region')
                workflow_bucket = v.get('workflow_bucket')
                
                config = MinioConfig(
                    id=id_count,
                    name=name,
                    host=host,
                    port=port,
                    user=user,
                    password=password,
                    region=region,
                    workflow_bucket=workflow_bucket,
                    service_type="file_store",
                    store_type="minio",
                    detail_func_name="check_minio_alive"
                )
                configurations.append(config)
                id_count += 1
                
            case "redis":
                # Redis 配置
                name = 'redis'
                url = v['host']
                parts = url.split(':', 1)
                host = parts[0]
                port = int(parts[1])
                password = v.get('password') or None
                db = v.get('db', 0)
                config = RedisConfig(
                    id=id_count,
                    name=name,
                    host=host,
                    port=port,
                    password=password,
                    database=db,
                    service_type="message_queue",
                    mq_type="redis",
                    detail_func_name="get_redis_info"
                )
                configurations.append(config)
                id_count += 1
                
            case "admin":
                # 管理后台配置（不加入服务列表，单独处理）
                logging.info(f"Admin service config: {v}")
                pass

            case "task_executor":
                name: str = 'task_executor'
                host: str = v.get('host', '')
                port: int = v.get('port', 0)
                message_queue_type: str = v.get('message_queue_type')
                config = TaskExecutorConfig(id=id_count, name=name, host=host, port=port, message_queue_type=message_queue_type,
                                            service_type="task_executor", detail_func_name="check_task_executor_alive")
                configurations.append(config)
                id_count += 1

            case _:
                # 其他配置项（如 oauth, authentication 等）跳过
                logging.debug(f"Skipping configuration key: {k}")
                continue

    logging.info(f"Loaded {len(configurations)} service configurations")
    return configurations
