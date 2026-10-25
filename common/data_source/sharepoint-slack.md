# SharePoint and Slack ingestion

The Python sync worker supports `sharepoint` and `slack` sources through the
existing connector and dataset binding APIs. Saving configuration does not
contact a provider. The saved-connector `/test` endpoint currently supports
`rest_api` only; these sources report validation failures in their sync logs.

SharePoint configuration uses `credentials.site_url`, `tenant_id`, `client_id`
and `client_secret`. App-only Microsoft Graph access requires admin-consented
`Sites.Read.All` and `Files.Read.All`. Credentials are acquired lazily; every
site library and nested folder is paginated before traversal finishes. File
IDs include the drive ID. Incremental reads use `lastModifiedDateTime` in
`(checkpoint, run_end]`; missing timestamps, malformed pages, permission errors
and download failures fail the task. Content is stored as the actual file bytes.

Slack configuration uses `credentials.slack_bot_token`, optional `channels`
and optional boolean `channel_regex_enabled`. The token must permit channel
listing, history, replies and user lookup for the configured scope. Required
scopes include `channels:read`, `channels:history`, `users:read`, and
`groups:read` / `groups:history` for private channels. Token and channel access
must permit [`conversations.replies`](https://docs.slack.dev/reference/methods/conversations.replies/).
Omitted or null `channels` selects all visible channels; explicit `[]` selects
none. A nonempty array contains channel names, optionally prefixed with `#`.
The worker also accepts comma-separated names and rejects blank entries.
Public channels not yet joined are joined before history reads; private-channel
membership must already be granted. Scope or page failures fail the task rather
than retrying with a narrower public-only scope.

Each Slack thread produces one `.txt` document with UTF-8 messages separated
by blank lines and a stable channel/parent ID. Incremental reads inspect older
parents so new replies and message edits are not missed by a history timestamp
filter; the final thread update time is filtered to `(checkpoint, run_end]`.
This trades additional history/reply API calls for correctness and is subject
to Slack rate limits. Rate-limit failures remain retryable task failures.

Both sources check task cancellation and deadline between requests/pages.
Graph connect/read timeouts are capped at 10/60 seconds, and Slack requests
at 30 seconds, with both constrained by the remaining task budget before each
API call. MSAL uses a 30-second cap, reduced to the remaining budget when its
client is created. Slack connection retries are
finite but can span several transport timeouts before control returns. In-flight
calls and SDK retry loops may therefore delay cancellation; post-request, batch
and final completion guards prevent late successful writes or completion. No source ACLs are mapped into local dataset authorization.

Deleted-file synchronization remains disabled for both sources: a token's
visible site/channel inventory is not proven authoritative after permission
changes. No partial or empty provider result can authorize document pruning.
Failed syncs retain the original retry window and do not schedule a successful
successor. Existing `poll_range_end` may show batch progress on a failed run;
`DONE` plus the atomically scheduled successor identifies a committed checkpoint.
