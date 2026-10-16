"""AI review-reply engine (model-agnostic, offline by default).

Modules:
    gbp.brand              -- brand profile loading / per-location isolation
    gbp.reply.classify     -- review classification (stars, rating-only, sensitive)
    gbp.reply.model_client -- ModelClient protocol + mock + OpenRouter client
    gbp.reply.duplicate    -- duplicate / boilerplate detection
    gbp.reply.validator    -- the 7-point quality checklist
    gbp.reply.generator    -- prompt building + generation pipeline
    gbp.reply.engine       -- persistence of drafts + audit logs
"""

from __future__ import annotations

from ..brand import BrandLoader, BrandProfile, load_brand_profiles  # noqa: F401
from .classify import Classification, classify  # noqa: F401
from .duplicate import DuplicateChecker, DuplicateResult  # noqa: F401
from .engine import ReviewReplyEngine  # noqa: F401
from .generator import ReplyGenerator  # noqa: F401
from .model_client import (  # noqa: F401
    ApiError,
    MockModelClient,
    ModelClient,
    OpenRouterModelClient,
)
from .validator import CheckResult, ValidationResult, validate  # noqa: F401

__all__ = [
    "BrandLoader",
    "BrandProfile",
    "load_brand_profiles",
    "Classification",
    "classify",
    "DuplicateChecker",
    "DuplicateResult",
    "ReviewReplyEngine",
    "ReplyGenerator",
    "ApiError",
    "MockModelClient",
    "ModelClient",
    "OpenRouterModelClient",
    "CheckResult",
    "ValidationResult",
    "validate",
]
