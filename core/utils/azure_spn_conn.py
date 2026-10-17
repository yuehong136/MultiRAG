import logging
import os
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from azure.core import MatchConditions
from azure.core.exceptions import ResourceExistsError, ResourceModifiedError
from azure.identity import AzureAuthorityHosts, ClientSecretCredential
from azure.storage.filedatalake import DataLakeServiceClient, FileSasPermissions, FileSystemClient, generate_file_sas

from common import settings
from common.decorator import singleton

_CLOUD_AUTHORITY_MAP = {
    "public": AzureAuthorityHosts.AZURE_PUBLIC_CLOUD,
    "china": AzureAuthorityHosts.AZURE_CHINA,
    "government": AzureAuthorityHosts.AZURE_GOVERNMENT,
    "germany": AzureAuthorityHosts.AZURE_GERMANY,
}


@singleton
class MultiRAGAzureSpnBlob:
    def __init__(self):
        self.conn = None
        self.account_url = os.getenv("ACCOUNT_URL", settings.AZURE["account_url"])
        self.client_id = os.getenv("CLIENT_ID", settings.AZURE["client_id"])
        self.secret = os.getenv("SECRET", settings.AZURE["secret"])
        self.tenant_id = os.getenv("TENANT_ID", settings.AZURE["tenant_id"])
        self.container_name = os.getenv("CONTAINER_NAME", settings.AZURE["container_name"])
        self.cloud = os.getenv("AZURE_CLOUD", settings.AZURE.get("cloud", "public")).lower()
        self.__open__()

    def __open__(self):
        try:
            if self.conn:
                self.__close__()
        except Exception:
            pass

        try:
            authority = _CLOUD_AUTHORITY_MAP.get(self.cloud, AzureAuthorityHosts.AZURE_PUBLIC_CLOUD)
            credentials = ClientSecretCredential(tenant_id=self.tenant_id, client_id=self.client_id, client_secret=self.secret, authority=authority)
            self.conn = FileSystemClient(account_url=self.account_url, file_system_name=self.container_name, credential=credentials)
        except Exception:
            logging.exception("Fail to connect %s" % self.account_url)

    def __close__(self):
        del self.conn
        self.conn = None

    def health(self) -> Any:
        return self.put("txtxtxtxt1", "txtxtxtxt1", b"_t@@@1")

    def put(self, bucket: str, fnm: str, binary: bytes, tenant_id: str | None = None) -> Any:
        blob_name = f"{bucket}/{fnm}"
        for attempt in range(3):
            try:
                # ADLS requires each parent directory to exist. Do not reset
                # metadata/ACLs when another writer has already created it.
                parts = blob_name.split("/")
                for index in range(1, len(parts)):
                    try:
                        self.conn.create_directory("/".join(parts[:index]), match_condition=MatchConditions.IfMissing)
                    except ResourceExistsError:
                        pass
                    except ResourceModifiedError as error:
                        # If-None-Match: * may report an existing path as 412.
                        if error.error_code != "ConditionNotMet":
                            raise
                client = self.conn.create_file(blob_name)
                client.append_data(binary, offset=0, length=len(binary))
                return client.flush_data(len(binary))
            except Exception:
                logging.exception(f"Fail put {blob_name}")
                if attempt == 2:
                    raise
                self.__open__()
                time.sleep(2**attempt)

    def rm(self, bucket: str, fnm: str, tenant_id: str | None = None) -> None:
        try:
            self.conn.delete_file(f"{bucket}/{fnm}")
        except Exception:
            logging.exception(f"Fail rm {bucket}/{fnm}")
            raise

    def get(self, bucket: str, fnm: str, tenant_id: str | None = None) -> bytes | None:
        blob_name = f"{bucket}/{fnm}"
        for attempt in range(3):
            try:
                return self.conn.get_file_client(blob_name).download_file().readall()
            except Exception:
                logging.exception(f"Fail get {blob_name}")
                if attempt == 2:
                    raise
                self.__open__()
                time.sleep(2**attempt)
        return None

    def obj_exist(self, bucket: str, fnm: str, tenant_id: str | None = None) -> bool:
        try:
            return self.conn.get_file_client(f"{bucket}/{fnm}").exists()
        except Exception:
            logging.exception(f"Fail obj_exist {bucket}/{fnm}")
        return False

    def get_presigned_url(self, bucket: str, fnm: str, expires: int, tenant_id: str | None = None) -> str:
        # A delegation key can live for at most seven days, including skew.
        if not 0 < expires <= 7 * 24 * 60 * 60 - 300:
            raise ValueError("Azure URL expiry must be between 1 and 604500 seconds")
        now = datetime.now(UTC)
        expiry = now + timedelta(seconds=expires)
        with DataLakeServiceClient(account_url=self.account_url, credential=self.conn.credential) as service:
            key = service.get_user_delegation_key(now - timedelta(minutes=5), expiry)
        path = f"{bucket}/{fnm}"
        directory, _, filename = path.rpartition("/")
        sas = generate_file_sas(
            account_name=self.conn.account_name,
            file_system_name=self.conn.file_system_name,
            directory_name=directory,
            file_name=filename,
            credential=key,
            permission=FileSasPermissions(read=True),
            expiry=expiry,
            protocol="https",
        )
        return f"{self.conn.get_file_client(path).url}?{sas}"
