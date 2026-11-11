# minutaur

Takes the minutes of your meetings. minutaur records your Google Meet (or any browser) calls on Linux, transcribes them on your machine as they happen, and keeps a set of notes up to date during the meeting. What goes into the notes is decided by **skills**: folders with a `SKILL.md` that you drop into `~/.config/minutaur/skills/`.

```
 PipeWire / PulseAudio                                      ~/Meetings/2026-11-10_100000_standup/
 ┌──────────────────────┐   ┌───────────┐   ┌─────────┐      ├── audio.flac        L = you, R = everyone else
 │ output monitor (them)│──▶│ segmenter │──▶│ Whisper │──┬──▶├── transcript.md     timestamped, "Me" / "Others"
 │ microphone     (you) │──▶│ (per src) │   │ (local) │  │   ├── transcript.jsonl
 └──────────────────────┘   └───────────┘   └─────────┘  │   ├── notes.md          every skill's section, rewritten live
                                                         ▼   ├── notes/<skill>.md
                    skills ──▶ notes engine (Claude on Bedrock)└── session.json
```

Audio and transcription stay on your machine. Only transcript text goes to the notes model: Claude in Amazon Bedrock, inside your own AWS account.

## Install

Ubuntu 22.04 or newer (PipeWire or PulseAudio) and [uv](https://docs.astral.sh/uv/). uv provides Python 3.11+ itself if your system doesn't have it.

```bash
sudo apt install pulseaudio-utils ffmpeg libnotify-bin
curl -LsSf https://astral.sh/uv/install.sh | sh     # if you don't have uv yet
make install             # no sudo: everything goes under your home directory
aws sso login            # or `aws configure`: any credentials that can call Bedrock
minutaur doctor
```

If your AWS profile has no region, add `AWS_REGION=us-west-2` (or yours) to `~/.config/minutaur/env`. Claude Haiku 5.5 and Claude Sonnet 5.5 must be enabled for your account in the Bedrock console.

| `make` target | What it does |
|---|---|
| `make install` | Install or upgrade, at exactly the versions in `uv.lock`: an environment in `~/.local/share/minutaur`, the `minutaur` command in `~/.local/bin`, a systemd user unit |
| `make enable` / `make disable` | Start / stop recording every browser call automatically, from login |
| `make uninstall` | Remove the program; your config, skills and meetings stay |
| `make test` | Run the tests with `uv run` (no audio hardware, model download or credentials needed). `make test ARGS="-k notes"` passes options to pytest |
| `make lock` | Move `uv.lock` to the newest versions `pyproject.toml` allows; then `make test` and commit it |
| `make clean` | Remove the dev `.venv` and build leftovers |

The Whisper model (about 500 MB for `small.en`) downloads the first time you record.

## Use

| Command | What it does |
|---|---|
| `minutaur record --title "Design review"` | Record now. The transcript prints as it is recognised; Ctrl-C stops and writes the final notes. |
| `minutaur watch` | Wait in the background and record every browser call (what `make enable` runs). |
| `minutaur notes <meeting folder>` | Rebuild a past meeting's notes with whatever skills you have now. |
| `minutaur transcribe recording.flac` | Run an existing recording through the same pipeline. Add `--split-stereo` for minutaur's own recordings. |
| `minutaur skills` / `minutaur skills new <name>` | List skills / scaffold a new one. |
| `minutaur doctor` | Check audio devices, AWS credentials, skills. |

`record`, `transcribe` and `notes` accept `--skills a,b` to run a specific set; `record` and `transcribe` accept `--no-notes`.

During a meeting, open `notes.md` in any editor or Markdown viewer that reloads on change. It is replaced atomically on every update.

## Skills

A skill owns one section of `notes.md`. It is a folder with a `SKILL.md`, in the same shape as an Agent Skill: YAML frontmatter with `name` and `description`, then instructions in Markdown.

```markdown
---
name: customer-call
description: Captures what a customer told us - pain points, feature requests, objections, competitors mentioned.
minutaur:
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

Put it in `~/.config/minutaur/skills/customer-call/SKILL.md` and it runs from the next meeting. `minutaur skills new customer-call` writes a template for you.

Everything under `minutaur:` is optional:

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

How skills run: every `interval` seconds, each live skill with new transcript lines gets one model call containing its instructions, its notes so far, and the new lines, and returns the revised notes (or `NO_CHANGE`). When the meeting ends, each skill gets one more call with the whole transcript to correct and tidy. Skills run independently and in parallel, so adding one never changes another's output.

Two skills ship built in: `meeting-notes` (summary, decisions, open questions) and `action-items`. A skill of the same name in your own folder replaces the built-in. To switch one off, list it under `disabled_skills` in the config, or set `skills = [...]` to an explicit list.

## Configuration

`~/.config/minutaur/config.toml` is created on first run with every option and its default. The ones you are most likely to change:

```toml
output_dir = "~/Meetings"   # where meeting folders go

[speakers]
me = "Mark"                 # your name instead of "Me", so action items are attributed properly

[transcription]
model = "small.en"          # base.en is faster, medium.en / large-v3 more accurate (use a GPU)
vocabulary = "Kubernetes, OKRs"   # names and jargon Whisper should know

[notes]
aws_region = "us-west-2"    # or leave empty for AWS_REGION / your AWS profile's region
aws_profile = ""            # a named profile from ~/.aws/config, if not the default
interval = 45               # seconds between live updates
```

**Notes model.** By default notes go through Claude in Amazon Bedrock: `anthropic.claude-haiku-5-5` for live updates, `anthropic.claude-sonnet-5-5` for the final pass. Credentials come from the usual AWS chain (environment, `~/.aws` including SSO, instance role), or a Bedrock bearer token in `AWS_BEARER_TOKEN_BEDROCK`. To use the Claude API instead, set `provider = "anthropic"` and put `ANTHROPIC_API_KEY` in `~/.config/minutaur/env`. Model names work with or without the `anthropic.` prefix on either provider.

**The background service** reads `~/.config/minutaur/env` for `AWS_REGION`, `AWS_PROFILE` or keys. SSO sessions expire; when one does, notes stop updating (the transcript and recording carry on) and `minutaur notes <meeting folder>` rebuilds them after you log in again.

## How call detection works

`minutaur watch` polls the audio server for a recording stream opened by a browser. A browser holding the microphone for 5 seconds starts a recording; releasing it for 30 seconds ends it. You get a desktop notification at both ends.

The audio server knows which program has the mic, not which site, so this triggers on any browser call (Meet, and also Zoom or Teams in a tab). Calls in native apps are not detected; use `minutaur record` for those.

## Things to know

- **Use headphones.** On speakers your mic also hears the other participants. minutaur drops mic lines that duplicate what was just played, but that filter is best-effort.
- **"Others" is one stream.** Remote participants are not told apart. Names come from the conversation itself.
- **It records what the default output plays**, not just the Meet tab, so music or a video playing during the call is captured too. If you switch output device mid-call (plugging in a headset), restart the recording.
- **CPU speed.** `small.en` should keep up in real time on a recent laptop CPU. If the log says transcription is falling behind, use `base.en` or a GPU.
- **Consent.** Recording a call without telling the other participants is illegal in many places, including California and other all-party-consent states. Tell people you are recording.

## Code

Development uses uv: `uv run minutaur ...` runs the command from the checkout, `uv add <package>` adds a dependency and updates `uv.lock`.


| File | Role |
|---|---|
| `audio.py` | Capture with `parec`, call detection with `pactl`, decoding and encoding with ffmpeg |
| `segmenter.py` | Cuts each stream into utterances |
| `transcribe.py` | faster-whisper |
| `transcript.py` | The running transcript, on disk and in memory, with echo removal |
| `skills.py` | Finds and parses skills |
| `notes.py` | The notes engine and its prompts |
| `llm.py` | Claude via Bedrock or the Claude API |
| `session.py` | Wires one meeting together |
| `watch.py` | Background call detection |
| `cli.py` | Commands |

The tests run the whole pipeline, including live capture and call detection, against fake `pactl`/`parec` programs, a scripted transcriber and a scripted model, and exercise the Bedrock and Claude API clients against a mock HTTP transport.

## License

MIT. See [LICENSE](LICENSE).
