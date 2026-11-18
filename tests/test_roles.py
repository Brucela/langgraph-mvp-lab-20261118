from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import json
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from issue_to_pr import roles


ISSUE = {"number": 7, "title": "Normalize output"}
FILES = {"demo/formatter.py": "def format_output(value):\n    return value\n"}
REQUIREMENTS = {
    "status": "ready",
    "scope": ["Normalize output"],
    "criteria": ["Strip enclosing whitespace"],
    "questions": [],
}
EXECUTION = {"exit_code": 1, "log": "AssertionError: expected 'abc'"}


class ExampleBackend:
    def __init__(self, reply):
        self.reply = reply
        self.requests = []

    def complete(self, role, prompt, schema):
        self.requests.append((role, prompt, schema))
        return self.reply


def test_analysis_exposes_scope_and_acceptance_criteria():
    backend = ExampleBackend(json.dumps(REQUIREMENTS))
    assert roles.Roles(backend).analyze(ISSUE, FILES) == REQUIREMENTS
    role, prompt, schema = backend.requests[0]
    assert role == "analyze"
    assert ISSUE["title"] in prompt
    assert "demo/formatter.py" in prompt
    assert set(schema["required"]) == {"status", "scope", "criteria", "questions"}


def test_developer_returns_full_allowlisted_replacements():
    backend = ExampleBackend(
        {
            "summary": "Normalize output",
            "files": [{"path": "demo/formatter.py", "content": "new source\n"}],
        }
    )
    assert roles.Roles(backend).develop(ISSUE, REQUIREMENTS, FILES, "Prior test failed") == {
        "summary": "Normalize output",
        "files": {"demo/formatter.py": "new source\n"},
    }
    assert "Prior test failed" in backend.requests[0][1]


@pytest.mark.parametrize(
    "replacements",
    [
        [{"path": "../formatter.py", "content": "unauthorized"}],
        [
            {"path": "demo/formatter.py", "content": "first"},
            {"path": "demo/formatter.py", "content": "second"},
        ],
    ],
)
def test_developer_rejects_out_of_scope_or_duplicate_replacements(replacements):
    backend = ExampleBackend({"summary": "Normalize output", "files": replacements})
    with pytest.raises(roles.RoleError):
        roles.Roles(backend).develop(ISSUE, REQUIREMENTS, FILES, "")


def test_tester_summarizes_actual_runner_evidence():
    backend = ExampleBackend({"summary": "The assertion failed."})
    assert roles.Roles(backend).test(ISSUE, REQUIREMENTS, EXECUTION) == {
        "summary": "The assertion failed."
    }
    role, prompt, schema = backend.requests[0]
    assert role == "test"
    assert EXECUTION["log"] in prompt
    assert schema["required"] == ["summary"]


@pytest.mark.parametrize(
    "reply",
    [
        '{"summary":"Pass","exit_code":0}',
        '{"summary":1}',
        '{"summary":"one","summary":"two"}',
        '```json\n{"summary":"failed"}\n```',
    ],
)
def test_tester_rejects_unvalidated_or_overreaching_response(reply):
    with pytest.raises(roles.RoleError):
        roles.Roles(ExampleBackend(reply)).test(ISSUE, REQUIREMENTS, EXECUTION)


@pytest.mark.parametrize("line,status", [(True, "changes_required"), (3, "pass")])
def test_review_requires_a_real_line_and_status_consistent_with_findings(line, status):
    backend = ExampleBackend(
        {
            "status": status,
            "findings": [
                {
                    "severity": "blocking",
                    "path": "demo/formatter.py",
                    "line": line,
                    "detail": "Untrimmed output",
                }
            ],
        }
    )
    with pytest.raises(roles.RoleError):
        roles.Roles(backend).review(ISSUE, REQUIREMENTS, "a diff", EXECUTION)


def test_review_accepts_a_pass_with_a_nonblocking_suggestion():
    outcome = {
        "status": "pass",
        "findings": [
            {
                "severity": "suggestion",
                "path": "demo/formatter.py",
                "line": None,
                "detail": "Optional comment",
            }
        ],
    }
    backend = ExampleBackend(outcome)
    assert roles.Roles(backend).review(ISSUE, REQUIREMENTS, "a diff", EXECUTION) == outcome
    assert "a diff" in backend.requests[0][1]


def test_oversized_context_fails_before_calling_the_backend():
    backend = ExampleBackend(REQUIREMENTS)
    with pytest.raises(roles.RoleError):
        roles.Roles(backend).analyze(ISSUE, {"demo/formatter.py": "é" * 150_000})
    assert backend.requests == []


