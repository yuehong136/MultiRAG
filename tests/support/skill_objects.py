"""Strict in-memory object store shared by isolated Skills state tests."""


class MemoryObjects:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    def put(self, bucket: str, key: str, data: bytes) -> None:
        self.objects[bucket, key] = data

    def get(self, bucket: str, key: str) -> bytes:
        return self.objects[bucket, key]

    def get_bytes(self, bucket: str, key: str) -> bytes | None:
        return self.objects.get((bucket, key))

    def rm(self, bucket: str, key: str) -> None:
        self.objects.pop((bucket, key), None)

    def obj_exist(self, bucket: str, key: str) -> bool:
        return (bucket, key) in self.objects
