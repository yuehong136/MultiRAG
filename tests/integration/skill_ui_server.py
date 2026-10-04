"""Explicit opt-in temporary HTTP fixture for browser/CLI acceptance.

Run with pytest and SKILL_UI_HANDOFF_PATH pointing to a private local file.
The browser owner writes <path>.done after acceptance; teardown removes all
owned SQL/object/index resources and the credential handoff file.
"""

import asyncio
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from tests.integration.test_skill_http import install
from tests.support.skills_http import bootstrapped_engine as bootstrapped_engine
from tests.support.skills_http import image_http_api as image_http_api
from tests.support.skills_http import image_http_database as image_http_database
from tests.support.skills_http import image_resources as image_resources
from tests.support.skills_http import skill_complete, skill_request
from tests.support.skills_http import skill_http as skill_http


def test_browser_session(skill_http: dict[str, Any]) -> None:
    env = skill_http
    handoff = Path(os.environ["SKILL_UI_HANDOFF_PATH"])
    done = Path(str(handoff) + ".done")
    assert not handoff.exists() and not done.exists(), "Choose a fresh private handoff path"
    space = skill_request(env, "POST", "/spaces", body={"name": "浏览器验收技能库", "description": "隔离测试环境；本地HTTP模型替身，真实SQL/MinIO/Milvus。"})
    config = skill_request(env, "GET", "/spaces/" + space["id"] + "/config")
    skill_request(env, "PATCH", "/spaces/" + space["id"] + "/config", body={"revision": config["revision"], "embedding_model_id": env["skill_models"][0], "rerank_model_id": env["skill_models"][2]})
    operation = skill_complete(env, install(env, space["id"], "1.0.0"))
    stop = threading.Event()

    def run_worker() -> None:
        while not stop.wait(0.2):
            asyncio.run(env["skill_worker"].run_once())

    thread = threading.Thread(target=run_worker, daemon=True)
    thread.start()
    try:
        with handoff.open("x") as stream:
            handoff.chmod(0o600)
            json.dump(
                {
                    "apiOrigin": env["base"],
                    "token": env["tokens"]["owner"],
                    "userId": env["ids"]["owner"],
                    "email": env["ids"]["owner"] + "@image.test",
                    "spaceId": space["id"],
                    "skillId": operation["result"]["skill_id"],
                    "modelIds": env["skill_models"],
                },
                stream,
            )
        deadline = time.monotonic() + 1800
        while not done.exists() and time.monotonic() < deadline:
            time.sleep(0.5)
        assert done.exists(), "Browser acceptance did not acknowledge completion before timeout"
    finally:
        stop.set()
        thread.join(timeout=120)
        assert not thread.is_alive()
        handoff.unlink(missing_ok=True)
        done.unlink(missing_ok=True)
