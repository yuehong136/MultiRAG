"""WeCom managed worker descriptor."""

from api.channels.managed import ManagedWorkerProvider
from api.channels.wecom.channel import WeComChannel

WORKER_PROVIDER = ManagedWorkerProvider("wecom", WeComChannel)
