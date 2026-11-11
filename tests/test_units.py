from __future__ import annotations

import time

import pytest

from minutaur import audio, skills as skills_mod
from minutaur.config import Notes, Speakers, load_config
from minutaur.notes import NotesEngine
from minutaur.segmenter import Segmenter
from minutaur.transcript import Transcript, Utterance

from conftest import FakeLLM, burst_signal

# ------------------------------------------------------------------ segmenter


def segments_of(pattern):
    out = []
    seg = Segmenter(lambda start, samples: out.append((start, len(samples) / audio.RATE)))
    audio.feed_samples(burst_signal(pattern), seg.feed)
    seg.flush()
    return out


def test_segmenter_splits_on_pauses():
    segs = segments_of([(1.0, False), (2.0, True), (1.5, False), (1.0, True), (1.0, False)])
    assert len(segs) == 2
    (s1, d1), (s2, d2) = segs
    assert s1 == pytest.approx(0.7, abs=0.11)   # 1.0s minus pre-roll
    assert d1 == pytest.approx(2.5, abs=0.25)   # pre-roll + speech + short tail
    assert s2 == pytest.approx(4.2, abs=0.11)


def test_segmenter_ignores_silence_and_clicks():
    assert segments_of([(5.0, False)]) == []
    assert segments_of([(1.0, False), (0.1, True), (2.0, False)]) == []


def test_segmenter_caps_long_speech():
    segs = segments_of([(60.0, True)])
    assert len(segs) >= 2
    assert all(d <= 25.01 for _, d in segs)
    assert sum(d for _, d in segs) == pytest.approx(60.0, abs=0.5)


def test_segmenter_flushes_speech_in_progress():
    assert len(segments_of([(0.5, False), (2.0, True)])) == 1


# ---------------------------------------------------------------------- pactl

SOURCE_OUTPUTS = """\
Source Output #88
\tDriver: PipeWire
\tOwner Module: n/a
\tClient: 87
\tSource: 51
\tCorked: no
\tProperties:
\t\tapplication.name = "Google Chrome input"
\t\tapplication.process.binary = "chrome"
\t\tmedia.name = "RecordStream"

Source Output #90
\tDriver: PipeWire
\tClient: 91
\tCorked: no
\tProperties:
\t\tapplication.name = "minutaur"
\t\tapplication.process.binary = "parec"

Source Output #95
\tClient: 96
\tCorked: yes
\tProperties:
\t\tapplication.name = "Firefox"
\t\tapplication.process.binary = "firefox"

Source Output #97
\tClient: 98
\tCorked: no
\tProperties:
\t\tapplication.name = "PulseAudio Volume Control"
\t\tapplication.process.binary = "pavucontrol"
"""


def test_parse_pactl_list():
    items = audio.parse_pactl_list(SOURCE_OUTPUTS)
    assert [i["_header"] for i in items] == [f"Source Output #{n}" for n in (88, 90, 95, 97)]
    assert items[0]["application.process.binary"] == "chrome"
    assert items[0]["Corked"] == "no"


def test_browser_mic_streams(monkeypatch):
    monkeypatch.setattr(audio, "pactl", lambda *a: SOURCE_OUTPUTS)
    found = audio.browser_mic_streams(["chrome", "firefox"])
    # Chrome counts; our own capture, a paused Firefox stream and pavucontrol do not.
    assert [i["_header"] for i in found] == ["Source Output #88"]
    monkeypatch.setattr(audio, "pactl", lambda *a: "")
    assert audio.browser_mic_streams(["chrome"]) == []


def test_default_devices(monkeypatch):
    replies = {("get-default-sink",): "alsa_output.usb\n", ("get-default-source",): "alsa_input.usb\n"}
    monkeypatch.setattr(audio, "pactl", lambda *a: replies[a])
    assert audio.default_monitor() == "alsa_output.usb.monitor"
    assert audio.default_source() == "alsa_input.usb"


# ----------------------------------------------------------------- transcript


def test_transcript_files_and_reload(tmp_path):
    t = Transcript(tmp_path, "Standup")
    t.add(Utterance(3.0, 5.0, "Me", "hello there"))
    t.add(Utterance(3725.0, 3727.0, "Others", "bye"))
    md = (tmp_path / "transcript.md").read_text()
    assert "**[00:00:03] Me:** hello there" in md and "**[01:02:05] Others:** bye" in md
    again = Transcript.load(tmp_path)
    assert [u.text for u in again.snapshot()] == ["hello there", "bye"]


