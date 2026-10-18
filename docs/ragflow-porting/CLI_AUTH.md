# Go CLI authentication transport

The Go CLI builds ordinary, benchmark, streaming, Context Engine and Skill
requests from `/api/v1`. The Context Engine is the local counterpart of the
upstream filesystem client. Go-only metadata/index/chunk helpers are registered
under that same authenticated namespace; they are not Python retrieval routes.
The production Python retrieval workbench uses `/datasets/{id}/search`.

User login uses public `GET /api/v1/system/ping`, then RSA-encrypted
`POST /api/v1/auth/login` with `username` (the email address) and `password`.
This retains the local Python/Go REST contract and current account policy.
Admin login uses `/api/v1/admin/ping` and `/api/v1/admin/login` with `email`.
The two authentication domains remain separate. Public system configuration,
version, login channels and registration do not expose protected system configs.
The old Go public `/v1/user/login` and `/v1/user/register` routes are removed.

Authentication responses require an explicit successful business code and HTTP
200. The CLI accepts Go `code/message` and current Python `retcode/retmsg`;
missing or conflicting codes fail. SQL `LOGIN ... WITH PASSWORD ...` works in
single-command mode without a second password prompt.

Registration executes `REGISTER USER 'email' AS 'nickname' PASSWORD 'password';`
through public `POST /api/v1/users`, encrypting the password with RSA before
sending it. Go registration stores the current Python bcrypt format so new
accounts can authenticate on either backend. Registration creates a regular user and does not reuse or overwrite
an existing login session. User logout is authenticated `POST /api/v1/auth/logout` with a `Bearer` session token
(the CLI normalizes an existing prefix without duplicating it);
admin logout retains its separate `GET /api/v1/admin/logout` contract. A successful
logout clears the CLI session, while a rejected logout preserves it.

The independent Web Vite configuration already proxies all `/api` requests to
its configured backend. It covers users and auth without an upstream-specific
Go proxy split or a production configuration change.

The CLI retains the existing shared chat error formatter. Both streaming and
non-streaming chat failures distinguish EOF and timeout and preserve the cause
for `errors.Is`/`errors.As`. A second formatter provides no additional behavior.

This file describes local behavior; batch mappings and machine-specific
acceptance records are held by the integration owner outside the repository.
