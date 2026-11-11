"""Settings: ~/.config/minutaur/config.toml laid over the defaults below."""
from __future__ import annotations

import dataclasses
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "minutaur"
CONFIG_PATH = CONFIG_DIR / "config.toml"
ENV_PATH = CONFIG_DIR / "env"
USER_SKILLS_DIR = CONFIG_DIR / "skills"

DEFAULT_CONFIG_TOML = '''\
# minutaur configuration. Every key is optional; these are the defaults.

output_dir = "~/Meetings"   # one folder per meeting is created in here

[speakers]
me = "Me"          # label for your microphone; your name here gives better notes
others = "Others"  # label for everyone else on the call

[audio]
capture_system = true   # what you hear (the other participants)
capture_mic = true      # what you say
system_device = ""      # empty = monitor of the default output
mic_device = ""         # empty = default input
keep_audio = true       # keep the recording next to the transcript
format = "flac"         # flac | opus | wav

[transcription]
model = "small.en"      # tiny.en / base.en / small.en / medium.en / large-v3 ...
device = "auto"         # auto | cpu | cuda
language = "en"         # empty = auto-detect
beam_size = 2
vocabulary = ""         # names and jargon to help recognition, e.g. "Kubernetes, OKRs"

[notes]
enabled = true
provider = "bedrock"                         # "bedrock", or "anthropic" for the Claude API
live_model = "anthropic.claude-haiku-5-5"    # in-meeting updates
final_model = "anthropic.claude-sonnet-5-5"  # pass over the whole transcript at the end
interval = 45           # seconds between live updates (a skill can override)
aws_region = ""         # empty = AWS_REGION, then your AWS profile's region
aws_profile = ""        # empty = default AWS credential chain
skills = []             # empty = every skill found; or a list of names
disabled_skills = []
skills_dirs = []        # extra directories to search for skills

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
    model: str = "small.en"
    device: str = "auto"
    language: str = "en"
    beam_size: int = 2
    vocabulary: str = ""


@dataclass
class Notes:
    enabled: bool = True
    provider: str = "bedrock"
    live_model: str = "anthropic.claude-haiku-5-5"
    final_model: str = "anthropic.claude-sonnet-5-5"
    interval: float = 45
    aws_region: str = ""
    aws_profile: str = ""
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
        if dataclasses.is_dataclass(getattr(obj, key)):
            _overlay(getattr(obj, key), value, f"{where}{key}.")
        else:
            setattr(obj, key, value)


def load_config(path: Path | None = None) -> Config:
    """Load the config file, writing the commented default on first run."""
    path = path or CONFIG_PATH
    cfg = Config()
    if path.exists():
        with path.open("rb") as fh:
            _overlay(cfg, tomllib.load(fh))
    elif path == CONFIG_PATH:
        USER_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(DEFAULT_CONFIG_TOML)
    return cfg


def load_env_file(path: Path = ENV_PATH) -> None:
    """Read KEY=VALUE lines (AWS settings, API keys) without overriding the real
    environment. The systemd unit reads the same file."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key and not key.startswith("#"):
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))
