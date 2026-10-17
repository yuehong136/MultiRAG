import logging
import os
import time
from io import BytesIO
from typing import Any

from azure.storage.blob import ContainerClient

from common import settings
from common.decorator import singleton


@singleton
class MultiRAGAzureSasBlob:
    def __init__(self):
        self.conn = None
        self.container_url = os.getenv("CONTAINER_URL", settings.AZURE["container_url"])
        self.sas_token = os.getenv("SAS_TOKEN", settings.AZURE["sas_token"])
        self.__open__()

    def __open__(self):
        try:
            if self.conn:
                self.__close__()
        except Exception:
            pass

        try:
            self.conn = ContainerClient.from_container_url(self.container_url + "?" + self.sas_token)
        except Exception:
            logging.exception("Fail to connect %s " % self.container_url)

    def __close__(self):
        del self.conn
        self.conn = None

    def health(self) -> Any:
        return self.put("txtxtxtxt1", "txtxtxtxt1", b"_t@@@1")

    def put(self, bucket: str, fnm: str, binary: bytes, tenant_id: str | None = None) -> Any:
        blob_name = f"{bucket}/{fnm}"
        for attempt in range(3):
            try:
                return self.conn.upload_blob(name=blob_name, data=BytesIO(binary), length=len(binary), overwrite=True)
            except Exception:
                logging.exception(f"Fail put {blob_name}")
                if attempt == 2:
                    raise
                self.__open__()
                time.sleep(2**attempt)

    def rm(self, bucket: str, fnm: str, tenant_id: str | None = None) -> None:
        try:
            self.conn.delete_blob(f"{bucket}/{fnm}")
        except Exception:
            logging.exception(f"Fail rm {bucket}/{fnm}")
            raise

    def get(self, bucket: str, fnm: str, tenant_id: str | None = None) -> bytes | None:
        blob_name = f"{bucket}/{fnm}"
        for attempt in range(3):
            try:
                return self.conn.download_blob(blob_name).readall()
            except Exception:
                logging.exception(f"Fail get {blob_name}")
                if attempt == 2:
                    raise
                self.__open__()
                time.sleep(2**attempt)
        return None

    def obj_exist(self, bucket: str, fnm: str, tenant_id: str | None = None) -> bool:
        try:
            return self.conn.get_blob_client(f"{bucket}/{fnm}").exists()
        except Exception:
            logging.exception(f"Fail obj_exist {bucket}/{fnm}")
        return False

    def get_presigned_url(self, bucket: str, fnm: str, expires: int, tenant_id: str | None = None) -> str:
        # A container SAS cannot sign a new, narrower SAS or honor expires.
        # Returning BlobClient.url would disclose the container credential.
        raise NotImplementedError("Azure SAS credentials cannot sign an object-scoped URL; use Azure SPN for presigned URLs")
