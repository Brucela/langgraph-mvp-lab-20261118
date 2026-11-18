import unittest

from issue_to_pr.graph import build_graph


BASE = "a" * 40
FIRST = "b" * 40
SECOND = "c" * 40
THIRD = "d" * 40


class DemoServices:
    def __init__(self, *, ready=True, candidates=(FIRST,), tests=(), reviews=()):
        self.ready = ready
        self.candidates = iter(candidates)
        self.tests = iter(tests)
        self.reviews = iter(reviews)
        self.development_feedback = []
        self.published = []
        self.reported = []

    def analyze(self, state):
        return {
            "status": "ready" if self.ready else "needs_input",
            "scope": "Fix the fixture",
            "criteria": ["The fixture tests pass"],
            "questions": [] if self.ready else ["Which behavior is expected?"],
        }

    def develop(self, state):
        self.development_feedback.append(state.get("feedback", ""))
        return {"sha": next(self.candidates), "summary": "Update the fixture"}

    def test(self, state):
        return {
            "sha": state["candidate"]["sha"],
            "status": "pass",
            "exit_code": 0,
            "log": "Tests passed",
        } | next(self.tests, {})

    def review(self, state):
        return {
            "sha": state["candidate"]["sha"],
            "status": "pass",
            "findings": [],
        } | next(self.reviews, {})

    def publish(self, state):
        sha = state["candidate"]["sha"]
        self.published.append((sha, state["tests"]["sha"], state["review"]["sha"]))
        return {"number": 7, "url": "https://example.test/pull/7", "sha": sha}

    def report(self, state):
        self.reported.append(state["status"])


class IssueToPrGraphTest(unittest.TestCase):
    def run_graph(self, services):
        return build_graph(services).invoke({
            "issue": {"number": 4, "title": "Fix fixture", "body": "Details", "comments": []},
            "base_sha": BASE,
            "start_sha": BASE,
            "workspace": "/fixture",
            "history": [{"node": "initial"}],
        })

    def test_publishes_a_candidate_after_a_real_test_failure_is_repaired(self):
        services = DemoServices(
            candidates=(FIRST, SECOND),
            tests=({"status": "fail", "exit_code": 1, "log": "fixture test failed"},),
        )

        result = self.run_graph(services)

        self.assertEqual(result["status"], "published")
        self.assertEqual(result["repair_count"], 1)
        self.assertEqual(result["pr"]["number"], 7)
        self.assertEqual(services.published, [(SECOND, SECOND, SECOND)])
        self.assertIn("fixture test failed", services.development_feedback[1])
        self.assertEqual(result["history"][0], {"node": "initial"})
        attempts = [event for event in result["history"] if event["node"] == "test"]
        self.assertEqual(
            [(event["sha"], event["status"], event["exit_code"]) for event in attempts],
            [(FIRST, "fail", 1), (SECOND, "pass", 0)],
        )

    def test_shares_two_repairs_across_test_failures_and_review_findings(self):
        finding = {"message": "Handle empty input"}
        services = DemoServices(
            candidates=(FIRST, SECOND, THIRD),
            tests=({}, {"status": "fail", "exit_code": 1, "log": "new failure"}),
            reviews=(
                {"status": "changes_required", "findings": [finding]},
                {"status": "changes_required", "findings": [finding]},
            ),
        )

        result = self.run_graph(services)

        self.assertEqual(result["status"], "repair_limit")
        self.assertEqual(result["repair_count"], 2)
        self.assertEqual(len(services.development_feedback), 3)
        self.assertIn("Handle empty input", services.development_feedback[1])
        self.assertIn("new failure", services.development_feedback[2])
        self.assertEqual(services.published, [])
        self.assertEqual(services.reported, ["repair_limit"])

    def test_needs_input_stops_before_development(self):
        services = DemoServices(ready=False, candidates=())

        result = self.run_graph(services)

        self.assertEqual(result["status"], "needs_input")
        self.assertEqual(result["requirements"]["questions"], ["Which behavior is expected?"])
        self.assertEqual(services.development_feedback, [])
        self.assertEqual(services.reported, ["needs_input"])

    def test_no_change_stops_without_testing_or_publishing(self):
        services = DemoServices(candidates=(BASE,))

        result = self.run_graph(services)

        self.assertEqual(result["status"], "no_change")
        self.assertNotIn("test", [event["node"] for event in result["history"]])
        self.assertEqual(services.published, [])
        self.assertEqual(services.reported, ["no_change"])

    def test_repeat_candidate_on_repair_clears_current_evidence_and_stops(self):
        services = DemoServices(
            candidates=(FIRST, FIRST),
            tests=({"status": "fail", "exit_code": 1},),
        )

        result = self.run_graph(services)

        self.assertEqual(result["status"], "no_change")
        self.assertEqual(result["repair_count"], 1)
        self.assertIsNone(result["tests"])
        self.assertIsNone(result["review"])
        attempts = [event for event in result["history"] if event["node"] == "test"]
        self.assertEqual(len(attempts), 1)
        self.assertEqual(services.published, [])

    def test_invalid_test_evidence_stops_before_review(self):
        cases = (
            {"status": "infra_error", "exit_code": None},
            {"sha": BASE},
            {"status": "pass", "exit_code": 1},
            {"status": "pass", "exit_code": False},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                services = DemoServices(tests=(overrides,))

                result = self.run_graph(services)

                self.assertEqual(result["status"], "infra_error")
                self.assertEqual(result["repair_count"], 0)
                self.assertNotIn("review", [event["node"] for event in result["history"]])
                self.assertEqual(services.published, [])
                self.assertEqual(services.reported, ["infra_error"])

    def test_stale_review_cannot_authorize_publish(self):
        services = DemoServices(reviews=({"sha": BASE},))

        result = self.run_graph(services)

        self.assertEqual(result["status"], "infra_error")
        self.assertEqual(services.published, [])
        self.assertEqual(services.reported, ["infra_error"])
