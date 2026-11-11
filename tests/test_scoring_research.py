"""Known-answer policy checks. They establish correctness, not predictive usefulness."""

import copy
import json
import subprocess
from datetime import timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from gtm.agent import agent_schema, run_research, validate_batch
from gtm.cli import app
from gtm.models import utcnow
from gtm.research import (
    AccountResearch,
    Evidence,
    Finding,
    ResearchStore,
    profile_hash,
    research_queue,
)
from gtm.scoring import score, score_rows
from gtm.workflows import apply_reports, enrichment_queue, portfolio_metrics


@pytest.fixture
def policy(system):
    return copy.deepcopy(system[0].rules["scoring"])


def report(policy, **changes):
    now = utcnow() - timedelta(seconds=1)
    evidence = Evidence(
        id="official",
        url="https://synthetic.example/careers",
        title="Synthetic Careers",
        excerpt="Build marketing automation and API integrations. This role is open worldwide.",
        source_type="official",
        retrieved_at=now,
    )
    findings = {
        key: Finding(
            value=value,
            evidence_ids=["official"],
            rationale="Synthetic independently declared test evidence",
            eligibility_scope=scope,
        )
        for key, value, scope in (
            ("role_fit", "strong", "unknown"),
            ("hiring", "strong", "unknown"),
            ("contracting", "unknown", "unknown"),
            ("full_time_remote", "eligible", "worldwide"),
            ("contract_remote", "uncertain", "unknown"),
        )
    }
    payload = dict(
        domain="synthetic.example",
        company_name="Synthetic Co",
        profile_hash=profile_hash(policy),
        researched_at=now,
        researcher="synthetic",
        status="partial",
        identity_confirmed=True,
        identity_evidence_ids=["official"],
        evidence=[evidence],
        findings=findings,
        next_action="Review exact technical marketing role before contacting",
        gaps=["Contract demand unknown"],
    )
    payload.update(changes)
    return AccountResearch(**payload)


def person(**changes):
    return {
        "id": "synthetic-person",
        "full_name": "Synthetic Person",
        "company_name": "Synthetic Co",
        "domain": "synthetic.example",
        "job_title": "Founder",
        "connection_status": "discovered",
        "status_changed_at": (utcnow() - timedelta(days=1)).isoformat(),
        "do_not_contact": "false",
        "activity_history": "[]",
        "email": "",
        "linkedin_url": "",
        **changes,
    }


def test_known_answer_technical_role_worldwide_tier1(policy):
    result = score(person(), report(policy), policy)
    assert result["account_fit_score"] == 40 and result["lead_fit_score"] == 10
    assert result["hiring_intent_score"] == 30 and result["engagement_score"] == 0
    assert result["full_time_priority"] == 80
    assert result["full_time_tier"] == "tier_1" and result["tier"] == "tier_1"
    assert result["contract_priority"] == "" and result["contract_tier"] == ""


def test_unknown_data_is_not_tier3_or_zero(policy):
    result = score(person(), None, policy)
    assert result["tier"] == "" and result["account_fit_score"] == ""
    assert result["score_status"] == "needs_research"


def test_strategy_heavy_counterexample_not_promoted(policy):
    r = report(policy)
    r.findings["role_fit"].value = "weak"
    result = score(person(connection_status="replied"), r, policy)
    assert result["tier"] == "tier_3" and result["account_fit_score"] == 0


def test_unknown_location_prevents_false_global_remote_claim(policy):
    r = report(policy)
    r.findings["full_time_remote"].eligibility_scope = "candidate_location"
    result = score(person(), r, policy)
    assert result["full_time_tier"] == "tier_2"
    assert "full_time_remote=uncertain" in result["score_reasons"]


def test_contract_eligibility_does_not_grant_employee_eligibility(policy):
    policy["profile"]["work_location"] = "India"
    r = report(policy)
    r.findings["full_time_remote"] = Finding(
        value="ineligible",
        evidence_ids=["official"],
        rationale="Synthetic employee role is limited to the US",
        eligibility_scope="restricted",
    )
    r.findings["contract_remote"] = Finding(
        value="eligible",
        evidence_ids=["official"],
        rationale="Synthetic contractor opening is worldwide",
        eligibility_scope="worldwide",
    )
    r.findings["contracting"].value = "strong"
    result = score(person(), r, policy)
    assert result["full_time_tier"] == "tier_3"
    assert result["contract_tier"] == "tier_1" and result["tier"] == "tier_1"


