"""Trusted worker for scoped Git edits, commit-specific tests and GitHub handoff."""

import json
import os
import re
import selectors
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path, PurePosixPath


DEFAULT_PATHS = ("demo/calculator.py",)
DEFAULT_TEST_ARGV = (sys.executable, "-m", "unittest", "discover", "-s", "demo", "-v")
MAX_TEST_LOG_BYTES = 16_384
COMMIT_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
REPO_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+")
FILE_PATH = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_. /-]*")
WORKER_NAME = "Issue to PR Worker"
WORKER_EMAIL = "issue-to-pr@users.noreply.github.com"


def _git_env(extra=None):
    return {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(Path.home()),
        "LANG": "C",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        **(extra or {}),
    }


def _git(repo, *args, data=None, env=None, check=True):
    completed = subprocess.run(
        ("git", "-C", str(repo), "-c", f"core.hooksPath={os.devnull}", *args),
        input=data,
        capture_output=True,
        env=_git_env(env),
        timeout=90,
    )
    if check and completed.returncode:
        reason = completed.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"Git {args[0]} failed: {reason}")
    return completed


def _git_text(repo, *args, env=None):
    return _git(repo, *args, env=env).stdout.decode().rstrip("\n")


def _require_commit(repo, sha):
    if not isinstance(sha, str) or not COMMIT_SHA.fullmatch(sha):
        raise ValueError("Expected a full candidate or base commit SHA")
    if _git_text(repo, "cat-file", "-t", sha) != "commit":
        raise ValueError("The SHA must identify a Git commit")
    return sha


def _require_ancestor(repo, older, newer):
    _require_commit(repo, older)
    _require_commit(repo, newer)
    if _git(repo, "merge-base", "--is-ancestor", older, newer, check=False).returncode:
        raise ValueError("Candidate commit is outside the starting Git history")


def _file_mode(repo, sha, name):
    entry = _git_text(repo, "ls-tree", sha, "--", name)
    mode = entry.partition(" ")[0]
    if mode not in {"100644", "100755"}:
        raise ValueError(f"Allowlisted file must be a tracked regular file: {name}")
    return mode


def _read_files(repo, sha, names):
    files = {}
    for name in names:
        _file_mode(repo, sha, name)
        files[name] = _git(repo, "show", f"{sha}:{name}").stdout.decode()
    return files


