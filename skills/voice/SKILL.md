---
description: Pick the reading voice — list, audition, set (one per language for system voices)
argument-hint: [voice id to set directly]
disable-model-invocation: true
---

If the user supplied an argument, treat it as a voice id: run step 3 with it.

1. List what's available:

```
sh "${CLAUDE_PLUGIN_ROOT}/scripts/speak.sh" --list-voices --turn "${CLAUDE_SESSION_ID}"
```

Show the result as a short readable list (label and id). If it's long, show
the first ~20 and say how many more there are; offer to filter by name.

2. When the user wants to hear one, audition it WITHOUT saving:

```
sh "${CLAUDE_PLUGIN_ROOT}/scripts/speak.sh" --detach --voice VOICE_ID --text "This is how I'd sound reading your replies." --turn "${CLAUDE_SESSION_ID}"
```

(For a Russian voice, audition with Russian text instead.) Repeat for as
many voices as they like.

3. When they choose, save it and confirm in the new voice:

```
sh "${CLAUDE_PLUGIN_ROOT}/scripts/speak.sh" --set-voice VOICE_ID --turn "${CLAUDE_SESSION_ID}"
sh "${CLAUDE_PLUGIN_ROOT}/scripts/speak.sh" --detach --text "Voice saved. This is me from now on." --turn "${CLAUDE_SESSION_ID}"
```

System voices can be set per language, so a reply mixing Russian and English
is read by a Russian and an English voice. For that, add `--lang ru` or
`--lang en` to the `--set-voice` command (the voice list shows each voice's
language); `--set-voice auto --lang ru` goes back to picking automatically.

Voices belong to the current provider (see `/read-aloud:speak-status`). To
change provider, edit the config file it names — or `/read-aloud:voice-setup`
installs the free Kokoro voices.