def test_engagement_never_changes_account_fit_and_moderate_intent_cannot_be_tier1(policy):
    r = report(policy)
    r.findings["hiring"].value = "moderate"
    before = score(person(), r, policy)
    after = score(person(connection_status="replied"), r, policy)
    assert before["account_fit_score"] == after["account_fit_score"] == 40
    assert after["full_time_priority"] == 80
    assert after["tier"] == "tier_2"


@pytest.mark.parametrize("age,status", [(31, "stale_research"), (-2, "future_research")])
def test_stale_and_future_research_not_scored(policy, age, status):
    r = report(policy, researched_at=utcnow() - timedelta(days=age))
    assert score(person(), r, policy)["score_status"] == status
    assert score(person(), r, policy)["tier"] == ""


def test_fresh_receipt_does_not_make_old_evidence_fresh(policy):
    r = report(policy)
    r.evidence[0].retrieved_at = utcnow() - timedelta(days=40)
    result = score(person(), r, policy)
    assert result["score_status"] == "stale_identity" and result["tier"] == ""


def test_third_party_claims_do_not_establish_current_hiring(policy):
    r = report(policy)
    r.evidence.append(
        Evidence(
            id="third",
            url="https://news.example/story",
            title="News",
            excerpt="Synthetic historical hiring claim",
            source_type="third_party",
            retrieved_at=utcnow() - timedelta(seconds=1),
        )
    )
    r.findings["hiring"].evidence_ids = ["third"]
    result = score(person(), r, policy)
    assert result["hiring_intent_score"] == "" and result["tier"] == ""


def test_profile_change_requires_new_research(policy):
    r = report(policy)
    policy["profile"]["target_roles"] = ["Different Target"]
    assert score(person(), r, policy)["score_status"] == "profile_changed"


def test_evidence_schema_rejects_unsupported_uncited_wrong_identity(policy):
    payload = report(policy).model_dump(mode="json")
    payload["findings"]["hiring"]["evidence_ids"] = []
    with pytest.raises(ValidationError):
        AccountResearch.model_validate(payload)
    payload = report(policy).model_dump(mode="json")
    payload["evidence"][0]["url"] = "https://unrelated.example/careers"
    with pytest.raises(ValidationError):
        AccountResearch.model_validate(payload)


def test_batch_parity_order_invariance_and_all_rows_preserved(policy, tmp_path):
    research = ResearchStore(tmp_path / "research")
    research.save(report(policy))
    people = [person(), person(id="other", domain="unknown.example")]
    single = score_rows(people[:1], research, policy)
    batch = score_rows(people, research, policy)
    reversed_batch = score_rows(list(reversed(people)), research, policy)
    assert single[0] == batch[0] == reversed_batch[1]
    assert len(batch) == 2 and batch[1]["tier"] == ""


def test_dnc_excluded_even_with_perfect_research(policy):
    result = score(person(do_not_contact="true"), report(policy), policy)
    assert result["score_status"] == "excluded" and result["tier"] == ""


def seed(system):
    from gtm.models import Prospect

    service, settings = system
    service.research = ResearchStore(settings.data_directory / "research")
    rows = []
    service.merge(
        rows,
        Prospect(
            source="grok",
            full_name="Synthetic Person",
            company_name="Synthetic Co",
            domain="synthetic.example",
            job_title="Founder",
            linkedin_url="linkedin.com/in/synthetic-person",
        ),
        {"key": "seed", "data": {}},
    )
    service.store.write(rows)
    return service, settings, rows[0]["id"]


def test_apply_dry_run_no_writes_then_idempotent_receipts(system, policy):
    service, settings, _ = seed(system)
    r = report(policy)
    before = settings.tracker.read_bytes()
    assert apply_reports(service, [r], dry_run=True)["accounts"] == 1
    assert settings.tracker.read_bytes() == before
    assert not service.research.path(r.domain).exists()
    apply_reports(service, [r])
    apply_reports(service, [r])
    assert (
        len(list((settings.data_directory / "research/history/synthetic.example").glob("*.json")))
        == 1
    )
    assert service.store.load()[0]["tier"] == "tier_1"
    assert service.store.load()[0]["connection_status"] == "discovered"


def test_apply_bad_domain_profile_future_refuses_writes(system, policy):
    service, settings, _ = seed(system)
    before = settings.tracker.read_bytes()
    r = report(policy)
    r.domain = "unknown.example"
    with pytest.raises(ValueError, match="outside"):
        apply_reports(service, [r])
    r = report(policy, researched_at=utcnow() + timedelta(days=1))
    with pytest.raises(ValueError, match="Future"):
        apply_reports(service, [r])
    assert settings.tracker.read_bytes() == before


