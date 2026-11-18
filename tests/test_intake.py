import hashlib
import hmac
import json
import os
from http import HTTPStatus
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from issue_to_pr.intake import PostgresQueue, authenticate_event, parse_event, process_webhook


REPO = "owner/demo"
ACTOR = "maintainer"
SECRET = b"test-webhook-secret"
EXAMPLE = {
    "action": "labeled",
    "label": {"name": "ai-ready"},
    "repository": {"full_name": REPO},
    "sender": {"login": ACTOR},
    "issue": {"number": 13},
}


def signed(body):
    return "sha256=" + hmac.new(SECRET, body, hashlib.sha256).hexdigest()


def test_authentication_checks_raw_bytes_and_handles_missing_or_malformed_headers():
    body = b'{"number": 13}\n'
    assert authenticate_event(body, signed(body), SECRET)
    assert not authenticate_event(body.rstrip(), signed(body), SECRET)
    assert not authenticate_event(body, None, SECRET)
    assert not authenticate_event(body, "sha256=不可讀", SECRET)


@pytest.mark.parametrize("overrides", [
    {"action": "opened"},
    {"label": {"name": "bug"}},
    {"repository": {"full_name": "owner/other"}},
    {"sender": {"login": "outsider"}},
    {"issue": {"number": True}},
    {"issue": {"number": "13"}},
    {"issue": {"number": 0}},
    {"issue": {"number": 13, "pull_request": {}}},
    {"issue": None},
])
def test_parse_event_ignores_unmatched_and_invalid_events(overrides):
    assert parse_event(EXAMPLE | overrides, REPO, ACTOR) is None


def test_parse_event_returns_exact_issue_number():
    assert parse_event(EXAMPLE, REPO, ACTOR) == 13


class LocalQueue:
    def __init__(self):
        self.enqueued = []

    def enqueue(self, delivery_id, issue_number):
        entry = (delivery_id, issue_number)
        if entry in self.enqueued:
            return False
        self.enqueued.append(entry)
        return True


def test_webhook_accepts_valid_delivery_and_acknowledges_redelivery():
    queue = LocalQueue()
    body = json.dumps(EXAMPLE).encode()
    delivery_id = str(uuid4())
    headers = {
        "X-Hub-Signature-256": signed(body),
        "X-GitHub-Event": "issues",
        "X-GitHub-Delivery": delivery_id,
    }

    assert process_webhook(body, headers, queue, REPO, ACTOR, SECRET) == HTTPStatus.ACCEPTED
    assert process_webhook(body, headers, queue, REPO, ACTOR, SECRET) == HTTPStatus.OK
    assert queue.enqueued == [(delivery_id, 13)]


@pytest.mark.parametrize(("overrides", "expected"), [
    ({"X-Hub-Signature-256": "sha256=bad"}, HTTPStatus.UNAUTHORIZED),
    ({"X-GitHub-Event": "pull_request"}, HTTPStatus.OK),
    ({"X-GitHub-Delivery": "not-a-delivery-id"}, HTTPStatus.BAD_REQUEST),
    ({"X-GitHub-Delivery": None}, HTTPStatus.BAD_REQUEST),
])
def test_webhook_verifies_headers_before_enqueuing(overrides, expected):
    queue = LocalQueue()
    body = json.dumps(EXAMPLE).encode()
    headers = {
        "X-Hub-Signature-256": signed(body),
        "X-GitHub-Event": "issues",
        "X-GitHub-Delivery": str(uuid4()),
    } | overrides

    assert process_webhook(body, headers, queue, REPO, ACTOR, SECRET) == expected
    assert queue.enqueued == []


@pytest.mark.parametrize("body", [b"null", b"{invalid", b"\xff"])
def test_webhook_rejects_malformed_signed_json(body):
    queue = LocalQueue()
    headers = {
        "X-Hub-Signature-256": signed(body),
        "X-GitHub-Event": "issues",
        "X-GitHub-Delivery": str(uuid4()),
    }

    assert process_webhook(body, headers, queue, REPO, ACTOR, SECRET) == HTTPStatus.BAD_REQUEST
    assert queue.enqueued == []


@pytest.fixture
def postgres_queue():
    dsn = os.environ.get("MVP_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("set MVP_TEST_POSTGRES_DSN to run the PostgreSQL queue integration test")
    schema = "queue_test_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            queue = PostgresQueue(make_conninfo(dsn, options=f"-csearch_path={schema}"))
            queue.setup()
            yield queue
        finally:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_postgres_recovers_same_job_and_deduplicates_suppressed_deliveries(postgres_queue):
    queue = postgres_queue
    assert queue.enqueue("delivery-1", 13)
    assert not queue.enqueue("delivery-1", 42)
    assert not queue.enqueue("delivery-2", 13)
    claimed = queue.claim()
    assert claimed[1] == 13
    assert not queue.enqueue("delivery-3", 13)
    assert queue.claim() is None
    assert queue.recover() == 1
    assert queue.claim() == claimed
    queue.finish(claimed[0])
    assert not queue.enqueue("delivery-2", 13)
    assert not queue.enqueue("delivery-3", 13)
    assert queue.enqueue("delivery-4", 13)
    assert queue.claim()[0] != claimed[0]
