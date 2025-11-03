# coding=utf-8
"""
@project: multirag
@Author：龙
@file： minio_conn.py
@date：2024/7/19 14:00
@desc:
"""
import logging
import time
from minio import Minio, S3Error
from io import BytesIO
from core import settings
from core.utils import singleton


@singleton
class MultiRAGMinio:
    def __init__(self):
        self.conn = None
        self.__open__()

    def __open__(self):
        try:
            if self.conn:
                self.__close__()
        except Exception:
            pass

        try:
            # Create connection parameters dictionary
            conn_params = {
                "endpoint": settings.MINIO["host"],
                "access_key": settings.MINIO["user"],
                "secret_key": settings.MINIO["password"],
                "secure": False
            }

            # Only add region parameter if it's not empty
            if settings.MINIO["region"]:
                conn_params["region"] = settings.MINIO["region"]

            # Initialize MinIO client with the parameters
            self.conn = Minio(**conn_params)
        except Exception:
            logging.exception(
                "Fail to connect %s " % settings.MINIO["host"])

    def __close__(self):
        del self.conn
        self.conn = None

    def health(self):
        bucket, fnm, binary = "txtxtxtxt1", "txtxtxtxt1", b"_t@@@1"
        if not self.conn.bucket_exists(bucket):
            self.conn.make_bucket(bucket)
        r = self.conn.put_object(bucket, fnm,
                                 BytesIO(binary),
                                 len(binary)
                                 )
        return r

    def put(self, bucket, fnm, binary, tenant_id=None):
        for _ in range(3):
            try:
                if not self.conn.bucket_exists(bucket):
                    self.conn.make_bucket(bucket)

                r = self.conn.put_object(bucket, fnm,
                                         BytesIO(binary),
                                         len(binary)
                                         )
                return r
            except Exception:
                logging.exception(f"Fail to put {bucket}/{fnm}:")
                self.__open__()
                time.sleep(1)

    def rm(self, bucket, fnm, tenant_id=None):
        try:
            self.conn.remove_object(bucket, fnm)
        except Exception:
            logging.exception(f"Fail to remove {bucket}/{fnm}:")

    def get(self, bucket, filename, tenant_id=None):
        for _ in range(1):
            try:
                r = self.conn.get_object(bucket, filename)
                return r.read()
            except Exception:
                logging.exception(f"Fail to get {bucket}/{filename}")
                self.__open__()
                time.sleep(1)
        return

    def obj_exist(self, bucket, filename, tenant_id=None):
        try:
            if not self.conn.bucket_exists(bucket):
                return False
            if self.conn.stat_object(bucket, filename):
                return True
            else:
                return False
        except S3Error as e:
            if e.code in ["NoSuchKey", "NoSuchBucket", "ResourceNotFound"]:
                return False
        except Exception:
            logging.exception(f"obj_exist {bucket}/{filename} got exception")
            return False

    def bucket_exists(self, bucket):
        try:
            if not self.conn.bucket_exists(bucket):
                return False
            else:
                return True
        except S3Error as e:
            if e.code in ["NoSuchKey", "NoSuchBucket", "ResourceNotFound"]:
                return False
        except Exception:
            logging.exception(f"bucket_exist {bucket} got exception")
            return False

    def get_presigned_url(self, bucket, fnm, expires, tenant_id=None):
        for _ in range(10):
            try:
                return self.conn.get_presigned_url("GET", bucket, fnm, expires)
            except Exception:
                logging.exception(f"Fail to get_presigned {bucket}/{fnm}:")
                self.__open__()
                time.sleep(1)
        return

    def remove_bucket(self, bucket):
        """
        Deletes a bucket from MinIO.

        :param bucket: Name of the bucket to delete.
        """
        try:
            if self.conn.bucket_exists(bucket):
                # Ensure the bucket is empty before deleting
                objects = self.conn.list_objects(bucket, recursive=True)
                for obj in objects:
                    self.conn.remove_object(bucket, obj.object_name)
                self.conn.remove_bucket(bucket)
                logging.info(f"Bucket {bucket} successfully deleted.")
            else:
                logging.warning(f"Bucket {bucket} does not exist.")
        except Exception:
            logging.exception(f"Failed to delete bucket {bucket}.")