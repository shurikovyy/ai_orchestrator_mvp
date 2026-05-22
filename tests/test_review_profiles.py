from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import json
import sys
import unittest
from typing import get_args

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_orchestrator.cli import list_review_profiles_main, show_review_profile_main
from ai_orchestrator.deterministic_review import DeterministicReviewCheckResult
from ai_orchestrator.review_profiles import (
    BUILTIN_REVIEW_PROFILES,
    ReviewProfile,
    format_review_profiles_text,
    is_known_review_profile,
    list_review_profiles,
)
from ai_orchestrator.schemas import ReviewFinding


def output_value(output: str, key: str) -> str:
    prefix = f"{key}="
    for line in output.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    raise AssertionError(f"missing output line for {key!r} in:\n{output}")


class ReviewProfilesTests(unittest.TestCase):
    def test_builtin_registry_contains_expected_profiles(self) -> None:
        profile_ids = {profile.id for profile in BUILTIN_REVIEW_PROFILES}
        self.assertEqual(
            profile_ids,
            {"deterministic", "qa", "architecture", "ops", "security", "business", "data"},
        )

    def test_profile_ids_are_unique(self) -> None:
        profile_ids = [profile.id for profile in BUILTIN_REVIEW_PROFILES]
        self.assertEqual(len(profile_ids), len(set(profile_ids)))

    def test_all_profiles_validate(self) -> None:
        for profile in BUILTIN_REVIEW_PROFILES:
            self.assertIsInstance(profile, ReviewProfile)

    def test_all_profile_categories_are_valid_review_finding_categories(self) -> None:
        valid_categories = set(get_args(ReviewFinding.model_fields["category"].annotation))
        for profile in BUILTIN_REVIEW_PROFILES:
            self.assertTrue(set(profile.finding_categories).issubset(valid_categories))

    def test_deterministic_profile_reviewer_type_is_deterministic(self) -> None:
        deterministic = next(profile for profile in BUILTIN_REVIEW_PROFILES if profile.id == "deterministic")
        self.assertEqual(deterministic.reviewer_type, "deterministic")

    def test_non_deterministic_profiles_are_contracts_not_active_agents(self) -> None:
        for profile in BUILTIN_REVIEW_PROFILES:
            if profile.id == "deterministic":
                continue
            self.assertIn(profile.reviewer_type, {"llm_future", "human"})

    def test_list_review_profiles_text_output_includes_profiles_total_and_known_ids(self) -> None:
        stdout = StringIO()
        with redirect_stdout(stdout):
            exit_code = list_review_profiles_main([])
        output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "profiles_total"), "7")
        self.assertIn("profile_id=qa", output)
        self.assertIn("profile_id=security", output)

    def test_list_review_profiles_json_output_returns_valid_json(self) -> None:
        stdout = StringIO()
        with redirect_stdout(stdout):
            exit_code = list_review_profiles_main(["--format", "json"])
        payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["profiles_total"], 7)
        self.assertTrue(any(profile["id"] == "qa" for profile in payload["profiles"]))

    def test_show_review_profile_text_output_includes_focus_areas_and_categories(self) -> None:
        stdout = StringIO()
        with redirect_stdout(stdout):
            exit_code = show_review_profile_main(["qa"])
        output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "profile_id"), "qa")
        self.assertEqual(output_value(output, "reviewer_type"), "llm_future")
        self.assertIn("categories=qa,correctness,maintainability", output)
        self.assertIn("focus_area=", output)

    def test_show_review_profile_json_returns_full_profile(self) -> None:
        stdout = StringIO()
        with redirect_stdout(stdout):
            exit_code = show_review_profile_main(["security", "--format", "json"])
        payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["id"], "security")
        self.assertIn("focus_areas", payload)
        self.assertIn("output_contract", payload)
        self.assertIn("prompt_template", payload)

    def test_show_review_profile_missing_id_returns_nonzero_and_clear_error(self) -> None:
        stdout = StringIO()
        with redirect_stdout(stdout):
            exit_code = show_review_profile_main(["missing-profile"])
        output = stdout.getvalue()

        self.assertEqual(exit_code, 1, output)
        self.assertIn("status=failed", output)
        self.assertIn("review profile not found: missing-profile", output)

    def test_deterministic_review_reviewer_id_matches_known_profile(self) -> None:
        finding = DeterministicReviewCheckResult(
            category="qa",
            severity="major",
            title="Missing regression test",
            evidence="Observed gap.",
            required_action="Add test.",
        )
        self.assertTrue(is_known_review_profile(finding.reviewer))
        self.assertEqual(finding.reviewer, "deterministic")

    def test_format_review_profiles_text_is_stable(self) -> None:
        output = format_review_profiles_text(list_review_profiles())
        self.assertTrue(output.startswith("profiles_total=7"))
        self.assertIn('title="QA Reviewer"', output)

