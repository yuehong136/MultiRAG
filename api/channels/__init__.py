"""External chat-channel transports.

Transport packages self-register their builders when imported. Runtime
supervision and business-message handling intentionally live outside the
upstream-shaped ``core`` and transport boundaries.
"""
