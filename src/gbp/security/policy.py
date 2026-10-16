"""Password policy (Group 8B / T25).

Length-first and NIST-style: no arbitrary composition rules, but the obvious
weak choices are rejected -- too short, whitespace-padded, a single repeated
character, a straight keyboard/alphabet sequence, a name-derived password or an
entry from the small well-known-bad list below.

``WEAK_PASSWORDS`` is a *public denylist of famously bad passwords*, not a
credential or a secret. Error messages never contain the password.

Standard library only.
"""

from __future__ import annotations

import re
from typing import List, Optional

__all__ = [
    "MIN_PASSWORD_LENGTH",
    "MAX_PASSWORD_LENGTH",
    "WEAK_PASSWORDS",
    "PasswordPolicyError",
    "password_problems",
    "password_is_acceptable",
    "validate_password",
]

#: Minimum accepted password length (characters).
MIN_PASSWORD_LENGTH = 12

#: Hard upper bound so a pasted megablob cannot reach the KDF.
MAX_PASSWORD_LENGTH = 1024

#: Well-known bad passwords. Public data, not a credential.
WEAK_PASSWORDS = frozenset({
    "password", "password1", "password12", "password123", "password1234",
    "passw0rd", "p@ssw0rd", "p@ssword123", "changeme", "secret", "letmein",
    "welcome", "welcome1", "admin", "administrator", "root", "toor",
    "qwerty", "qwerty123", "qwertyuiop", "asdfghjkl", "iloveyou", "monkey",
    "dragon", "sunshine", "princess", "football", "baseball", "master",
    "trustno1", "abc123", "abcd1234", "111111", "11111111", "000000",
    "123456", "1234567", "12345678", "123456789", "1234567890", "12345678901",
    "test1234", "demo1234", "gbp123456", "nailsalon", "nail123456",
})

#: Straight sequences that must not appear as a run of >= 4 characters.
_SEQUENCES = ("0123456789", "abcdefghijklmnopqrstuvwxyz")


class PasswordPolicyError(ValueError):
    """Raised when a password fails the policy.

    The message lists the RULES that failed -- it never echoes the password.
    """


def _has_straight_run(low: str, width: int = 4) -> bool:
    for seq in _SEQUENCES:
        for base in (seq, seq[::-1]):
            for i in range(len(base) - width + 1):
                if base[i:i + width] in low:
                    return True
    return False


def _normalise_label(value: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def password_problems(
    password: object,
    *,
    username: Optional[str] = None,
    business_name: Optional[str] = None,
) -> List[str]:
    """Return the list of policy violations (empty list == acceptable).

    The password itself is never included in the returned strings.
    """
    if not isinstance(password, str) or password == "":
        return ["must be a non-empty string"]

    problems: List[str] = []
    if len(password) < MIN_PASSWORD_LENGTH:
        problems.append(
            f"must be at least {MIN_PASSWORD_LENGTH} characters long")
    if len(password) > MAX_PASSWORD_LENGTH:
        problems.append(
            f"must be at most {MAX_PASSWORD_LENGTH} characters long")
    if password != password.strip():
        problems.append("must not begin or end with whitespace")

    low = password.lower()
    if low in WEAK_PASSWORDS:
        problems.append("is a well-known weak password")
    if len(set(password)) == 1:
        problems.append("must not be a single repeated character")
    if _has_straight_run(low):
        problems.append(
            "must not contain a straight run like 'abcd' or '1234'")

    normalised = re.sub(r"[^a-z0-9]", "", low)
    for label in (username, business_name):
        token = _normalise_label(label)
        if len(token) >= 4 and token in normalised:
            problems.append("must not contain the account or shop name")
            break
    return problems


def password_is_acceptable(
    password: object,
    *,
    username: Optional[str] = None,
    business_name: Optional[str] = None,
) -> bool:
    """Convenience boolean wrapper around :func:`password_problems`."""
    return not password_problems(
        password, username=username, business_name=business_name)


def validate_password(
    password: object,
    *,
    username: Optional[str] = None,
    business_name: Optional[str] = None,
) -> None:
    """Raise :class:`PasswordPolicyError` when ``password`` is unacceptable."""
    problems = password_problems(
        password, username=username, business_name=business_name)
    if problems:
        raise PasswordPolicyError("; ".join(problems))
