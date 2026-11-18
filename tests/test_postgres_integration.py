"""Integration checks against an isolated schema on a real PostgreSQL server."""

import hashlib
import hmac
from http import HTTPStatus
import json
import os
from uuid import uuid4

from langgraph.checkpoint.postgres import PostgresSaver
import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
import pytest

from issue_to_pr.graph import build_graph


BASE = "a" * 40
FIRST_CANDIDATE = "b" * 40
REPAIRED_CANDIDATE = "c" * 40


@pytest.fixture
def postgres_dsn():
    dsn = os.environ.get("MVP_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("set MVP_TEST_DATABASE_URL to run the PostgreSQL integration tests")

    schema = "mvp_test_" + uuid4().hex
    dsn = make_conninfo(dsn, connect_timeout=5)
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            yield make_conninfo(dsn, options=f"-csearch_path={schema} -cstatement_timeout=5000")
        finally:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_signed_intake_persists_one_job_across_queue_instances(postgres_dsn):
    from issue_to_pr.intake import PostgresQueue, process_webhook

    intake = PostgresQueue(postgres_dsn)
    intake.setup()
    repo = "owner/demo"
    actor = "maintainer"
    secret = b"test-secret"
    body = json.dumps({
        "action": "labeled",
        "label": {"name": "ai-ready"},
        "repository": {"full_name": repo},
        "sender": {"login": actor},
        "issue": {"number": 17},
    }).encode("utf-8")
    headers = {
        "X-GitHub-Event": "issues",
        "X-GitHub-Delivery": str(uuid4()),
        "X-Hub-Signature-256": "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest(),
    }

    assert process_webhook(body + b" ", headers, intake, repo, actor, secret) == HTTPStatus.UNAUTHORIZED
    assert process_webhook(body, headers, intake, repo, actor, secret) == HTTPStatus.ACCEPTED
    assert process_webhook(body, headers, intake, repo, actor, secret) == HTTPStatus.OK

    worker = PostgresQueue(postgres_dsn)
    job_id, issue_number = worker.claim()
    assert issue_number == 17
    assert worker.claim() is None
    worker.finish(job_id)

    restarted_worker = PostgresQueue(postgres_dsn)
    assert restarted_worker.recover() == 0
    assert restarted_worker.claim() is None
    assert process_webhook(body, headers, intake, repo, actor, secret) == HTTPStatus.OK
    assert restarted_worker.claim() is None


class CheckpointServices:
    def __init__(self, *, restarted=False):
        self.restarted = restarted
        self.published = []

    def analyze(self, state):
        assert not self.restarted, "Analysis was already checkpointed"
        return {
            "status": "ready",
            "scope": ["Repair the fixture"],
            "criteria": ["The fixture test passes"],
            "questions": [],
        }

    def develop(self, state):
        if state["repair_count"] == 0:
            assert not self.restarted, "First candidate was already checkpointed"
            return {"sha": FIRST_CANDIDATE, "summary": "First candidate"}
        if not self.restarted:
            raise TimeoutError("Simulated interruption while repairing")
        assert "fixture regression" in state["feedback"]
        return {"sha": REPAIRED_CANDIDATE, "summary": "Repair the fixture"}

    def test(self, state):
        sha = state["candidate"]["sha"]
        if sha == FIRST_CANDIDATE:
            assert not self.restarted, "First test was already checkpointed"
            return {"sha": sha, "status": "fail", "exit_code": 1, "log": "fixture regression"}
        return {"sha": sha, "status": "pass", "exit_code": 0, "log": "fixture test passed"}

    def review(self, state):
        return {"sha": state["candidate"]["sha"], "status": "pass", "findings": []}

    def publish(self, state):
        sha = state["candidate"]["sha"]
        self.published.append(sha)
        return {"number": 3, "url": "https://example.test/pull/3", "sha": sha}

    def report(self, state):
        pytest.fail(f"Unexpected workflow status {state['status']}")


def test_postgres_checkpoint_resumes_pending_repair_on_new_connection(postgres_dsn):
    config = {"configurable": {"thread_id": "postgres-checkpoint-test"}}
    initial_state = {
        "issue": {"number": 17, "title": "Repair the fixture", "body": "Use stable output"},
        "base_sha": BASE,
        "start_sha": BASE,
        "workspace": "/test-fixture",
        "history": [],
    }
    first_worker = CheckpointServices()
    with PostgresSaver.from_conn_string(postgres_dsn) as saver:
        saver.setup()
        graph = build_graph(first_worker, checkpointer=saver)
        with pytest.raises(TimeoutError, match="Simulated interruption"):
            graph.invoke(initial_state, config, durability="sync")
    assert first_worker.published == []

    restarted_worker = CheckpointServices(restarted=True)
    with PostgresSaver.from_conn_string(postgres_dsn) as saver:
        graph = build_graph(restarted_worker, checkpointer=saver)
        snapshot = graph.get_state(config)
        assert snapshot.next == ("develop",)
        assert snapshot.values["repair_count"] == 1
        assert snapshot.values["tests"]["sha"] == FIRST_CANDIDATE
        assert snapshot.values["tests"]["exit_code"] == 1
        result = graph.invoke(None, config, durability="sync")
        assert graph.get_state(config).next == ()

    assert result["status"] == "published"
    assert result["repair_count"] == 1
    assert result["pr"]["sha"] == REPAIRED_CANDIDATE
    assert restarted_worker.published == [REPAIRED_CANDIDATE]
    assert [
        (step["sha"], step["exit_code"]) for step in result["history"] if step["node"] == "test"
    ] == [(FIRST_CANDIDATE, 1), (REPAIRED_CANDIDATE, 0)]
