"""OpenAI-shaped errors returned by the gateway data plane."""

from typing import Any


class GatewayError(Exception):
    """A request outcome that maps to one HTTP status and one error body."""

    def __init__(self, status_code: int, message: str, *, error_type: str, code: str, param: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.error_type = error_type
        self.code = code
        self.param = param

    def body(self) -> dict[str, Any]:
        return {"error": {"message": self.message, "type": self.error_type, "param": self.param, "code": self.code}}


def invalid_api_key() -> GatewayError:
    return GatewayError(401, "Incorrect API key provided.", error_type="invalid_request_error", code="invalid_api_key")


def api_key_disabled() -> GatewayError:
    return GatewayError(401, "This API key has been disabled or revoked.", error_type="invalid_request_error", code="api_key_disabled")


def api_key_expired() -> GatewayError:
    return GatewayError(401, "This API key has expired.", error_type="invalid_request_error", code="api_key_expired")


def invalid_request(message: str, *, param: str | None = None, code: str = "invalid_request") -> GatewayError:
    return GatewayError(400, message, error_type="invalid_request_error", code=code, param=param)


def request_too_large(limit: int) -> GatewayError:
    return GatewayError(413, f"Request body exceeds the {limit} byte limit.", error_type="invalid_request_error", code="request_too_large")


def model_not_allowed(model: str) -> GatewayError:
    return GatewayError(403, f"This API key is not allowed to use the model '{model}'.", error_type="invalid_request_error", code="model_not_allowed", param="model")


def model_not_found(model: str) -> GatewayError:
    return GatewayError(404, f"The model '{model}' does not exist.", error_type="invalid_request_error", code="model_not_found", param="model")


def model_unavailable(model: str) -> GatewayError:
    return GatewayError(503, f"The model '{model}' is currently unavailable.", error_type="server_error", code="model_unavailable", param="model")


def upstream_auth_error() -> GatewayError:
    """The gateway's own credential was rejected upstream; the caller's key is fine."""
    return GatewayError(502, "The model service rejected the gateway's credentials.", error_type="upstream_error", code="upstream_auth_error")


def upstream_error(status_code: int) -> GatewayError:
    return GatewayError(502, f"The model service failed with status {status_code}.", error_type="upstream_error", code="upstream_error")


def upstream_unreachable() -> GatewayError:
    return GatewayError(502, "The model service could not be reached.", error_type="upstream_error", code="upstream_unreachable")


def upstream_timeout() -> GatewayError:
    return GatewayError(504, "The model service did not respond in time.", error_type="upstream_error", code="upstream_timeout")


def not_found() -> GatewayError:
    return GatewayError(404, "Unknown request URL.", error_type="invalid_request_error", code="not_found")


def method_not_allowed() -> GatewayError:
    return GatewayError(405, "Method not allowed for this URL.", error_type="invalid_request_error", code="method_not_allowed")


def internal_error() -> GatewayError:
    return GatewayError(500, "The gateway failed to process the request.", error_type="server_error", code="internal_error")
