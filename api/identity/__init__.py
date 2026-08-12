"""Enterprise identity domain and persistence boundary."""

from api.identity.contracts import ProviderContext
from api.identity.principal import (
    AuthenticationContext,
    AuthenticationSource,
    IdentityAssurance,
    Principal,
    build_principal_from_authenticated_actor,
    build_principal_from_resolved_identity,
)
from api.identity.provisioning import HmacLinkCodeCodec, IdentityProvisioningService
from api.identity.service import IdentityService

__all__ = [
    "AuthenticationContext",
    "AuthenticationSource",
    "HmacLinkCodeCodec",
    "IdentityAssurance",
    "IdentityProvisioningService",
    "IdentityService",
    "Principal",
    "ProviderContext",
    "build_principal_from_authenticated_actor",
    "build_principal_from_resolved_identity",
]
