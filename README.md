# meetscribe

Records your Google Meet calls on Linux, transcribes them as they happen, and keeps a set of notes up to date during the meeting. What the notes contain is decided by **skills**: folders with a `SKILL.md` that you drop into `~/.config/meetscribe/skills/`.

```
 PipeWire / PulseAudio                                      ~/Meetings/2026-11-10_100000_standup/
 ┌──────────────────────┐   ┌───────────┐   ┌─────────┐      ├── audio.flac        L = you, R = everyone else
 │ output monitor (them)│──▶│ segmenter │──▶│ Whisper │──┬──▶├── transcript.md     timestamped, "Me" / "Others"
 │ microphone     (you) │──▶│ (per src) │   │ (local) │  │   ├── transcript.jsonl
 └──────────────────────┘   └───────────┘   └─────────┘  │   ├── notes.md          every skill's section, rewritten live
                                                         ▼   ├── notes/<skill>.md
                              skills ──▶ notes engine (Claude)└── session.json
```

Audio and transcription stay on your machine. Only transcript text is sent to the notes model, which by default is Claude in Amazon Bedrock, inside your own AWS account.

## Install

Ubuntu 22.04 or newer (PipeWire or PulseAudio), Python 3.10+.

```bash
sudo apt install pulseaudio-utils ffmpeg libnotify-bin python3-venv
./install.sh
aws sso login            # or `aws configure`: any credentials that can call Bedrock
echo 'AWS_REGION=us-west-2' >> ~/.config/meetscribe/env   # only if your AWS profile has no region
meetscribe doctor
```

