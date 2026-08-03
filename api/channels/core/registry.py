#
#  Copyright 2024 The InfiniFlow Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from .base import Channel

LOGGER = logging.getLogger(__name__)

ChannelConfig = dict[str, Any]
ChannelBuilder = Callable[[str, ChannelConfig], Channel]

_BUILDERS: dict[str, ChannelBuilder] = {}


def register_channel(name: str, builder: ChannelBuilder) -> None:
    _BUILDERS[name] = builder


def registered_channel_ids() -> list[str]:
    return sorted(_BUILDERS)


def build_channels(config: ChannelConfig) -> list[Channel]:
    """Construct one channel per ``channels.<name>.accounts.<id>`` entry."""

    instances: list[Channel] = []
    channels_config = config.get("channels") or {}
    if not isinstance(channels_config, dict):
        return instances

    for name, raw in channels_config.items():
        if not isinstance(name, str) or not isinstance(raw, dict):
            continue
        if raw.get("enabled") is False:
            continue

        builder = _BUILDERS.get(name)
        if builder is None:
            LOGGER.warning(
                "channel_event=builder_missing channel=%s result=skipped error_code=CHANNEL_NOT_REGISTERED",
                name,
            )
            continue

        accounts = raw.get("accounts") or {}
        if not accounts:
            accounts = {"default": {key: value for key, value in raw.items() if key != "accounts"}}
        if not isinstance(accounts, dict):
            continue

        shared = {key: value for key, value in raw.items() if key not in ("accounts", "default_account")}
        for account_id, account_config in accounts.items():
            if not isinstance(account_config, dict):
                continue
            if account_config.get("enabled") is False:
                continue
            merged = {**shared, **account_config}
            instances.append(builder(str(account_id), merged))

    return instances
