#
#  Copyright 2025 The InfiniFlow Authors. All Rights Reserved.
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

from typing import Any

import jwt

from common.http_client import sync_request

from .oauth import OAuthClient, UserInfo

_ALLOWED_OIDC_SIGNING_ALGS = frozenset({"RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "PS256", "PS384", "PS512", "EdDSA"})


def _resolve_id_token_signing_algs(metadata: dict[str, Any]) -> list[str]:
    """Pin asymmetric verification policy from discovery, never the token header."""
    advertised = metadata.get("id_token_signing_alg_values_supported")
    if not isinstance(advertised, (list, tuple)):
        return ["RS256"]
    safe = [algorithm for algorithm in advertised if isinstance(algorithm, str) and algorithm in _ALLOWED_OIDC_SIGNING_ALGS]
    return list(dict.fromkeys(safe)) or ["RS256"]


class OIDCClient(OAuthClient):
    def __init__(self, config: dict[str, Any]) -> None:
        """
        Initialize the OIDCClient with the provider's configuration.
        Use `issuer` as the single source of truth for configuration discovery.
        """
        self.issuer = config.get("issuer")
        if not self.issuer:
            raise ValueError("Missing issuer in configuration.")

        oidc_metadata = self._load_oidc_metadata(self.issuer)
        config.update(
            {
                "issuer": oidc_metadata["issuer"],
                "jwks_uri": oidc_metadata["jwks_uri"],
                "authorization_url": oidc_metadata["authorization_endpoint"],
                "token_url": oidc_metadata["token_endpoint"],
                "userinfo_url": oidc_metadata["userinfo_endpoint"],
            }
        )

        super().__init__(config)
        self.issuer = config["issuer"]
        self.jwks_uri = config["jwks_uri"]
        self.id_token_signing_algs = _resolve_id_token_signing_algs(oidc_metadata)

    @staticmethod
    def _load_oidc_metadata(issuer: str) -> dict[str, Any]:
        """
        Load OIDC metadata from `/.well-known/openid-configuration`.
        """
        try:
            metadata_url = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
            response = sync_request("GET", metadata_url, timeout=7)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            raise ValueError(f"Failed to fetch OIDC metadata: {e}")

    def parse_id_token(self, id_token: str) -> dict[str, Any]:
        """
        Parse and validate OIDC ID Token (JWT format) with signature verification.
        """
        try:
            # Use PyJWT's PyJWKClient to fetch JWKS and find signing key
            jwks_cli = jwt.PyJWKClient(self.jwks_uri)
            signing_key = jwks_cli.get_signing_key_from_jwt(id_token).key

            # Decode and verify signature
            decoded_token = jwt.decode(
                id_token,
                key=signing_key,
                algorithms=list(self.id_token_signing_algs),
                audience=str(self.client_id),
                issuer=self.issuer,
            )
            return decoded_token
        except Exception as e:
            raise ValueError(f"Error parsing ID Token: {e}")

    def fetch_user_info(self, access_token, id_token=None, **kwargs):
        """
        Fetch user info (synchronous).
        """
        user_info = {}
        if id_token:
            user_info = self.parse_id_token(id_token)

        # Get user info from userinfo endpoint
        oauth_user_info = super().fetch_user_info(access_token)
        user_info.update(oauth_user_info.to_dict())
        return self.normalize_user_info(user_info)

    async def async_fetch_user_info(self, access_token, id_token=None, **kwargs):
        """Async variant of fetch_user_info."""
        user_info = {}
        if id_token:
            user_info = self.parse_id_token(id_token)
        oauth_user_info = await super().async_fetch_user_info(access_token)
        user_info.update(oauth_user_info.to_dict())
        return self.normalize_user_info(user_info)

    def normalize_user_info(self, user_info):
        email = user_info.get("email")
        username = user_info.get("preferred_username", user_info.get("username", str(email).split("@")[0] if email else ""))
        nickname = user_info.get("name", user_info.get("nickname", username))
        avatar_url = user_info.get("picture", user_info.get("avatar_url", ""))
        return UserInfo(email=email, username=username, nickname=nickname, avatar_url=avatar_url)
