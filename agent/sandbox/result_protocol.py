#
#  Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
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

import base64
import binascii
import json
from typing import Any


RESULT_MARKER_PREFIX = "__RAGFLOW_RESULT__:"


def build_python_wrapper(code: str, args_json: str) -> str:
    return f"""{code}

if __name__ == "__main__":
    import base64
    import json

    result = main(**json.loads({args_json!r}))
    payload = json.dumps({{"present": True, "value": result, "type": "json"}}, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    print("\\n{RESULT_MARKER_PREFIX}" + base64.b64encode(payload.encode("utf-8")).decode("ascii"))
"""


def build_javascript_wrapper(code: str, args_json: str) -> str:
    return f"""{code}

const __ragflowArgs = {args_json};

(async () => {{
  const __ragflowMain = typeof main !== 'undefined' ? main : module.exports && module.exports.main;
  if (typeof __ragflowMain !== 'function') {{
    throw new Error('main() must be defined or exported.');
  }}
  const output = await Promise.resolve(__ragflowMain(__ragflowArgs));
  if (typeof output === 'undefined') {{
    throw new Error('main() must return a value. Use null for an empty result.');
  }}
  const serializedOutput = JSON.stringify(output);
  if (typeof serializedOutput === 'undefined') {{
    throw new Error('main() returned a non-JSON-serializable value.');
  }}
  const payload = JSON.stringify({{ present: true, value: JSON.parse(serializedOutput), type: 'json' }});
  console.log('\\n{RESULT_MARKER_PREFIX}' + Buffer.from(payload, 'utf8').toString('base64'));
}})();
"""


def extract_structured_result(stdout: str) -> tuple[str, dict[str, Any]]:
    if not stdout:
        return "", {}

    cleaned_lines: list[str] = []
    structured_result: dict[str, Any] = {}

    for line in str(stdout).splitlines():
        if line.startswith(RESULT_MARKER_PREFIX):
            payload_b64 = line[len(RESULT_MARKER_PREFIX) :].strip()
            if not payload_b64:
                cleaned_lines.append(line)
                continue
            try:
                payload = base64.b64decode(payload_b64, validate=True).decode("utf-8")
                candidate = json.loads(payload)
                if not isinstance(candidate, dict) or candidate.get("present") is not True or candidate.get("type") != "json" or "value" not in candidate:
                    cleaned_lines.append(line)
                    continue
                if cleaned_lines and cleaned_lines[-1] == "":
                    cleaned_lines.pop()
                structured_result = candidate
            except (ValueError, UnicodeError, binascii.Error):
                cleaned_lines.append(line)
            continue
        cleaned_lines.append(line)

    cleaned_stdout = "\n".join(cleaned_lines)
    if stdout.endswith("\n") and cleaned_stdout and not cleaned_stdout.endswith("\n"):
        cleaned_stdout += "\n"
    return cleaned_stdout, structured_result
