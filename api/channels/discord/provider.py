"""Discord managed worker descriptor."""

from api.channels.discord.channel import DiscordChannel
from api.channels.managed import ManagedWorkerProvider

WORKER_PROVIDER = ManagedWorkerProvider("discord", DiscordChannel)