def test_transcript_drops_mic_echo_only():
    t = Transcript(echo_of=("Me", "Others"))
    assert t.add(Utterance(10, 14, "Others", "So the launch is moving to the third of March."))
    # The same words picked up by the mic from the speakers, slightly garbled.
    assert not t.add(Utterance(10.3, 14.2, "Me", "so the launch is moving to the 3rd of march"))
    # Something the user actually said at the same time is kept.
    assert t.add(Utterance(12, 15, "Me", "Wait, does that affect the pricing page?"))
    # Short interjections are never dropped.
    assert t.add(Utterance(13, 13.5, "Me", "the launch"))
    # The same words much later are not an echo.
    assert t.add(Utterance(300, 304, "Me", "so the launch is moving to the third of March"))
    assert len(t) == 4


# --------------------------------------------------------------------- skills


def make_skill(root, name, front="", body="Capture things."):
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: test skill\n{front}---\n\n{body}\n")
    return folder


def test_builtin_skills_load():
    found = skills_mod.discover([skills_mod.BUILTIN_SKILLS_DIR])
    assert set(found) == {"meeting-notes", "action-items"}
    assert found["meeting-notes"].title == "Summary"
    assert found["action-items"].order > found["meeting-notes"].order


def test_skill_options_includes_and_override(tmp_path):
    user = tmp_path / "user"
    folder = make_skill(
        user, "risks",
        "minutaur:\n  title: Risks\n  order: 5\n  live: false\n  interval: 20\n"
        "  final_model: claude-opus-5-5\n  include: [glossary.md]\n",
    )
    (folder / "glossary.md").write_text("SLO means service level objective.")
    make_skill(user, "meeting-notes", body="My own summary rules.")  # overrides the built-in
    make_skill(user, "Bad Name")
    (user / "broken").mkdir()
    (user / "broken" / "SKILL.md").write_text("no frontmatter here")

    cfg = Notes(skills_dirs=[str(user)])
    enabled = skills_mod.load_enabled(cfg)
    # "risks" asks for order 5; the overriding meeting-notes sets none, so it sorts last.
    assert [s.name for s in enabled] == ["risks", "action-items", "meeting-notes"]
    risks = enabled[0]
    assert (risks.title, risks.live, risks.interval, risks.final_model) == ("Risks", False, 20.0, "claude-opus-5-5")
    assert "SLO means service level objective." in risks.instructions
    assert enabled[2].instructions == "My own summary rules."

    assert [s.name for s in skills_mod.load_enabled(Notes(skills_dirs=[str(user)], disabled_skills=["risks"]))] == [
        "action-items", "meeting-notes"]
    assert [s.name for s in skills_mod.load_enabled(cfg, ["action-items"])] == ["action-items"]
    with pytest.raises(skills_mod.SkillError):
        skills_mod.load_enabled(cfg, ["nope"])


def test_skill_include_cannot_escape_folder(tmp_path):
    (tmp_path / "secret.txt").write_text("x")
    folder = make_skill(tmp_path / "s", "leaky", "minutaur:\n  include: [../../secret.txt]\n")
    with pytest.raises(skills_mod.SkillError):
        skills_mod.parse_skill(folder / "SKILL.md")


def test_scaffold_template_is_a_valid_skill(tmp_path):
    folder = tmp_path / "my-skill"
    folder.mkdir()
    (folder / "SKILL.md").write_text(skills_mod.SKILL_TEMPLATE.format(name="my-skill", title="My skill"))
    skill = skills_mod.parse_skill(folder / "SKILL.md")
    assert (skill.name, skill.title, skill.order) == ("my-skill", "My skill", 50)


# ---------------------------------------------------------------------- notes


def engine(tmp_path, llm, skills, transcript, **cfg):
    return NotesEngine(transcript, skills, llm, Notes(**cfg), tmp_path, title="Sync", speakers=Speakers())


def two_skills():
    return skills_mod.load_enabled(Notes())


