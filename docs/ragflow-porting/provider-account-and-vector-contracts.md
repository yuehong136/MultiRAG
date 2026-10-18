# Go provider account and vector contracts

These contracts describe the parallel Go implementation. The active Python backend is separate.

## SiliconFlow balance

`SHOW BALANCE` uses the configured `user/info` endpoint with instance credentials. A successful response requires HTTP 200, business code 0, status true and a finite string amount. `totalBalance` takes precedence over `balance`; zero remains valid. The response is `{balance, currency: "CNY"}`. Missing or malformed amounts and provider errors return an error without including the provider body or credentials.

Requests honor cancellation and the existing non-streaming call timeout. Empty regions use the default; unknown regions at the driver boundary fail explicitly. Instance binding resolves any catalog fallback before creating the private driver.

Controlled HTTP tests cover response contracts and instance URL/key isolation; they do not establish real cloud account availability.
