# MCP protocol and Agent binding probes

From the repository root, run the existing inbound/outbound, modern/legacy
real-process compatibility matrix:

```bash
make mcp-compat
```

The Agent registration probe adds two independent MCP servers exposing the same
canonical tool. It exercises the actual Agent constructor, Canvas, delegation
policy, session transport and model tool router:

```bash
PYTHONPATH=. uv run --no-sync python tests/compat/mcp/agent_tool_binding_probe.py \
  --report-dir /tmp/multirag-agent-binding-unique-run
```

Use a fresh report directory per run. `ownership.json` records child PIDs and
random loopback ports; `result.json` records remote call/argument readback,
principal/resource isolation, original metadata preservation, and cleanup.
Server logs remain available on assertion failure. The probe checks that denied
canonical names and model aliases cause no token issuance or remote HTTP request,
including discovery, and that native alias collisions still dispatch locally.

The model, native tool, database configuration reads and token issuer are
controlled boundaries. The MCP servers and protocol are real local processes;
these checks do not establish third-party MCP, signing, production credentials or
full Agent conversation acceptance. Constructor regressions for both dictionary
and SDK 2 metadata also run in `tests/unit/test_mcp_tool_binding.py` without
external services.
