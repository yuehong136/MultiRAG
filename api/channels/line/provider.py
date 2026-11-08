"""LINE managed worker descriptor."""

from api.channels.line.channel import LineChannel
from api.channels.managed import ManagedWorkerProvider

WORKER_PROVIDER = ManagedWorkerProvider("line", LineChannel)
