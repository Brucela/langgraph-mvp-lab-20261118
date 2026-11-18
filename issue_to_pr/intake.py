"""Authenticate GitHub deliveries and enqueue eligible issues durably."""

import hashlib
import hmac
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import UUID

import psycopg


MAX_BODY_BYTES = 1024 * 1024
MAX_ISSUE_NUMBER = 2**63 - 1


def authenticate_event(raw: bytes, signature: str | None, secret: bytes | str) -> bool:
    if not secret or not isinstance(signature, str):
        return False
    key = secret.encode() if isinstance(secret, str) else secret
    digest = hmac.new(key, raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature.encode(), f"sha256={digest}".encode())


def parse_event(payload: dict, repo: str, actor: str) -> int | None:
    if not isinstance(payload, dict) or payload.get("action") != "labeled":
        return None
    label, repository, sender, issue = (payload.get(key) for key in (
        "label", "repository", "sender", "issue",
    ))
    if not all(isinstance(item, dict) for item in (label, repository, sender, issue)):
        return None
    if (
        label.get("name") != "ai-ready"
        or not repo or repository.get("full_name") != repo
        or not actor or sender.get("login") != actor
        or "pull_request" in issue
    ):
        return None
    number = issue.get("number")
    if type(number) is int and 0 < number <= MAX_ISSUE_NUMBER:
        return number
    return None


def process_webhook(raw, headers, queue, repo, actor, secret) -> HTTPStatus:
    if not authenticate_event(raw, headers.get("X-Hub-Signature-256"), secret):
        return HTTPStatus.UNAUTHORIZED
    if headers.get("X-GitHub-Event") != "issues":
        return HTTPStatus.OK

    delivery_id = headers.get("X-GitHub-Delivery")
    try:
        if not delivery_id or str(UUID(delivery_id)) != delivery_id:
            return HTTPStatus.BAD_REQUEST
        payload = json.loads(raw)
    except (ValueError, RecursionError):
        return HTTPStatus.BAD_REQUEST
    if not isinstance(payload, dict):
        return HTTPStatus.BAD_REQUEST
    issue_number = parse_event(payload, repo, actor)
    if issue_number is None:
        return HTTPStatus.OK
    if queue.enqueue(delivery_id, issue_number):
        return HTTPStatus.ACCEPTED
    return HTTPStatus.OK


class PostgresQueue:
    """Durable queue for the dedicated repository. A delivery is consumed once."""

    def __init__(self, dsn: str):
        self.dsn = dsn

    def setup(self) -> None:
        with psycopg.connect(self.dsn, connect_timeout=5) as connection:
            # Serialize schema setup when both the intake and worker start together.
            connection.execute("SELECT pg_advisory_xact_lock(814427, 1)")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS issue_pr_deliveries (
                    delivery_id text PRIMARY KEY,
                    received_at timestamptz NOT NULL DEFAULT now()
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS issue_pr_jobs (
                    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    issue_number bigint NOT NULL CHECK (issue_number > 0),
                    status text NOT NULL DEFAULT 'ready'
                        CHECK (status IN ('ready', 'running', 'done'))
                )
            """)
            connection.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS issue_pr_jobs_one_active_per_issue
                    ON issue_pr_jobs (issue_number) WHERE status IN ('ready', 'running')
            """)
            connection.execute("""
                CREATE INDEX IF NOT EXISTS issue_pr_jobs_ready
                    ON issue_pr_jobs (id) WHERE status = 'ready'
            """)

    def enqueue(self, delivery_id: str, issue_number: int) -> bool:
        with psycopg.connect(self.dsn, connect_timeout=5) as connection:
            row = connection.execute("""
                WITH received AS (
                    INSERT INTO issue_pr_deliveries (delivery_id) VALUES (%s)
                    ON CONFLICT (delivery_id) DO NOTHING
                    RETURNING delivery_id
                )
                INSERT INTO issue_pr_jobs (issue_number) SELECT %s FROM received
                ON CONFLICT (issue_number) WHERE status IN ('ready', 'running') DO NOTHING
                RETURNING id
            """, (delivery_id, issue_number)).fetchone()
        return row is not None

    def claim(self) -> tuple[int, int] | None:
        with psycopg.connect(self.dsn, connect_timeout=5) as connection:
            return connection.execute("""
                WITH next_job AS (
                    SELECT id FROM issue_pr_jobs WHERE status = 'ready'
                    ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED
                )
                UPDATE issue_pr_jobs AS jobs SET status = 'running'
                FROM next_job WHERE jobs.id = next_job.id
                RETURNING jobs.id, jobs.issue_number
            """).fetchone()

    def finish(self, job_id: int) -> None:
        with psycopg.connect(self.dsn, connect_timeout=5) as connection:
            connection.execute("""
                UPDATE issue_pr_jobs SET status = 'done' WHERE id = %s AND status = 'running'
            """, (job_id,))

    def recover(self) -> int:
        """Restore interrupted jobs when the single worker starts exclusively."""
        with psycopg.connect(self.dsn, connect_timeout=5) as connection:
            return connection.execute("""
                UPDATE issue_pr_jobs SET status = 'ready' WHERE status = 'running'
            """).rowcount


def serve(queue, repo, actor, secret, host="127.0.0.1", port=8080) -> None:
    if not repo or not actor or not secret:
        raise ValueError("Webhook requires the repository, allowed actor, and signing secret")
    queue.setup()

    class Handler(BaseHTTPRequestHandler):
        timeout = 10

        def do_POST(self):
            if self.path != "/webhook":
                self._respond(HTTPStatus.NOT_FOUND)
                return
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
                self._respond(HTTPStatus.BAD_REQUEST)
                return
            # Compare the digit count before converting a potentially unbounded integer.
            if len(lengths[0]) > len(str(MAX_BODY_BYTES)) or int(lengths[0]) > MAX_BODY_BYTES:
                self._respond(HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                return
            length = int(lengths[0])
            try:
                raw = self.rfile.read(length)
            except TimeoutError:
                self._respond(HTTPStatus.REQUEST_TIMEOUT)
                return
            if len(raw) != length:
                self._respond(HTTPStatus.BAD_REQUEST)
                return
            try:
                self._respond(process_webhook(raw, self.headers, queue, repo, actor, secret))
            except psycopg.Error:
                self.log_error("Could not durably enqueue GitHub delivery")
                self._respond(HTTPStatus.SERVICE_UNAVAILABLE)

        def _respond(self, status):
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

    with ThreadingHTTPServer((host, port), Handler) as server:
        server.serve_forever()
