"""External chat-channel transports.

The pure provider registry resolves worker descriptors lazily. Runtime
supervision and business-message handling live outside transport adapters;
provider packages do not self-register or start listeners during import.
"""