def test_new_manual_events_and_duplicate_event_id(system, policy):
    service, _, pid = seed(system)
    apply_reports(service, [report(policy)])
    service.record_event(pid, "connection_sent", event_id="sent")
    service.record_event(pid, "connection_sent", event_id="sent")
    service.record_event(pid, "connection_accepted", event_id="accepted")
    assert service.store.load()[0]["engagement_score"] == "10"
    service.record_event(pid, "outreach_sent", event_id="message")
    assert service.store.load()[0]["next_followup_at"]
    service.record_event(pid, "positive_reply", event_id="reply")
    row = service.store.load()[0]
    assert row["connection_status"] == "replied" and row["next_followup_at"] == ""
    assert row["engagement_score"] == "20" and row["account_fit_score"] == "40"
    assert len(json.loads(row["activity_history"])) == 4
    assert service.actions()[0]["priority"] == 1
    with pytest.raises(ValueError, match="different event"):
        service.record_event(pid, "note", event_id="reply")


def test_targeted_enrichment_and_dnc_event_guard(system, policy):
    service, _, pid = seed(system)
    apply_reports(service, [report(policy)])
    assert enrichment_queue(service)[0]["missing"] == ["email"]
    service.record_event(pid, "do_not_contact", notes="Requested no further outreach")
    assert enrichment_queue(service) == [] and service.actions() == []
    with pytest.raises(ValueError, match="Do-not-contact"):
        service.record_event(pid, "outreach_sent")


def test_portfolio_report_has_no_private_prospect_values(system, policy):
    service, _, _ = seed(system)
    apply_reports(service, [report(policy)])
    encoded = json.dumps(portfolio_metrics(service))
    assert "synthetic.example" not in encoded and "Synthetic Person" not in encoded
    assert "not_established" in encoded


def test_agent_bounded_output_schema_and_validation(policy, tmp_path):
    requested = [
        {
            "domain": "synthetic.example",
            "company_names": ["Synthetic Co"],
            "job_titles": ["Founder"],
            "source_hints": [],
        }
    ]
    payload = {"accounts": [report(policy).model_dump(mode="json")]}

    def fake_runner(args, **kwargs):
        assert "--ignore-user-config" in args and args[args.index("--sandbox") + 1] == "read-only"
        assert "--dangerously-bypass-approvals-and-sandbox" not in args
        assert kwargs["stdout"] == subprocess.DEVNULL
        assert "technical execution" in kwargs["input"]
        result_path = args[args.index("--output-last-message") + 1]
        from pathlib import Path

        Path(result_path).write_text(json.dumps(payload))
        return SimpleNamespace(returncode=0)

    batch, receipt = run_research(
        requested, policy, tmp_path, runner=fake_runner, executable="/fake/codex"
    )
    assert len(batch.accounts) == 1 and (receipt / "status.json").is_file()
    schema = agent_schema()
    assert (
        schema["$defs"]["AccountResearch"]["properties"]["findings"]["additionalProperties"]
        is False
    )
    with pytest.raises(ValueError, match="exactly"):
        validate_batch(payload, [], policy)


def test_agent_timeout_does_not_apply_or_silently_succeed(policy, tmp_path):
    requested = [
        {
            "domain": "synthetic.example",
            "company_names": ["Synthetic Co"],
            "job_titles": [],
            "source_hints": [],
        }
    ]

    def fail(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 300)

    with pytest.raises(RuntimeError, match="timed out"):
        run_research(requested, policy, tmp_path, runner=fail, executable="/fake/codex")
    assert not (tmp_path / "accounts").exists()


def test_research_queue_deduplicates_accounts_and_keeps_import_claims_unverified(system):
    service, _, _ = seed(system)
    rows = service.store.load()
    rows[0]["signal"] = "Synthetic funding claim"
    queue = research_queue(rows + rows, service.research, service.rules["scoring"])
    assert len(queue) == 1
    assert queue[0]["source_hints"][0]["status"] == "unverified_import_hint"


def test_new_commands_help_and_technical_profile(system):
    runner = CliRunner()
    for command in (
        ["score", "--help"],
        ["research", "run", "--help"],
        ["event", "--help"],
        ["portfolio-report", "--help"],
    ):
        result = runner.invoke(app, command)
        assert result.exit_code == 0, result.output
    profile = system[0].rules["scoring"]["profile"]
    assert profile["target_roles"] == ["GTM Engineer", "Growth Engineer", "Marketing Engineer"]
    assert profile["strategy_preference"] == "technical_execution_over_business_strategy"


