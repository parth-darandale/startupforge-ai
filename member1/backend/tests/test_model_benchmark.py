"""Check benchmark measurement/reporting behavior without loading model weights."""
import importlib.util
import json
from pathlib import Path
import sys

import httpx
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "benchmark_models.py"
spec = importlib.util.spec_from_file_location("model_benchmark", SCRIPT)
benchmark = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = benchmark
spec.loader.exec_module(benchmark)


def case(name):
    return next(item for item in benchmark.make_cases() if item.case_id == name)


def test_cases_are_identical_independent_fixtures_and_bounded():
    assert len(benchmark.make_cases()) == 7
    assert len(benchmark.make_cases(True)) == 3
    for item in benchmark.make_cases():
        messages = benchmark.build_messages(item.task, item.payload, item.contract.model_json_schema())
        assert sum(len(message["content"]) for message in messages) < 12000
    assert case("critic_known_errors").payload["initial_analysis"] == benchmark.FLAWED_ANALYSIS
    assert case("revision_known_errors").payload["critic"] == benchmark.KNOWN_CRITIC


def test_reports_schema_errors_without_repair():
    result = benchmark.inspect_output(case("debate_for"), {"message": "A pilot could help"})
    assert result["schema_valid"] is False
    assert len(result["schema_errors"]) == 2


def test_reports_unknown_citations_and_figure_lineage_before_guards():
    result = benchmark.inspect_output(case("debate_for"), {
        "message": "The free board may be enough; test commission willingness instead of asserting 80% will pay.",
        "evidence_ids": ["not_evidence"], "figure_ids": ["fig_interest", "fake_figure"]})
    assert result["unknown_evidence_ids"] == ["not_evidence"]
    assert result["unknown_figure_ids"] == ["fake_figure"]
    assert result["figure_lineage_errors"] == ["fig_interest"]
    assert result["numerical_mentions_forbidden_by_debate_prompt"] == ["80%"]
    assert result["objection_overlap_at_least_two_terms"] is True


def test_supported_uncited_and_numeric_invention_recorded():
    analysis = {"summary": "Unknown demand", "sections": [{"section_id": "a", "heading": "Fit", "claims": [
        {"claim_id": "a", "text": "The market is worth INR 9000.", "kind": "supported", "evidence_ids": []}]}], "gaps": []}
    checks = benchmark.inspect_output(case("sourced_analysis"), analysis)
    assert checks["supported_uncited_claim_ids"] == ["a"]
    assert checks["numeric_mentions_not_in_fixture_or_expected_calculation"] == ["9000"]


def test_known_bad_revision_claims_do_not_pass_as_supported():
    checks = benchmark.inspect_output(case("revision_known_errors"), benchmark.FLAWED_ANALYSIS)
    assert checks["known_false_claims_still_supported"] == ["bad_payment", "bad_profit"]


def test_financial_derived_values_are_checked_and_not_marked_invented():
    data = {"summary": "Fictional exercise only", "sections": [{"section_id": "cash", "heading": "Cash", "claims": [
        {"claim_id": "closing", "text": "5000 - 1200 + 800 = 4600. Net cash change is -400, a decrease of 400.",
         "kind": "supported", "evidence_ids": ["ev_cashbook"]}]}], "gaps": []}
    checks = benchmark.inspect_output(case("financial_arithmetic"), data)
    assert checks["closing_cash_4600_present"] is True
    assert checks["net_decrease_400_present"] is True
    assert checks["numeric_mentions_not_in_fixture_or_expected_calculation"] == []


@pytest.mark.asyncio
async def test_recording_adapter_keeps_invalid_raw_json_and_fixed_options():
    def respond(request):
        body = json.loads(request.content)
        assert body["options"] == {"temperature": 0, "seed": 42, "num_ctx": 4096, "num_predict": 1024}
        assert body["stream"] is False
        return httpx.Response(200, json={"message": {"content": "not-json"}, "done": True, "done_reason": "length"})
    model = benchmark.RecordingOllama("http://localhost:11434", "fake:7b", transport=httpx.MockTransport(respond))
    with pytest.raises(ValueError):
        await model.generate_json("extract", {"idea_text": benchmark.IDEA_TEXT}, benchmark.IdeaSpec.model_json_schema())
    assert model.last_raw_text == "not-json"
    assert model.last_metrics["done_reason"] == "length"


@pytest.mark.asyncio
@pytest.mark.parametrize("capabilities, expected_think", [(["completion", "thinking"], False), (["completion"], None)])
async def test_thinking_control_is_capability_based(capabilities, expected_think):
    def respond(request):
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": capabilities})
        body = json.loads(request.content)
        if expected_think is False:
            assert body["think"] is False
        else:
            assert "think" not in body
        return httpx.Response(200, json={"message": {"content": "{}"}, "done": True,
                                        "prompt_eval_count": 3500})
    model = benchmark.RecordingOllama("http://localhost:11434", "fake:7b", transport=httpx.MockTransport(respond))
    assert await model.inspect_capabilities() == capabilities
    await model.generate_json("extract", {"idea_text": benchmark.IDEA_TEXT}, benchmark.IdeaSpec.model_json_schema())
    assert model.think is expected_think
    assert model.last_metrics["prompt_near_context_capacity"] is True
    assert "possible, not proven" in model.last_metrics["context_capacity_warning"]


def test_checkpoint_writes_real_json_and_replaces_atomically(tmp_path):
    output = tmp_path / "trials" / "comparison.json"
    benchmark.checkpoint(output, {"models": [{"model": "first"}]})
    benchmark.checkpoint(output, {"models": [{"model": "second"}]})
    assert json.loads(output.read_text())["models"][0]["model"] == "second"
    assert not output.with_suffix(".json.tmp").exists()


