"""Run an Issue from a verified event or a signed webhook job."""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

LAB_REPO = "Brucela/langgraph-mvp-lab-20261118"
DEMO_FILE = "demo/calculator.py"


def command(argv, *, cwd=None, payload=None, timeout=30):
    executed = subprocess.run(
        argv, cwd=cwd, input=payload, timeout=timeout, text=True, capture_output=True,
        check=False,
    )
    if executed.returncode:
        raise RuntimeError(f"{argv[0]} {argv[1]} failed: {executed.stderr.strip()}")
    return executed.stdout.strip()


def gh_json(repo, *args):
    return json.loads(command(["gh", "api", "-X", "GET", f"repos/{repo}/{'/'.join(args)}"]))


def git(path, *args):
    return command(
        ["git", "-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential", *args],
        cwd=path,
    )


def intake_issue(repo, actor, run_id):
    from .intake import parse_event

    run = gh_json(repo, "actions", "runs", str(run_id))
    if (
        run.get("status") != "completed"
        or run.get("conclusion") != "success"
        or run.get("event") != "issues"
        or run.get("path") != ".github/workflows/intake.yml"
        or run.get("head_branch") != "main"
    ):
        raise ValueError("The Actions intake run must be completed and from the trusted main workflow")
    with TemporaryDirectory(prefix="mvp-event-") as directory:
        command(["gh", "run", "download", str(run_id), "-R", repo, "-n", "issue-event", "-D", directory])
        event = json.loads((Path(directory) / "issue-event.json").read_text(encoding="utf-8"))
    issue = parse_event(event, repo, actor)
    if issue is None:
        raise ValueError("The recorded event was outside the configured issue, actor or label scope")
    return issue


class RepairDrill:
    """Introduce a controlled first-round defect in the calculator lab."""

    def __init__(self, roles):
        self.roles = roles

    def __getattr__(self, name):
        return getattr(self.roles, name)

    def develop(self, issue, requirements, files, feedback):
        output = self.roles.develop(issue, requirements, files, feedback)
        if feedback or DEMO_FILE not in output["files"]:
            return output
        replacements = dict(output["files"])
        replacements[DEMO_FILE] += (
            "\n\n# Repair drill: temporary first-round regression.\n"
            "def apply_discount(price_cents: int, discount_percent: int) -> int:\n"
            "    return price_cents\n"
        )
        return {**output, "files": replacements, "summary": output["summary"] + " (fail-first drill)"}


def actions_publish(repo, branch, base, title, body, issue, sha):
    command(
        ["gh", "workflow", "run", "publish.yml", "--repo", repo, "--ref", base, "--json"],
        payload=json.dumps({"issue": str(issue), "sha": sha, "title": title, "body": body}),
    )
    deadline = time.monotonic() + 150
    while time.monotonic() < deadline:
        rows = json.loads(command(
            ["gh", "pr", "list", "--repo", repo, "--state", "open", "--head", branch,
             "--base", base, "--json", "number,url,headRefOid"],
        ))
        if rows:
            pr = rows[0]
            if pr["headRefOid"] != sha:
                raise RuntimeError("PR head differs from the tested candidate")
            return {"number": pr["number"], "url": pr["url"], "sha": sha}
        time.sleep(3)
    raise TimeoutError("The Actions publisher has not returned a PR. Inspect the publish workflow run.")


@contextmanager
def checkpointer(data, dsn):
    if dsn:
        from langgraph.checkpoint.postgres import PostgresSaver

        with PostgresSaver.from_conn_string(dsn) as saver:
            saver.setup()
            yield saver
    else:
        from langgraph.checkpoint.sqlite import SqliteSaver

        with SqliteSaver.from_conn_string(str(data / "checkpoints.sqlite")) as saver:
            yield saver


def prepare_workspace(repo, workspace, branch):
    if not workspace.exists():
        git(workspace.parent, "clone", "--no-tags", f"https://github.com/{repo}.git", str(workspace))
    if git(workspace, "remote", "get-url", "origin") != f"https://github.com/{repo}.git":
        raise RuntimeError("Unexpected origin in the task workspace")
    git(workspace, "fetch", "--no-tags", "origin")
    remote_main = git(workspace, "rev-parse", "origin/main")
    branches = git(workspace, "branch", "-r", "--list", f"origin/{branch}")
    start = git(workspace, "rev-parse", f"origin/{branch}") if branches else remote_main
    base = git(workspace, "merge-base", remote_main, start)
    git(workspace, "checkout", "--detach", start)
    return base, start