Notes are written by Claude through Amazon Bedrock, so you need an AWS account with access to Claude Haiku 5.5 and Claude Sonnet 5.5 enabled in the Bedrock console. See [Notes model](#notes-model) for credentials and for using the Claude API or a local model instead.

`install.sh` puts everything under your home directory (a venv in `~/.local/share/meetscribe`, a launcher in `~/.local/bin`, a systemd user unit). The Whisper model (about 500 MB for `small.en`) downloads the first time you record.

## Use

| Command | What it does |
|---|---|
| `meetscribe record --title "Design review"` | Record now. The transcript prints as it is recognised; Ctrl-C stops and writes the final notes. |
| `meetscribe watch` | Wait in the background and record every browser call automatically. |
| `systemctl --user enable --now meetscribe` | Run `watch` as a service from login. |
| `meetscribe notes <session folder>` | Rebuild a past meeting's notes with whatever skills you have now. |
| `meetscribe transcribe recording.flac` | Run an existing recording through the same pipeline. Add `--split-stereo` for meetscribe's own recordings. |
| `meetscribe skills` / `meetscribe skills new <name>` | List skills / scaffold a new one. |
| `meetscribe doctor` | Check audio devices, AWS credentials, skills. |

`record`, `transcribe` and `notes` accept `--skills a,b` to run a specific set; `record` and `transcribe` accept `--no-notes`.

During a meeting, open `notes.md` in any editor or Markdown viewer that reloads on change. It is replaced atomically on every update.

## Skills

A skill owns one section of `notes.md`. It is a folder with a `SKILL.md`, in the same shape as an Agent Skill: YAML frontmatter with `name` and `description`, then instructions in Markdown.

```markdown
---
name: customer-call
description: Captures what a customer told us - pain points, feature requests, objections, competitors mentioned.
meetscribe:
  title: Customer insights
  order: 30
---

# Customer insights

Record what the customer says about their own situation, in their words where possible...

## Format
### Pain points
### Requests
### Objections
```

Put it in `~/.config/meetscribe/skills/customer-call/SKILL.md` and it runs from the next meeting. No restart, no registration. `meetscribe skills new customer-call` writes a template for you.

Everything under `meetscribe:` is optional:

| Key | Default | Meaning |
|---|---|---|
| `title` | from the name | Heading of the skill's section |
| `order` | 100 | Position in `notes.md`, lowest first |
| `live` | true | Update during the meeting |
| `final` | true | Rewrite from the full transcript when the meeting ends |
| `interval` | config (45 s) | Seconds between live updates |
| `live_model`, `final_model` | config | Model override for this skill |
| `max_tokens` | 4000 | Upper bound on the size of this skill's notes |
| `include` | none | Other files in the folder to append to the instructions (a glossary, a team roster) |

How skills run: every `interval` seconds, each live skill with new transcript lines gets one model call containing its instructions, its notes so far, and the new lines; it returns the revised notes (or `NO_CHANGE`). When the meeting ends, each skill gets one more call with the whole transcript to correct and tidy. Skills run independently and in parallel, so adding one never changes another's output.

Two skills ship built in: `meeting-notes` (summary, decisions, open questions) and `action-items`. A skill of the same name in your own folder replaces the built-in. To switch one off, list it under `disabled_skills` in the config, or set `skills = [...]` to an explicit list.

## Configuration

`~/.config/meetscribe/config.toml` is created on first run with every option and its default. The ones you are most likely to change:

```toml
[speakers]
me = "Mark"                 # your name instead of "Me", so action items are attributed properly

[transcription]
model = "small.en"          # base.en is faster, medium.en / large-v3 more accurate (use a GPU)
vocabulary = "Kubernetes, OKRs, Charlotte"   # names and jargon Whisper should know

[notes]
interval = 45               # seconds between live updates
```

To transcribe in the cloud instead of locally, set `[transcription] backend = "openai"` to send audio to any OpenAI-compatible `/audio/transcriptions` endpoint.

## Notes model

Three providers, chosen with `[notes] provider`:

| `provider` | Talks to | Credentials | Default models |
|---|---|---|---|
| `bedrock` (default) | Claude in Amazon Bedrock, `bedrock-mantle.<region>.api.aws` | AWS credential chain, or a Bedrock bearer token | `anthropic.claude-haiku-5-5` live, `anthropic.claude-sonnet-5-5` final |
| `anthropic` | The Claude API | `ANTHROPIC_API_KEY` | same models (the `anthropic.` prefix is dropped for you) |
| `openai` | Any chat-completions endpoint via `api_base` (Ollama, vLLM, LM Studio, OpenAI) | `OPENAI_API_KEY` if the endpoint needs one | set `live_model` and `final_model` yourself |

**Bedrock setup**

```toml
[notes]
provider = "bedrock"
aws_region = "us-west-2"    # or leave empty and use AWS_REGION / the region in your AWS profile
aws_profile = ""            # a named profile from ~/.aws/config, if not the default
```

- **Credentials** are found the way every AWS tool finds them: environment variables, `~/.aws` (including SSO sessions), or an instance role. Alternatively put a Bedrock bearer token in `AWS_BEARER_TOKEN_BEDROCK`; if it is set it takes priority.
- **For the background service**, put `AWS_REGION`, `AWS_PROFILE` or the bearer token in `~/.config/meetscribe/env`. SSO sessions expire; when one does, notes stop updating (the transcript and recording carry on) and `meetscribe notes <session folder>` rebuilds them after you log in again.
- **IAM**: the identity needs permission to invoke the two models in that region, and the models must be enabled for your account in the Bedrock console.
- **Model names**: use Bedrock's IDs (`anthropic.claude-...`). A skill that overrides its model with a Claude API name such as `claude-opus-5-5` still works; the prefix is added for you.
- `meetscribe doctor` reports the region and where the credentials came from.

## How call detection works

`meetscribe watch` polls the audio server for a recording stream opened by a browser. A browser holding the microphone for 5 seconds starts a recording; releasing it for 30 seconds ends it. You get a desktop notification at both ends.

The audio server knows which program has the mic, not which site, so this triggers on any browser call (Meet, and also Zoom or Teams in a tab). Calls in native apps are not detected; use `meetscribe record` for those.

## Things to know

- **Use headphones.** On speakers your mic also hears the other participants. meetscribe drops mic lines that duplicate what was just played, but that filter is best-effort.
- **"Others" is one stream.** Remote participants are not told apart. Names come from the conversation itself.
- **It records what the default output plays**, not just the Meet tab, so music or a video playing during the call is captured too. If you switch output device mid-call (plugging in a headset), restart the recording.
- **CPU speed.** `small.en` should keep up in real time on a recent laptop CPU. If the log says transcription is falling behind, use `base.en` or a GPU.
- **Consent.** Recording a call without telling the other participants is illegal in many places, including California and other all-party-consent states. Tell people you are recording.

## Development

```bash
pip install -e '.[dev]'
pytest
```

The tests run the whole pipeline, including live capture and call detection, against fake `pactl`/`parec` programs, a scripted transcriber and a scripted LLM, and exercise the Bedrock and Claude API clients against a mock HTTP transport, so they need no audio hardware, model download or credentials.

Layout: `audio.py` (capture, call detection, recording), `segmenter.py` (utterance endpointing), `transcribe.py` (speech-to-text backends), `transcript.py`, `skills.py`, `notes.py` (the engine and its prompts), `llm.py` (Bedrock, Claude API and OpenAI-compatible backends), `session.py` (wires one meeting together), `watch.py`, `cli.py`.
