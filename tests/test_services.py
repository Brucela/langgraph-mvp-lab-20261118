import subprocess
import sys

import pytest

from issue_to_pr.services import GitHub, Services


def git(repo, *args):
    return subprocess.check_output(("git", "-C", str(repo), *args), text=True).strip()


@pytest.fixture
def checkout(tmp_path):
    repo = tmp_path / "checkout"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    (repo / "demo").mkdir()
    (repo / "demo" / "calculator.py").write_text("answer = 0\n")
    (repo / "check.py").write_text(
        "import os\n"
        "from demo.calculator import answer\n"
        "assert 'GH_TOKEN' not in os.environ\n"
        "assert 'OPENAI_API_KEY' not in os.environ\n"
        "assert os.environ['MVP_REQUIRE_DISCOUNT'] == '1'\n"
        "assert answer == 42, f'got {answer}'\n"
    )
    git(repo, "add", ".")
    git(repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.com", "commit", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    return {
        "workspace": str(repo),
        "base_sha": base,
        "start_sha": base,
        "issue": {"number": 7, "title": "Calculate a value", "body": "", "comments": []},
        "requirements": {"status": "ready"},
        "feedback": "",
    }


class Roles:
    def __init__(self, files=None):
        self.files = {"demo/calculator.py": "answer = 42\n"} if files is None else files

    def analyze(self, issue, files):
        assert issue["number"] == 7
        assert files == {"demo/calculator.py": "answer = 0\n"}
        return {"status": "ready", "scope": ["calculator"], "criteria": [], "questions": []}

    def develop(self, issue, requirements, files, feedback):
        return {"summary": "Set the answer", "files": self.files}

    def test(self, issue, requirements, execution):
        return {"summary": f"Observed exit {execution['exit_code']}", "status": "pass", "exit_code": 0}

    def review(self, issue, requirements, diff, test_result):
        assert "+answer = 42" in diff
        assert test_result["status"] == "pass"
        return {"status": "pass", "findings": [], "sha": "different"}


def make_services(roles=None, **kwargs):
    return Services(
        roles or Roles(),
        test_argv=(sys.executable, "check.py"),
        test_env={"MVP_REQUIRE_DISCOUNT": "1"},
        **kwargs,
    )


def test_executes_only_candidate_sha_with_configured_environment(checkout, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "credential")
    monkeypatch.setenv("OPENAI_API_KEY", "credential")
    services = make_services()
    assert services.analyze(checkout)["status"] == "ready"
    checkout["candidate"] = services.develop(checkout)
    sha = checkout["candidate"]["sha"]
    assert git(checkout["workspace"], "show", f"{sha}:demo/calculator.py") == "answer = 42"
    assert git(checkout["workspace"], "rev-parse", "HEAD") == checkout["start_sha"]
    checkout["tests"] = services.test(checkout)
    assert checkout["tests"]["status"] == "pass"
    assert checkout["tests"]["exit_code"] == 0
    assert checkout["tests"]["sha"] == sha
    assert services.review(checkout) == {"sha": sha, "status": "pass", "findings": []}


def test_nonzero_exit_remains_failure_despite_model_summary(checkout):
    services = make_services(Roles({"demo/calculator.py": "answer = 41\n"}))
    checkout["candidate"] = services.develop(checkout)
    result = services.test(checkout)
    assert result["status"] == "fail"
    assert result["exit_code"] == 1
    assert "got 41" in result["log"]


def test_development_rejects_entire_patch_outside_allowlist(checkout):
    roles = Roles({"demo/calculator.py": "answer = 42\n", "check.py": ""})
    with pytest.raises(ValueError, match="allowlist"):
        make_services(roles).develop(checkout)
    assert git(checkout["workspace"], "status", "--porcelain") == ""
    assert git(checkout["workspace"], "for-each-ref", "refs/issue-to-pr") == ""


def test_unchanged_proposal_reports_prior_commit(checkout):
    services = make_services(Roles({"demo/calculator.py": "answer = 0\n"}))
    assert services.develop(checkout)["sha"] == checkout["start_sha"]


def test_repairs_are_committed_on_the_previous_candidate(checkout):
    services = make_services(Roles({"demo/calculator.py": "answer = 41\n"}))
    previous = services.develop(checkout)
    checkout["candidate"] = previous
    repaired = make_services().develop(checkout)
    assert git(checkout["workspace"], "rev-parse", f"{repaired['sha']}^") == previous["sha"]


def test_timeout_is_an_infrastructure_error(checkout):
    services = Services(
        Roles(), test_argv=(sys.executable, "-c", "import time; time.sleep(10)"), test_timeout=0.1
    )
    checkout["candidate"] = services.develop(checkout)
    result = services.test(checkout)
    assert result["status"] == "infra_error"
    assert result["exit_code"] is None
    assert "timed out" in result["log"]