def test_snapshot_freezes_exact_messages_and_validates_case_inputs(tmp_path, monkeypatch):
    snapshot = tmp_path / "prompts.json"
    benchmark.write_prompt_snapshot(snapshot)
    frozen, digest = benchmark.load_prompt_snapshot(snapshot, benchmark.make_cases())
    assert len(frozen) == 7
    assert len(digest) == 64
    original = frozen["extraction"][0]["content"]
    monkeypatch.setattr(benchmark, "build_messages", lambda *args: [{"role": "system", "content": "CHANGED"}])
    still_frozen, still_digest = benchmark.load_prompt_snapshot(snapshot, benchmark.make_cases(True))
    assert still_frozen["extraction"][0]["content"] == original
    assert still_digest == digest
    modified_case = benchmark.TrialCase("extraction", "extract", {"idea_text": "Different idea contents"}, benchmark.IdeaSpec)
    with pytest.raises(ValueError, match="input/schema"):
        benchmark.load_prompt_snapshot(snapshot, [modified_case])


def test_snapshot_rejects_duplicate_cases_and_modified_embedded_user_payload(tmp_path):
    snapshot = tmp_path / "prompts.json"
    benchmark.write_prompt_snapshot(snapshot)
    data = json.loads(snapshot.read_text())
    data["cases"].append(data["cases"][0])
    benchmark.checkpoint(snapshot, data)
    with pytest.raises(ValueError, match="Duplicate"):
        benchmark.load_prompt_snapshot(snapshot, benchmark.make_cases())
    data["cases"].pop()
    data["cases"][0]["messages"][1]["content"] = "{}"
    benchmark.checkpoint(snapshot, data)
    with pytest.raises(ValueError, match="user data"):
        benchmark.load_prompt_snapshot(snapshot, benchmark.make_cases())


@pytest.mark.asyncio
async def test_adapter_uses_frozen_messages_without_rebuilding_app_prompts(monkeypatch):
    frozen = [{"role": "system", "content": "Frozen original rules"}, {"role": "user", "content": "Frozen user data"}]

    def respond(request):
        assert json.loads(request.content)["messages"] == frozen
        return httpx.Response(200, json={"message": {"content": "{}"}, "done": True})

    model = benchmark.RecordingOllama("http://localhost:11434", "fake:7b", transport=httpx.MockTransport(respond))
    model.frozen_messages = frozen
    monkeypatch.setattr(benchmark, "build_messages", lambda *args: pytest.fail("Frozen protocol must not rebuild messages"))
    assert await model.generate_json("extract", {}, {}) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("use_snapshot", [False, True])
async def test_runner_checkpoints_failures_and_unloads_before_next_model(monkeypatch, tmp_path, use_snapshot):
    events = []

    class FakeOllama:
        def __init__(self, base_url, model, **kwargs):
            self.model = model
            self.last_raw_text = None
            self.last_metrics = {}
            events.append((model, "create"))

        async def generate_json(self, task, payload, schema):
            assert (self.frozen_messages is not None) is use_snapshot
            events.append((self.model, task))
            self.last_raw_text = "invalid raw text"
            self.last_metrics = {"done_reason": "length"}
            raise ValueError("Invalid JSON")

        async def inspect_capabilities(self):
            self.think = False
            return ["completion", "thinking"]

        async def unload(self):
            events.append((self.model, "unload"))

    monkeypatch.setattr(benchmark, "RecordingOllama", FakeOllama)
    output = tmp_path / "report.json"
    args = benchmark.parser().parse_args(["--models", "first", "second", "--quick-smoke", "--output", str(output)])
    if use_snapshot:
        args.prompt_snapshot = tmp_path / "protocol.json"
        benchmark.write_prompt_snapshot(args.prompt_snapshot)
    report = await benchmark.run_trials(args)
    assert report["status"] == "completed"
    assert events.index(("first", "unload")) < events.index(("second", "create"))
    saved = json.loads(output.read_text())
    assert len(saved["models"][0]["tasks"]) == 3
    first_task = saved["models"][0]["tasks"][0]
    assert first_task["raw_text"] == "invalid raw text"
    assert first_task["error"]["type"] == "ValueError"
    assert first_task["output_truncated"] is True
    assert saved["models"][0]["capabilities"] == ["completion", "thinking"]
    assert saved["models"][0]["think_setting"] is False
    if use_snapshot:
        assert saved["config"]["prompt_snapshot_sha256"] == benchmark.hashlib.sha256(args.prompt_snapshot.read_bytes()).hexdigest()
    else:
        assert saved["config"]["prompt_snapshot_sha256"] is None


@pytest.mark.asyncio
async def test_runner_stops_when_model_unload_fails(monkeypatch, tmp_path):
    created = []

    class FakeOllama:
        def __init__(self, base_url, model, **kwargs):
            created.append(model)
            self.last_raw_text = None
            self.last_metrics = {}

        async def generate_json(self, task, payload, schema):
            raise TimeoutError("Test timeout")

        async def inspect_capabilities(self):
            self.think = None
            return ["completion"]

        async def unload(self):
            raise httpx.ConnectError("Test unload failure")

    monkeypatch.setattr(benchmark, "RecordingOllama", FakeOllama)
    args = benchmark.parser().parse_args(["--models", "first", "second", "--quick-smoke",
                                         "--output", str(tmp_path / "report.json")])
    report = await benchmark.run_trials(args)
    assert created == ["first"]
    assert report["status"] == "stopped_unload_failed"
    assert report["models"][0]["unloaded"] is False
