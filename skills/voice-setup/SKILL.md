---
description: Install the free Kokoro neural voice (one-time ~340MB download; English and 8 other languages, no Russian)
disable-model-invocation: true
---

Run this bash command with a **10-minute timeout** and relay its progress
lines to the user as they appear (it creates a private virtualenv, installs
kokoro-onnx, and downloads the voice model — a few minutes on first run):

```
sh "${CLAUDE_PLUGIN_ROOT}/scripts/speak.sh" --setup kokoro --turn "${CLAUDE_SESSION_ID}"
```

When it finishes successfully, demo the new voice:

```
sh "${CLAUDE_PLUGIN_ROOT}/scripts/speak.sh" --detach --text "Kokoro is installed. This is your new reading voice." --turn "${CLAUDE_SESSION_ID}"
```

Then tell the user setup is done and that `voice` in the config file the
output named can be any of the 54 Kokoro voices (am_michael, af_heart,
bf_emma, …). Kokoro has no Russian voice: Russian sentences keep being read
by the system's Russian voice. If either step fails, show the error and do
not guess at fixes.
