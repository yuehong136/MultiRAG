"""Telegram managed worker descriptor."""

from api.channels.managed import ManagedWorkerProvider
from api.channels.telegram.channel import TelegramChannel

WORKER_PROVIDER = ManagedWorkerProvider("telegram", TelegramChannel)
