---
description: Show read-aloud configuration (provider, voices, auto-read, last error)
disable-model-invocation: true
---

Run exactly this one bash command and show the user its output verbatim:

```
sh "${CLAUDE_PLUGIN_ROOT}/scripts/speak.sh" --status --turn "${CLAUDE_SESSION_ID}"
```

Then, if the user wants changes, edit the config file the output names —
fields: `provider` (system / kokoro / speechify / elevenlabs / openai / command),
`voice`, `voices` (system voice per language, e.g. `{"ru": "Microsoft Irina
Desktop"}`), `speed`, `auto_read`, `stop_on_prompt`. API keys belong in
environment variables (`SPEECHIFY_API_KEY`, `ELEVENLABS_API_KEY`,
`OPENAI_API_KEY`), not the file.
