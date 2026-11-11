---
name: action-items
description: Tracks commitments made during the meeting - who agreed to do what, and by when.
minutaur:
  title: Action items
  order: 20
---

# Action items

Track every commitment made in the meeting so nothing agreed out loud gets lost.

## What counts

- Someone says they will do something ("I'll send the draft tonight").
- Someone is asked to do something and accepts, or is assigned it without objecting.
- The group agrees something needs to happen, even with no owner yet.

Ideas, suggestions and "we should probably" remarks that nobody picks up are not action items.

## Format

A markdown checklist, one line per item:

`- [ ] **Owner**: what they will do (due: when)`

- Use the person's name when the conversation makes it clear. Use the user's speaker label for things the user commits to. Write **Unassigned** when nobody took it.
- Start the task with a verb and make it specific enough to act on without the transcript.
- Include `(due: ...)` only when a date or deadline was said, in the words used ("Friday", "before the launch").
- If a later part of the meeting changes an item (new owner, new date, cancelled, already done), update that line. Do not add a second one.
- Group the user's own items first, then everyone else's.

If there are no action items yet, the notes are the single line `_None yet._`
