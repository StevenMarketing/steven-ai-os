"""Prompt building + the controlled generation pipeline.

The pipeline is: classify -> load brand -> build prompt -> model.complete ->
parse JSON ``{"reply": ...}`` -> validate -> duplicate check -> at most ONE
controlled repair attempt. Timeouts, API errors and invalid JSON are returned
as a ``failed`` status instead of raising.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..brand import BrandLoader, BrandProfile
from .classify import Classification, classify
from .duplicate import DuplicateChecker
from .model_client import ApiError, MockModelClient, ModelClient
from .validator import ValidationResult, validate

__all__ = ["ReplyGenerator", "SYSTEM_PROMPT", "detect_language", "extract_json"]

SYSTEM_PROMPT = """You are the review-reply writer for ONE nail salon. You write DRAFTS only.

HARD RULES (never break):
1. Write the reply in the language of the review. If the review language is
   unclear, use the brand's configured language. A Vietnamese review gets a
   Vietnamese reply; do not answer it in English (or the reverse).
2. Never use a fixed template. Vary the opening and the closing every time and
   vary the length. Do not reuse a canned "thank you for your feedback" opener.
3. Only mention services that (a) the customer mentioned in the review, or
   (b) appear in the VERIFIED SERVICES list below AND the brand flag
   has_services is true. Never mention any other service.
4. NEVER invent staff names, staff members, prices, promotions, locations or
   branches. Do not invent the customer's experience, room, timing or process.
5. NEVER promise compensation: no discount, refund, voucher, free service or
   "we'll make it right next time" unless it is explicitly approved (it is not).
6. Grounding is mandatory: never state or imply a city, state, service, staff
   name, price or promotion that is NOT grounded in (a) the review text, or
   (b) the brand profile with the matching completeness flag set. If has_city
   is false, NO city name may appear. If has_state is false, no state. If
   has_services is false, do not name services beyond what the customer wrote.
7. Local SEO: use a keyword ONLY when the review context makes it relevant
   (the customer mentions the service). Never stuff keywords: at most 2 keyword
   hits total and never two keywords in one sentence. Do not repeat the
   business name or the city more than once. Do not claim Google ranking
   improvements.
8. Never reveal personal data (phone numbers, emails, addresses, medical info).
9. Be human and specific: reference at least one real detail from the review.
10. Do not mention any "do not mention" term.

LENGTH:
- Ordinary POSITIVE reviews (4-5 stars, not sensitive): 1-3 sentences, natural,
  no filler.
- NEGATIVE / sensitive reviews (1-2 stars or sensitive): up to 4 sentences,
  empathetic, never argumentative, never promising refunds or discounts.

OUTPUT: reply with ONE JSON object only, no prose, of the form
{"reply": "<the reply text>"}"""


_VI_HINTS = re.compile(
    r"[ăâđêôơưàáảãạằắẳẵặầấẩẫậèéẻẽẹềếểễệìíỉĩịòóỏõọồốổỗộờớởỡợùúủũụừứửữựỳýỷỹỵ]",
    re.IGNORECASE,
)


def detect_language(review: Any, brand: BrandProfile) -> str:
    """Best-effort language of the review, falling back to the brand's."""
    comment = ""
    if isinstance(review, dict):
        comment = str(review.get("comment") or "")
    else:
        comment = str(getattr(review, "comment", "") or _row_get(review, "comment") or "")
    if _VI_HINTS.search(comment):
        return "vi"
    return "en"


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except (TypeError, KeyError, IndexError):
        return getattr(row, key, default)