def run_issue(repo, data, issue_number, run_id, dsn, *, lab_bot=False, repair_drill=False):
    from .graph import build_graph
    from .roles import from_env
    from .services import GitHub, Services

    roles = from_env()
    if repair_drill:
        roles = RepairDrill(roles)
    github = GitHub(repo, publish_pr=actions_publish if lab_bot else None)
    key = sha256(f"{repo}:{issue_number}:{run_id}".encode()).hexdigest()[:24]
    data.mkdir(parents=True, exist_ok=True)
    workspaces = data / "workspaces"
    workspaces.mkdir(exist_ok=True)
    workspace = workspaces / key
    branch = f"ai/issue-{issue_number}"
    config = {"configurable": {"thread_id": f"{repo}:{issue_number}:{key}"}}
    proof = data / f"{key}.json"

    services = Services(
        roles,
        github=github,
        allowed_paths=(DEMO_FILE,),
        test_argv=(sys.executable, "-m", "unittest", "discover", "-s", "demo", "-v"),
        test_env={"MVP_REQUIRE_DISCOUNT": "1"},
    )
    with checkpointer(data, dsn) as saver:
        graph = build_graph(services, saver)
        snapshot = graph.get_state(config)
        if snapshot.values:
            if not (workspace / ".git").is_dir() and snapshot.next:
                raise RuntimeError("Persisted worktree missing; restore it before resuming this run")
            result = graph.invoke(None, config, durability="sync") if snapshot.next else snapshot.values
        else:
            issue = github.fetch_issue(issue_number)
            base, start = prepare_workspace(repo, workspace, branch)
            result = graph.invoke(
                {"issue": issue, "workspace": str(workspace), "base_sha": base, "start_sha": start,
                 "base": "main", "branch": branch, "history": [], "repair_count": 0},
                config, durability="sync",
            )
    proof.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"status": result["status"], "pr": result.get("pr"), "evidence": str(proof)}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=os.getenv("MVP_REPO", LAB_REPO))
    parser.add_argument("--actor", default=os.getenv("MVP_ALLOWED_ACTOR"))
    parser.add_argument("--data", type=Path, default=Path(".local"))
    subcommands = parser.add_subparsers(dest="mode", required=True)
    once = subcommands.add_parser("run")
    origin = once.add_mutually_exclusive_group(required=True)
    origin.add_argument("--issue", type=int)
    origin.add_argument("--intake-run", type=int)
    once.add_argument("--attempt", default="manual-1")
    once.add_argument("--lab-bot", action="store_true")
    once.add_argument("--repair-drill", action="store_true")
    webhook = subcommands.add_parser("webhook")
    webhook.add_argument("--host", default="127.0.0.1")
    webhook.add_argument("--port", type=int, default=8080)
    subcommands.add_parser("worker")
    args = parser.parse_args()

    if not re.fullmatch(r"[a-zA-Z0-9-]+/[a-zA-Z0-9_.-]+", args.repo):
        parser.error("--repo must be a GitHub owner/repository")
    actor = args.actor or args.repo.split("/", 1)[0]
    dsn = os.getenv("MVP_DATABASE_URL")
    if args.mode == "run":
        if (args.lab_bot or args.repair_drill) and args.repo != LAB_REPO:
            parser.error("The lab publisher and fail-first probe are scoped to the private lab repository")
        if args.intake_run:
            issue = intake_issue(args.repo, actor, args.intake_run)
            run_id = f"actions:{args.intake_run}"
        else:
            issue, run_id = args.issue, args.attempt
        if issue is None or issue <= 0:
            parser.error("Issue number must be positive")
        run_issue(args.repo, args.data.resolve(), issue, run_id, dsn,
                  lab_bot=args.lab_bot, repair_drill=args.repair_drill)
        return

    from .intake import PostgresQueue, serve

    if not dsn:
        parser.error("MVP_DATABASE_URL is required for the webhook and worker")
    queue = PostgresQueue(dsn)
    queue.setup()
    if args.mode == "webhook":
        secret = os.getenv("MVP_WEBHOOK_SECRET")
        if not secret:
            parser.error("MVP_WEBHOOK_SECRET is required")
        serve(queue, args.repo, actor, secret, args.host, args.port)
    else:
        queue.recover()
        while True:
            job = queue.claim()
            if job is None:
                time.sleep(2)
                continue
            job_id, issue_number = job
            run_issue(args.repo, args.data.resolve(), issue_number, f"job:{job_id}", dsn)
            queue.finish(job_id)


if __name__ == "__main__":
    main()
