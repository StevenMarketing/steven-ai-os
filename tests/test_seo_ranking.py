"""Issue #90 -- offline ranking MEASUREMENT FRAMEWORK on simulated data only.

Standard library only, SIMULATED data only: no network, no rank checking, no
scraping, no Local Rank Grid, no live Google call, no persistence. Every
position used by these tests comes from the in-module seeded generator. Run
with:

    cd /opt/data/gbp-integration && PYTHONPATH=src python3 -m unittest tests.test_seo_ranking
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import socket
import sys
import unittest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(_ROOT, "src"))

from gbp.seo import ranking as ranking_mod  # noqa: E402
from gbp.seo.ranking import (  # noqa: E402
    DEFAULT_KEYWORDS,
    DEFAULT_LOCATIONS,
    DEFAULT_SEED,
    DEFAULT_WEEKS,
    MAX_POSITION,
    SIMULATED,
    SIMULATED_LABEL,
    VISIBILITY_INDEX_LABEL,
    LocationPeriodSummary,
    RankObservation,
    RankSeries,
    RankingReport,
    average_position,
    best_position,
    coverage,
    iso_week_range,
    next_week,
    parse_week,
    simulate_dataset,
    simulate_keyword_series,
    summarize_location,
    summarize_series,
    top_share,
    visibility_by_keyword,
    visibility_index,
    week_label,
    week_over_week,
    worst_position,
)

_LOC_1 = DEFAULT_LOCATIONS[0]
_LOC_2 = DEFAULT_LOCATIONS[1]
_FIXTURE = simulate_dataset()


def _series(location_id: str, keyword: str, positions) -> tuple:
    """Build observations from a list of positions, using the fixture weeks."""
    return tuple(
        RankObservation(location_id=location_id, keyword=keyword,
                        week=week, position=position)
        for week, position in zip(DEFAULT_WEEKS, positions)
    )


class ObservationModelTests(unittest.TestCase):
    def test_valid_observation_serializes_the_simulated_marker(self):
        obs = RankObservation(location_id=_LOC_1, keyword="gel nails",
                              week="2026-W10", position=5)
        payload = obs.as_dict()
        self.assertTrue(payload["simulated"])
        self.assertEqual(payload["source"], SIMULATED)
        self.assertEqual(obs.source, SIMULATED)
        self.assertTrue(payload["in_top_20"])
        self.assertEqual(payload["position"], 5)
        self.assertEqual(json.loads(obs.to_json()), payload)

    def test_position_outside_the_accepted_range_is_rejected(self):
        for bad in (0, -1, MAX_POSITION + 1, 100):
            with self.subTest(position=bad):
                with self.assertRaises(ValueError) as ctx:
                    RankObservation(_LOC_1, "gel nails", "2026-W10", bad)
                self.assertIn(str(bad), str(ctx.exception))

    def test_position_must_be_an_int_not_a_bool_or_string(self):
        for bad in (True, False, "3", 3.0, 3.5):
            with self.subTest(position=bad):
                with self.assertRaises(ValueError):
                    RankObservation(_LOC_1, "gel nails", "2026-W10", bad)

    def test_none_position_means_outside_the_top_20(self):
        obs = RankObservation(_LOC_1, "gel nails", "2026-W10", None)
        self.assertIsNone(obs.position)
        self.assertFalse(obs.in_top_20)
        self.assertFalse(obs.as_dict()["in_top_20"])

    def test_every_non_simulated_source_is_refused_with_a_clear_error(self):
        refused = ("scraped", "local-rank-grid", "grid", "live", "", "unknown",
                   "SIMULATED", " simulated ", None, 0)
        for source in refused:
            with self.subTest(source=source):
                with self.assertRaises(ValueError) as ctx:
                    RankObservation(_LOC_1, "gel nails", "2026-W10", 5,
                                    source=source)  # type: ignore[arg-type]
                message = str(ctx.exception)
                self.assertIn("refused source", message)
                self.assertIn(repr(source), message)
                self.assertIn(SIMULATED, message)

    def test_blank_location_and_keyword_are_rejected(self):
        for kwargs in ({"location_id": ""}, {"location_id": "   "},
                       {"location_id": None}, {"keyword": ""},
                       {"keyword": "  "}, {"keyword": None}):
            with self.subTest(**kwargs):
                args = {"location_id": _LOC_1, "keyword": "gel nails",
                        "week": "2026-W10", "position": 5}
                args.update(kwargs)
                with self.assertRaises(ValueError):
                    RankObservation(**args)  # type: ignore[arg-type]

    def test_malformed_week_labels_are_rejected(self):
        for bad in ("2026-10", "2026-W1", "2026-W54", "2026-W00", "W10-2026",
                    "2026-W10-01", 20261, None, ""):
            with self.subTest(week=bad):
                with self.assertRaises(ValueError):
                    RankObservation(_LOC_1, "gel nails", bad, 5)  # type: ignore[arg-type]

    def test_location_and_keyword_whitespace_is_trimmed(self):
        obs = RankObservation(f"  {_LOC_1}  ", "  gel nails  ", "2026-W10", 5)
        self.assertEqual(obs.location_id, _LOC_1)
        self.assertEqual(obs.keyword, "gel nails")

    def test_observation_is_frozen(self):
        obs = RankObservation(_LOC_1, "gel nails", "2026-W10", 5)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            obs.position = 6  # type: ignore[misc]


class SeriesTests(unittest.TestCase):
    def test_duplicate_cell_is_rejected(self):
        series = RankSeries([RankObservation(_LOC_1, "gel nails",
                                             "2026-W10", 5)])
        with self.assertRaises(ValueError) as ctx:
            series.add(RankObservation(_LOC_1, "gel nails", "2026-W10", 9))
        self.assertIn("duplicate (location_id, keyword, week)", str(ctx.exception))
        self.assertEqual(len(series), 1)

    def test_the_same_week_for_another_keyword_or_location_is_not_a_duplicate(self):
        series = RankSeries([
            RankObservation(_LOC_1, "gel nails", "2026-W10", 5),
            RankObservation(_LOC_1, "nail salon", "2026-W10", 5),
            RankObservation(_LOC_2, "gel nails", "2026-W10", 5),
        ])
        self.assertEqual(len(series), 3)

    def test_non_observation_values_are_rejected(self):
        with self.assertRaises(ValueError):
            RankSeries([{"location_id": _LOC_1}])  # type: ignore[list-item]

    def test_observations_are_sorted_and_selectable(self):
        series = RankSeries([
            RankObservation(_LOC_2, "gel nails", "2026-W11", 5),
            RankObservation(_LOC_1, "gel nails", "2026-W10", 5),
        ])
        self.assertEqual([obs.key for obs in series.observations],
                         [(_LOC_1, "gel nails", "2026-W10"),
                          (_LOC_2, "gel nails", "2026-W11")])
        self.assertEqual(series.locations(), (_LOC_1, _LOC_2))
        self.assertEqual(series.keywords(), ("gel nails",))
        self.assertEqual(series.weeks(), ("2026-W10", "2026-W11"))
        self.assertEqual(len(series.select(location_id=_LOC_1)), 1)
        self.assertEqual(len(series.select(keyword="nail salon")), 0)


class WeekLabelTests(unittest.TestCase):
    def test_week_label_and_parse_round_trip(self):
        self.assertEqual(week_label(2026, 10), "2026-W10")
        self.assertEqual(week_label(2026, 3), "2026-W03")
        self.assertEqual(parse_week("2026-W03"), (2026, 3))

    def test_week_label_rejects_bad_numbers(self):
        for year, week in ((2026, 0), (2026, 54), (2026, -1), ("2026", 1),
                           (2026, True)):
            with self.subTest(year=year, week=week):
                with self.assertRaises(ValueError):
                    week_label(year, week)  # type: ignore[arg-type]

    def test_next_week_crosses_the_year_boundary(self):
        self.assertEqual(next_week("2020-W53"), "2021-W01")
        self.assertEqual(next_week("2026-W52"), "2026-W53")
        self.assertEqual(next_week("2026-W53"), "2027-W01")

    def test_iso_week_range_is_consecutive_and_counted(self):
        self.assertEqual(iso_week_range("2026-W52", 3),
                         ("2026-W52", "2026-W53", "2027-W01"))
        self.assertEqual(len(iso_week_range("2026-W10", 8)), 8)
        self.assertEqual(iso_week_range("2026-W10", 0), ())
        self.assertEqual(DEFAULT_WEEKS, iso_week_range("2026-W10", 8))

    # -- issue #92, defect 2: ISO week validation was too lax --------------
    def test_parse_week_accepts_ordinary_weeks_and_real_53_week_years(self):
        self.assertEqual(parse_week("2026-W10"), (2026, 10))
        self.assertEqual(parse_week("2026-W01"), (2026, 1))
        # 2020 and 2026 really do have 53 ISO weeks
        self.assertEqual(parse_week("2020-W53"), (2020, 53))
        self.assertEqual(parse_week("2026-W53"), (2026, 53))

    def test_parse_week_rejects_a_non_consecutive_calendar_week(self):
        # 2025 has 52 ISO weeks, so 2025-W53 does not exist
        for bad in ("2025-W53", "2021-W53", "0000-W01", "2026-W00",
                    "2026-W54"):
            with self.subTest(week=bad):
                with self.assertRaises(ValueError) as ctx:
                    parse_week(bad)
                self.assertIn("ISO week", str(ctx.exception))

    def test_parse_week_rejects_a_trailing_newline_and_junk(self):
        for bad in ("2026-W40\n", "2026-W40\r\n", "2026-W40 ", "\n2026-W40",
                    "2026-W40-", "x2026-W40", "2026-W40x"):
            with self.subTest(week=bad):
                with self.assertRaises(ValueError):
                    parse_week(bad)

    def test_parse_week_rejects_non_ascii_digits(self):
        # Arabic-Indic, Devanagari, full-width and superscript digits all look
        # numeric to a human and to ``\d`` -- none of them is an ISO week.
        for bad in ("\u0662\u0660\u0662\u0666-W40",   # ٢٠٢٦
                    "\u0662026-W40",                   # one Arabic-Indic 2
                    "2026-W\u0664\u0660",              # Arabic-Indic 40
                    "\uff12\uff10\uff12\uff16-W40",    # full-width 2026
                    "2026-W\u00b2\u2070"):             # superscript
            with self.subTest(week=bad):
                with self.assertRaises(ValueError):
                    parse_week(bad)

    def test_the_label_paths_agree_that_week_53_needs_a_53_week_year(self):
        # ``week_label`` cannot mint a label ``parse_week`` would refuse.
        self.assertEqual(week_label(2020, 53), "2020-W53")
        self.assertEqual(week_label(2026, 53), "2026-W53")
        with self.assertRaises(ValueError):
            week_label(2025, 53)
        with self.assertRaises(ValueError):
            week_label(0, 1)
        for label in ("2025-W53", "0000-W01", "2026-W40\n"):
            with self.assertRaises(ValueError):
                RankObservation(_LOC_1, "gel nails", label, 5)
            with self.assertRaises(ValueError):
                iso_week_range(label, 1)

    def test_a_53_week_year_is_not_skipped_over(self):
        # 2026 has 53 ISO weeks, so 2026-W52 -> 2027-W01 is NOT consecutive.
        self.assertEqual(next_week("2026-W52"), "2026-W53")
        self.assertEqual(next_week("2026-W53"), "2027-W01")
        self.assertNotEqual(next_week("2026-W52"), "2027-W01")
        # 2020 has 53 weeks and 2025 has 52: the boundary differs by real year.
        self.assertEqual(next_week("2020-W53"), "2021-W01")
        self.assertEqual(next_week("2025-W52"), "2026-W01")


class GeneratorTests(unittest.TestCase):
    def test_same_seed_gives_byte_identical_series(self):
        first = simulate_dataset(DEFAULT_SEED)
        second = simulate_dataset(DEFAULT_SEED)
        self.assertEqual(first.to_json(), second.to_json())
        self.assertEqual(hashlib.sha256(first.to_json().encode()).hexdigest(),
                         hashlib.sha256(second.to_json().encode()).hexdigest())

    def test_the_deterministic_mixer_is_frozen_by_constants(self):
        # Proof that the stream does not depend on ``random``'s implementation:
        # these values come from this module's own SplitMix64 constants.
        self.assertEqual(
            ranking_mod._seed_int(90, "a", "b"), 1980356466062855818)
        rng = ranking_mod._DeterministicRng(
            ranking_mod._seed_int(90, _LOC_1, "gel nails"))
        self.assertEqual([rng.randbelow(10) for _ in range(5)],
                         [4, 7, 9, 2, 2])

    def test_a_different_seed_gives_a_different_series(self):
        self.assertNotEqual(simulate_dataset(DEFAULT_SEED).to_json(),
                            simulate_dataset(DEFAULT_SEED + 1).to_json())

    def test_locations_and_keywords_order_does_not_change_the_dataset(self):
        forward = simulate_dataset(7, DEFAULT_LOCATIONS, DEFAULT_KEYWORDS,
                                   DEFAULT_WEEKS)
        backward = simulate_dataset(7, tuple(reversed(DEFAULT_LOCATIONS)),
                                    tuple(reversed(DEFAULT_KEYWORDS)),
                                    DEFAULT_WEEKS)
        self.assertEqual(forward.to_json(), backward.to_json())

    def test_the_week_sequence_is_the_time_axis(self):
        # reversing the weeks reverses the trajectory: both are reproducible,
        # they are simply different (correctly attributed) series.
        forward = simulate_dataset(7, DEFAULT_LOCATIONS, DEFAULT_KEYWORDS,
                                   DEFAULT_WEEKS)
        reversed_weeks = simulate_dataset(
            7, DEFAULT_LOCATIONS, DEFAULT_KEYWORDS,
            tuple(reversed(DEFAULT_WEEKS)))
        self.assertNotEqual(forward.to_json(), reversed_weeks.to_json())
        self.assertEqual(reversed_weeks.weeks(), DEFAULT_WEEKS)

    def test_one_keyword_series_does_not_depend_on_its_neighbours(self):
        alone = simulate_keyword_series(_LOC_1, "gel nails", DEFAULT_WEEKS, 90)
        self.assertEqual(alone, _FIXTURE.select(location_id=_LOC_1,
                                                keyword="gel nails"))

    def test_fixture_contains_inside_and_outside_top_20_weeks(self):
        positions = [obs.position for obs in _FIXTURE]
        self.assertIn(None, positions)
        self.assertTrue(any(p is not None for p in positions))
        loc2 = [obs.position for obs in _FIXTURE.select(location_id=_LOC_2)]
        self.assertIn(None, loc2)
        self.assertTrue(any(p is not None for p in loc2))

    def test_generated_positions_are_within_bounds_or_none(self):
        self.assertEqual(len(_FIXTURE),
                         len(DEFAULT_LOCATIONS) * len(DEFAULT_KEYWORDS)
                         * len(DEFAULT_WEEKS))
        for obs in _FIXTURE:
            self.assertIn(obs.source, (SIMULATED,))
            if obs.position is not None:
                self.assertTrue(1 <= obs.position <= MAX_POSITION, obs)

    def test_repeated_week_labels_are_rejected(self):
        with self.assertRaises(ValueError):
            simulate_keyword_series(_LOC_1, "gel nails",
                                    ["2026-W10", "2026-W10"], 90)

    def test_a_bad_week_in_the_input_is_rejected(self):
        for bad in ("2026-W99", 10, None):
            with self.subTest(week=bad):
                with self.assertRaises(ValueError):
                    simulate_keyword_series(_LOC_1, "gel nails", [bad], 90)  # type: ignore[list-item]

    def test_an_empty_week_list_returns_nothing(self):
        self.assertEqual(simulate_keyword_series(_LOC_1, "gel nails", [], 90),
                         ())

    def test_generator_rejects_blank_identifiers(self):
        with self.assertRaises(ValueError):
            simulate_keyword_series("", "gel nails", DEFAULT_WEEKS, 90)
        with self.assertRaises(ValueError):
            simulate_keyword_series(_LOC_1, "  ", DEFAULT_WEEKS, 90)


class MetricTests(unittest.TestCase):
    """Hand-computed against the seeded fixture printed in the PR evidence."""

    def setUp(self):
        self.loc1 = _FIXTURE.select(location_id=_LOC_1)
        self.loc2 = _FIXTURE.select(location_id=_LOC_2)

    def test_loc_001_metrics_match_the_hand_computation(self):
        # loc-001 positions (gel nails / nail salon / pedicure near me):
        #   5 5 5 7 7 7 8 9 | 11 10 8 11 11 10 9 10 | 5 4 3 2 2 2 4 6
        # sum 161 over 24 observations -> 161/24 = 6.7083
        # top-3: 2 2 2 3 -> 4/24 = 0.1667 ; top-10: 21/24 = 0.875
        self.assertEqual(len(self.loc1), 24)
        self.assertEqual(coverage(self.loc1), 1.0)
        self.assertEqual(average_position(self.loc1), 6.708)
        self.assertEqual(best_position(self.loc1), 2)
        self.assertEqual(worst_position(self.loc1), 11)
        self.assertEqual(top_share(self.loc1, 3), 0.167)
        self.assertEqual(top_share(self.loc1, 10), 0.875)
        self.assertEqual(visibility_index(self.loc1), 0.7146)

    def test_loc_002_metrics_match_the_hand_computation(self):
        # loc-002 has two None weeks (gel nails W16, W17):
        #   5 6 13 15 14 15 None None | 6 9 7 6 7 11 12 14 |
        #   7 8 9 10 10 8 7 9
        # 22 non-null of 24 -> coverage 0.9167 ; sum 208/22 = 9.4545
        # top-3: none -> 0.0 ; top-10: 15/24 = 0.625
        self.assertEqual(len(self.loc2), 24)
        self.assertEqual(coverage(self.loc2), 0.917)
        self.assertEqual(average_position(self.loc2), 9.455)
        self.assertEqual(best_position(self.loc2), 5)
        self.assertEqual(worst_position(self.loc2), 15)
        self.assertEqual(top_share(self.loc2, 3), 0.0)
        self.assertEqual(top_share(self.loc2, 10), 0.625)
        self.assertEqual(visibility_index(self.loc2), 0.5292)

    def test_visibility_by_keyword_matches_the_hand_computation(self):
        rows = {row["keyword"]: row for row in visibility_by_keyword(self.loc1)}
        self.assertEqual(set(rows), set(DEFAULT_KEYWORDS))
        # gel nails: (16*3 + 14*3 + 13 + 12) / (20*8) = 115/160
        self.assertEqual(rows["gel nails"]["score"], 0.7188)
        # nail salon: (10+11+13+10+10+11+12+11) / 160 = 88/160
        self.assertEqual(rows["nail salon"]["score"], 0.55)
        # pedicure near me: (16+17+18+19+19+19+17+15) / 160 = 140/160
        self.assertEqual(rows["pedicure near me"]["score"], 0.875)
        for row in rows.values():
            self.assertEqual(row["observations"], len(DEFAULT_WEEKS))
            self.assertEqual(row["in_top_20"], len(DEFAULT_WEEKS))

    def test_visibility_index_is_the_mean_over_keywords(self):
        rows = visibility_by_keyword(self.loc1)
        expected = round(sum(row["score"] for row in rows) / len(rows), 4)
        self.assertEqual(visibility_index(self.loc1), expected)
        # every keyword counts once, whatever its number of weeks
        uneven = self.loc1 + (RankObservation(_LOC_1, "gel nails",
                                              "2026-W18", 1),)
        rows_after = visibility_by_keyword(uneven)
        self.assertEqual(len(rows_after), 3)
        self.assertEqual(
            visibility_index(uneven),
            round(sum(row["score"] for row in rows_after) / 3, 4))

    def test_week_over_week_deltas_match_the_hand_computation(self):
        # weekly averages: 7.0 6.333 5.333 6.667 6.667 6.333 7.0 8.333
        rows = week_over_week(self.loc1)
        self.assertEqual(len(rows), len(DEFAULT_WEEKS) - 1)
        self.assertEqual([row["delta"] for row in rows],
                         [-0.667, -1.0, 1.334, 0.0, -0.334, 0.667, 1.333])
        self.assertEqual([row["direction"] for row in rows],
                         ["improved", "improved", "worsened", "flat",
                          "improved", "worsened", "worsened"])
        self.assertEqual(rows[0]["from_week"], "2026-W10")
        self.assertEqual(rows[0]["to_week"], "2026-W11")
        self.assertEqual(rows[0]["from_average_position"], 7.0)
        self.assertEqual(rows[0]["to_average_position"], 6.333)

    def test_week_over_week_sign_convention(self):
        better = _series(_LOC_1, "kw", [8, 6, None, None, None, None, None, None])
        self.assertEqual(week_over_week(better)[0]["delta"], -2.0)
        self.assertEqual(week_over_week(better)[0]["direction"], "improved")
        worse = _series(_LOC_1, "kw", [6, 8, None, None, None, None, None, None])
        self.assertEqual(week_over_week(worse)[0]["delta"], 2.0)
        self.assertEqual(week_over_week(worse)[0]["direction"], "worsened")

    def test_week_over_week_keeps_a_null_week_instead_of_dropping_it(self):
        obs = (RankObservation(_LOC_1, "kw", "2026-W01", 5),
               RankObservation(_LOC_1, "kw", "2026-W02", None),
               RankObservation(_LOC_1, "kw", "2026-W03", 7))
        rows = week_over_week(obs)
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertIsNone(row["delta"])
            self.assertEqual(row["direction"], "unknown")
        self.assertIsNone(rows[0]["to_average_position"])
        self.assertEqual(rows[0]["from_average_position"], 5.0)
        self.assertIsNone(rows[1]["from_average_position"])
        self.assertEqual(rows[1]["to_average_position"], 7.0)

    # -- issue #92, defect 1: a gap is not a week-over-week change ---------
    def test_week_over_week_marks_a_missing_week_as_a_gap(self):
        # the supervisor's proof case: W10 (avg 4) and W12 (avg 6) are TWO
        # weeks apart, so the +2.0 must not be reported as a weekly change.
        obs = (RankObservation(_LOC_1, "kw", "2026-W10", 4),
               RankObservation(_LOC_1, "kw", "2026-W12", 6))
        rows = week_over_week(obs)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["from_week"], "2026-W10")
        self.assertEqual(row["to_week"], "2026-W12")
        self.assertIsNone(row["delta"])
        self.assertEqual(row["direction"], "gap")
        self.assertNotIn("worsened", row["direction"])
        # the two averages are still shown, so the gap is visible, not hidden
        self.assertEqual(row["from_average_position"], 4.0)
        self.assertEqual(row["to_average_position"], 6.0)

    def test_week_over_week_gap_is_not_confused_with_a_null_week(self):
        obs = (RankObservation(_LOC_1, "kw", "2026-W01", 5),
               RankObservation(_LOC_1, "kw", "2026-W02", None),
               RankObservation(_LOC_1, "kw", "2026-W04", 6))
        rows = week_over_week(obs)
        self.assertEqual([row["direction"] for row in rows],
                         ["unknown", "gap"])
        self.assertTrue(all(row["delta"] is None for row in rows))

    def test_week_over_week_deltas_survive_a_year_boundary(self):
        # consecutive across a real year boundary -> delta present
        boundary = (RankObservation(_LOC_1, "kw", "2020-W53", 4),
                    RankObservation(_LOC_1, "kw", "2021-W01", 6))
        row = week_over_week(boundary)[0]
        self.assertEqual(row["delta"], 2.0)
        self.assertEqual(row["direction"], "worsened")
        # 2025 has 52 ISO weeks, so 2025-W52 -> 2026-W01 is consecutive too
        short = (RankObservation(_LOC_1, "kw", "2025-W52", 8),
                 RankObservation(_LOC_1, "kw", "2026-W01", 5))
        row = week_over_week(short)[0]
        self.assertEqual(row["delta"], -3.0)
        self.assertEqual(row["direction"], "improved")

    def test_week_over_week_does_not_jump_a_53_week_year(self):
        # 2026 has 53 ISO weeks: W52 -> 2027-W01 skips 2026-W53, so it is a gap
        obs = (RankObservation(_LOC_1, "kw", "2026-W52", 4),
               RankObservation(_LOC_1, "kw", "2027-W01", 9))
        row = week_over_week(obs)[0]
        self.assertIsNone(row["delta"])
        self.assertEqual(row["direction"], "gap")
        # ... while W52 -> W53 -> 2027-W01 is two consecutive deltas
        full = (RankObservation(_LOC_1, "kw", "2026-W52", 4),
                RankObservation(_LOC_1, "kw", "2026-W53", 7),
                RankObservation(_LOC_1, "kw", "2027-W01", 9))
        rows = week_over_week(full)
        self.assertEqual([row["delta"] for row in rows], [3.0, 2.0])

    def test_the_fixture_has_no_gaps_because_its_weeks_are_consecutive(self):
        for obs_list in (_FIXTURE.select(location_id=_LOC_1),
                         _FIXTURE.select(location_id=_LOC_2)):
            for row in week_over_week(obs_list):
                self.assertNotEqual(row["direction"], "gap")

    def test_null_weeks_stay_in_the_top_share_denominator(self):
        obs = (RankObservation(_LOC_1, "kw", "2026-W01", 1),
               RankObservation(_LOC_1, "kw", "2026-W02", None),
               RankObservation(_LOC_1, "kw", "2026-W03", None),
               RankObservation(_LOC_1, "kw", "2026-W04", None))
        # one qualifying week out of all four observations, not one out of one
        self.assertEqual(top_share(obs, 3), 0.25)
        self.assertEqual(top_share(obs, 10), 0.25)
        self.assertEqual(coverage(obs), 0.25)

    def test_average_position_ignores_null_weeks_but_shares_do_not(self):
        obs = (RankObservation(_LOC_1, "kw", "2026-W01", 5),
               RankObservation(_LOC_1, "kw", "2026-W02", None),
               RankObservation(_LOC_1, "kw", "2026-W03", 7))
        self.assertEqual(average_position(obs), 6.0)
        self.assertEqual(coverage(obs), 0.667)
        self.assertEqual(top_share(obs, 10), 0.667)

    def test_empty_period_metrics_are_explicit(self):
        self.assertEqual(coverage(()), 0.0)
        self.assertIsNone(average_position(()))
        self.assertIsNone(best_position(()))
        self.assertIsNone(worst_position(()))
        self.assertEqual(top_share((), 3), 0.0)
        self.assertEqual(top_share((), 10), 0.0)
        self.assertEqual(week_over_week(()), ())
        self.assertEqual(visibility_by_keyword(()), ())
        self.assertIsNone(visibility_index(()))

    def test_top_share_rejects_a_bad_limit(self):
        for bad in (0, -3, 1.5, "3", True):
            with self.subTest(limit=bad):
                with self.assertRaises(ValueError):
                    top_share(self.loc1, bad)  # type: ignore[arg-type]

    def test_metrics_reject_non_observation_input(self):
        with self.assertRaises(ValueError):
            coverage([{"position": 1}])  # type: ignore[list-item]
        with self.assertRaises(ValueError):
            visibility_index([None])  # type: ignore[list-item]


class DuplicateCellTests(unittest.TestCase):
    """Issue #92, defect 3: duplicates are refused centrally, by EVERY entry.

    ``(location_id, keyword, week)`` is the key; two readings of one cell in one
    ISO week cannot both be true, so no public entry point may silently average
    them. ``RankSeries`` already refused them -- these tests pin that the plain
    metric functions and the summary builders refuse them too, for a list, a
    tuple and a generator alike.
    """

    def setUp(self):
        self.duplicates = (RankObservation(_LOC_1, "kw", "2026-W10", 1),
                           RankObservation(_LOC_1, "kw", "2026-W10", 20))

    @staticmethod
    def _expected_average_if_averaged():
        # what the old code returned: (1 + 20) / 2 -- the distortion to prevent
        return 10.5

    def test_the_duplicate_really_would_change_the_numbers(self):
        # guard against a vacuous test: if these two rows were merged into one
        # observation the metrics would silently differ from the honest ones.
        self.assertEqual((1 + 20) / 2, self._expected_average_if_averaged())
        self.assertEqual(average_position(
            (RankObservation(_LOC_1, "kw", "2026-W10", 1),)), 1.0)

    def test_duplicate_rank_1_and_rank_20_raise_instead_of_averaging(self):
        with self.assertRaises(ValueError) as ctx:
            average_position(self.duplicates)
        message = str(ctx.exception)
        self.assertIn("duplicate (location_id, keyword, week)", message)
        self.assertIn("2026-W10", message)
        self.assertNotIn(str(self._expected_average_if_averaged()), message)

    def test_every_metric_entry_point_rejects_a_duplicate_list(self):
        calls = {
            "coverage": lambda: coverage(self.duplicates),
            "average_position": lambda: average_position(self.duplicates),
            "best_position": lambda: best_position(self.duplicates),
            "worst_position": lambda: worst_position(self.duplicates),
            "top_share": lambda: top_share(self.duplicates, 10),
            "week_over_week": lambda: week_over_week(self.duplicates),
            "visibility_by_keyword": lambda: visibility_by_keyword(
                self.duplicates),
            "visibility_index": lambda: visibility_index(self.duplicates),
            "summarize_location": lambda: summarize_location(
                _LOC_1, self.duplicates),
        }
        for name, call in calls.items():
            with self.subTest(entry_point=name):
                with self.assertRaises(ValueError) as ctx:
                    call()
                self.assertIn("duplicate (location_id, keyword, week)",
                              str(ctx.exception))

    def test_a_duplicate_exposed_only_once_through_a_generator_still_raises(
            self):
        # a generator is the nastiest input: it can be consumed only once and
        # hides its length, so the check cannot be a "len(set(...))" on a list
        # the caller already materialized.
        def feed():
            for observation in self.duplicates:
                yield observation

        with self.assertRaises(ValueError) as ctx:
            average_position(feed())
        self.assertIn("duplicate (location_id, keyword, week)",
                      str(ctx.exception))

        def feed_again():
            for observation in self.duplicates:
                yield observation

        with self.assertRaises(ValueError) as ctx:
            summarize_location(_LOC_1, feed_again())
        self.assertIn("duplicate (location_id, keyword, week)",
                      str(ctx.exception))

    def test_a_duplicate_tuple_and_a_duplicate_series_member_are_refused(self):
        with self.assertRaises(ValueError):
            coverage(tuple(self.duplicates))
        # a RankSeries cannot hold a duplicate at all
        with self.assertRaises(ValueError):
            RankSeries(self.duplicates)
        # ... and a *valid* series passed to a metric stays accepted
        series = RankSeries([RankObservation(_LOC_1, "kw", "2026-W10", 3)])
        self.assertEqual(average_position(series), 3.0)

    def test_the_same_week_for_a_different_keyword_or_location_is_allowed(self):
        distinct = (RankObservation(_LOC_1, "kw", "2026-W10", 6),
                    RankObservation(_LOC_1, "other kw", "2026-W10", 8),
                    RankObservation(_LOC_2, "kw", "2026-W10", 10))
        self.assertEqual(average_position(distinct), 8.0)
        self.assertEqual(len(summarize_location(
            _LOC_1, distinct[:2]).visibility_by_keyword), 2)

    def test_the_fixture_dataset_has_no_duplicates_and_still_summarizes(self):
        self.assertEqual(len(_FIXTURE), len(_FIXTURE.observations))
        summary = summarize_location(_LOC_1, _FIXTURE.select(location_id=_LOC_1))
        self.assertEqual(summary.observation_count, 24)


class SummaryTests(unittest.TestCase):
    def test_the_summary_requires_an_explicit_simulated_flag(self):
        # omitting ``simulated`` is a TypeError: the field has no default
        with self.assertRaises(TypeError):
            LocationPeriodSummary(  # type: ignore[call-arg]
                location_id=_LOC_1, weeks=DEFAULT_WEEKS, keyword_count=3,
                observation_count=24, coverage=1.0, average_position=6.708,
                best_position=2, worst_position=11, top_3_share=0.167,
                top_10_share=0.875, visibility_index=0.7146)
        # passing anything but True is refused outright
        for flag in (False, None, 0, "", 1):
            with self.subTest(simulated=flag):
                with self.assertRaises(ValueError):
                    LocationPeriodSummary(  # type: ignore[arg-type]
                        simulated=flag, location_id=_LOC_1, weeks=DEFAULT_WEEKS,
                        keyword_count=3, observation_count=24, coverage=1.0,
                        average_position=6.708, best_position=2,
                        worst_position=11, top_3_share=0.167,
                        top_10_share=0.875, visibility_index=0.7146)

    def test_a_report_cannot_be_built_without_the_simulated_flag(self):
        summary = summarize_location(
            _LOC_1, _FIXTURE.select(location_id=_LOC_1))
        with self.assertRaises(TypeError):
            RankingReport(seed=90, weeks=DEFAULT_WEEKS,  # type: ignore[call-arg]
                          summaries=(summary,))
        with self.assertRaises(ValueError):
            RankingReport(simulated=False, seed=90, weeks=DEFAULT_WEEKS,
                          summaries=(summary,))

    def test_a_report_refuses_a_summary_that_lost_the_marker(self):
        summary = summarize_location(
            _LOC_1, _FIXTURE.select(location_id=_LOC_1))
        forged = dataclasses.replace(summary)
        object.__setattr__(forged, "simulated", False)
        with self.assertRaises(ValueError):
            RankingReport(simulated=True, seed=90, weeks=DEFAULT_WEEKS,
                          summaries=(forged,))

    def test_the_builders_always_set_the_marker(self):
        summary = summarize_location(
            _LOC_1, _FIXTURE.select(location_id=_LOC_1))
        self.assertIs(summary.simulated, True)
        self.assertIs(summary.as_dict()["simulated"], True)
        report = summarize_series(_FIXTURE)
        self.assertIs(report.simulated, True)
        self.assertTrue(all(item.simulated is True
                            for item in report.summaries))

    def test_summary_metrics_agree_with_the_metric_functions(self):
        items = _FIXTURE.select(location_id=_LOC_2)
        summary = summarize_location(_LOC_2, items)
        self.assertEqual(summary.coverage, coverage(items))
        self.assertEqual(summary.average_position, average_position(items))
        self.assertEqual(summary.best_position, best_position(items))
        self.assertEqual(summary.worst_position, worst_position(items))
        self.assertEqual(summary.top_3_share, top_share(items, 3))
        self.assertEqual(summary.top_10_share, top_share(items, 10))
        self.assertEqual(summary.visibility_index, visibility_index(items))
        self.assertEqual(summary.week_over_week, week_over_week(items))
        self.assertEqual(summary.location_id, _LOC_2)
        self.assertEqual(summary.observation_count, len(items))
        self.assertEqual(summary.keyword_count, len(DEFAULT_KEYWORDS))
        self.assertEqual(summary.weeks, DEFAULT_WEEKS)

    def test_summary_rejects_observations_from_another_location(self):
        with self.assertRaises(ValueError) as ctx:
            summarize_location(_LOC_1, _FIXTURE)
        self.assertIn("one summary is one location", str(ctx.exception))

    def test_an_explicit_period_is_normalized_and_ordered(self):
        items = _FIXTURE.select(location_id=_LOC_1)
        summary = summarize_location(_LOC_1, items,
                                     ["2026-W10", "2026-W11"])
        self.assertEqual(summary.weeks, ("2026-W10", "2026-W11"))
        self.assertEqual(len(summary.weeks), 2)
        self.assertEqual(len(summary.week_over_week), 1)
        # the period bounds the metrics: 2 weeks x 3 keywords
        self.assertEqual(summary.observation_count, 6)
        self.assertEqual(summary.keyword_count, 3)
        self.assertEqual(len(summary.visibility_by_keyword), 3)
        with self.assertRaises(ValueError):
            summarize_location(_LOC_1, items, ["2026-W10", "2026-W10"])

    def test_a_summary_reports_a_week_gap_instead_of_a_weekly_change(self):
        obs = (RankObservation(_LOC_1, "kw", "2026-W10", 4),
               RankObservation(_LOC_1, "kw", "2026-W12", 6))
        summary = summarize_location(_LOC_1, obs, ["2026-W10", "2026-W12"])
        self.assertEqual(summary.weeks, ("2026-W10", "2026-W12"))
        self.assertEqual(len(summary.week_over_week), 1)
        row = summary.week_over_week[0]
        self.assertIsNone(row["delta"])
        self.assertEqual(row["direction"], "gap")
        # the metrics of the two-week period itself stay honest
        self.assertEqual(summary.average_position, 5.0)
        self.assertEqual(summary.coverage, 1.0)
        self.assertEqual(summary.observation_count, 2)

    def test_a_summary_rejects_a_duplicate_cell_through_any_iterable(self):
        duplicates = [RankObservation(_LOC_1, "kw", "2026-W10", 1),
                      RankObservation(_LOC_1, "kw", "2026-W10", 20)]
        for source in (duplicates, tuple(duplicates),
                       (obs for obs in duplicates)):
            with self.subTest(kind=type(source).__name__):
                with self.assertRaises(ValueError) as ctx:
                    summarize_location(_LOC_1, source)
                self.assertIn("duplicate (location_id, keyword, week)",
                              str(ctx.exception))

    def test_summary_serialization_is_deterministic(self):
        items = _FIXTURE.select(location_id=_LOC_1)
        first = summarize_location(_LOC_1, items).to_json()
        second = summarize_location(_LOC_1, items).to_json()
        self.assertEqual(first, second)
        self.assertEqual(hashlib.sha256(first.encode()).hexdigest(),
                         hashlib.sha256(second.encode()).hexdigest())

    def test_summary_carries_the_simulated_marker_end_to_end(self):
        payload = summarize_location(
            _LOC_1, _FIXTURE.select(location_id=_LOC_1)).as_dict()
        self.assertIs(payload["simulated"], True)
        self.assertEqual(payload["label"], SIMULATED_LABEL)
        self.assertEqual(payload["label_line"],
                         f"{SIMULATED_LABEL} DATA -- {payload['source_label']}")
        self.assertIn("no live Google connectivity", payload["disclaimer"])
        self.assertIn("no Local Rank Grid", payload["disclaimer"])
        self.assertEqual(payload["visibility_index_label"],
                         VISIBILITY_INDEX_LABEL)

    def test_the_visibility_index_label_never_claims_a_google_metric(self):
        summary = summarize_location(
            _LOC_1, _FIXTURE.select(location_id=_LOC_1)).as_dict()
        label = summary["visibility_index_label"]
        self.assertIn("internal visibility index", label)
        self.assertIn("not a Google metric", label)
        self.assertEqual(summary["metrics"]["visibility_index"],
                         visibility_index(_FIXTURE.select(location_id=_LOC_1)))

    def test_the_report_covers_every_location_deterministically(self):
        first = summarize_series(_FIXTURE)
        second = summarize_series(_FIXTURE)
        self.assertEqual(first.to_json(), second.to_json())
        self.assertEqual(first.location_ids, DEFAULT_LOCATIONS)
        self.assertEqual(len(first.summaries), len(DEFAULT_LOCATIONS))
        payload = first.as_dict()
        self.assertIs(payload["simulated"], True)
        self.assertEqual(payload["mode"], ranking_mod.MODE)
        self.assertEqual(payload["seed"], DEFAULT_SEED)
        self.assertEqual(payload["week_count"], len(DEFAULT_WEEKS))
        self.assertEqual(payload["location_count"], len(DEFAULT_LOCATIONS))
        self.assertIn("metric_definitions", payload)
        for summary in payload["summaries"]:
            self.assertIs(summary["simulated"], True)
            self.assertEqual(summary["label"], SIMULATED_LABEL)

    def test_summarize_series_rejects_a_plain_iterable(self):
        with self.assertRaises(ValueError):
            summarize_series(list(_FIXTURE))  # type: ignore[arg-type]

    def test_metric_definitions_document_every_metric(self):
        definitions = ranking_mod.METRIC_DEFINITIONS
        for name in ("coverage", "average_position", "best_position",
                     "worst_position", "top_3_share", "top_10_share",
                     "week_over_week", "visibility_index"):
            self.assertIn(name, definitions)
            self.assertTrue(definitions[name].strip())
        self.assertIn("not a Google metric", definitions["visibility_index"])


class OfflineGuardTests(unittest.TestCase):
    def test_no_network_is_attempted(self):
        original_socket = socket.socket
        original_create = socket.create_connection

        def _boom(*args, **kwargs):
            raise AssertionError("the ranking framework must never touch a socket")

        socket.socket = _boom  # type: ignore[assignment]
        socket.create_connection = _boom  # type: ignore[assignment]
        try:
            series = simulate_dataset()
            report = summarize_series(series)
            self.assertEqual(len(report.summaries), len(DEFAULT_LOCATIONS))
        finally:
            socket.socket = original_socket
            socket.create_connection = original_create

    def test_module_source_has_no_network_scraping_or_random_imports(self):
        with open(os.path.join(_ROOT, "src", "gbp", "seo", "ranking.py"),
                  "r", encoding="utf-8") as handle:
            source = handle.read()
        for forbidden in ("import socket", "import urllib", "import requests",
                          "import http", "http.client", "import ssl",
                          "googleapiclient", "import subprocess", "import os",
                          "import random", "selenium", "bs4", "curl",
                          "rank_checker", "local_rank_grid"):
            self.assertNotIn(forbidden, source)

    def test_the_module_exposes_no_local_rank_grid_surface(self):
        names = [name for name in dir(ranking_mod) if not name.startswith("__")]
        for name in names:
            self.assertNotIn("grid", name.lower(), name)
            self.assertNotIn("scrape", name.lower(), name)
            self.assertNotIn("fetch", name.lower(), name)


if __name__ == "__main__":
    unittest.main()