def test_old_published_intent_is_not_refreshed_by_retrieval(policy):
    r = report(policy)
    r.evidence[0].published_at = utcnow() - timedelta(days=90)
    result = score(person(), r, policy)
    assert result["hiring_intent_score"] == "" and result["tier"] == ""


def test_exact_threshold_and_stale_engagement(policy):
    r = report(policy)
    result = score(person(job_title="Other"), r, policy)
    assert result["full_time_priority"] == 70 and result["tier"] == "tier_1"
    result = score(
        person(
            connection_status="replied",
            status_changed_at=(utcnow() - timedelta(days=60)).isoformat(),
        ),
        r,
        policy,
    )
    assert result["engagement_score"] == 0


def test_research_failed_retained_as_unknown_not_negative(policy):
    r = report(policy, status="failed")
    result = score(person(), r, policy)
    assert result["score_status"] == "research_failed" and result["tier"] == ""


def test_unknown_country_does_not_make_regional_role_ineligible(policy):
    r = report(policy)
    r.findings["full_time_remote"] = Finding(
        value="ineligible",
        evidence_ids=["official"],
        rationale="Synthetic role is US-only, candidate country unknown",
        eligibility_scope="restricted",
    )
    assert score(person(), r, policy)["full_time_tier"] == "tier_2"


def test_portfolio_unrecognized_event_types_do_not_leak_private_values(system):
    service, _, pid = seed(system)
    service.update(pid, activity="private-person@synthetic.example", notes="Private prospect notes")
    encoded = json.dumps(portfolio_metrics(service))
    assert "private-person" not in encoded and "Private prospect notes" not in encoded
    assert "other_manual_activity" in encoded


def test_outreach_rates_use_actual_denominators(system):
    from gtm.workflows import outreach_summary

    service, _, pid = seed(system)
    assert outreach_summary(service.store.load())["connection_acceptance_rate"] is None
    service.record_event(pid, "connection_sent")
    service.record_event(pid, "connection_accepted")
    service.record_event(pid, "outreach_sent")
    service.record_event(pid, "reply")
    result = outreach_summary(service.store.load())
    assert result["connection_acceptance_rate"] == 1.0
    assert result["positive_reply_rate"] == 0.0
    service.record_event(pid, "positive_reply")
    assert outreach_summary(service.store.load())["positive_reply_rate"] == 1.0


