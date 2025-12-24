import os
from common.config_utils import get_base_config, decrypt_database_config

EMBEDDING_MDL = ""

EMBEDDING_CFG = ""

DOC_ENGINE = os.getenv('DOC_ENGINE', 'elasticsearch')

docStoreConn = None

retriever = None

# move from core.settings
ES = {}
MILVUS = {}
VASTBASE = {}
INFINITY = {}
AZURE = {}
S3 = {}
MINIO = {}
OSS = {}
OS = {}
REDIS = {}

STORAGE_IMPL_TYPE = os.getenv('STORAGE_IMPL', 'MINIO')

# Initialize the selected configuration data based on environment variables to solve the problem of initialization errors due to lack of configuration
if DOC_ENGINE == 'elasticsearch':
    ES = get_base_config("es", {})
elif DOC_ENGINE == 'opensearch':
    OS = get_base_config("os", {})
elif DOC_ENGINE == 'milvus':
    MILVUS = get_base_config("milvus", {})
elif DOC_ENGINE == 'infinity':
    INFINITY = get_base_config("infinity", {"uri": "infinity:23817"})
elif DOC_ENGINE == 'vastbase':
    VASTBASE = get_base_config("vastbase", {})

if STORAGE_IMPL_TYPE in ['AZURE_SPN', 'AZURE_SAS']:
    AZURE = get_base_config("azure", {})
elif STORAGE_IMPL_TYPE == 'AWS_S3':
    S3 = get_base_config("s3", {})
elif STORAGE_IMPL_TYPE == 'MINIO':
    MINIO = decrypt_database_config(name="minio")
elif STORAGE_IMPL_TYPE == 'OSS':
    OSS = get_base_config("oss", {})

try:
    REDIS = decrypt_database_config(name="redis")
except Exception:
    try:
        REDIS = get_base_config("redis", {})
    except Exception:
        REDIS = {}
