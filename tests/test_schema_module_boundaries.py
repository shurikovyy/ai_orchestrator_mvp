from __future__ import annotations

import unittest


class SchemaModuleBoundaryTests(unittest.TestCase):
    def test_compatibility_imports_still_work(self) -> None:
        from ai_orchestrator.schemas import (
            ReviewArbitrationReport as CompatArbitrationReport,
            ReviewFinding as CompatReviewFinding,
            RiskClassification as CompatRiskClassification,
            RunState,
        )
        from ai_orchestrator.review_arbitration_schemas import ReviewArbitrationReport
        from ai_orchestrator.review_findings_schemas import ReviewFinding
        from ai_orchestrator.risk_schemas import RiskClassification

        self.assertIs(CompatReviewFinding, ReviewFinding)
        self.assertIs(CompatArbitrationReport, ReviewArbitrationReport)
        self.assertIs(CompatRiskClassification, RiskClassification)
        self.assertEqual(RunState.__name__, "RunState")

    def test_review_finding_json_round_trip_is_unchanged(self) -> None:
        from ai_orchestrator.schemas import ReviewFinding as CompatReviewFinding
        from ai_orchestrator.review_findings_schemas import ReviewFinding

        payload = {
            "id": "F001",
            "reviewer": "qa",
            "category": "qa",
            "severity": "major",
            "title": "Missing regression test",
            "evidence": "Source behavior changed without a regression test.",
            "required_action": "Add regression coverage for the changed behavior.",
            "file": "tests/test_example.py",
            "line": 12,
            "status": "open",
        }

        compat_model = CompatReviewFinding.model_validate(payload)
        domain_model = ReviewFinding.model_validate_json(compat_model.model_dump_json())

        self.assertEqual(compat_model.model_dump(mode="json"), domain_model.model_dump(mode="json"))
        self.assertTrue(domain_model.blocking)

    def test_review_findings_report_json_round_trip_is_unchanged(self) -> None:
        from ai_orchestrator.review_findings_schemas import ReviewFindingsReport

        payload = {
            "schema_version": "1.0",
            "run_id": "run_schema_boundary",
            "summary": "QA found a blocking test gap.",
            "overall_decision": "needs_rework",
            "source_profile": "qa",
            "source_kind": "reviewer_profile",
            "findings": [
                {
                    "id": "F001",
                    "reviewer": "qa",
                    "category": "qa",
                    "severity": "major",
                    "title": "Missing regression test",
                    "evidence": "Source behavior changed without a regression test.",
                    "required_action": "Add regression coverage for the changed behavior.",
                    "file": "tests/test_example.py",
                    "status": "open",
                }
            ],
        }

        report = ReviewFindingsReport.model_validate(payload)
        round_tripped = ReviewFindingsReport.model_validate_json(report.model_dump_json())

        self.assertEqual(report.model_dump(mode="json"), round_tripped.model_dump(mode="json"))
        self.assertEqual(round_tripped.counts.blocking_open, 1)

    def test_review_arbitration_report_json_round_trip_is_unchanged(self) -> None:
        from ai_orchestrator.schemas import ReviewArbitrationReport as CompatReviewArbitrationReport
        from ai_orchestrator.review_arbitration_schemas import ReviewArbitrationReport

        payload = {
            "schema_version": "1.0",
            "run_id": "run_schema_boundary",
            "source_findings_path": ".runs/run_schema_boundary/REVIEW_FINDINGS.json",
            "source_findings_sha256": "a" * 64,
            "arbiter": "manual",
            "summary": "Finding downgraded after evidence review.",
            "overall_decision": "pass",
            "arbitrated_findings": [
                {
                    "finding_id": "F001",
                    "source_reviewer": "qa",
                    "original_severity": "major",
                    "final_severity": "minor",
                    "original_blocking": True,
                    "final_blocking": False,
                    "status": "downgraded",
                    "reason": "Evidence supports a non-blocking follow-up rather than rework.",
                }
            ],
        }

        compat_report = CompatReviewArbitrationReport.model_validate(payload)
        domain_report = ReviewArbitrationReport.model_validate_json(compat_report.model_dump_json())

        self.assertEqual(compat_report.model_dump(mode="json"), domain_report.model_dump(mode="json"))
        self.assertEqual(domain_report.counts.downgraded, 1)

    def test_risk_classification_json_round_trip_is_unchanged(self) -> None:
        from ai_orchestrator.schemas import RiskClassification as CompatRiskClassification
        from ai_orchestrator.risk_schemas import RiskClassification

        payload = {
            "schema_version": "1.0",
            "run_id": "run_schema_boundary",
            "risk_level": "medium",
            "change_type": "source_and_tests",
            "changed_files": ["src/example.py", "tests/test_example.py"],
            "risk_reasons": [
                {
                    "id": "R001",
                    "severity": "warning",
                    "category": "source",
                    "message": "Source and tests changed together.",
                    "file": "src/example.py",
                    "reviewer_profiles": ["qa", "architecture"],
                }
            ],
            "required_review_profiles": ["qa"],
            "optional_review_profiles": ["architecture"],
            "policy_notes": ["Schema module split must not alter artifact shape."],
        }

        compat_classification = CompatRiskClassification.model_validate(payload)
        domain_classification = RiskClassification.model_validate_json(compat_classification.model_dump_json())

        self.assertEqual(
            compat_classification.model_dump(mode="json"),
            domain_classification.model_dump(mode="json"),
        )
        self.assertEqual(domain_classification.required_review_profiles, ["qa"])


if __name__ == "__main__":
    unittest.main()
