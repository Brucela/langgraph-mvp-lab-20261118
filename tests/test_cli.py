from issue_to_pr.cli import RepairDrill, intake_issue


def test_intake_rejects_untrusted_actions_workflow(monkeypatch):
    def gh_json(repo, *args):
        return {
            "status": "completed",
            "conclusion": "success",
            "event": "issues",
            "path": ".github/workflows/untrusted.yml",
            "head_branch": "main",
        }

    monkeypatch.setattr("issue_to_pr.cli.gh_json", gh_json)
    try:
        intake_issue("owner/repo", "owner", 123)
    except ValueError as error:
        assert "trusted" in str(error)
    else:
        raise AssertionError("the intake artifact came from a different workflow")


def test_repair_drill_only_changes_initial_candidate():
    class Developer:
        def develop(self, *args):
            return {"summary": "Implement", "files": {"demo/calculator.py": "original\n"}}

    roles = RepairDrill(Developer())
    first = roles.develop({}, {}, {}, "")
    assert first["files"]["demo/calculator.py"].startswith("original\n")
    assert "return price_cents" in first["files"]["demo/calculator.py"]
    retry = roles.develop({}, {}, {}, "The acceptance test failed")
    assert retry["files"]["demo/calculator.py"] == "original\n"