def test_targeted_agent_dry_run_does_not_start_an_agent(system, monkeypatch):
    from contextlib import contextmanager

    import gtm.cli as cli

    service, settings, _ = seed(system)

    @contextmanager
    def runtime(**kwargs):
        yield service, settings

    monkeypatch.setattr(cli, "runtime", runtime)
    monkeypatch.setattr(
        cli, "run_research", lambda *a, **k: pytest.fail("Dry run started an agent")
    )
    before = settings.tracker.read_bytes()
    result = CliRunner().invoke(
        app, ["research", "run", "--domain", "synthetic.example", "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["domains"] == ["synthetic.example"]
    assert settings.tracker.read_bytes() == before


def test_invalid_policy_is_rejected_and_controlled_bad_expectation_fails(policy):
    from gtm.scoring import validate_policy

    broken = copy.deepcopy(policy)
    broken["weights"]["account_fit"]["strong"] = 500
    with pytest.raises(ValueError, match="total 100"):
        validate_policy(broken)
    result = score(person(), report(policy), policy)
    # An intentionally wrong expected answer must fail; this is an evaluation sensitivity check.
    with pytest.raises(AssertionError):
        assert result["full_time_priority"] == 99


def test_legacy_agent_unknown_remote_is_uncertainty_not_eligibility(policy):
    payload = report(policy).model_dump(mode="json")
    payload["findings"]["contract_remote"]["value"] = "unknown"
    payload["findings"]["full_time_remote"]["value"] = "unknown"
    parsed = AccountResearch.model_validate(payload)
    assert parsed.findings["contract_remote"].value == "uncertain"
    assert parsed.findings["full_time_remote"].value == "uncertain"
    assert payload["findings"]["contract_remote"]["value"] == "unknown"
    assert score(person(), parsed, policy)["tier"] == "tier_2"
    payload["findings"]["hiring"]["value"] = "uncertain"
    with pytest.raises(ValidationError):
        AccountResearch.model_validate(payload)


def test_each_agent_finding_value_has_its_own_enum():
    from gtm.research import ALLOWED

    findings = agent_schema()["$defs"]["AccountResearch"]["properties"]["findings"]
    for key, allowed in ALLOWED.items():
        assert set(findings["properties"][key]["properties"]["value"]["enum"]) == allowed
        assert findings["properties"][key]["additionalProperties"] is False
    assert "unknown" not in findings["properties"]["contract_remote"]["properties"]["value"]["enum"]


def test_invalid_agent_output_has_actionable_receipt_without_rejected_values(policy, tmp_path):
    requested = [
        {
            "domain": "synthetic.example",
            "company_names": ["Synthetic Co"],
            "job_titles": [],
            "source_hints": [],
        }
    ]
    payload = {"accounts": [report(policy).model_dump(mode="json")]}
    payload["accounts"][0]["findings"]["hiring"]["value"] = "private-invalid-token"

    def fake_runner(args, **kwargs):
        from pathlib import Path

        Path(args[args.index("--output-last-message") + 1]).write_text(json.dumps(payload))
        return SimpleNamespace(returncode=0)

    with pytest.raises(RuntimeError) as caught:
        run_research(requested, policy, tmp_path, runner=fake_runner, executable="/fake/codex")
    assert "accounts.0.hiring: allowed values:" in str(caught.value)
    assert "private-invalid-token" not in str(caught.value)
    assert "research resume" in str(caught.value)
    receipt = next((tmp_path / "runs").glob("*/status.json"))
    status = json.loads(receipt.read_text())
    assert status["status"] == "invalid_output" and status["results_applied"] is False
    assert "private-invalid-token" not in receipt.read_text()
    assert (receipt.parent / "result.json").exists()


def test_resume_saved_legacy_output_never_calls_an_agent(system, policy, monkeypatch):
    from contextlib import contextmanager

    import gtm.cli as cli

    service, settings, _ = seed(system)
    run_id = "c" * 32
    directory = settings.data_directory / "research/runs" / run_id
    directory.mkdir(parents=True)
    (directory / "request.json").write_text(
        json.dumps({"domains": ["synthetic.example"], "profile_hash": profile_hash(policy)})
    )
    payload = {"accounts": [report(policy).model_dump(mode="json")]}
    payload["accounts"][0]["findings"]["contract_remote"]["value"] = "unknown"
    original = json.dumps(payload)
    (directory / "result.json").write_text(original)

    @contextmanager
    def runtime(**kwargs):
        yield service, settings

    monkeypatch.setattr(cli, "runtime", runtime)
    monkeypatch.setattr(
        cli, "run_research", lambda *a, **k: pytest.fail("Resume started a new agent")
    )
    before = settings.tracker.read_bytes()
    runner = CliRunner()
    preview = runner.invoke(app, ["research", "resume", run_id, "--dry-run"])
    assert preview.exit_code == 0, preview.output
    assert settings.tracker.read_bytes() == before
    assert not service.research.path("synthetic.example").exists()
    for _ in range(2):
        result = runner.invoke(app, ["research", "resume", run_id])
        assert result.exit_code == 0, result.output
    assert service.store.load()[0]["tier"] == "tier_1"
    assert service.store.load()[0]["connection_status"] == "discovered"
    assert (directory / "result.json").read_text() == original
    assert json.loads((directory / "status.json").read_text())["resumed"] is True
    assert (
        len(list((settings.data_directory / "research/history/synthetic.example").glob("*.json")))
        == 1
    )


def test_research_validation_cli_prints_reason_not_private_values(
    system, policy, monkeypatch, tmp_path
):
    from contextlib import contextmanager

    import gtm.cli as cli

    service, settings, _ = seed(system)

    @contextmanager
    def runtime(**kwargs):
        yield service, settings

    monkeypatch.setattr(cli, "runtime", runtime)
    payload = {"accounts": [report(policy).model_dump(mode="json")]}
    payload["accounts"][0]["findings"]["hiring"]["value"] = "private-invalid-token"
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(payload))
    result = CliRunner().invoke(app, ["research", "apply", str(path), "--dry-run"])
    assert result.exit_code == 1
    assert "accounts.0.hiring: allowed values:" in result.output
    assert "private-invalid-token" not in result.output


def test_resume_rejects_path_traversal():
    result = CliRunner().invoke(app, ["research", "resume", "../../secret"])
    assert result.exit_code == 1 and "32-character run ID" in result.output
