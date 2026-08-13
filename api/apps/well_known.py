"""Public, input-free metadata routes that live outside the versioned API."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from api.identity.mcp_issuer.runtime import get_mcp_token_issuer
from api.identity.mcp_issuer.service import McpTokenIssuer
from common.app_config import AppConfigError

router = APIRouter()
logger = logging.getLogger(__name__)


def require_mcp_token_issuer() -> McpTokenIssuer | None:
    """Resolve the issuer without exposing config, path, or parser failures."""

    try:
        return get_mcp_token_issuer()
    except AppConfigError:
        return None
    except Exception as exc:
        logger.error(
            "mcp_issuer_event=jwks_unavailable result=failed error_type=%s",
            type(exc).__name__,
        )
        return None


@router.get("/.well-known/jwks.json", include_in_schema=False)
def public_mcp_jwks(
    issuer: Annotated[McpTokenIssuer | None, Depends(require_mcp_token_issuer)],
) -> JSONResponse:
    if issuer is None:
        return JSONResponse(
            status_code=503,
            content={"detail": "MCP issuer unavailable"},
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )
    return JSONResponse(
        content=issuer.jwks_document(),
        headers={
            "Cache-Control": f"public, max-age={issuer.jwks_cache_ttl_seconds}",
            "X-Content-Type-Options": "nosniff",
        },
    )


__all__ = ["public_mcp_jwks", "require_mcp_token_issuer", "router"]