def _execute_tests(argv, cwd, env, timeout):
    deadline = time.monotonic() + timeout
    output = bytearray()
    try:
        with subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        ) as process, selectors.DefaultSelector() as stream:
            stream.register(process.stdout, selectors.EVENT_READ)
            try:
                while stream.get_map():
                    if not stream.select(max(0, deadline - time.monotonic())):
                        raise TimeoutError
                    data = os.read(process.stdout.fileno(), 8192)
                    if not data:
                        stream.unregister(process.stdout)
                        break
                    output.extend(data)
                    if len(output) > MAX_TEST_LOG_BYTES:
                        del output[:-MAX_TEST_LOG_BYTES]
                    if time.monotonic() >= deadline:
                        raise TimeoutError
                code = process.wait(timeout=max(0, deadline - time.monotonic()))
            except (TimeoutError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                return None, output.decode(errors="replace") + "\nTest command timed out."
    except OSError as exc:
        return None, f"Could not launch or read test command: {exc}"
    return code, output.decode(errors="replace")


def _issue_number(number):
    if type(number) is not int or number < 1:
        raise ValueError("Issue number must be a positive integer")
    return number


class Services:
    """Implement the graph's side effects for an isolated repository clone."""

    def __init__(
        self, roles, *, github=None, allowed_paths=DEFAULT_PATHS, test_argv=None,
        test_env=None, test_timeout=60, report_callback=None,
    ):
        self.roles = roles
        self.github = github
        self.allowed_paths = tuple(dict.fromkeys(allowed_paths))
        for name in self.allowed_paths:
            path = PurePosixPath(name)
            if (
                not FILE_PATH.fullmatch(name)
                or path.as_posix() != name
                or {"..", ".git"} & set(path.parts)
                or path.parts[0] == ".github"
            ):
                raise ValueError(f"Invalid allowlisted repository path: {name}")
        self.test_argv = tuple(DEFAULT_TEST_ARGV if test_argv is None else test_argv)
        self.test_env = dict(test_env or {})
        self.test_timeout = test_timeout
        self.report_callback = report_callback

    def analyze(self, state):
        repo = Path(state["workspace"]).resolve()
        start = _require_commit(repo, state["start_sha"])
        _require_ancestor(repo, state["base_sha"], start)
        return self.roles.analyze(state["issue"], _read_files(repo, start, self.allowed_paths))

    def develop(self, state):
        repo = Path(state["workspace"]).resolve()
        previous = state.get("candidate")
        start = _require_commit(repo, state["start_sha"])
        base = state["base_sha"]
        _require_ancestor(repo, base, start)
        parent = self._candidate(state) if previous else start
        files = _read_files(repo, parent, self.allowed_paths)
        proposal = self.roles.develop(
            state["issue"], state["requirements"], files, state.get("feedback", "")
        )
        replacements = proposal["files"]
        if not isinstance(replacements, dict) or not replacements.keys() <= files.keys():
            raise ValueError("Proposed files must be within the repository allowlist")
        if any(not isinstance(content, str) for content in replacements.values()):
            raise ValueError("Proposed files must contain full text")
        summary = proposal["summary"]
        if not isinstance(summary, str):
            raise ValueError("Proposed commit summary must be text")
        changed = {name: content for name, content in replacements.items() if files[name] != content}
        if not changed:
            return {"sha": parent, "summary": summary}

        # Git objects keep the checkpointed candidate independent from the checkout and its index.
        with tempfile.TemporaryDirectory(prefix="issue-to-pr-index-") as temp:
            env = {"GIT_INDEX_FILE": str(Path(temp) / "index")}
            _git(repo, "read-tree", parent, env=env)
            for name, content in changed.items():
                mode = _file_mode(repo, parent, name)
                blob = _git(repo, "hash-object", "-w", "--stdin", data=content.encode()).stdout.decode().strip()
                _git(repo, "update-index", "--cacheinfo", mode, blob, name, env=env)
            tree = _git_text(repo, "write-tree", env=env)
            if tree == _git_text(repo, "rev-parse", f"{base}^{{tree}}"):
                return {"sha": base, "summary": summary}
            sha = _git_text(
                repo, "-c", f"user.name={WORKER_NAME}", "-c", f"user.email={WORKER_EMAIL}",
                "-c", "commit.gpgsign=false", "commit-tree", tree, "-p", parent,
                "-m", f"Work on issue #{_issue_number(state['issue']['number'])}",
            )
        _git(repo, "update-ref", f"refs/issue-to-pr/candidates/{sha}", sha)
        return {"sha": sha, "summary": summary}

    def _candidate(self, state):
        repo = Path(state["workspace"]).resolve()
        sha = state["candidate"]["sha"]
        base = state["base_sha"]
        _require_ancestor(repo, state["start_sha"], sha)
        _require_ancestor(repo, base, sha)
        changed = set(_git_text(
            repo, "diff", "--name-only", "--no-renames", "-z", base, sha, "--"
        ).split("\0")) - {""}
        if sha == state["start_sha"] or not changed:
            raise ValueError("Candidate contains no changes for this issue run")
        if not changed <= set(self.allowed_paths):
            raise ValueError("Candidate changes files outside the repository allowlist")
        for name in sorted(changed):
            if _file_mode(repo, base, name) != _file_mode(repo, sha, name):
                raise ValueError(f"Candidate changed an allowlisted file's mode: {name}")
        return sha

    def test(self, state):
        sha = self._candidate(state)
        repo = Path(state["workspace"]).resolve()
        with tempfile.TemporaryDirectory(prefix="issue-to-pr-test-") as temp:
            root = Path(temp)
            checkout = root / "candidate"
            checkout.mkdir()
            archive = root / "candidate.tar"
            _git(repo, "archive", "--format=tar", f"--output={archive}", sha)
            with tarfile.open(archive) as candidate:
                candidate.extractall(checkout, filter="data")
            home = root / "home"
            home.mkdir()
            env = {
                "PATH": os.defpath,
                "HOME": str(home),
                "TMPDIR": str(root),
                "LANG": "C",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONNOUSERSITE": "1",
                **self.test_env,
            }
            code, log = _execute_tests(self.test_argv, checkout, env, self.test_timeout)
        status = "infra_error" if code is None or code < 0 else "pass" if code == 0 else "fail"
        result = {"sha": sha, "status": status, "exit_code": code, "log": log}
        if status != "infra_error":
            summary = self.roles.test(state["issue"], state["requirements"], result.copy())
            result["summary"] = summary["summary"]
        return result

    def review(self, state):
        sha = self._candidate(state)
        if not self._tests_pass(state, sha):
            raise ValueError("Review requires passing tests for the candidate SHA")
        diff = _git_text(
            state["workspace"], "diff", "--no-ext-diff", "--no-textconv", state["base_sha"], sha, "--"
        )
        review = self.roles.review(state["issue"], state["requirements"], diff, state["tests"].copy())
        return {"sha": sha, "status": review["status"], "findings": review["findings"]}

    @staticmethod
    def _tests_pass(state, sha):
        tests = state.get("tests") or {}
        code = tests.get("exit_code")
        return tests.get("sha") == sha and tests.get("status") == "pass" and type(code) is int and code == 0

    def publish(self, state):
        sha = self._candidate(state)
        review = state.get("review") or {}
        if (
            not self._tests_pass(state, sha)
            or review.get("sha") != sha
            or review.get("status") != "pass"
            or not isinstance(review.get("findings"), list)
        ):
            raise ValueError("Publication requires passing test and review evidence for the candidate SHA")
        if self.github is None:
            raise RuntimeError("GitHub publisher has not been configured")
        return self.github.publish(
            workspace=state["workspace"],
            start_sha=state["start_sha"],
            sha=sha,
            issue=state["issue"],
            base=state.get("base", "main"),
            branch=state.get("branch"),
        )

    def report(self, state):
        if self.report_callback is not None:
            self.report_callback(state)


class GitHub:
    """Restrict Issue reads, branch updates and PR handoff to one configured repository."""

    def __init__(self, repo, *, token=None, publish_pr=None):
        if not REPO_NAME.fullmatch(repo) or repo.split("/")[1] in {".", ".."}:
            raise ValueError("Expected GitHub repository owner/name")
        self.repo = repo
        self.owner = repo.partition("/")[0]
        self.token = token if token is not None else os.environ.get("GH_TOKEN")
        self.publish_pr = publish_pr
        self._remote_url = f"https://github.com/{repo}.git"

    def _gh_env(self):
        env = {"GH_HOST": "github.com", "GH_PROMPT_DISABLED": "1"}
        if self.token is not None:
            env["GH_TOKEN"] = self.token
        if os.environ.get("GH_CONFIG_DIR"):
            env["GH_CONFIG_DIR"] = os.environ["GH_CONFIG_DIR"]
        return env

    def _api(self, method, endpoint, fields=None, *, paginate=False):
        args = ["gh", "api", "--hostname", "github.com", "--method", method, endpoint]
        if paginate:
            args.extend(("--paginate", "--slurp"))
        for key, value in (fields or {}).items():
            args.extend(("--raw-field", f"{key}={value}"))
        result = subprocess.run(args, capture_output=True, timeout=45, env=_git_env(self._gh_env()))
        if result.returncode:
            message = result.stderr.decode(errors="replace").strip()
            raise RuntimeError(f"GitHub {method} {endpoint} failed: {message}")
        return json.loads(result.stdout)

    def fetch_issue(self, number):
        number = _issue_number(number)
        endpoint = f"repos/{self.repo}/issues/{number}"
        issue = self._api("GET", endpoint)
        if issue.get("number") != number or "pull_request" in issue:
            raise ValueError("GitHub response did not identify the requested Issue")
        pages = self._api("GET", f"{endpoint}/comments", {"per_page": 100}, paginate=True)
        return {
            "number": number,
            "title": issue["title"],
            "body": issue.get("body") or "",
            "author": issue["user"]["login"],
            "url": issue["html_url"],
            "comments": [
                {"author": comment["user"]["login"], "body": comment.get("body") or ""}
                for page in pages for comment in page
            ],
        }

    def _remote_git(self, workspace, *args, check=True):
        return _git(
            workspace, "-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential",
            *args, env=self._gh_env(), check=check,
        )

    def clone(self, workspace, base="main"):
        workspace = Path(workspace).resolve()
        self._check_base(workspace.parent, base)
        self._remote_git(
            workspace.parent, "clone", "--origin", "origin", "--single-branch", "--branch", base,
            "--", self._remote_url, str(workspace),
        )
        return _git_text(workspace, "rev-parse", "HEAD")

    @staticmethod
    def _check_base(workspace, base):
        if (
            not isinstance(base, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", base)
            or _git(workspace, "check-ref-format", f"refs/heads/{base}", check=False).returncode
        ):
            raise ValueError("Invalid Git base branch")

    def _remote_head(self, workspace, ref):
        result = self._remote_git(workspace, "ls-remote", "--exit-code", "--refs", self._remote_url, ref, check=False)
        if result.returncode == 2:
            return None
        if result.returncode:
            raise RuntimeError("Could not read the remote issue branch")
        sha, matched_ref = result.stdout.decode().strip().split("\t", 1)
        if not COMMIT_SHA.fullmatch(sha) or matched_ref != ref:
            raise RuntimeError("Unexpected Git remote branch response")
        return sha

    def _pr_result(self, pr, branch, base, sha):
        for key, ref in (("head", branch), ("base", base)):
            source = pr[key]
            if source["repo"]["full_name"].lower() != self.repo.lower() or source["ref"] != ref:
                raise RuntimeError("GitHub returned a PR for a different head or base branch")
        if pr["head"]["sha"] != sha:
            raise RuntimeError("GitHub PR head does not match the tested candidate")
        self._check_pr_link(pr["number"], pr["html_url"])
        return {"number": pr["number"], "url": pr["html_url"], "sha": sha}

    def _check_pr_link(self, number, url):
        _issue_number(number)
        if not isinstance(url, str) or url.lower() != f"https://github.com/{self.repo}/pull/{number}".lower():
            raise ValueError("Unexpected GitHub pull request URL")

    def publish(self, *, workspace, start_sha, sha, issue, base="main", branch=None):
        number = _issue_number(issue["number"])
        issue_branch = f"ai/issue-{number}"
        if branch is not None and branch != issue_branch:
            raise ValueError("Publication is restricted to the issue branch")
        branch = issue_branch
        self._check_base(workspace, base)
        if base == branch:
            raise ValueError("The base and issue branch must differ")
        _require_ancestor(workspace, start_sha, sha)

        ref = f"refs/heads/{branch}"
        remote_sha = self._remote_head(workspace, ref)
        if remote_sha not in {None, sha, start_sha}:
            raise RuntimeError("The remote branch changed since the workspace was prepared")
        if remote_sha != sha:
            self._remote_git(workspace, "push", self._remote_url, f"{sha}:{ref}")
        if self._remote_head(workspace, ref) != sha:
            raise RuntimeError("Remote branch head does not match the tested candidate")

        endpoint = f"repos/{self.repo}/pulls"
        matches = self._api("GET", endpoint, {"state": "open", "head": f"{self.owner}:{branch}", "base": base})
        title = f"Issue #{number}: {' '.join(issue['title'].split())}"[:240]
        body = f"Closes #{number}\n\nLocal tests and automated review passed for commit `{sha}`."
        fields = {"title": title, "body": body}
        if len(matches) > 1:
            raise RuntimeError("Multiple open pull requests matched the issue branch")
        if matches:
            current = self._pr_result(matches[0], branch, base, sha)
            updated = self._api("PATCH", f"{endpoint}/{current['number']}", fields)
            return self._pr_result(updated, branch, base, sha)
        if self.publish_pr is not None:
            created = self.publish_pr(self.repo, branch, base, title, body, number, sha)
            self._check_pr_link(created["number"], created["url"])
            confirmed = self._api("GET", f"{endpoint}/{created['number']}")
            return self._pr_result(confirmed, branch, base, sha)
        created = self._api("POST", endpoint, {**fields, "head": branch, "base": base})
        return self._pr_result(created, branch, base, sha)
