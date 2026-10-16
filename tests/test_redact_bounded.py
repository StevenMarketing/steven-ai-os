"""Regression tests: redaction must be BOUNDED (Group 8A / T24a).

Bug being pinned
----------------
The Cookie/Set-Cookie scrubber captured ``[^\\r\\n]+`` -- the whole rest of the
line -- so legitimate text after a masked value was destroyed:

    redact('prefix cookie=abc123 suffix KEEPME')
      before -> 'prefix cookie=[REDACTED]'      # ' suffix KEEPME' lost
      after  -> 'prefix cookie=[REDACTED] suffix KEEPME'

The masker must still remove the secret itself (no leak), but must preserve all
non-sensitive text BEFORE and AFTER the masked value, on the same line, in the
middle of a line, and with several secrets per line.
"""

import unittest

from gbp.security.redact import REDACTED, redact, redact_headers, redact_text


class RedactBoundedTests(unittest.TestCase):
    # --- helpers ---------------------------------------------------------
    def assertBounded(self, raw: str, *, secrets: list[str], kept: list[str]):
        out = redact_text(raw)
        for s in secrets:
            self.assertNotIn(s, out, f"secret {s!r} leaked in: {out!r}")
            self.assertIn(REDACTED, out, f"nothing masked in: {out!r}")
        for k in kept:
            self.assertIn(k, out, f"non-sensitive text {k!r} was destroyed in: {out!r}")
        return out

    # --- position matrix -------------------------------------------------
    def test_secret_at_start_of_string(self):
        out = self.assertBounded(
            "password=hunter2 and the rest stays",
            secrets=["hunter2"],
            kept=["and the rest stays"],
        )
        self.assertTrue(out.startswith("password=" + REDACTED), out)

    def test_secret_in_middle_of_string(self):
        out = self.assertBounded(
            "before password=hunter2 after KEEPME",
            secrets=["hunter2"],
            kept=["before ", "after KEEPME"],
        )
        self.assertTrue(out.startswith("before password="), out)

    def test_secret_at_end_of_string(self):
        out = self.assertBounded(
            "keep this: token=abc123",
            secrets=["abc123"],
            kept=["keep this: "],
        )
        self.assertTrue(out.endswith(REDACTED), out)

    def test_multiple_secrets_on_one_line(self):
        self.assertBounded(
            "a=1 password=AAA b=2 token=BBB c=3 KEEPME",
            secrets=["AAA", "BBB"],
            kept=["a=1", "b=2", "c=3", "KEEPME"],
        )

    def test_line_without_secret_is_unchanged(self):
        raw = "no sensitive material here, nothing to mask, keep KEEPME"
        self.assertEqual(redact_text(raw), raw)

    def test_secret_with_special_characters(self):
        self.assertBounded(
            'password="p@ss w0rd!$&;," tail KEEPME',
            secrets=["p@ss w0rd!$&;,"],
            kept=["tail KEEPME"],
        )

    # --- the exact reported bug ------------------------------------------
    def test_cookie_value_masking_is_bounded_regression(self):
        out = redact_text("prefix cookie=abc123 suffix KEEPME")
        self.assertNotIn("abc123", out)
        self.assertIn(REDACTED, out)
        self.assertIn("prefix ", out)
        self.assertIn(" suffix KEEPME", out)
        self.assertEqual(out, "prefix cookie=" + REDACTED + " suffix KEEPME")

    def test_cookie_header_keeps_names_attributes_and_trailing_text(self):
        out = redact_text("Cookie: gbp_session=xyz; HttpOnly; Path=/ trailing prose")
        self.assertNotIn("xyz", out)
        self.assertIn("gbp_session=" + REDACTED, out)
        self.assertIn("HttpOnly", out)          # attribute name survives
        self.assertIn("trailing prose", out)    # text after the header survives

    def test_set_cookie_header_is_masked_and_bounded(self):
        out = redact_text(
            "Set-Cookie: gbp_session=xyz; HttpOnly; SameSite=Lax; Max-Age=3600 ENDMARK"
        )
        self.assertNotIn("xyz", out)
        self.assertIn("gbp_session=" + REDACTED, out)
        self.assertIn("SameSite", out)
        self.assertIn("ENDMARK", out)

    # --- other header/token forms ----------------------------------------
    def test_authorization_header_keeps_trailing_text(self):
        out = redact_text(
            "Authorization: Bearer sk-or-v1-ABCDEF123456 trailing KEEPME"
        )
        self.assertNotIn("sk-or-v1-ABCDEF123456", out)
        self.assertIn("Authorization:", out)
        self.assertIn("trailing KEEPME", out)

    def test_bare_bearer_token_is_masked_and_bounded(self):
        out = redact_text("sent Bearer ABCDEF123456 then KEEPME")
        self.assertNotIn("ABCDEF123456", out)
        self.assertIn("then KEEPME", out)

    def test_provider_token_masked_and_trailing_text_kept(self):
        out = redact_text("key sk-or-v1-ABCDEF123456 end KEEPME")
        self.assertNotIn("sk-or-v1-ABCDEF123456", out)
        self.assertIn("end KEEPME", out)

    def test_embedded_session_cookie_keeps_trailing_text(self):
        out = redact_text("x gbp_session=SECRETVALUE; Path=/ KEEPME")
        self.assertNotIn("SECRETVALUE", out)
        self.assertIn("KEEPME", out)

    # --- mapping / headers ----------------------------------------------
    def test_mapping_masks_sensitive_keys_only(self):
        out = redact(
            {
                "user": "bob",
                "password": "hunter2",
                "nested": {"csrf_token": "abc", "note": "hello KEEPME"},
            }
        )
        self.assertEqual(out["user"], "bob")
        self.assertEqual(out["password"], REDACTED)
        self.assertEqual(out["nested"]["csrf_token"], REDACTED)
        self.assertIn("hello KEEPME", out["nested"]["note"])

    def test_redact_headers_masks_only_sensitive_headers(self):
        out = redact_headers(
            {
                "Cookie": "gbp_session=xyz",
                "Authorization": "Bearer sk-or-v1-ABCDEF123456",
                "X-Actor": "manager",
            }
        )
        self.assertEqual(out["Cookie"], REDACTED)
        self.assertEqual(out["Authorization"], REDACTED)
        self.assertEqual(out["X-Actor"], "manager")

    # --- nothing leaks, nothing over-eats -------------------------------
    def test_no_secret_survives_and_every_tail_survives(self):
        samples = [
            "password=AAA tail1",
            "prefix token=BBB suffix2",
            "cookie=CCC middle3 ENDMARK3",
            "Set-Cookie: gbp_session=DDD; HttpOnly ENDMARK4",
            "Authorization: Bearer sk-or-v1-EEE ENDMARK5",
        ]
        secrets = ["AAA", "BBB", "CCC", "DDD", "sk-or-v1-EEE"]
        tails = ["tail1", "suffix2", "ENDMARK3", "ENDMARK4", "ENDMARK5"]
        for raw, sec, tail in zip(samples, secrets, tails):
            out = redact_text(raw)
            self.assertNotIn(sec, out, out)
            self.assertIn(tail, out, out)


if __name__ == "__main__":
    unittest.main()