def test_directory_backend_delivers_request_and_validates_response(tmp_path, monkeypatch):
    monkeypatch.setenv("ROLE_BACKEND", "directory")
    monkeypatch.setenv("ROLE_MAILBOX_DIR", str(tmp_path))
    monkeypatch.setenv("ROLE_TIMEOUT_SECONDS", "2")

    with ThreadPoolExecutor(max_workers=1) as pool:
        response = pool.submit(roles.from_env().analyze, ISSUE, FILES)
        deadline = time.monotonic() + 1
        requests = []
        while not requests and time.monotonic() < deadline:
            time.sleep(0.01)
            requests = list(tmp_path.glob("*.request.json"))
        assert len(requests) == 1
        request_file = requests[0]
        request = json.loads(request_file.read_text())
        assert set(request) == {"role", "prompt", "schema"}
        assert request["role"] == "analyze"
        response_file = tmp_path / request_file.name.replace(".request.json", ".response.json")
        partial_file = response_file.with_suffix(".partial")
        partial_file.write_text(json.dumps(REQUIREMENTS))
        partial_file.replace(response_file)
        assert response.result(timeout=2) == REQUIREMENTS


def test_directory_backend_times_out_without_response(tmp_path, monkeypatch):
    monkeypatch.setenv("ROLE_BACKEND", "directory")
    monkeypatch.setenv("ROLE_MAILBOX_DIR", str(tmp_path))
    monkeypatch.setenv("ROLE_TIMEOUT_SECONDS", "0.02")
    with pytest.raises(roles.RoleError, match="timed out"):
        roles.from_env().analyze(ISSUE, FILES)


def test_codex_passes_schema_and_prompt_with_an_isolated_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("ROLE_BACKEND", "codex")
    monkeypatch.setenv("GH_TOKEN", "github-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "api-secret")
    monkeypatch.chdir(tmp_path)

    def run(args, **kwargs):
        assert "--ephemeral" in args
        assert "--ignore-user-config" in args
        assert args[args.index("--sandbox") + 1] == "read-only"
        assert kwargs["cwd"] != tmp_path
        assert "GH_TOKEN" not in kwargs["env"]
        assert "OPENAI_API_KEY" not in kwargs["env"]
        assert ISSUE["title"] in kwargs["input"]
        schema = json.loads(Path(args[args.index("--output-schema") + 1]).read_text())
        assert "questions" in schema["required"]
        Path(args[args.index("--output-last-message") + 1]).write_text(json.dumps(REQUIREMENTS))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(roles.subprocess, "run", run)
    assert roles.from_env().analyze(ISSUE, FILES) == REQUIREMENTS


@pytest.mark.parametrize("reply", [{"summary": "a" * 260_000}, '{"summary":"\\ud800"}'])
def test_valid_json_shape_can_still_exceed_size_or_contain_invalid_utf8(reply):
    with pytest.raises(roles.RoleError):
        roles.Roles(ExampleBackend(reply)).test(ISSUE, REQUIREMENTS, EXECUTION)


def test_openai_backend_requests_strict_schema_and_extracts_model_content(monkeypatch):
    monkeypatch.setenv("ROLE_BACKEND", "openai")
    monkeypatch.setenv("ROLE_OPENAI_API_KEY", "demo-credential")
    monkeypatch.setenv("ROLE_MODEL", "selected-model")
    monkeypatch.setenv("ROLE_OPENAI_BASE_URL", "https://model.example/v1")
    monkeypatch.setenv("ROLE_TIMEOUT_SECONDS", "4")

    def open_request(call, *, timeout):
        assert call.full_url == "https://model.example/v1/chat/completions"
        assert call.get_header("Authorization") == "Bearer demo-credential"
        assert timeout == 4
        body = json.loads(call.data)
        assert body["model"] == "selected-model"
        assert EXECUTION["log"] in body["messages"][0]["content"]
        assert body["response_format"]["json_schema"]["strict"] is True
        assert body["response_format"]["json_schema"]["schema"]["required"] == ["summary"]
        envelope = {
            "choices": [
                {"finish_reason": "stop", "message": {"content": '{"summary":"Assertion failed."}'}}
            ]
        }
        return BytesIO(json.dumps(envelope).encode())

    monkeypatch.setattr(roles.request, "build_opener", lambda *args: SimpleNamespace(open=open_request))
    assert roles.from_env().test(ISSUE, REQUIREMENTS, EXECUTION) == {"summary": "Assertion failed."}
