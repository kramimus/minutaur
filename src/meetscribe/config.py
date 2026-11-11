"""Configuration: ~/.config/meetscribe/config.toml overlaid on defaults."""
from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "meetscribe"
CONFIG_PATH = CONFIG_DIR / "config.toml"
ENV_PATH = CONFIG_DIR / "env"
USER_SKILLS_DIR = CONFIG_DIR / "skills"

DEFAULT_CONFIG_TOML = '''\
# meetscribe configuration. Every key is optional; these are the defaults.

output_dir = "~/Meetings"

[speakers]
me = "Me"          # label for your microphone; put your name here for better notes
others = "Others"  # label for everyone else on the call

[audio]
capture_system = true   # what you hear (the other participants)
capture_mic = true      # what you say
system_device = ""      # empty = monitor of the default output
mic_device = ""         # empty = default input
keep_audio = true       # keep the recording next to the transcript
format = "flac"         # flac | opus | wav (flac and opus need ffmpeg)

[transcription]
backend = "faster-whisper"   # "faster-whisper" (local) or "openai" (any OpenAI-compatible endpoint)
model = "small.en"           # tiny.en / base.en / small.en / medium.en / large-v3 / distil-large-v3 ...
device = "auto"              # auto | cpu | cuda
compute_type = "auto"        # auto = int8 on CPU, float16 on GPU
language = "en"              # empty = auto-detect
beam_size = 2
vocabulary = ""              # names and jargon to bias recognition, e.g. "Kubernetes, Charlotte, OKRs"
# Only for backend = "openai":
api_base = "https://api.openai.com/v1"
api_key_env = "OPENAI_API_KEY"
api_model = "whisper-1"

[notes]
enabled = true
provider = "bedrock"              # "bedrock", "anthropic" (Claude API key) or "openai" (OpenAI-compatible, e.g. Ollama)
live_model = "anthropic.claude-haiku-5-5"    # used for the in-meeting updates
final_model = "anthropic.claude-sonnet-5-5"  # used for the pass over the whole transcript at the end
interval = 45                     # seconds between live updates (a skill can override)
aws_region = ""                   # bedrock: empty = AWS_REGION, then the profile's region
aws_profile = ""                  # bedrock: empty = default AWS credential chain
api_base = ""                     # empty = provider default
api_key_env = ""                  # empty = AWS_BEARER_TOKEN_BEDROCK / ANTHROPIC_API_KEY / OPENAI_API_KEY
skills = []                       # empty = every skill found; or a list of names
disabled_skills = []
skills_dirs = []                  # extra directories to search, after ~/.config/meetscribe/skills

[watch]
poll_seconds = 3
start_after = 5    # a browser must hold the mic this long before recording starts
stop_after = 30    # ...and release it this long before recording stops
browsers = ["chrome", "chromium", "firefox", "brave", "edge", "vivaldi", "opera"]
'''


@dataclass
class Speakers:
    me: str = "Me"
    others: str = "Others"


@dataclass
class Audio:
    capture_system: bool = True
    capture_mic: bool = True
    system_device: str = ""
    mic_device: str = ""
    keep_audio: bool = True
    format: str = "flac"


@dataclass
class Transcription:
    backend: str = "faster-whisper"
    model: str = "small.en"
    device: str = "auto"
    compute_type: str = "auto"
    language: str = "en"
    beam_size: int = 2
    vocabulary: str = ""
    api_base: str = "https://api.openai.com/v1"
    api_key_env: str = "OPENAI_API_KEY"
    api_model: str = "whisper-1"


@dataclass
class Notes:
    enabled: bool = True
    provider: str = "bedrock"
    live_model: str = "anthropic.claude-haiku-5-5"
    final_model: str = "anthropic.claude-sonnet-5-5"
    interval: float = 45
    aws_region: str = ""
    aws_profile: str = ""
    api_base: str = ""
    api_key_env: str = ""
    skills: list[str] = field(default_factory=list)
    disabled_skills: list[str] = field(default_factory=list)
    skills_dirs: list[str] = field(default_factory=list)


@dataclass
class Watch:
    poll_seconds: float = 3
    start_after: float = 5
    stop_after: float = 30
    browsers: list[str] = field(
        default_factory=lambda: ["chrome", "chromium", "firefox", "brave", "edge", "vivaldi", "opera"]
    )


@dataclass
class Config:
    output_dir: str = "~/Meetings"
    speakers: Speakers = field(default_factory=Speakers)
    audio: Audio = field(default_factory=Audio)
    transcription: Transcription = field(default_factory=Transcription)
    notes: Notes = field(default_factory=Notes)
    watch: Watch = field(default_factory=Watch)

    @property
    def output_path(self) -> Path:
        return Path(self.output_dir).expanduser()


def _overlay(obj, data: dict, where: str = "") -> None:
    known = {f.name for f in dataclasses.fields(obj)}
    for key, value in data.items():
        if key not in known:
            raise ValueError(f"unknown config key: {where}{key}")
        current = getattr(obj, key)
        if dataclasses.is_dataclass(current):
            if not isinstance(value, dict):
                raise ValueError(f"config key {where}{key} must be a table")
            _overlay(current, value, f"{where}{key}.")
        else:
            setattr(obj, key, value)


def load_config(path: Path | None = None) -> Config:
    """Load the config file, creating a commented default the first time."""
    path = path or CONFIG_PATH
    cfg = Config()
    if not path.exists():
        if path == CONFIG_PATH:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(DEFAULT_CONFIG_TOML)
            USER_SKILLS_DIR.mkdir(exist_ok=True)
        return cfg
    with path.open("rb") as fh:
        _overlay(cfg, tomllib.load(fh))
    return cfg


def load_env_file(path: Path = ENV_PATH) -> None:
    """Load KEY=VALUE lines (API keys) without overriding the real environment.

    The systemd unit reads the same file, so keys live in one place.
    """
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))
