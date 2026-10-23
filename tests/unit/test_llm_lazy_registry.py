"""Fresh interpreters prove lazy imports, first-use concurrency and retries."""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any


def fresh_process(program: str) -> dict[str, Any]:
    env = dict(os.environ, LITELLM_LOCAL_MODEL_COST_MAP="True", HF_HUB_OFFLINE="1")
    result = subprocess.run([sys.executable, "-c", textwrap.dedent(program)], cwd=Path(__file__).resolve().parents[2], env=env, text=True, capture_output=True, timeout=90, check=True)
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_provider_metadata_does_not_import_model_families() -> None:
    observed = fresh_process("""
        import json
        import sys
        from core import llm

        families = ['chat', 'cv', 'embedding', 'rerank', 'sequence2txt', 'tts', 'ocr_model']
        assert not any('core.llm.' + family in sys.modules for family in families)
        assert not any(name in vars(llm) for name in llm.__all__)
        assert set(llm.__all__) <= set(dir(llm))
        assert llm.FACTORY_DEFAULT_BASE_URL[llm.SupportedLiteLLMProvider.OpenAI] == 'https://api.openai.com/v1'
        try:
            llm.UnknownRegistry
        except AttributeError:
            pass
        else:
            raise AssertionError('unknown registry must raise')
        print(json.dumps({'familiesLoaded': [], 'publicRegistries': len(llm.__all__)}))
    """)
    assert observed == {"familiesLoaded": [], "publicRegistries": 7}


def test_first_factory_initialization_is_atomic_and_failed_import_can_retry() -> None:
    observed = fresh_process("""
        import json
        import sys
        import threading
        from concurrent.futures import ThreadPoolExecutor
        from sqlalchemy.orm import Session
        from core import llm
        from api.db.services.tenant_llm_service import TenantLLMService

        assert 'EmbeddingModel' not in vars(llm)
        original = llm.importlib.import_module
        attempts = []
        loading = threading.Event()
        release = threading.Event()
        def import_provider(name):
            if name == 'core.llm.embedding':
                attempts.append(name)
                if len(attempts) == 1:
                    raise ImportError('controlled initialization failure')
                loading.set()
                assert release.wait(20)
            return original(name)
        llm.importlib.import_module = import_provider
        config = {'mdl_type':'embedding', 'llm_factory':'OpenAI', 'llm_name':'text-embedding-3-small', 'api_key':'synthetic-key', 'api_base':'http://127.0.0.1:1/v1'}
        def construct():
            with Session() as db:
                model = TenantLLMService.model_instance(db, 'tenant', config)
            return model, id(llm.EmbeddingModel)
        try:
            construct()
        except ImportError as error:
            assert str(error) == 'controlled initialization failure'
        else:
            raise AssertionError('initialization error was swallowed')
        assert 'EmbeddingModel' not in vars(llm)
        with ThreadPoolExecutor(max_workers=8) as workers:
            jobs = [workers.submit(construct) for _ in range(8)]
            assert loading.wait(20)
            assert 'EmbeddingModel' not in vars(llm)
            release.set()
            models = [job.result(timeout=30) for job in jobs]
        assert len(attempts) == 2
        assert len({registry for model, registry in models}) == 1
        assert all(type(model).__name__ == 'OpenAIEmbed' for model, registry in models)
        assert all(model.client.base_url.host == '127.0.0.1' for model, registry in models)
        assert not any('core.llm.' + family in sys.modules for family in ['chat', 'cv', 'rerank', 'sequence2txt', 'tts', 'ocr_model'])
        for model, registry in models:
            model.client.close()
        print(json.dumps({'attempts':len(attempts), 'models':len(models), 'registryIdentities':1}))
    """)
    assert observed == {"attempts": 2, "models": 8, "registryIdentities": 1}
