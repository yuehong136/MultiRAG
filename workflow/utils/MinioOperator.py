from minio import Minio
from minio.error import S3Error
from core import settings
import io


class MinioOperator:
    # def __init__(self, endpoint, access_key, secret_key, secure=True):
    #     self.client = Minio(
    #         endpoint,
    #         access_key=access_key,
    #         secret_key=secret_key,
    #         secure=secure
    #     )

    def __init__(self):
        self.client = Minio(settings.MINIO["host"],
                            access_key=settings.MINIO["user"],
                            secret_key=settings.MINIO["password"],
                            secure=False
                            )

    def create_bucket(self, bucket_name):
        try:
            if not self.client.bucket_exists(bucket_name):
                self.client.make_bucket(bucket_name)
                print(f"桶 '{bucket_name}' 创建成功")
            else:
                print(f"桶 '{bucket_name}' 已经存在")
        except S3Error as e:
            print(f"创建桶时发生错误: {e}")

    def upload_file(self, bucket_name, object_name, file_path):
        try:
            self.client.fput_object(bucket_name, object_name, file_path)
            print(f"文件 '{file_path}' 成功上传到 '{bucket_name}/{object_name}'")
        except S3Error as e:
            print(f"上传文件时发生错误: {e}")

    def upload_file_from_memory(self, bucket_name, object_name, file_data,
                                content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'):
        try:
            # 将字节串转换为 BytesIO 对象
            file_stream = io.BytesIO(file_data)

            # 获取数据的长度
            file_size = len(file_data)

            # 使用 put_object 上传数据
            self.client.put_object(
                bucket_name,
                object_name,
                file_stream,
                file_size,
                content_type=content_type
            )
            print(f"文件成功上传到 '{bucket_name}/{object_name}'")
            return bucket_name, object_name
        except Exception as e:
            print(f"上传文件时发生错误: {e}")

    def download_file(self, bucket_name, object_name, file_path):
        try:
            self.client.fget_object(bucket_name, object_name, file_path)
            print(f"文件 '{bucket_name}/{object_name}' 成功下载到 '{file_path}'")
        except S3Error as e:
            print(f"下载文件时发生错误: {e}")

    def download_to_memory(self, bucket_name, object_name):
        try:
            # 获取对象
            response = self.client.get_object(bucket_name, object_name)

            # 读取所有数据到一个 BytesIO 对象
            data = io.BytesIO(response.read())

            # 确保在使用完 response 后关闭它
            response.close()
            response.release_conn()

            print(f"文件 '{bucket_name}/{object_name}' 成功下载到内存")

            # 返回 BytesIO 对象
            return data
        except Exception as e:
            print(f"下载文件到内存时发生错误: {e}")
            return None

    def list_objects(self, bucket_name, prefix=None, recursive=False):
        try:
            objects = self.client.list_objects(bucket_name, prefix=prefix, recursive=recursive)
            result = []

            if recursive:
                # 列出所有对象
                for obj in objects:
                    result.append({
                        "name": obj.object_name,
                        "size": obj.size,
                        "type": "file"
                    })
            else:
                # 只列出最浅一层的内容
                seen_prefixes = set()
                for obj in objects:
                    # 分割对象名，获取最浅层的部分
                    parts = obj.object_name.split('/')
                    if prefix:
                        # 如果有前缀，去掉前缀部分
                        prefix_parts = prefix.rstrip('/').split('/')
                        parts = parts[len(prefix_parts):]

                    if len(parts) > 1:
                        # 这是一个更深层次的对象，只添加其前缀
                        top_level = parts[0] + '/'
                        if top_level not in seen_prefixes:
                            result.append({
                                "name": prefix + top_level if prefix else top_level,
                                "type": "directory"
                            })
                            seen_prefixes.add(top_level)
                    else:
                        # 这是最浅层的对象
                        result.append({
                            "name": obj.object_name,
                            "size": obj.size,
                            "type": "file"
                        })

            return result
        except S3Error as e:
            print(f"列出对象时发生错误: {e}")
            return []

    def delete_object(self, bucket_name, object_name):
        try:
            self.client.remove_object(bucket_name, object_name)
            print(f"对象 '{bucket_name}/{object_name}' 已成功删除")
        except S3Error as e:
            print(f"删除对象时发生错误: {e}")


# 使用示例
if __name__ == "__main__":
    # 替换为您的 MinIO 服务器信息
    minio_handler = MinioOperator()

    bucket_name = "2024"
    # minio_handler.create_bucket(bucket_name)

    # 上传文件
    # minio_handler.upload_file(bucket_name, "test-file.txt", "/path/to/local/file.txt")

    # 列出桶中的对象
    list = minio_handler.list_objects(bucket_name="2024", prefix="/01/01/", recursive=True)
    print(list)

    # 下载文件
    # minio_handler.download_file(bucket_name, "test-file.txt", "/path/to/download/file.txt")

    # 删除文件
    # minio_handler.delete_object(bucket_name, "test-file.txt")
