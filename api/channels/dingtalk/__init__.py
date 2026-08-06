"""DingTalk transport.

Imports nothing at package level: `api/channels/feishu/__init__.py` eagerly
imports its SDK, and anything that reaches a provider package by name must be
able to do so without paying for a transport it is not going to use.
"""
