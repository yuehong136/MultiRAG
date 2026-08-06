"""Feishu transport.

Imports nothing at package level. It used to re-export ``FeishuChannel`` and
friends, which meant reaching *any* module in this package -- including
``verify``, which the API process imports and which deliberately avoids the
SDK -- eagerly loaded lark-oapi and its process-global event loop. Same rule
the DingTalk package already states; Feishu just predates it. Import the
submodule you need (``from api.channels.feishu.channel import FeishuChannel``).
"""
