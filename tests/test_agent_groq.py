"""Groq provider for the brain: request translation, reply parsing, errors (no network)."""
import json

from papertrader.agent import brain
from papertrader.agent.brain import BrainConfig, brain_finding, review_brief
from papertrader.agent.groq_client import GroqClient, build_body
from papertrader.agent.types import Finding


class FakeGroq(GroqClient):
    def __init__(self, reply=None, finish="stop", exc=None):
        super().__init__(key="test-key")
        self.reply, self.finish, self.exc, self.bodies = reply, finish, exc, []

    def _post(self, body):
        self.bodies.append(body)
        if self.exc:
            raise self.exc
        return {"model": body["model"], "usage": {"prompt_tokens": 900, "completion_tokens": 120},
                "choices": [{"message": {"content": self.reply}, "finish_reason": self.finish}]}


CFG = BrainConfig(enabled=True, provider="groq", model="openai/gpt-oss-120b")
FINDINGS = [Finding("news", "warn", "INFY: four downgrades ...")]


def setup_function():
    brain.reset_run_counter()


def test_body_puts_schema_in_system_and_asks_for_json():
    body = build_body("openai/gpt-oss-120b", 500, [{"type": "text", "text": "SYS"}],
                      [{"role": "user", "content": "DATA"}], {"format": {"schema": {"type": "object"}}})
    assert body["messages"][0]["role"] == "system" and body["messages"][0]["content"].startswith("SYS")
    assert '"type": "object"' in body["messages"][0]["content"]
    assert body["response_format"] == {"type": "json_object"} and body["messages"][1]["content"] == "DATA"


def test_veto_from_groq_blocks_and_is_free(tmp_path):
    cfg = BrainConfig(**{**CFG.__dict__, "usage_path": tmp_path / "u.json"})
    reply = json.dumps({"decision": "veto", "confidence": 0.8, "rationale": "Downgrades.", "risks": ["news"]})
    v = review_brief("INFY.NS", "MA cross", "crossed up", FINDINGS, cfg=cfg, client=FakeGroq(reply))
    assert v.decision == "veto" and v.cost_usd == 0.0 and v.input_tokens == 900
    f = brain_finding(v, cfg)
    assert f.status == "block" and f.summary.startswith("Groq gpt-oss-120b:")


def test_bad_json_and_cutoff_abstain(tmp_path):
    cfg = BrainConfig(**{**CFG.__dict__, "usage_path": tmp_path / "u.json"})
    assert review_brief("X.NS", "s", "r", FINDINGS, cfg=cfg, client=FakeGroq("not json")).decision == "abstain"
    v = review_brief("X.NS", "s", "r", FINDINGS, cfg=cfg, client=FakeGroq('{"decision"', finish="length"))
    assert v.decision == "abstain" and "max_tokens" in v.error


def test_groq_error_never_raises(tmp_path):
    from papertrader.agent.groq_client import GroqError
    cfg = BrainConfig(**{**CFG.__dict__, "usage_path": tmp_path / "u.json"})
    v = review_brief("X.NS", "s", "r", FINDINGS, cfg=cfg, client=FakeGroq(exc=GroqError("rate limit (429)")))
    assert v.decision == "abstain" and "429" in v.error
    assert brain_finding(v, cfg).status == "unknown"


def test_no_groq_key_means_unavailable(monkeypatch):
    monkeypatch.setattr("papertrader.agent.groq_client.groq_key", lambda: None)
    ok, why = brain.brain_available(CFG)
    assert not ok and "GROQ_API_KEY" in why
