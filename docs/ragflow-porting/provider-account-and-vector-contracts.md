# Go provider account and vector contracts

These contracts describe the parallel Go implementation. The active Python backend is separate.

## SiliconFlow balance

`SHOW BALANCE` uses the configured `user/info` endpoint with instance credentials. A successful response requires HTTP 200, business code 0, status true and a finite string amount. `totalBalance` takes precedence over `balance`; zero remains valid. The response is `{balance, currency: "CNY"}`. Missing or malformed amounts and provider errors return an error without including the provider body or credentials.

Requests honor cancellation and the existing non-streaming call timeout. Empty regions use the default; unknown regions at the driver boundary fail explicitly. Instance binding resolves any catalog fallback before creating the private driver.

Controlled HTTP tests cover response contracts and instance URL/key isolation; they do not establish real cloud account availability.

## DeepSeek balance

`user/balance` returns the first currency in the common `balance/currency` fields and retains `is_available` plus every entry in `balance_infos`. Amounts are finite numbers; currencies remain separate. Availability false is a valid account state, including zero balances, rather than a transport failure. Missing availability, currencies, total amounts or balance entries fail explicitly. Optional granted and topped-up amounts are validated when present.