def test_publishing_requires_passing_evidence_at_candidate_sha(checkout):
    services = make_services(github=object())
    checkout["candidate"] = services.develop(checkout)
    checkout["tests"] = {"sha": checkout["start_sha"], "status": "pass", "exit_code": 0}
    checkout["review"] = {"sha": checkout["candidate"]["sha"], "status": "pass", "findings": []}
    with pytest.raises(ValueError, match="evidence"):
        services.publish(checkout)


def test_publisher_reuses_existing_pull_request(checkout, tmp_path, monkeypatch):
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "--bare")
    repo = checkout["workspace"]
    git(repo, "push", str(remote), "HEAD:refs/heads/main")
    created = {}
    github = GitHub("owner/lab")
    monkeypatch.setattr(github, "_remote_url", str(remote))

    def github_api(method, endpoint, fields=None):
        if method == "GET":
            assert endpoint == "repos/owner/lab/pulls"
            assert fields == {"state": "open", "head": "owner:ai/issue-7", "base": "main"}
            return [created] if created else []
        assert fields["title"].startswith("Issue #7")
        assert "Closes #7" in fields["body"]
        if method == "POST":
            assert not created
            branch = fields["head"]
            created.update({
                "number": 19,
                "html_url": "https://github.com/owner/lab/pull/19",
                "head": {
                    "sha": git(remote, "rev-parse", f"refs/heads/{branch}"),
                    "ref": branch,
                    "repo": {"full_name": "owner/lab"},
                },
                "base": {"ref": "main", "repo": {"full_name": "owner/lab"}},
            })
            return created
        assert method == "PATCH"
        assert endpoint == "repos/owner/lab/pulls/19"
        return created

    monkeypatch.setattr(github, "_api", github_api)
    services = make_services(github=github)
    checkout["candidate"] = services.develop(checkout)
    checkout["tests"] = services.test(checkout)
    checkout["review"] = services.review(checkout)
    assert services.publish(checkout) == services.publish(checkout) == {
        "number": 19,
        "url": "https://github.com/owner/lab/pull/19",
        "sha": checkout["candidate"]["sha"],
    }
    assert len(git(remote, "for-each-ref", "refs/heads/ai").splitlines()) == 1


def test_publish_fails_if_remote_branch_drifted(checkout, tmp_path, monkeypatch):
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "--bare")
    github = GitHub("owner/lab")
    monkeypatch.setattr(github, "_remote_url", str(remote))
    unrelated = make_services(Roles({"demo/calculator.py": "answer = 100\n"})).develop(checkout)
    git(checkout["workspace"], "push", str(remote), f"{unrelated['sha']}:refs/heads/ai/issue-7")
    services = make_services(github=github)
    checkout["candidate"] = services.develop(checkout)
    checkout["tests"] = services.test(checkout)
    checkout["review"] = services.review(checkout)
    with pytest.raises(RuntimeError, match="remote branch changed"):
        services.publish(checkout)
    assert git(remote, "rev-parse", "refs/heads/ai/issue-7") == unrelated["sha"]


def test_actions_callback_pr_is_checked_against_github(checkout, tmp_path, monkeypatch):
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "--bare")
    called = []

    def publish_pr(repo, branch, base, title, body, issue, sha):
        assert (repo, branch, base, issue) == ("owner/lab", "ai/issue-7", "main", 7)
        assert git(remote, "rev-parse", "refs/heads/ai/issue-7") == sha
        called.append(sha)
        return {"number": 19, "url": "https://github.com/owner/lab/pull/19", "sha": sha}

    def github_api(method, endpoint, fields=None):
        assert method == "GET"
        if endpoint == "repos/owner/lab/pulls":
            return []
        assert endpoint == "repos/owner/lab/pulls/19"
        return {
            "number": 19,
            "html_url": "https://github.com/owner/lab/pull/19",
            "head": {"sha": checkout["start_sha"], "ref": "ai/issue-7", "repo": {"full_name": "owner/lab"}},
            "base": {"ref": "main", "repo": {"full_name": "owner/lab"}},
        }

    github = GitHub("owner/lab", publish_pr=publish_pr)
    monkeypatch.setattr(github, "_remote_url", str(remote))
    monkeypatch.setattr(github, "_api", github_api)
    services = make_services(github=github)
    checkout["candidate"] = services.develop(checkout)
    checkout["tests"] = services.test(checkout)
    checkout["review"] = services.review(checkout)
    with pytest.raises(RuntimeError, match="PR head does not match"):
        services.publish(checkout)
    assert called == [checkout["candidate"]["sha"]]


def test_repair_that_restores_the_base_tree_reports_no_change(checkout):
    checkout["candidate"] = make_services().develop(checkout)
    services = make_services(Roles({"demo/calculator.py": "answer = 0\n"}))
    assert services.develop(checkout)["sha"] == checkout["base_sha"]


def test_retains_bounded_test_output_and_preserves_the_tail(checkout):
    services = Services(
        Roles(), test_argv=(sys.executable, "-c", "print('x' * 50000); print('last line')")
    )
    checkout["candidate"] = services.develop(checkout)
    result = services.test(checkout)
    assert result["status"] == "pass"
    assert len(result["log"]) <= 16_384
    assert result["log"].endswith("last line\n")
