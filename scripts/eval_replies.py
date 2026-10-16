#!/usr/bin/env python3
"""Real-model sample evaluation for the AI review-reply engine (Group 3).

Runs the REAL pipeline (classify -> brand -> generate via
``OpenRouterModelClient`` -> validate -> duplicate check) over exactly TEN
hand-picked fixture reviews, then writes:

* ``docs/SAMPLES.md``  -- human-readable, one section per case (reply verbatim)
* ``docs/samples.json`` -- machine-readable results

COST CONTROL: exactly 10 reviews are generated. The only retry allowed is the
generator's single built-in repair attempt; a failed call is recorded, never
looped. Standard library only.

Usage:
    cd /opt/data/gbp-integration && PYTHONPATH=src python3 scripts/eval_replies.py
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Optional

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(_ROOT, "src"))

from gbp.brand import BrandLoader  # noqa: E402
from gbp.reply.classify import classify  # noqa: E402
from gbp.reply.duplicate import (  # noqa: E402
    DuplicateChecker,
    jaccard,
    ngrams,
    normalize_tokens,
)
from gbp.reply.generator import ReplyGenerator  # noqa: E402
from gbp.reply.model_client import (  # noqa: E402
    DEFAULT_MODEL,
    ApiError,
    OpenRouterModelClient,
)

FIXTURES = os.path.join(_ROOT, "fixtures")
DOCS = os.path.join(_ROOT, "docs")
BRAND_FIXTURE = os.path.join(FIXTURES, "brand_profiles.json")

LOC1 = "accounts/1001/locations/loc-001"
LOC2 = "accounts/1001/locations/loc-002"
LOC5 = "accounts/1001/locations/loc-005"


# --------------------------------------------------------------------------- #
# counting model client (delegates to the real OpenRouter client)
# --------------------------------------------------------------------------- #
class CountingModelClient:
    """Wrap a model client and count how many ``complete`` calls it makes."""

    def __init__(self, inner: OpenRouterModelClient):
        self.inner = inner
        self.calls = 0

    def complete(self, system: str, user: str, timeout: float = 30.0) -> str:
        self.calls += 1
        return self.inner.complete(system, user, timeout)


# --------------------------------------------------------------------------- #
# the ten hand-picked cases (review dicts mirror the fixture rows)
# --------------------------------------------------------------------------- #
CASES: List[Dict[str, Any]] = [
    {
        "id": "case-01",
        "label": "5-star EN with text + service mentioned",
        "location_id": LOC1,
        "fixture": "reviews_page1.json",
        "reviewId": "r-1011",
        "starRating": "FIVE",
        "comment": "Loved the nail art design, I will definitely come back.",
    },
    {
        "id": "case-02",
        "label": "5-star Vietnamese (loc-005)",
        "location_id": LOC5,
        "fixture": "reviews_loc005_vi_page1.json",
        "reviewId": "r-5001",
        "starRating": "FIVE",
        "comment": (
            "Tiệm làm móng rất đẹp, nhân viên dễ thương và chu đáo. "
            "Tôi rất hài lòng và sẽ quay lại."
        ),
    },
    {
        "id": "case-03",
        "label": "4-star with mild complaint",
        "location_id": LOC1,
        "fixture": "reviews_page1.json",
        "reviewId": "r-1003",
        "starRating": "FOUR",
        "comment": "Great job on my pedicure, though I waited about 20 minutes.",
    },
    {
        "id": "case-04",
        "label": "3-star",
        "location_id": LOC1,
        "fixture": "reviews_page1.json",
        "reviewId": "r-1007",
        "starRating": "THREE",
        "comment": "It was okay, nothing special. The wait was fine but the finish chipped fast.",
    },
    {
        "id": "case-05",
        "label": "2-star downgrade case",
        "location_id": LOC1,
        "fixture": "reviews_page2.json",
        "reviewId": "r-1011",
        "starRating": "TWO",
        "comment": (
            "Loved the nail art design, I will definitely come back. "
            "(downgraded: design chipped within two days)"
        ),
    },
    {
        "id": "case-06",
        "label": "1-star negative",
        "location_id": LOC1,
        "fixture": "reviews_page2.json",
        "reviewId": "r-1004",
        "starRating": "ONE",
        "comment": "Terrible experience. They chipped my nails the same day and the staff was rude.",
    },
    {
        "id": "case-07",
        "label": "rating-only (no comment)",
        "location_id": LOC1,
        "fixture": "reviews_page1.json",
        "reviewId": "r-1002",
        "starRating": "FIVE",
        "comment": None,
    },
    {
        "id": "case-08",
        "label": "review naming a staff member",
        "location_id": LOC1,
        "fixture": "reviews_page2.json",
        "reviewId": "r-1006",
        "starRating": "FIVE",
        "comment": "Anna is the sweetest — she did an amazing job and my nails look flawless.",
    },
    {
        "id": "case-09",
        "label": "review of a DIFFERENT shop (loc-002, brand isolation)",
        "location_id": LOC2,
        "fixture": "reviews_loc002_page1.json",
        "reviewId": "r-2001",
        "starRating": "FIVE",
        "comment": "Wonderful acrylics, very neat work at the San Jose shop.",
    },
    {
        "id": "case-10",
        "label": "sensitive review (health)",
        "location_id": LOC1,
        "fixture": "reviews_sensitive.json",
        "reviewId": "r-3001",
        "starRating": "ONE",
        "sensitiveCategory": "health_safety",
        "comment": (
            "I got a serious infection after my pedicure here, my foot was bleeding. "
            "They clearly do not sterilize their tools."
        ),
    },
]


def expected_status(classification) -> str:
    """The status the engine would assign: needs_review on manual approval."""
    return "needs_review" if classification.requires_manual_approval else "draft"


def actual_status(meta: dict) -> str:
    """Map the generator's meta onto the engine's draft/needs_review.

    Mirrors ``ReviewReplyEngine.process``: a review only becomes a normal
    ``draft`` when generation succeeded AND it does not require manual
    approval; 1-2 star, sensitive and low-star rating-only reviews stay
    ``needs_review`` even when the validator passes.
    """
    if meta.get("status") == "ok" and not meta.get("requires_manual_approval"):
        return "draft"
    return "needs_review"


def max_jaccard_vs(prior_texts: List[str], draft: str) -> float:
    """Max 4-gram Jaccard similarity of ``draft`` against same-location priors."""
    if not draft:
        return 0.0
    grams = ngrams(normalize_tokens(draft))
    best = 0.0
    for text in prior_texts:
        score = jaccard(grams, ngrams(normalize_tokens(text)))
        if score > best:
            best = score
    return best


def snippet(comment: Optional[str], limit: int = 120) -> str:
    text = (comment or "").strip() or "(rating only, no comment)"
    return text if len(text) <= limit else text[:limit] + "…"


def main() -> int:
    if not os.environ.get("OPENROUTER_API_KEY"):
        print("FATAL: OPENROUTER_API_KEY is not set in the environment.", file=sys.stderr)
        return 2

    loader = BrandLoader.from_file(BRAND_FIXTURE)
    dup_checker = DuplicateChecker()
    counter = CountingModelClient(OpenRouterModelClient())
    generator = ReplyGenerator(counter, loader, dup_checker)

    #: location_id -> reply texts already produced this run (scope: same shop).
    produced: Dict[str, List[str]] = {}
    results: List[Dict[str, Any]] = []

    print(f"Model: {DEFAULT_MODEL}  |  cases: {len(CASES)}")
    print("-" * 72)

    for case in CASES:
        location_id = case["location_id"]
        review = {
            "reviewId": case["reviewId"],
            "location_id": location_id,
            "starRating": case["starRating"],
            "comment": case["comment"],
        }
        if case.get("sensitiveCategory"):
            review["sensitiveCategory"] = case["sensitiveCategory"]

        classification = classify(review)
        expected = expected_status(classification)
        prior = list(produced.get(location_id, []))

        draft, meta = generator.generate(
            review, location_id, recent_replies=prior, brand_key=location_id
        )

        status = actual_status(meta)
        validation = meta.get("validation") or {}
        similarity = max_jaccard_vs(prior, draft)

        record = {
            "id": case["id"],
            "label": case["label"],
            "fixture": case["fixture"],
            "reviewId": case["reviewId"],
            "location_id": location_id,
            "brand_key": meta.get("brand_key"),
            "brand_complete": meta.get("brand_complete"),
            "stars": meta.get("stars"),
            "language": meta.get("language"),
            "is_sensitive": meta.get("is_sensitive"),
            "signals": meta.get("signals"),
            "is_rating_only": meta.get("is_rating_only"),
            "requires_manual_approval": meta.get("requires_manual_approval"),
            "review_snippet": snippet(case["comment"]),
            "expected_status": expected,
            "actual_status": status,
            "generation_status": meta.get("status"),
            "error": meta.get("error"),
            "message": meta.get("message"),
            "attempts": meta.get("attempts"),
            "repair_used": bool(meta.get("repair_used")),
            "reply": draft,
            "validation": validation,
            "validation_reason": meta.get("validation_reason"),
            "duplicate": meta.get("duplicate"),
            "max_jaccard": round(similarity, 4),
            "status_match": status == expected,
        }
        results.append(record)

        if draft:
            produced.setdefault(location_id, []).append(draft)

        print(
            f"{case['id']}  {status:12s} (exp {expected:12s}) "
            f"val_ok={validation.get('ok')} repair={record['repair_used']} "
            f"jac={similarity:.3f}  | {case['label']}"
        )

    write_json(results)
    write_markdown(results)
    print_summary(results, counter.calls)
    return 0


# --------------------------------------------------------------------------- #
# outputs
# --------------------------------------------------------------------------- #
def write_json(results: List[Dict[str, Any]]) -> str:
    os.makedirs(DOCS, exist_ok=True)
    path = os.path.join(DOCS, "samples.json")
    payload = {
        "model": DEFAULT_MODEL,
        "cases": len(results),
        "results": results,
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return path


def _checks_line(validation: dict) -> str:
    checks = (validation or {}).get("checks") or []
    parts = [f"{c['name']}={c['status']}" for c in checks]
    return ", ".join(parts) if parts else "(none)"


def write_markdown(results: List[Dict[str, Any]]) -> str:
    os.makedirs(DOCS, exist_ok=True)
    path = os.path.join(DOCS, "SAMPLES.md")
    lines: List[str] = []
    lines.append("# SAMPLES — real-model reply evaluation (Group 3)")
    lines.append("")
    lines.append(
        "Generated by `scripts/eval_replies.py` using the REAL OpenRouter model "
        f"`{DEFAULT_MODEL}` over 10 fixture reviews. Every reply below is model "
        "output (verbatim), persisted as a **draft** only — never published."
    )
    lines.append("")
    lines.append(
        "Status mapping: generator `ok` -> `draft`, anything else -> `needs_review` "
        "(identical to `ReviewReplyEngine.process`)."
    )
    lines.append("")
    lines.append("| # | Case | Stars | Brand complete | Expected | Actual | Val ok | Repair | Max Jaccard |")
    lines.append("|---|------|-------|----------------|----------|--------|--------|--------|-------------|")
    for r in results:
        lines.append(
            "| {id} | {label} | {stars} | {bc} | {exp} | {act} | {vok} | {rep} | {jac:.3f} |".format(
                id=r["id"],
                label=r["label"],
                stars=r["stars"],
                bc=r["brand_complete"],
                exp=r["expected_status"],
                act=r["actual_status"],
                vok=(r["validation"] or {}).get("ok"),
                rep=r["repair_used"],
                jac=r["max_jaccard"],
            )
        )
    lines.append("")

    for r in results:
        lines.append(f"## {r['id']} — {r['label']}")
        lines.append("")
        lines.append(f"- Fixture: `{r['fixture']}` (reviewId `{r['reviewId']}`)")
        lines.append(f"- Location: `{r['location_id']}` (brand_key `{r['brand_key']}`, complete={r['brand_complete']})")
        lines.append(f"- Stars: {r['stars']}  |  Language: {r['language']}  |  Sensitive: {r['is_sensitive']} {r['signals']}")
        lines.append(f"- Rating-only: {r['is_rating_only']}  |  Requires manual approval: {r['requires_manual_approval']}")
        lines.append(f"- Review: {r['review_snippet']}")
        lines.append(f"- Expected status: **{r['expected_status']}**  |  Actual status: **{r['actual_status']}**  (match={r['status_match']})")
        lines.append(f"- Generation: status={r['generation_status']}, attempts={r['attempts']}, repair_used={r['repair_used']}, error={r['error']}")
        lines.append(f"- Validator: ok={ (r['validation'] or {}).get('ok') }  ({_checks_line(r['validation'])})")
        if r["validation_reason"] and r["validation_reason"] != "ok":
            lines.append(f"- Validator notes: {r['validation_reason']}")
        lines.append(f"- Duplicate check: {r['duplicate']}")
        lines.append(f"- Max Jaccard vs same-location replies this run: {r['max_jaccard']:.4f}")
        lines.append("")
        lines.append("Reply (verbatim):")
        lines.append("")
        lines.append("> " + (r["reply"].replace("\n", "\n> ") if r["reply"] else "(empty — generation failed)"))
        lines.append("")

    lines.append("## Summary")
    lines.append("")
    n_generated = sum(1 for r in results if r["reply"])
    n_needs = sum(1 for r in results if r["actual_status"] == "needs_review")
    n_val_ok = sum(1 for r in results if (r["validation"] or {}).get("ok"))
    n_repair = sum(1 for r in results if r["repair_used"])
    jacs = [r["max_jaccard"] for r in results if r["reply"]]
    mean_jac = sum(jacs) / len(jacs) if jacs else 0.0
    max_jac = max(jacs) if jacs else 0.0
    lines.append(f"- Generated replies: {n_generated}")
    lines.append(f"- Needs review: {n_needs}")
    lines.append(f"- Validation pass: {n_val_ok}/{len(results)}")
    lines.append(f"- Repair attempts used: {n_repair}")
    lines.append(f"- Mean Jaccard: {mean_jac:.4f}  |  Max Jaccard: {max_jac:.4f}")
    lines.append("")

    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
        handle.write("\n")
    return path


def print_summary(results: List[Dict[str, Any]], model_calls: int) -> None:
    n_generated = sum(1 for r in results if r["reply"])
    n_needs = sum(1 for r in results if r["actual_status"] == "needs_review")
    n_val_ok = sum(1 for r in results if (r["validation"] or {}).get("ok"))
    n_repair = sum(1 for r in results if r["repair_used"])
    n_failed = sum(1 for r in results if not r["reply"])
    jacs = [r["max_jaccard"] for r in results if r["reply"]]
    mean_jac = sum(jacs) / len(jacs) if jacs else 0.0
    max_jac = max(jacs) if jacs else 0.0

    print("-" * 72)
    print("SUMMARY")
    print(f"  cases                : {len(results)}")
    print(f"  generated            : {n_generated}")
    print(f"  generation failed    : {n_failed}")
    print(f"  needs_review         : {n_needs}")
    print(f"  validation pass      : {n_val_ok}/{len(results)}")
    print(f"  repair attempts used : {n_repair}")
    print(f"  mean Jaccard         : {mean_jac:.4f}")
    print(f"  max Jaccard          : {max_jac:.4f}")
    print(f"  total model calls    : {model_calls}")


if __name__ == "__main__":
    raise SystemExit(main())