def test_live_updates_are_incremental(tmp_path):
    llm, t = FakeLLM(), Transcript()
    eng = engine(tmp_path, llm, two_skills(), t, interval=0)
    eng.start()
    try:
        t.add(Utterance(1, 2, "Me", "first line"))
        deadline = time.time() + 5
        while len(llm.calls) < 2 and time.time() < deadline:
            time.sleep(0.05)
        assert len(llm.calls) == 2  # one per skill
        t.add(Utterance(3, 4, "Others", "second line"))
        while len(llm.calls) < 4 and time.time() < deadline:
            time.sleep(0.05)
    finally:
        eng.stop(final=False)
    assert len(llm.calls) == 4
    first = next(c for c in llm.calls[:2] if 'skill name="meeting-notes"' in c["system"])
    second = next(c for c in llm.calls[2:] if 'skill name="meeting-notes"' in c["system"])
    assert first["model"] == "anthropic.claude-haiku-5-5"
    assert "[00:00:01] Me: first line" in first["user"].split("<new_transcript_lines>")[1]
    new_part = second["user"].split("<new_transcript_lines>")[1]
    assert "second line" in new_part and "first line" not in new_part
    assert "first line" in second["user"]            # still there as context
    assert "- note v" in second["user"]              # previous notes are passed back
    combined = (tmp_path / "notes.md").read_text()
    assert combined.startswith("# Sync\n") and "## Summary" in combined and "## Action items" in combined
    assert combined.index("## Summary") < combined.index("## Action items")
    assert (tmp_path / "notes" / "action-items.md").read_text().startswith("- note v")


def test_no_change_failure_and_fences(tmp_path):
    t = Transcript()
    t.add(Utterance(1, 2, "Me", "hello"))
    skill = two_skills()[:1]

    eng = engine(tmp_path, FakeLLM("```markdown\n- fenced\n```"), skill, t)
    eng._update_live(skill[0])
    assert eng.notes["meeting-notes"] == "- fenced"

    eng.llm = FakeLLM("NO_CHANGE")
    t.add(Utterance(3, 4, "Me", "um"))
    eng._update_live(skill[0])
    assert eng.notes["meeting-notes"] == "- fenced" and eng._cursor["meeting-notes"] == 2

    def boom(_call):
        raise RuntimeError("api down")

    eng.llm = FakeLLM(boom)
    t.add(Utterance(5, 6, "Me", "important"))
    eng._update_live(skill[0])
    assert eng.notes["meeting-notes"] == "- fenced"
    assert eng._cursor["meeting-notes"] == 2  # not advanced: these lines are retried


def test_final_pass_uses_full_transcript_and_final_model(tmp_path):
    llm, t = FakeLLM(), Transcript()
    for i in range(3):
        t.add(Utterance(i, i + 1, "Others", f"line {i}"))
    eng = engine(tmp_path, llm, two_skills(), t)
    eng.finalize()
    assert len(llm.calls) == 2
    assert all(c["model"] == "anthropic.claude-sonnet-5-5" for c in llm.calls)
    assert all("line 0" in c["user"] and "line 2" in c["user"] for c in llm.calls)
    assert "Final" in (tmp_path / "notes.md").read_text()


def test_no_llm_calls_for_an_empty_meeting(tmp_path):
    llm = FakeLLM()
    engine(tmp_path, llm, two_skills(), Transcript()).finalize()
    assert llm.calls == []
    assert "_Nothing yet._" in (tmp_path / "notes.md").read_text()


# --------------------------------------------------------------------- config


def test_config_overlay_and_unknown_keys(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text('output_dir = "/x"\n[notes]\ninterval = 10\nskills = ["a"]\n[speakers]\nme = "Mark"\n')
    cfg = load_config(path)
    assert (cfg.output_dir, cfg.notes.interval, cfg.notes.skills, cfg.speakers.me) == ("/x", 10, ["a"], "Mark")
    assert cfg.notes.live_model == "anthropic.claude-haiku-5-5"
    path.write_text("[notes]\nintervall = 10\n")
    with pytest.raises(ValueError, match="notes.intervall"):
        load_config(path)


def test_default_config_file_matches_defaults(tmp_path):
    from minutaur.config import DEFAULT_CONFIG_TOML, Config

    path = tmp_path / "c.toml"
    path.write_text(DEFAULT_CONFIG_TOML)
    assert load_config(path) == Config()
