"""Provider boundary for imagery catalogue, quality, preview and download access.

The boundary deliberately uses plain mappings.  The imagery service owns the
Pydantic contracts; providers only translate a remote catalogue into values
that those contracts can validate.  This keeps a provider replaceable without
making the workflow depend on a vendor SDK.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class ProviderFailureKind(StrEnum):
    """Stable, user-visible categories for provider failures."""

    DEPENDENCY_MISSING = "dependency_missing"
    CONFIGURATION_MISSING = "configuration_missing"
    AUTHENTICATION = "authentication"
    PERMISSION = "permission"
    NETWORK = "network"
    QUOTA = "quota"
    INVALID_REQUEST = "invalid_request"
    NO_DATA = "no_data"
    REMOTE = "remote"


class ImageryProviderError(RuntimeError):
    """An expected provider failure with a safe, machine-readable category."""

    def __init__(
        self,
        kind: ProviderFailureKind,
        message: str,
        *,
        retryable: bool = False,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable
        self.cause = cause

    def to_dict(self) -> dict[str, Any]:
        """Return log-safe details; exception internals and credentials are omitted."""

        return {
            "kind": self.kind.value,
            "message": str(self),
            "retryable": self.retryable,
        }


@dataclass(frozen=True, slots=True)
class CatalogPage:
    """A bounded page from a remote catalogue."""

    items: tuple[Mapping[str, Any], ...]
    total_matches: int
    offset: int
    limit: int
    has_more: bool
    catalogue_truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": [dict(item) for item in self.items],
            "total_matches": self.total_matches,
            "offset": self.offset,
            "limit": self.limit,
            "returned": len(self.items),
            "has_more": self.has_more,
            "is_candidate_subset": self.has_more or self.catalogue_truncated,
            "catalogue_truncated": self.catalogue_truncated,
        }


@dataclass(frozen=True, slots=True, repr=False)
class PreviewURLs:
    """Ephemeral provider URLs.

    Signed URLs must never be persisted or emitted to model/event logs.  The
    custom representation and :meth:`to_log_dict` make the safe behaviour the
    easy behaviour while still allowing the fetcher to read the URL fields.
    """

    rgb_url: str
    quality_url: str
    provider: str
    candidate_id: str
    warnings: tuple[str, ...] = ()

    def __repr__(self) -> str:
        return (
            "PreviewURLs(rgb_url='<redacted>', quality_url='<redacted>', "
            f"provider={self.provider!r}, candidate_id={self.candidate_id!r})"
        )

    def to_log_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "candidate_id": self.candidate_id,
            "rgb_url": "<redacted-provider-url>",
            "quality_url": "<redacted-provider-url>",
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True, repr=False)
class ProviderDownloadRequest:
    """One approved, short-lived provider download request."""

    url: str
    provider: str
    tile_id: str
    content_definition_hash: str
    expected_media_type: str = "image/tiff"
    expected_extension: str = ".tif"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        return (
            "ProviderDownloadRequest(url='<redacted>', "
            f"provider={self.provider!r}, tile_id={self.tile_id!r}, "
            f"content_definition_hash={self.content_definition_hash!r})"
        )

    def to_log_dict(self) -> dict[str, Any]:
        return {
            "url": "<redacted-provider-url>",
            "provider": self.provider,
            "tile_id": self.tile_id,
            "content_definition_hash": self.content_definition_hash,
            "expected_media_type": self.expected_media_type,
            "expected_extension": self.expected_extension,
            "metadata": dict(self.metadata),
        }


@runtime_checkable
class BaseImageryProvider(Protocol):
    """Replaceable imagery provider used by the interactive workflow."""

    name: str
    is_mock: bool

    def doctor(self, *, check_remote: bool = False) -> Mapping[str, Any]: ...

    def search_optical(
        self,
        *,
        aoi: Mapping[str, Any] | Any,
        start: str,
        end: str,
        limit: int = 5,
        offset: int = 0,
        window_id: str | None = None,
    ) -> Mapping[str, Any]: ...

    def create_preview_urls(
        self,
        *,
        scene: Mapping[str, Any],
        aoi: Mapping[str, Any] | Any,
        max_dimension: int = 768,
    ) -> PreviewURLs: ...

    def assess_optical_quality(
        self,
        *,
        scene: Mapping[str, Any],
        aoi: Mapping[str, Any] | Any,
        cloud_score_threshold: float = 0.60,
        scale_meters: int = 20,
    ) -> Mapping[str, Any]: ...

    def search_sar(
        self,
        *,
        aoi: Mapping[str, Any] | Any,
        start: str,
        end: str,
        limit: int = 5,
        offset: int = 0,
        window_id: str | None = None,
        target_optical: tuple[Mapping[str, Any], ...] = (),
    ) -> Mapping[str, Any]: ...

    def create_download_request(
        self,
        *,
        scene: Mapping[str, Any],
        tile: Mapping[str, Any],
        approval: Mapping[str, Any],
    ) -> ProviderDownloadRequest: ...
