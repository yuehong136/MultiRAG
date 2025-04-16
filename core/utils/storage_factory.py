import os
from enum import Enum

from core.utils.azure_sas_conn import MultiRAGAzureSasBlob
from core.utils.azure_spn_conn import MultiRAGAzureSpnBlob
from core.utils.minio_conn import MultiRAGMinio
from core.utils.s3_conn import MultiRAGS3
from core.utils.oss_conn import MultiRAGOSS


class Storage(Enum):
    MINIO = 1
    AZURE_SPN = 2
    AZURE_SAS = 3
    AWS_S3 = 4
    OSS = 5


class StorageFactory:
    storage_mapping = {
        Storage.MINIO: MultiRAGMinio,
        Storage.AZURE_SPN: MultiRAGAzureSpnBlob,
        Storage.AZURE_SAS: MultiRAGAzureSasBlob,
        Storage.AWS_S3: MultiRAGS3,
        Storage.OSS: MultiRAGOSS,
    }

    @classmethod
    def create(cls, storage: Storage):
        return cls.storage_mapping[storage]()


STORAGE_IMPL_TYPE = os.getenv('STORAGE_IMPL', 'MINIO')
STORAGE_IMPL = StorageFactory.create(Storage[STORAGE_IMPL_TYPE])
