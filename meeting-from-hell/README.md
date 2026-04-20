# Meeting from Hell

A browser game where a **locally-running LLM** is your middle-manager,
and your **face** is the controller. Fake enthusiasm during absurd
corporate check-ins. Your detected enthusiasm drives the character's
reaction AND your in-game salary.

> **Built on** the open-source [MetaHuman → GLB pipeline][mh-pipeline].
> The game loads the pipeline's deployed viewer + characters over HTTPS
> from GitHub Pages, so upstream improvements show up automatically —
> no submodule, no duplicated assets.

[mh-pipeline]: https://github.com/smorchj/metahuman-to-glb

## Why this exists

Every "watch Claude build a game from scratch" video hits the same wall:
the AI can hammer out code, but the art is always cubes or bad
AI-generated slop. **Flip that.** Ship Claude a quality rigged character
on turn one and see what gameplay falls out.

What also falls out: a demo of **three bleeding-edge capabilities wired
together in the browser**:

1. **Browser-hostable LLM** — Chrome's built-in Gemini Nano
   (`LanguageModel` API) or WebLLM (`@mlc-ai/web-llm`) runs the boss's
   dialogue without a server.
2. **Face-rig as controller** — MediaPipe FaceLandmarker classifies the
   player's enthusiasm in real time; the character mirrors it.
3. **Audio-driven viseme lipsync** — the character's synthesized voice
   drives its own mouth movement via FFT band analysis in the upstream
   viewer.

## Run it

```bash
# Build site/ from src/
python build.py

# Serve locally
python -m http.server 8000 -d site
# open http://localhost:8000/
```

Game needs camera + microphone permission. Pick an LLM backend in
Settings (Chrome built-in is the zero-install option; Groq free tier
works everywhere else).

## Dependencies on MetaHuman → GLB

The game makes HTTPS requests to the deployed pipeline site:

| Resource | URL |
|---|---|
| Viewer module | `https://smorchj.github.io/metahuman-to-glb/assets/viewer.js` |
| Ada GLB | `https://smorchj.github.io/metahuman-to-glb/characters/ada/ada.glb` |
| Ada material map | `https://smorchj.github.io/metahuman-to-glb/characters/ada/mh_materials.json` |
| Taro GLB / map | `.../characters/taro/...` |

These URLs can be overridden in `src/game.js` if you fork both projects
together and want local-only serving.

## LLM backends

Ranked by demo-ability for a drive-by visitor:

| Backend | First load | Requires | Story |
|---|---|---|---|
| **Chrome built-in** (`LanguageModel`) | 0 MB | Chrome/Edge w/ Prompt API | Zero install, fully private, instant. |
| **Groq** hosted Gemma | 0 MB | User's free API key | Fast, reliable, needs sign-up. |
| **WebLLM** | 1–3 GB one-time | WebGPU browser | Fully offline after first load. |
| **Ollama** local | 0 MB (if installed) | User running Ollama | Privacy maximalists. |

The common interface is a tiny `LLMClient` shape:

```js
const client = await getClient(settings);
const text = await client.complete({
  system:  "You are Margaret, middle-manager...",
  user:    "Generate the next round topic...",
  schema:  "json",
});
```

See `src/game.js` → `buildClient()` for the adapters.

## Face-capture scoring

We sum MediaPipe blendshape influences from the reaction window:

```
enthusiasm = clamp(
    0.50 * (smileL + smileR) / 2
  + 0.20 * (cheekSquintL + cheekSquintR) / 2
  + 0.15 * browInnerUp
  + 0.15 * max(0, eyeWideL + eyeWideR) / 2
  - 0.40 * (frownL + frownR) / 2
  - 0.30 * (browDownL + browDownR) / 2
  , 0, 1)
```

Salary delta per round = `round_multiplier * (enthusiasm_p50 − 0.35)`.

## Round loop

1. LLM generates `{ topic, dialogue, required_expression }`.
2. SpeechSynthesis speaks `dialogue` aloud.
3. Character audio-viseme layer (from upstream viewer) lipsyncs to the
   synthesized voice automatically.
4. Reaction window opens when TTS ends. Enthusiasm sampled at ~30 Hz.
5. Score → salary delta. LLM generates a ≤1-sentence follow-up.
6. Loop for `rounds` (default 5).
7. Final screen: total salary change + an end-of-meeting blurb.

## License

MIT. PRs welcome.
