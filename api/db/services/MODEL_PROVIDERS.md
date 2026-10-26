# Tenant model providers and instances

The Python API manages a tenant's provider, its independently configured instances,
and each instance's models through `/api/v1/providers` and `/api/v1/models`.
`provider_api.py` owns HTTP validation and request transactions;
`model_provider_service.py` uses the caller's `AsyncSession`. It never commits
inside lifecycle operations. Mutations lock the tenant before checking conflicts
or references, and the route commits once or rolls back the request.

A provider name is unique within a tenant; an instance name is unique within that
provider. Credential values are not identities. Two instances can share a key and
use different endpoints. `api_key` is a string, including an existing provider's
complete JSON credential envelope. The API preserves provider-specific fields.
Instance listings return `has_api_key`, never the credential. An omitted or blank
PATCH credential retains it; `clear_api_key: true` explicitly clears it only for
providers that allow keyless configuration. An empty region is valid.

A model has an integer ID, raw provider model name, explicit capability list,
nonnegative token limit and per-model tool capability. Model references exposed
to selectors are decimal strings of that ID. Callers resolving a model with
multiple capabilities must supply the capability they need. Configuration
resolution checks tenant ownership and effective provider/instance/model status
before exposing credentials, and supplies the raw model name to the driver.
Worker rebinding must retain the authorized numeric ID and capability.

`GET /api/v1/models/default` and `PATCH /api/v1/models/default` manage defaults.
The PATCH body is `{ "model_type": "chat", "model_id": 123 }`; a null ID clears
only that capability. OCR is included. Lists return `items`, `total`, `page` and
`page_size`; pages start at one and page size is limited to 100. Query keys cannot
be duplicated, and boolean query values are exactly `true` or `false`.
An explicit empty `model_ids` list creates an instance without catalog models.
Omitting it selects that provider's catalog. Model route IDs require canonical
positive decimal integers; malformed, overflowing and noncanonical forms fail.

Deletion checks persisted defaults and model-reference fields in datasets,
documents, dialogs, memory, canvases and versions, saved searches, skills and
execution configurations before deleting any model. Unrelated free text and
other tenants' legacy names do not count as references. An empty provider can be
deleted. A conflict leaves the whole request unchanged.

## Existing installations

Alembic revision `b8d0f2a4c6e8` adds three tables and OCR default fields and widens
the legacy endpoint column. It keeps `TenantLLM` IDs, accounting, stored names,
status and default references. Each legacy credential-envelope/endpoint group
becomes a deterministic instance. Different stored credentials remain distinct.
Backfill is idempotent and also links models created during user initialization.
Deploy through the repository's normal Alembic process before serving the new API.
This document does not authorize a production migration.

New models use opaque legacy storage keys; public projections and driver calls
resolve them to the raw model name. Existing exact legacy names remain valid,
including numeric-looking aliases. A numeric reference colliding with a distinct
persisted legacy alias fails rather than selecting a different model.

Legacy model management routes remain deprecated for the active admin client.
Their mutations reject providers already linked to the instance model with HTTP
409, before probing credentials or writing. Use the provider-instance API for
those providers. Legacy read projections retain disabled entries and numeric
references without exposing opaque keys or credentials.

Downgrade deliberately refuses to delete model registrations or restore the
shorter endpoint column. Take a database backup before migration. A rollback
requires an explicit export/conversion of any newly created instances and model
references, followed by reviewed schema changes; an automatic destructive
rollback cannot preserve those identities.

## Verification boundaries

The integration suite `tests/integration/test_provider_instances.py` uses an
isolated PostgreSQL database and a controlled OpenAI-compatible HTTP server. It
covers request rollback, independent SQL readback, ownership, references,
idempotent migration, defaults, credential separation and usage accounting.
It does not prove vendor entitlement, paid-provider behavior or a production
worker run. Browser acceptance against the Python application and final combined
CI are separate checks.
