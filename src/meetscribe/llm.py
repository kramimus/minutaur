"""LLM backends for the notes engine."""
from __future__ import annotations

import os
from typing import Protocol

from .config import Notes


class LLMError(RuntimeError):
    pass


class LLM(Protocol):
    def complete(self, *, model: str, system: str, user: str, max_tokens: int) -> str: ...


class _MessagesLLM:
    """Shared by every backend that speaks the Anthropic Messages API."""

    client: object

    def _model(self, model: str) -> str:
        return model

    def complete(self, *, model: str, system: str, user: str, max_tokens: int) -> str:
        msg = self.client.messages.create(
            model=self._model(model),
            max_tokens=max_tokens,
            # The system prompt (the skill) is identical on every update: cache it.
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
        )
        if msg.stop_reason == "max_tokens":
            raise LLMError("notes were cut off at max_tokens; raise max_tokens in the skill")
        return "".join(block.text for block in msg.content if block.type == "text")


def bedrock_region(cfg: Notes) -> str | None:
    """Region from config, then the environment, then the AWS profile's own config."""
    region = cfg.aws_region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if not region:
        try:
            import boto3

            region = boto3.Session(profile_name=cfg.aws_profile or None).region_name
        except Exception:
            region = None
    return region or None


def bedrock_status(cfg: Notes) -> tuple[bool, str]:
    """Whether a region and some form of AWS credentials can be found (for `doctor`)."""
    region = bedrock_region(cfg)
    if not region and not cfg.api_base:
        return False, "no AWS region: set aws_region in the config, or AWS_REGION"
    token_env = cfg.api_key_env or "AWS_BEARER_TOKEN_BEDROCK"
    if os.environ.get(token_env):
        return True, f"{region or cfg.api_base}, bearer token from {token_env}"
    try:
        import boto3

        creds = boto3.Session(profile_name=cfg.aws_profile or None).get_credentials()
    except ImportError:
        return False, "boto3 missing: pip install 'anthropic[bedrock]'"
    except Exception as exc:
        return False, f"AWS credentials: {exc}"
    if creds is None:
        return False, "no AWS credentials found (aws configure / aws sso login, or AWS_BEARER_TOKEN_BEDROCK)"
    return True, f"{region}, AWS credentials via {creds.method}"


class BedrockLLM(_MessagesLLM):
    """Claude in Amazon Bedrock (the Messages API on the bedrock-mantle endpoint).

    Credentials come from the usual AWS chain (environment, ~/.aws, SSO, instance
    role) or from a Bedrock bearer token in AWS_BEARER_TOKEN_BEDROCK.
    """

    def __init__(self, cfg: Notes):
        try:
            from anthropic import AnthropicBedrockMantle
        except ImportError as exc:
            raise LLMError("this anthropic SDK has no Bedrock client: pip install -U 'anthropic[bedrock]'") from exc
        kwargs: dict = {
            "aws_region": bedrock_region(cfg),
            "aws_profile": cfg.aws_profile or None,
            "base_url": cfg.api_base or None,
            "max_retries": 3,
            "timeout": 180,
        }
        if cfg.api_key_env and os.environ.get(cfg.api_key_env):
            kwargs["api_key"] = os.environ[cfg.api_key_env]
        try:
            self.client = AnthropicBedrockMantle(**kwargs)
        except Exception as exc:
            raise LLMError(f"Bedrock: {exc}") from exc

    def _model(self, model: str) -> str:
        # Accept Claude API names too, so skills with a model override stay portable.
        return "anthropic." + model if model.startswith("claude-") else model


class AnthropicLLM(_MessagesLLM):
    """The Claude API directly, with an Anthropic API key."""

    def __init__(self, cfg: Notes):
        import anthropic

        key_env = cfg.api_key_env or "ANTHROPIC_API_KEY"
        if not os.environ.get(key_env):
            raise LLMError(f"{key_env} is not set (export it, or put it in ~/.config/meetscribe/env)")
        self.client = anthropic.Anthropic(
            api_key=os.environ[key_env],
            base_url=cfg.api_base or None,
            max_retries=3,
            timeout=180,
        )

    def _model(self, model: str) -> str:
        return model.removeprefix("anthropic.")


class OpenAICompatLLM:
    """Chat-completions endpoint: OpenAI, Ollama, vLLM, LM Studio, ..."""

    def __init__(self, cfg: Notes):
        import httpx

        key = os.environ.get(cfg.api_key_env or "OPENAI_API_KEY", "")
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        base = (cfg.api_base or "https://api.openai.com/v1").rstrip("/")
        self.client = httpx.Client(base_url=base, headers=headers, timeout=180)

    def complete(self, *, model: str, system: str, user: str, max_tokens: int) -> str:
        resp = self.client.post(
            "/chat/completions",
            json={
                "model": model,
                "max_tokens": max_tokens,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
        )
        resp.raise_for_status()
        choice = resp.json()["choices"][0]
        if choice.get("finish_reason") == "length":
            raise LLMError("notes were cut off at max_tokens; raise max_tokens in the skill")
        return choice["message"]["content"] or ""


def make_llm(cfg: Notes) -> LLM:
    if cfg.provider == "bedrock":
        return BedrockLLM(cfg)
    if cfg.provider == "anthropic":
        return AnthropicLLM(cfg)
    if cfg.provider == "openai":
        return OpenAICompatLLM(cfg)
    raise ValueError(f"unknown notes provider: {cfg.provider!r}")
