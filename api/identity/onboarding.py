"""Verified, dry-run-first orchestration for Channel identity onboarding."""

from __future__ import annotations

from weakref import WeakValueDictionary

from api.identity.contracts import IdentityErrorCode
from api.identity.onboarding_contracts import (
    ChannelOnboardingCredentialSource,
    ChannelOnboardingError,
    ChannelOnboardingPlan,
    ChannelOnboardingRequest,
    ChannelOnboardingResult,
    FeishuInstallationVerifier,
    IdentityLinkCodeCodecFactory,
    VerifiedChannelOnboardingIntent,
    VerifiedChannelOnboardingRepository,
)
from api.identity.providers.contracts import ProviderErrorCode
from api.identity.provisioning import HmacLinkCodeCodec


class ChannelOnboardingService:
    """Create a verified dry-run plan, then explicitly apply that exact plan.

    Provider verification is complete before either repository method is
    entered.  The concrete repository owns only short database scopes and the
    apply method owns the single authoritative write transaction.
    """

    def __init__(
        self,
        source: ChannelOnboardingCredentialSource,
        verifier: FeishuInstallationVerifier,
        repository: VerifiedChannelOnboardingRepository,
        link_code_codec_factory: IdentityLinkCodeCodecFactory,
    ) -> None:
        self._source = source
        self._verifier = verifier
        self._repository = repository
        self._link_code_codec_factory = link_code_codec_factory
        self._plan_seal = object()
        self._issued_plans: WeakValueDictionary[int, ChannelOnboardingPlan] = WeakValueDictionary()

    async def plan(self, request: ChannelOnboardingRequest) -> ChannelOnboardingPlan:
        """Run a verified, non-authoritative dry-run without database writes."""

        self._require_link_code_readiness()
        try:
            source = await self._source.load(request.channel_id)
        except ChannelOnboardingError as error:
            raise ChannelOnboardingError(error.code) from None
        except Exception:
            raise ChannelOnboardingError(ProviderErrorCode.CREDENTIAL_UNAVAILABLE) from None

        if source.snapshot.channel_id != request.channel_id:
            raise ChannelOnboardingError(IdentityErrorCode.ASSERTION_INVALID)

        try:
            proof = await self._verifier.verify(source)
        except ChannelOnboardingError as error:
            raise ChannelOnboardingError(error.code) from None
        except Exception:
            raise ChannelOnboardingError(ProviderErrorCode.PROVIDER_UNAVAILABLE) from None

        intent = VerifiedChannelOnboardingIntent(
            source=source.snapshot,
            proof=proof,
            mode=request.mode,
            link_code_ttl_seconds=request.link_code_ttl_seconds,
        )
        try:
            actions = await self._repository.preview(intent)
        except ChannelOnboardingError as error:
            raise ChannelOnboardingError(error.code) from None
        except Exception:
            raise ChannelOnboardingError(IdentityErrorCode.REPOSITORY_UNAVAILABLE) from None
        plan = ChannelOnboardingPlan(
            actions=actions,
            mode=intent.mode,
            link_code_ttl_seconds=intent.link_code_ttl_seconds,
            _intent=intent,
            _seal=self._plan_seal,
        )
        self._issued_plans[id(plan)] = plan
        return plan

    async def apply(self, plan: ChannelOnboardingPlan) -> ChannelOnboardingResult:
        """Apply one plan after readiness is rechecked, before any DB write."""

        # Consume the exact object before the first await.  A copied plan can
        # retain the private seal while replacing its hidden intent, so the
        # seal alone is not an integrity boundary.
        registered = self._issued_plans.pop(id(plan), None) if isinstance(plan, ChannelOnboardingPlan) else None
        if registered is not plan or plan._seal is not self._plan_seal or plan.mode is not plan._intent.mode or plan.link_code_ttl_seconds != plan._intent.link_code_ttl_seconds:
            raise ChannelOnboardingError(IdentityErrorCode.ASSERTION_INVALID)
        self._require_link_code_readiness()
        try:
            return await self._repository.apply(plan._intent)
        except ChannelOnboardingError as error:
            raise ChannelOnboardingError(error.code) from None
        except Exception:
            raise ChannelOnboardingError(IdentityErrorCode.REPOSITORY_UNAVAILABLE) from None

    def _require_link_code_readiness(self) -> None:
        try:
            codec = self._link_code_codec_factory()
        except Exception:
            raise ChannelOnboardingError(IdentityErrorCode.POLICY_UNAVAILABLE) from None
        if not isinstance(codec, HmacLinkCodeCodec):
            raise ChannelOnboardingError(IdentityErrorCode.POLICY_UNAVAILABLE)
