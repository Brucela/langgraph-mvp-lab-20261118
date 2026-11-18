"""Route one issue to a PR using commit-specific test and review evidence."""

import json
import operator
from typing import Annotated, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph


MAX_REPAIRS = 2


class IssueState(TypedDict, total=False):
    issue: dict
    base_sha: str
    start_sha: str
    workspace: str
    history: Annotated[list[dict], operator.add]
    repair_count: int
    requirements: dict
    candidate: dict | None
    tests: dict | None
    review: dict | None
    pr: dict | None
    feedback: str
    status: str


class Services(Protocol):
    def analyze(self, state: IssueState) -> dict: ...
    def develop(self, state: IssueState) -> dict: ...
    def test(self, state: IssueState) -> dict: ...
    def review(self, state: IssueState) -> dict: ...
    def publish(self, state: IssueState) -> dict: ...
    def report(self, state: IssueState) -> None: ...


def _event(node, status, repair_count, **details):
    return [{"node": node, "status": status, "repair_count": repair_count, **details}]


def _test_result(tests, sha):
    if tests is None or not sha or tests.get("sha") != sha:
        return "infra_error"

    status, exit_code = tests.get("status"), tests.get("exit_code")
    if type(exit_code) is int:
        if status == "pass" and exit_code == 0:
            return "pass"
        if status == "fail" and exit_code > 0:
            return "fail"

    return "infra_error"


def _repair_update(state, feedback):
    if state["repair_count"] >= MAX_REPAIRS:
        return {"status": "repair_limit", "feedback": feedback}
    return {
        "status": "developing",
        "repair_count": state["repair_count"] + 1,
        "feedback": feedback,
    }


def build_graph(services: Services, checkpointer=None):
    """Compile the issue workflow. Services own side effects and their retries."""

    def analyze(state: IssueState):
        reset = {
            "candidate": None,
            "tests": None,
            "review": None,
            "pr": None,
            "repair_count": 0,
            "feedback": "",
        }
        requirements = services.analyze({**state, **reset})
        outcome = requirements.get("status")
        status = {"ready": "developing", "needs_input": "needs_input"}.get(outcome)
        return {
            **reset,
            "requirements": requirements,
            "status": status or "infra_error",
            "history": _event("analyze", outcome, 0),
        }

    def develop(state: IssueState):
        previous = state["candidate"]
        previous_sha = previous["sha"] if previous else state["start_sha"]
        candidate = services.develop(state)
        sha = candidate.get("sha")
        status = "testing"
        if not isinstance(sha, str) or not sha:
            status = "infra_error"
        elif sha in {previous_sha, state["base_sha"]}:
            status = "no_change"
        return {
            "candidate": candidate,
            "tests": None,
            "review": None,
            "status": status,
            "history": _event("develop", status, state["repair_count"], sha=sha),
        }

    def test(state: IssueState):
        tests = services.test(state)
        outcome = _test_result(tests, state["candidate"]["sha"])
        log = tests.get("log", "")
        if outcome == "pass":
            update = {"status": "reviewing", "feedback": ""}
        elif outcome == "fail":
            update = _repair_update(state, f"Tests failed (exit {tests['exit_code']}).\n{log}")
        else:
            update = {"status": "infra_error", "feedback": f"Unusable test evidence.\n{log}"}
        return {
            **update,
            "tests": tests,
            "history": _event(
                "test", outcome, state["repair_count"], sha=tests.get("sha"),
                exit_code=tests.get("exit_code"), log=log,
            ),
        }

    def review(state: IssueState):
        review_result = services.review(state)
        findings = review_result.get("findings")
        outcome = review_result.get("status")
        if review_result.get("sha") != state["candidate"]["sha"] or not isinstance(findings, list):
            outcome = "infra_error"
        if outcome == "pass":
            update = {"status": "publishing", "feedback": ""}
        elif outcome == "changes_required":
            update = _repair_update(state, f"Review findings:\n{json.dumps(findings, ensure_ascii=False)}")
        else:
            outcome = "infra_error"
            update = {"status": outcome, "feedback": "Unusable review evidence."}
        return {
            **update,
            "review": review_result,
            "history": _event(
                "review", outcome, state["repair_count"], sha=review_result.get("sha"), findings=findings,
            ),
        }

    def publish(state: IssueState):
        sha = state["candidate"]["sha"]
        review_result = state["review"] or {}
        if (
            _test_result(state["tests"], sha) != "pass"
            or review_result.get("sha") != sha
            or review_result.get("status") != "pass"
        ):
            return {
                "status": "infra_error",
                "feedback": "Publishing requires passing test and review evidence for the candidate commit.",
                "history": _event("publish", "infra_error", state["repair_count"], sha=sha),
            }
        pr = services.publish(state)
        if pr.get("sha") != sha:
            raise RuntimeError("Publisher returned a PR with a different commit SHA")
        return {
            "pr": pr,
            "status": "published",
            "history": _event("publish", "published", state["repair_count"], sha=sha),
        }

    def report(state: IssueState):
        services.report(state)
        return {"history": _event("report", state["status"], state["repair_count"])}

    def route(state: IssueState):
        return state["status"]

    graph = StateGraph(IssueState)
    graph.add_node("analyze", analyze)
    graph.add_node("develop", develop)
    graph.add_node("test", test)
    graph.add_node("review", review)
    graph.add_node("publish", publish)
    graph.add_node("report", report)
    graph.add_edge(START, "analyze")
    graph.add_conditional_edges("analyze", route, {
        "developing": "develop", "needs_input": "report", "infra_error": "report",
    })
    graph.add_conditional_edges("develop", route, {
        "testing": "test", "no_change": "report", "infra_error": "report",
    })
    graph.add_conditional_edges("test", route, {
        "reviewing": "review", "developing": "develop", "repair_limit": "report", "infra_error": "report",
    })
    graph.add_conditional_edges("review", route, {
        "publishing": "publish", "developing": "develop", "repair_limit": "report", "infra_error": "report",
    })
    graph.add_conditional_edges("publish", route, {"published": END, "infra_error": "report"})
    graph.add_edge("report", END)
    return graph.compile(checkpointer=checkpointer)
