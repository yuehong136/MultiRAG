import base64
import io
import pickle
from collections.abc import Buffer
from typing import Any

from api.utils.common import bytes_to_string, string_to_bytes

safe_module = {"numpy", "multi_rag"}


class RestrictedUnpickler(pickle.Unpickler):
    def find_class(self, module, name):
        import importlib

        if module.split(".")[0] in safe_module:
            _module = importlib.import_module(module)
            return getattr(_module, name)
        # Forbid everything else.
        raise pickle.UnpicklingError("global '%s.%s' is forbidden" % (module, name))


def restricted_loads(src):
    """Helper function analogous to pickle.loads()."""
    return RestrictedUnpickler(io.BytesIO(src)).load()


def serialize_b64(src, to_str=False):
    dest = base64.b64encode(pickle.dumps(src))
    if not to_str:
        return dest
    else:
        return bytes_to_string(dest)


def deserialize_b64(src: str | Buffer) -> Any:
    payload = base64.b64decode(string_to_bytes(src) if isinstance(src, str) else src)
    return restricted_loads(payload)
