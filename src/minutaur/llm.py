"""Claude, reached through Amazon Bedrock (default) or the Claude API."""
from __future__ import annotations

import os

from .config import Notes


class LLMError(RuntimeError):
    pass


def bedrock_region(cfg: Notes) -> str | None:
    """Region from config, then the environment, then the AWS profile."""
    region = cfg.aws_region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if not region:
        try:
            import boto3

            region = boto3.Session(profile_name=cfg.aws_profile or None).region_name
        except Exception:
            pass
    return region or None


def check(cfg: Notes) -> tuple[bool, str]:
    """Whether the notes model looks reachable, for `minutaur doctor`."""
    if cfg.provider == "anthropic":
        ok = bool(os.environ.get("ANTHROPIC_API_KEY"))
        return ok, "Claude API, ANTHROPIC_API_KEY " + ("set" if ok else "not set")
    region = bedrock_region(cfg)
    if not region:
        return False, "Bedrock: no AWS region (set aws_region in the config, or AWS_REGION)"
    if os.environ.get("AWS_BEARER_TOKEN_BEDROCK"):
        return True, f"Bedrock {region}, bearer token from AWS_BEARER_TOKEN_BEDROCK"
    try:
        import boto3

        creds = boto3.Session(profile_name=cfg.aws_profile or None).get_credentials()
    except Exception as exc:
        return False, f"Bedrock {region}: {exc}"
    if creds is None:
        return False, f"Bedrock {region}: no AWS credentials (aws configure / aws sso login)"
    return True, f"Bedrock {region}, AWS credentials from {creds.method}"


class Claude:
    def __init__(self, cfg: Notes):
        import anthropic

        common = {"max_retries": 3, "timeout": 180}
        if cfg.provider == "bedrock":
            # The Messages API on the bedrock-mantle endpoint. Credentials come from
            # the AWS chain (env, ~/.aws, SSO, instance role) or AWS_BEARER_TOKEN_BEDROCK.
            self.prefix = "anthropic."
            try:
                self.client = anthropic.AnthropicBedrockMantle(
                    aws_region=bedrock_region(cfg), aws_profile=cfg.aws_profile or None, **common)
            except Exception as exc:
                raise LLMError(f"Bedrock: {exc}") from exc
        elif cfg.provider == "anthropic":
            if not os.environ.get("ANTHROPIC_API_KEY"):
                raise LLMError("ANTHROPIC_API_KEY is not set (put it in ~/.config/minutaur/env)")
            self.prefix = ""
            self.client = anthropic.Anthropic(**common)
        else:
            raise LLMError(f"unknown notes provider {cfg.provider!r} (use bedrock or anthropic)")

    def model_id(self, model: str) -> str:
        """Accept either spelling ("claude-x" or "anthropic.claude-x") on either provider."""
        return self.prefix + model.removeprefix("anthropic.")

    def complete(self, *, model: str, system: str, user: str, max_tokens: int) -> str:
        msg = self.client.messages.create(
            model=self.model_id(model),
            max_tokens=max_tokens,
            # The skill's prompt is identical on every update, so cache it.
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
        )
        if msg.stop_reason == "max_tokens":
            raise LLMError("notes were cut off at max_tokens; raise max_tokens in the skill")
        return "".join(block.text for block in msg.content if block.type == "text")