def extract_json(text: Optional[str]) -> Optional[dict]:
    """Tolerant JSON extraction from a model completion."""
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```\s*$", "", cleaned).strip()
    try:
        data = json.loads(cleaned)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(0))
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            return None
    return None


class ReplyGenerator:
    """Model-agnostic draft generator with exactly one repair attempt."""

    def __init__(
        self,
        model_client: ModelClient,
        brand_loader: BrandLoader,
        dup_checker: DuplicateChecker,
        validator: Optional[Callable[..., ValidationResult]] = None,
        max_repair: int = 1,
    ):
        self.model_client = model_client
        self.brand_loader = brand_loader
        self.dup_checker = dup_checker
        self.validator = validator or validate
        #: at most ONE controlled repair attempt (never an unbounded loop).
        self.max_repair = max(0, min(int(max_repair), 1))

    # ------------------------------------------------------------------ #
    # prompt
    # ------------------------------------------------------------------ #
    def build_prompt(
        self,
        review: Any,
        brand: BrandProfile,
        classification: Classification,
        recent_replies: Optional[Sequence[Any]] = None,
    ) -> Tuple[str, str]:
        brand = brand or BrandProfile.neutral()
        profile = brand.merge_for_prompt()
        review_comment = _row_get(review, "comment") or ""
        stars = classification.stars
        language = detect_language(review, brand)
        language_name = "Vietnamese" if language == "vi" else "English"

        # Grounding constraints derived from the completeness flags: the model
        # may only name a detail the profile actually verifies.
        constraints: List[str] = [f"Write the reply in {language_name}."]
        if profile["has_city"]:
            constraints.append(
                f"You may name the verified city '{profile['city']}' — at most once."
            )
        else:
            constraints.append("Do NOT mention any city or location name.")
        if profile["has_state"]:
            constraints.append(
                f"You may name the verified state '{profile['state']}' — at most once."
            )
        else:
            constraints.append("Do NOT mention any state or region.")
        if profile["has_services"]:
            constraints.append(
                "You may mention only these verified services: "
                + ", ".join(profile["services"])
                + "."
            )
        else:
            constraints.append("Do NOT name any service the customer did not mention.")
        if profile["has_seo_keywords"]:
            constraints.append(
                "Use at most one local-SEO keyword, and only when the review "
                "mentions the matching service."
            )
        else:
            constraints.append("Do NOT add any SEO keyword.")

        recent_texts = []
        for item in list(recent_replies or [])[:5]:
            text = item if isinstance(item, str) else _row_get(item, "reply_text") or _row_get(item, "draft_text") or ""
            text = str(text).strip()
            if text:
                recent_texts.append(text[:280])

        payload: Dict[str, Any] = {
            "review": {
                "stars": stars,
                "star_rating": _row_get(review, "starRating"),
                "comment": str(review_comment),
                "is_rating_only": classification.is_rating_only,
                "is_sensitive": classification.is_sensitive,
                "sensitive_signals": classification.signals,
            },
            "reply_language": language,
            "brand": {
                "business_name": profile["business_name"],
                "city": profile["city"],
                "state": profile["state"],
                "category": profile["category"],
                "verified_services": profile["services"],
                "tone": profile["tone"],
                "language": profile["language"],
                "allowed_seo_keywords": profile["seo_keywords"],
                "do_not_mention": profile["do_not_mention"],
                "profile_complete": profile["complete"],
                "has_city": profile["has_city"],
                "has_state": profile["has_state"],
                "has_services": profile["has_services"],
                "has_seo_keywords": profile["has_seo_keywords"],
            },
            "constraints": constraints,
            "must_not_use_as_previous_replies": recent_texts,
            "instructions": (
                "Write one reply draft. Reference a real detail from the review. "
                "Obey every constraint above and the system grounding rules. "
                "Return JSON only: {\"reply\": \"...\"}"
            ),
        }
        user = json.dumps(payload, ensure_ascii=False, indent=2)
        return SYSTEM_PROMPT, user

    def build_repair_prompt(
        self,
        draft: str,
        result: ValidationResult,
        review: Any,
        brand: BrandProfile,
        classification: Classification,
        recent_replies: Optional[Sequence[Any]] = None,
    ) -> Tuple[str, str]:
        system, user = self.build_prompt(review, brand, classification, recent_replies)
        feedback = {
            "previous_reply": draft,
            "problems": [f"{c.name}: {c.reason}" for c in result.checks if c.status != "pass"],
            "task": (
                "Rewrite the reply so every listed problem is fixed. Keep the same "
                "rules. Return JSON only: {\"reply\": \"...\"}"
            ),
        }
        user = user + "\n\nREPAIR REQUEST:\n" + json.dumps(feedback, ensure_ascii=False, indent=2)
        return system, user

    # ------------------------------------------------------------------ #
    # generation
    # ------------------------------------------------------------------ #
    def generate(
        self,
        review: Any,
        location_id: Any,
        recent_replies: Optional[Sequence[Any]] = None,
        brand_key: Any = None,
    ) -> Tuple[str, dict]:
        recent_replies = list(recent_replies or [])
        classification = classify(review)
        key = brand_key if brand_key is not None else location_id
        brand = self.brand_loader.get_for(key)

        meta: dict = {
            "location_id": location_id,
            "brand_key": key,
            "brand_complete": bool(brand.complete),
            "language": detect_language(review, brand),
            "stars": classification.stars,
            "is_sensitive": classification.is_sensitive,
            "signals": list(classification.signals),
            "is_rating_only": classification.is_rating_only,
            "requires_manual_approval": classification.requires_manual_approval,
            "attempts": 0,
            "model": type(self.model_client).__name__,
            "status": "ok",
            "error": None,
        }

        system, user = self.build_prompt(review, brand, classification, recent_replies)
        try:
            raw = self.model_client.complete(system, user)
        except TimeoutError as exc:
            meta.update(status="failed", error="timeout", message=str(exc))
            return "", meta
        except ApiError as exc:
            meta.update(status="failed", error="api_error", message=str(exc))
            return "", meta
        except Exception as exc:  # noqa: BLE001 - defensive: never crash the worker
            meta.update(status="failed", error="model_error", message=str(exc))
            return "", meta

        meta["attempts"] = 1
        parsed = extract_json(raw)
        if not parsed or not str(parsed.get("reply") or "").strip():
            meta.update(status="failed", error="invalid_json", raw=str(raw)[:500])
            return "", meta

        draft = str(parsed["reply"]).strip()
        result = self.validator(
            draft, review, brand, recent_replies, classification, self.dup_checker
        )
        meta["repair_used"] = False

        if not result.ok and self.max_repair > 0:
            meta["repair_used"] = True
            rsystem, ruser = self.build_repair_prompt(
                draft, result, review, brand, classification, recent_replies
            )
            try:
                raw2 = self.model_client.complete(rsystem, ruser)
                meta["attempts"] = 2
                parsed2 = extract_json(raw2)
                if parsed2 and str(parsed2.get("reply") or "").strip():
                    draft = str(parsed2["reply"]).strip()
                    result = self.validator(
                        draft, review, brand, recent_replies, classification, self.dup_checker
                    )
            except (TimeoutError, ApiError) as exc:
                meta["repair_error"] = f"{type(exc).__name__}: {exc}"

        dup = self.dup_checker.check(
            draft, recent_replies, location_id=location_id, brand=None
        )
        meta["duplicate"] = dup.to_dict()
        meta["validation"] = result.to_dict()
        meta["validation_reason"] = result.reason()
        meta["draft_text"] = draft
        meta["status"] = "ok" if (result.ok and dup.status != "fail") else "needs_review"
        return draft, meta
