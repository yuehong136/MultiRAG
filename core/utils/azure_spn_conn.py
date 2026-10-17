import logging
import os
import time
from typing import Any

from azure.core import MatchConditions
from azure.core.exceptions import ResourceExistsError
from azure.identity import AzureAuthorityHosts, ClientSecretCredential
from azure.storage.filedatalake import FileSystemClient

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

    def get_presigned_url(self, bucket, fnm, expires):
        for _ in range(10):
            try:
                return self.conn.get_presigned_url("GET", bucket, fnm, expires)
            except Exception:
                logging.exception(f"fail get {bucket}/{fnm}")
                self.__open__()
                time.sleep(1)
        return None
