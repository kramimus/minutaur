"""The notes backends, run against a mock HTTP transport (no network, no real credentials)."""
from __future__ import annotations

import json

import pytest

from minutaur import llm
from minutaur.config import Notes

httpx2 = pytest.importorskip("httpx2")  # the transport the anthropic SDK uses

AWS_VARS = ("AWS_REGION", "AWS_DEFAULT_REGION", "AWS_PROFILE", "AWS_BEARER_TOKEN_BEDROCK",
            "ANTHROPIC_AWS_API_KEY", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
            "ANTHROPIC_BEDROCK_MANTLE_BASE_URL", "ANTHROPIC_API_KEY")


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    for var in AWS_VARS:
        monkeypatch.delenv(var, raising=False)
    # Keep boto3 away from any real ~/.aws on the machine running the tests.
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "credentials"))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    return monkeypatch


def mock(backend, stop_reason="end_turn"):
    """Swap the backend's HTTP layer for one that records the request."""
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx2.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": seen["body"]["model"],
            "content": [{"type": "text", "text": "- a note"}], "stop_reason": stop_reason,
            "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 5},
        })

    backend.client = backend.client.copy(http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    return seen


def ask(backend, model):
    return backend.complete(model=model, system="SKILL", user="lines", max_tokens=100)


def test_bedrock_signs_with_aws_credentials(clean_env):
    clean_env.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
    clean_env.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    backend = llm.Claude(Notes(aws_region="us-west-2"))          # bedrock is the default provider
    assert backend.prefix == "anthropic."
    seen = mock(backend)
    assert ask(backend, "anthropic.claude-haiku-5-5") == "- a note"
    assert seen["url"] == "https://bedrock-mantle.us-west-2.api.aws/anthropic/v1/messages"
    assert seen["headers"]["authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIAEXAMPLE/")
    assert "/us-west-2/bedrock-mantle/aws4_request" in seen["headers"]["authorization"]
    assert seen["body"]["model"] == "anthropic.claude-haiku-5-5"
    assert seen["body"]["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_bedrock_bearer_token_and_region_from_env(clean_env):
    clean_env.setenv("AWS_REGION", "us-east-1")
    clean_env.setenv("AWS_BEARER_TOKEN_BEDROCK", "tok-123")
    backend = llm.Claude(Notes())
    seen = mock(backend)
    ask(backend, "claude-sonnet-5-5")                            # Claude API name is accepted
    assert seen["url"].startswith("https://bedrock-mantle.us-east-1.api.aws/")
    assert seen["headers"]["authorization"] == "Bearer tok-123"
    assert seen["body"]["model"] == "anthropic.claude-sonnet-5-5"
    assert llm.check(Notes()) == (True, "Bedrock us-east-1, bearer token from AWS_BEARER_TOKEN_BEDROCK")


def test_bedrock_reports_missing_region_and_credentials(clean_env):
    with pytest.raises(llm.LLMError, match="region"):
        llm.Claude(Notes())
    good, detail = llm.check(Notes())
    assert not good and "region" in detail
    good, detail = llm.check(Notes(aws_region="us-west-2"))
    assert not good and "credentials" in detail
    assert llm.check(Notes(provider="anthropic")) == (False, "Claude API, ANTHROPIC_API_KEY not set")


def test_bedrock_region_from_aws_profile(clean_env, tmp_path):
    (tmp_path / "config").write_text("[profile work]\nregion = eu-west-1\n")
    (tmp_path / "credentials").write_text("[work]\naws_access_key_id = AKIAWORK\naws_secret_access_key = s\n")
    cfg = Notes(aws_profile="work")
    assert llm.bedrock_region(cfg) == "eu-west-1"
    backend = llm.Claude(cfg)
    seen = mock(backend)
    ask(backend, "anthropic.claude-haiku-5-5")
    assert seen["url"].startswith("https://bedrock-mantle.eu-west-1.api.aws/")
    assert "Credential=AKIAWORK/" in seen["headers"]["authorization"]
    assert llm.check(cfg)[0]


def test_truncated_reply_is_an_error(clean_env):
    clean_env.setenv("AWS_BEARER_TOKEN_BEDROCK", "tok")
    backend = llm.Claude(Notes(aws_region="us-west-2"))
    mock(backend, stop_reason="max_tokens")
    with pytest.raises(llm.LLMError, match="max_tokens"):
        ask(backend, "anthropic.claude-haiku-5-5")


def test_unknown_provider(clean_env):
    with pytest.raises(llm.LLMError, match="unknown notes provider"):
        llm.Claude(Notes(provider="openai"))


def test_anthropic_provider(clean_env):
    with pytest.raises(llm.LLMError, match="ANTHROPIC_API_KEY"):
        llm.Claude(Notes(provider="anthropic"))
    clean_env.setenv("ANTHROPIC_API_KEY", "sk-test")
    backend = llm.Claude(Notes(provider="anthropic"))
    seen = mock(backend)
    ask(backend, "anthropic.claude-haiku-5-5")                   # Bedrock-style name is accepted
    assert seen["url"] == "https://api.anthropic.com/v1/messages"
    sent_key = seen["headers"].get("x-api-key") or seen["headers"].get("authorization", "")
    assert sent_key.endswith("sk-test")
    assert seen["body"]["model"] == "claude-haiku-5-5"
