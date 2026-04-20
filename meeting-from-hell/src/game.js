// Meeting from Hell — game module.
// Ties together:
//   - upstream MetaHuman viewer (character rendering)
//   - MediaPipe FaceLandmarker (player enthusiasm scoring)
//   - swappable LLM backends (Chrome built-in / Groq / WebLLM / Ollama)
//   - Web Speech API (boss dialogue audio)

import {
  mount as mountViewer,
  startLiveCapture,
} from 'https://smorchj.github.io/metahuman-to-glb/assets/viewer.js';

// ---------------- constants ----------------
const UPSTREAM = 'https://smorchj.github.io/metahuman-to-glb';
// MediaPipe / model URLs / CAPTURE_CALIBRATION all live in upstream
// metahuman-to-glb viewer.js. We never copy them here — we call
// startLiveCapture from upstream and trust its internals. When upstream
// tunes calibration or switches models, this game inherits it for free.

const STARTING_SALARY = 50_000;
// Maximum silence Margaret tolerates before interrupting the employee.
// If the employee STOPS TALKING earlier (jawOpen drops below threshold
// for SILENCE_END_MS continuously), Margaret responds immediately —
// no interrupt, just a reaction. If they keep talking past MAX_WAIT_MS,
// she interrupts and we measure whether they yield.
const MAX_WAIT_MS      = 20_000;
const SILENCE_END_MS   =  1_800;
const TALKING_JAW_MIN  =  0.12;   // jawOpen above this == "currently talking"
const YIELD_GRACE_MS   =  1_500;
// When we do interrupt, pick one of these Margaret-isms at random.
const INTERRUPT_PHRASES = [
  "Sorry — just to touch base on one thing —",
  "Wait — if I can just jump in here —",
  "Actually, let me touch base on that —",
  "If I could pause you there for a moment —",
  "Quick sidebar —",
  "Before you continue — just a thought —",
  "Let me just align on one thing with you —",
  "Sorry to cut in, but —",
  "Circling back for a second — touch base on this —",
  "Right, and if we can just step back there —",
];

const STORAGE_KEY = 'mh_meeting_game_v1';

const SYSTEM_PROMPT = `You ARE Margaret, a painfully earnest middle-manager holding a 1-on-1 check-in with your direct report. Your favourite phrase, by far, is "TOUCH BASE". You say it constantly — at the start of most questions, as the framing for every agenda item. You also love: KPIs, synergies, growth mindset, stretch goals, OKRs, psychological safety, bringing your whole self, Q3 pivots, wellness workshops, culture deck, stakeholder alignment, quarterly deliverables.

ABSOLUTE RULES:
1. Speak in FIRST PERSON, as Margaret. You are the character, not a narrator.
2. Output ONLY the words Margaret says out loud. Nothing else.
3. DO NOT describe her actions, facial expressions, tone, posture, or inner state. Forbidden words and patterns include: "smiles", "her eyes light up", "leans forward", "warmly", "*action*", "[action]", "nods", "(pause)", "she says", "Margaret says".
4. DO NOT use asterisks, brackets, parentheses, or stage directions of any kind.
5. Every turn you ask ONE pointed question your employee must answer. 10–22 words. Use a buzzword. Most questions (at least 2 out of 3) MUST contain the phrase "touch base" — as a verb, a noun, or both.

Examples of correct output (nothing around them):
Just wanted to touch base on how our Q3 KPIs are resonating with you?
Let's touch base real quick on whether you're bringing your whole self to OKR review?
Can we touch base about psychological safety — what does it mean to you, honestly?
Circling back to touch base — how excited are we about mandatory Thursday mindfulness?
Before we move on, can I touch base on your stretch goals for the quarter?`;

const TOPIC_USER_PROMPT = `Ask your employee a new question. Use a corporate buzzword. Do NOT repeat these topics: {past}`;

const FOLLOWUP_USER_PROMPT = `You just had a 1-on-1 exchange with your employee. What you observed:
- Body language / enthusiasm: {vibe}
- What actually happened: {yield_note}

React in ONE short sentence as Margaret. Do NOT ask a follow-up question — just acknowledge what you observed, in character. Pick a tone from:

- They yielded immediately when you cut in → glowing gratitude for "active listening" and "respecting everyone's time".
- They kept talking over your interruption → be POLITELY OFFENDED and gaslight them. Imply THEY were the one who interrupted. "Excuse me, I wasn't quite finished." / "Let's practice active listening here." / "I'd love if we could respect each other's speaking time." NEVER acknowledge that you were the one who actually interrupted first.
- They said nothing and just smiled silently → acknowledge the silence with passive-aggressive corporate concern. "I see we're taking a moment to reflect." / "Your silence is saying a lot." / "Are we… processing this internally?" You find it deeply unsettling but cover with buzzwords.
- They gave a short answer and trailed off → faint condescension. "Hmm. I was hoping we'd get a bit more colour on that."
- They actually answered at some length → treat them well if they also looked enthusiastic; if lukewarm, offer "I appreciate that you're processing this deeply" energy.

Never use the words "yield", "interrupt", "score", "enthusiasm", "vibe", or any number.`;

// ---------------- settings ----------------
const defaultSettings = {
  character: 'ada',
  backend: 'groq',
  groqKey: '',
  model: 'gemma2-9b-it',
  apiUrl: '',
  apiKey: '',
  rounds: 5,
};

function loadSettings() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? { ...defaultSettings, ...JSON.parse(raw) } : { ...defaultSettings };
  } catch { return { ...defaultSettings }; }
}
function saveSettings(s) {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(s)); } catch {}
}

// ---------------- LLM client adapters ----------------
// Common interface: client.complete(system, user) -> Promise<string>
class ChromeBuiltinClient {
  async init(settings) {
    if (!('LanguageModel' in window || (window.ai && window.ai.languageModel))) {
      throw new Error('Chrome built-in Prompt API not available. Try Chrome 127+ with the Prompt API flag.');
    }
    const LM = window.LanguageModel || window.ai.languageModel;
    const availability = LM.availability ? await LM.availability() : 'readily';
    if (availability === 'no') throw new Error('Chrome built-in model unavailable on this device.');
    this.model = await LM.create({ systemPrompt: SYSTEM_PROMPT });
  }
  async complete(system, user) {
    // Chrome's API carries systemPrompt at create-time. Fold any new system
    // text into the user turn for follow-ups.
    const combined = system === SYSTEM_PROMPT ? user : `${system}\n\n${user}`;
    return await this.model.prompt(combined);
  }
  dispose() { try { this.model?.destroy?.(); } catch {} }
}

class OpenAICompatibleClient {
  constructor({ url, key, model }) { this.url = url; this.key = key; this.model = model; }
  async init() { /* no-op, per-request calls */ }
  async complete(system, user) {
    const res = await fetch(this.url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(this.key ? { Authorization: `Bearer ${this.key}` } : {}),
      },
      body: JSON.stringify({
        model: this.model,
        messages: [
          { role: 'system', content: system },
          { role: 'user', content: user },
        ],
        temperature: 0.8,
        max_tokens: 400,
      }),
    });
    if (!res.ok) {
      const body = await res.text().catch(() => '');
      throw new Error(`LLM request failed ${res.status}: ${body.slice(0, 200)}`);
    }
    const data = await res.json();
    return data.choices?.[0]?.message?.content || '';
  }
  dispose() {}
}

// Module-level cache so repeated Start clicks in the same tab reuse the
// initialised engine instead of re-loading 1.5GB into GPU memory.
let _cachedWebllm = null;

class WebLLMClient {
  async init(settings, onProgress) {
    if (!navigator.gpu) {
      throw new Error('WebGPU not available. Need Chrome/Edge/Arc with WebGPU support.');
    }
    this.webllm = await import(/* @vite-ignore */ 'https://esm.run/@mlc-ai/web-llm');
    // Default: Llama-3.2-3B. ~1.8GB, smart enough to ask pointed
    // corporate questions in character and react contextually to the
    // previous exchange. 0.5B / 1B models tend to repeat themselves
    // and miss the "ask a question" framing. 3B is the sweet spot.
    this.modelId = settings.model && settings.model.includes('MLC')
      ? settings.model
      : 'Llama-3.2-3B-Instruct-q4f16_1-MLC';
    this.onProgress = onProgress;
    if (_cachedWebllm && _cachedWebllm.modelId === this.modelId && _cachedWebllm.engine) {
      this.engine = _cachedWebllm.engine;
      return;
    }
    this.engine = await this.webllm.CreateMLCEngine(this.modelId, {
      initProgressCallback: (report) => {
        if (onProgress) onProgress(report.text, report.progress ?? 0);
      },
    });
    _cachedWebllm = { modelId: this.modelId, engine: this.engine };
  }
  async _doComplete(system, user) {
    const reply = await this.engine.chat.completions.create({
      messages: [
        { role: 'system', content: system },
        { role: 'user', content: user },
      ],
      temperature: 0.8,
      max_tokens: 400,
    });
    return reply.choices?.[0]?.message?.content || '';
  }
  async complete(system, user) {
    try {
      return await this._doComplete(system, user);
    } catch (e) {
      const msg = (e && (e.message || String(e))) || '';
      // "A valid external Instance reference no longer exists" = WebGPU
      // adapter got invalidated. Re-create the engine once and retry.
      if (/Instance reference|adapter|device lost/i.test(msg)) {
        console.warn('[webllm] WebGPU instance invalidated; re-initialising engine');
        try { this.engine?.unload?.(); } catch {}
        this.engine = await this.webllm.CreateMLCEngine(this.modelId, {
          initProgressCallback: (report) => {
            if (this.onProgress) this.onProgress(report.text, report.progress ?? 0);
          },
        });
        return await this._doComplete(system, user);
      }
      throw e;
    }
  }
  dispose() { try { this.engine?.unload?.(); } catch {} }
}

async function buildClient(settings, onProgress) {
  let client;
  if (settings.backend === 'chrome') {
    client = new ChromeBuiltinClient();
  } else if (settings.backend === 'groq') {
    if (!settings.groqKey) throw new Error('Groq API key required. Open Settings and paste one.');
    client = new OpenAICompatibleClient({
      url: 'https://api.groq.com/openai/v1/chat/completions',
      key: settings.groqKey,
      model: settings.model || 'gemma2-9b-it',
    });
  } else if (settings.backend === 'ollama') {
    client = new OpenAICompatibleClient({
      url: 'http://localhost:11434/v1/chat/completions',
      key: '',
      model: settings.model || 'gemma3:12b',
    });
  } else if (settings.backend === 'openai') {
    if (!settings.apiUrl) throw new Error('Custom backend needs an API URL.');
    client = new OpenAICompatibleClient({
      url: settings.apiUrl,
      key: settings.apiKey,
      model: settings.model,
    });
  } else if (settings.backend === 'webllm') {
    client = new WebLLMClient();
  } else {
    throw new Error(`Unknown backend: ${settings.backend}`);
  }
  await client.init(settings, onProgress);
  return client;
}

// ---------------- JSON parsing (lenient for small models) ----------------
function extractJson(text) {
  if (!text) throw new Error('empty LLM response');
  // Strip code fences if present.
  const stripped = text.replace(/```json\s*|\s*```/gi, '').trim();
  // Find first { ... } block heuristically.
  const start = stripped.indexOf('{');
  const end = stripped.lastIndexOf('}');
  if (start < 0 || end < 0) throw new Error(`no JSON in LLM response: ${text.slice(0, 120)}`);
  return JSON.parse(stripped.slice(start, end + 1));
}

// ---------------- speech synthesis ----------------
async function pickVoice() {
  // Voices load asynchronously in most browsers.
  const tries = 20;
  for (let i = 0; i < tries; i++) {
    const voices = speechSynthesis.getVoices();
    if (voices.length > 0) {
      // Prefer a female en-US voice for Margaret.
      const prefer = voices.find((v) => v.lang.startsWith('en') && /female|zira|samantha|karen|google us/i.test(v.name));
      return prefer || voices.find((v) => v.lang.startsWith('en')) || voices[0];
    }
    await new Promise((r) => setTimeout(r, 100));
  }
  return null;
}

async function speak(text, voice) {
  return new Promise((resolve) => {
    if (!('speechSynthesis' in window)) return resolve();
    const u = new SpeechSynthesisUtterance(text);
    if (voice) u.voice = voice;
    u.rate = 1.02;
    u.pitch = 1.06;
    u.onend = () => resolve();
    u.onerror = () => resolve();
    speechSynthesis.cancel();
    speechSynthesis.speak(u);
  });
}

// ---------------- face capture: delegate to upstream startLiveCapture ----------------
// All face-cap logic (MediaPipe init, camera, calibration, audio visemes,
// blendshape driving) lives in the upstream viewer module. We supply the
// morphMeshes from the mounted character and a setInfluence shim that
// also tees values into `latestInfluence` so the round loop can read
// calibrated smile scores for salary scoring.
const morphMeshes = []; // [{ dict, influences }], filled post-mount
const latestInfluence = Object.create(null);
const setInfluence = (keyName, v) => {
  for (const { dict, influences } of morphMeshes) {
    const idx = dict[keyName];
    if (idx !== undefined) influences[idx] = v;
  }
  latestInfluence[keyName] = v;
};

async function initFaceCapture(container, statusFn) {
  const statusEl = { set textContent(msg) { statusFn(msg); } };
  return await startLiveCapture({ container, morphMeshes, setInfluence, statusEl });
}

function scoreFromLatest() {
  const smile = ((latestInfluence.mouthSmileLeft || 0) + (latestInfluence.mouthSmileRight || 0)) / 2;
  const brow  = latestInfluence.browInnerUp || 0;
  const frown = ((latestInfluence.mouthFrownLeft || 0) + (latestInfluence.mouthFrownRight || 0)) / 2;
  return Math.max(0, Math.min(1, smile + 0.2 * brow - 0.6 * frown));
}

function scoreToVibe(score) {
  if (score >= 0.70) return 'giddy';
  if (score >= 0.50) return 'enthusiastic';
  if (score >= 0.30) return 'politely engaged';
  if (score >= 0.15) return 'lukewarm';
  return 'visibly checked out';
}

// Collect reaction samples while Margaret waits for the employee to answer.
//
// Two ways the window ends:
//   (a) EARLY: employee's jawOpen has been below TALKING_JAW_MIN for
//       SILENCE_END_MS continuously → they're done (or silent-smiling) →
//       Margaret responds immediately, NO interrupt.
//   (b) INTERRUPT: employee is STILL talking at MAX_WAIT_MS → Margaret
//       barges in with a canned line. We then measure jawOpen for
//       YIELD_GRACE_MS to see if they yield.
//
// Also tracks total talking time so scoring can reward actually speaking
// over silent-smiling.
async function collectReactionWindow(onTick, voice) {
  const samples = [];
  const jawSamples = [];
  let lastTalkingAt = -1;        // performance.now() of most recent "talking" frame
  let firstTalkingAt = -1;        // first time they opened their mouth
  let talkingAccumMs = 0;         // cumulative "talking" time
  let interruptFired = false;
  let baselineJaw = 0;
  let yielded = null;
  let endReason = 'max';          // 'silence' | 'max'

  const start = performance.now();
  const preJawBuffer = [];

  await new Promise((resolve) => {
    let lastTickAt = start;
    const tick = () => {
      const now = performance.now();
      const elapsed = now - start;
      const dt = now - lastTickAt;
      lastTickAt = now;

      const score = scoreFromLatest();
      samples.push(score);
      const jaw = latestInfluence.jawOpen || 0;
      jawSamples.push(jaw);
      onTick(score, Math.min(1, elapsed / MAX_WAIT_MS));

      const isTalking = jaw > TALKING_JAW_MIN;
      if (isTalking) {
        talkingAccumMs += dt;
        if (firstTalkingAt < 0) firstTalkingAt = now;
        lastTalkingAt = now;
      }

      // (a) Early end: employee has been quiet for SILENCE_END_MS.
      // Grace of 500ms at the start so we don't fire the instant the
      // question ends (they need time to draw breath).
      const silentSince = lastTalkingAt > 0 ? (now - lastTalkingAt) : elapsed;
      if (elapsed > 500 && silentSince >= SILENCE_END_MS && !interruptFired) {
        endReason = 'silence';
        return resolve();
      }

      if (!interruptFired) {
        preJawBuffer.push(jaw);
        if (preJawBuffer.length > 30) preJawBuffer.shift();
      }

      // (b) Max time reached AND still talking → Margaret interrupts.
      if (!interruptFired && elapsed >= MAX_WAIT_MS) {
        interruptFired = true;
        endReason = 'max';
        baselineJaw = preJawBuffer.reduce((a, b) => a + b, 0) / (preJawBuffer.length || 1);
        const phrase = INTERRUPT_PHRASES[Math.floor(Math.random() * INTERRUPT_PHRASES.length)];
        showBubble(phrase);
        speak(phrase, voice); // fire-and-forget
        setTimeout(() => {
          const postJaw = latestInfluence.jawOpen || 0;
          yielded = (baselineJaw - postJaw) > 0.08 || postJaw < 0.08;
          console.log('[game][interrupt] baseline=%s post=%s → yielded=%s', baselineJaw.toFixed(2), postJaw.toFixed(2), yielded);
        }, YIELD_GRACE_MS);
        // Let the grace window play out before resolving.
        setTimeout(() => resolve(), YIELD_GRACE_MS + 200);
        return;
      }

      requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  });

  const talkingSec = talkingAccumMs / 1000;
  return { samples, yielded, interrupted: interruptFired, endReason, talkingSec };
}

function summarise(samples) {
  if (samples.length === 0) return { mean: 0, p50: 0, max: 0 };
  const sorted = [...samples].sort((a, b) => a - b);
  const mean = sorted.reduce((a, b) => a + b, 0) / sorted.length;
  return {
    mean: +mean.toFixed(3),
    p50: +sorted[Math.floor(sorted.length * 0.5)].toFixed(3),
    max: +sorted[sorted.length - 1].toFixed(3),
  };
}

// ---------------- UI ----------------
const $ = (sel) => document.querySelector(sel);

const state = {
  settings: loadSettings(),
  salary: STARTING_SALARY,
  round: 0,
  totalRounds: 5,
  history: [],
};

function setStatus(msg) { $('#status-line').textContent = msg; }

function showScreen(name) {
  // name === null  → hide every screen (we're in-game)
  // name === <id>  → show that screen, hide the rest
  for (const id of ['splash', 'settings', 'endscreen']) {
    const el = document.getElementById(id);
    if (el) el.classList.toggle('hidden', id !== name);
  }
  $('#hud').classList.toggle('hidden', name !== null);
  $('#bubble').classList.add('hidden');
  $('#enthusiasm').classList.add('hidden');
}

function updateSalary(delta) {
  state.salary = Math.max(0, Math.round(state.salary + delta));
  $('#salary-amount').textContent = state.salary.toLocaleString();
  const deltaEl = $('#salary-delta');
  if (delta === 0) { deltaEl.textContent = ''; return; }
  deltaEl.textContent = (delta >= 0 ? '+ $' : '− $') + Math.abs(Math.round(delta)).toLocaleString();
  deltaEl.className = 'salary-delta ' + (delta >= 0 ? 'up' : 'down');
  setTimeout(() => { deltaEl.textContent = ''; }, 2200);
}

function showBubble(text) {
  $('#bubble-text').textContent = text;
  $('#bubble').classList.remove('hidden');
}
function hideBubble() { $('#bubble').classList.add('hidden'); }

function showMeter() { $('#enthusiasm').classList.remove('hidden'); }
function hideMeter() { $('#enthusiasm').classList.add('hidden'); }
function updateMeter(score, progress) {
  $('#meter-fill').style.width = Math.round(score * 100) + '%';
  const remaining = MAX_WAIT_MS / 1000 * (1 - progress);
  $('#meter-timer').textContent = remaining.toFixed(1) + 's';
}

// Settings UI: show fields conditional on backend.
function syncSettingsFields() {
  const backend = $('#cfg-backend').value;
  for (const f of document.querySelectorAll('#settings .field[data-for]')) {
    const show = f.dataset.for.split(',').includes(backend);
    f.style.display = show ? '' : 'none';
  }
}

function openSettings() {
  const s = state.settings;
  $('#cfg-character').value = s.character;
  $('#cfg-backend').value = s.backend;
  $('#cfg-groq-key').value = s.groqKey;
  $('#cfg-model').value = s.model;
  $('#cfg-url').value = s.apiUrl;
  $('#cfg-api-key').value = s.apiKey;
  $('#cfg-rounds').value = s.rounds;
  syncSettingsFields();
  showScreen('settings');
}

function closeSettingsSaving() {
  state.settings.character = $('#cfg-character').value;
  state.settings.backend   = $('#cfg-backend').value;
  state.settings.groqKey   = $('#cfg-groq-key').value.trim();
  state.settings.model     = $('#cfg-model').value.trim() || defaultSettings.model;
  state.settings.apiUrl    = $('#cfg-url').value.trim();
  state.settings.apiKey    = $('#cfg-api-key').value.trim();
  state.settings.rounds    = Math.max(1, Math.min(20, parseInt($('#cfg-rounds').value, 10) || 5));
  saveSettings(state.settings);
  showScreen('splash');
}

// ---------------- round loop ----------------
async function runGame() {
  showScreen(null);

  setStatus('loading character…');
  try {
    const cid = state.settings.character;
    const handle = await mountViewer($('#viewer'), {
      glbUrl: `${UPSTREAM}/characters/${cid}/${cid}.glb`,
      mappingUrl: `${UPSTREAM}/characters/${cid}/mh_materials.json`,
      autoRotate: false,
      interactive: false,
    });
    // Collect every mesh that has morph targets so we can drive
    // blendshapes live from the player's face. Same shape as upstream
    // viewer's internal `morphMeshes`.
    morphMeshes.length = 0;
    handle.gltf.scene.traverse((obj) => {
      if ((obj.isMesh || obj.isSkinnedMesh) && obj.morphTargetDictionary && obj.morphTargetInfluences) {
        morphMeshes.push({ dict: obj.morphTargetDictionary, influences: obj.morphTargetInfluences });
      }
    });
    console.log(`[game] character loaded with ${morphMeshes.length} morph meshes`);
  } catch (err) {
    console.error('[game] character mount failed', err);
    showScreen('splash');
    showFatalBanner('Character mount failed: ' + (err?.message || err));
    return;
  }

  let voice = null, client = null;
  try {
    voice = await pickVoice();
    // Order matters: initialise the camera + MediaPipe FIRST so the
    // browser has settled on its GPU resource allocation before WebLLM
    // grabs its WebGPU adapter. Reverse order causes the WebLLM adapter
    // to be invalidated once webcam starts on Windows.
    setStatus('initialising face capture…');
    await initFaceCapture($('#viewer'), setStatus);
    setStatus('initialising LLM…');
    if (state.settings.backend === 'webllm') showLoadingOverlay('Loading model…', 'First visit downloads ~1.5 GB. Cached after that.');
    client = await buildClient(state.settings, (msg, pct) => {
      if (state.settings.backend === 'webllm') {
        updateLoadingOverlay(msg, pct);
      } else {
        setStatus(msg);
      }
    });
    hideLoadingOverlay();
  } catch (err) {
    console.error('[game] startup failed', err);
    hideLoadingOverlay();
    showScreen('splash');
    showFatalBanner('Startup failed: ' + (err?.message || err));
    return;
  }

  state.salary = STARTING_SALARY;
  state.round = 0;
  state.history = [];
  $('#round-total').textContent = '∞';
  updateSalary(0);
  setStatus('');

  // Strip anything that looks like labels, quotes, JSON, or stage-direction
  // narration that a small LLM accidentally spits out even when told not to.
  // Leave a plain first-person sentence that Margaret would actually speak.
  const cleanSentence = (raw) => {
    if (!raw) return '';
    let t = raw.trim();
    // Drop code fences.
    t = t.replace(/```[a-z]*\s*|\s*```/gi, '');
    // If it's JSON, grab any top-level dialogue/text field.
    if (t.startsWith('{')) {
      try {
        const j = JSON.parse(t);
        t = j.dialogue || j.text || j.message || Object.values(j).find((v) => typeof v === 'string') || t;
      } catch {}
    }
    // Strip stage directions: *her eyes light up*, [warmly], (pauses).
    t = t.replace(/\*[^*]*\*/g, ' ');       // *...*
    t = t.replace(/\[[^\]]*\]/g, ' ');      // [...]
    t = t.replace(/\([^)]*\)/g, ' ');       // (...)
    // Strip emoji action markers a lot of small models emit.
    t = t.replace(/[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}]/gu, ' ');
    // Strip leading role labels like `Margaret:` or `MARGARET -`.
    t = t.replace(/^\s*(margaret|boss|manager|hr|she|her)\s*[:\-—]\s*/i, '');
    // Strip wrapping quotes.
    t = t.replace(/^["'`]+|["'`]+$/g, '').trim();
    // Collapse whitespace.
    t = t.replace(/\s+/g, ' ').trim();

    // If the sentence is clearly third-person narration (leads with
    // "Margaret/She + verb"), drop it and try the next sentence.
    const narratorLead = /^(margaret|she|he|her|his)\b/i;
    if (narratorLead.test(t)) {
      // Try to find a first-person-ish continuation after the next period.
      const rest = t.replace(/^[^.!?]*[.!?]\s*/, '');
      if (rest && !narratorLead.test(rest)) t = rest;
    }

    // First sentence only if it ran long.
    if (t.length > 260) {
      const m = t.match(/[^.!?]*[.!?]/);
      if (m) t = m[0].trim();
    }
    return t;
  };

  let fatalError = null;
  let completedRounds = 0;
  let i = 0;
  while (true) {
    i += 1;
    state.round = i;
    $('#round-n').textContent = i;

    let topicLine;
    try {
      const past = state.history.slice(-8).map((h) => h.topic).join('; ') || '(none yet)';
      const raw = await client.complete(SYSTEM_PROMPT, TOPIC_USER_PROMPT.replace('{past}', past));
      topicLine = cleanSentence(raw);
      if (!topicLine) throw new Error('empty topic from LLM');
    } catch (err) {
      console.error('[game] LLM topic gen failed', err);
      fatalError = err?.message || String(err);
      break;
    }

    showBubble(topicLine);
    await speak(topicLine, voice);

    showMeter();
    const { samples, yielded, interrupted, talkingSec } = await collectReactionWindow(
      (score, progress) => updateMeter(score, progress),
      voice,
    );
    hideMeter();

    const s = summarise(samples);
    const multiplier = 4500 + Math.min(i, 10) * 500;
    // Base: enthusiasm from smile (0..1 → −0.35..+0.65 scaled by multiplier).
    let delta = multiplier * (s.p50 - 0.35);
    // Talking bonus: reward actually opening your mouth and speaking. Cap
    // at 4 seconds of cumulative talk time.
    const talkingBonus = multiplier * 0.3 * Math.min(1, talkingSec / 4);
    delta += talkingBonus;
    // Yield modifiers (only when she actually interrupted).
    if (yielded === true)  delta += multiplier * 0.5;   // bonus for shutting up
    if (yielded === false) delta -= multiplier * 0.7;   // penalty for "interrupting her"
    updateSalary(delta);
    state.history.push({ topic: topicLine.split(/\s+/).slice(0, 6).join(' '), score: s.p50 });

    // Build the talk/yield context for the follow-up LLM call.
    let talkNote;
    if (interrupted) {
      talkNote = yielded === true
        ? 'they were talking and then went IMMEDIATELY silent when you cut in — perfect deference'
        : 'they RUDELY kept talking right over you when you started speaking — in their head, THEY were the one being interrupted';
    } else if (talkingSec < 0.6) {
      // Never really opened their mouth — just silent-smiling.
      talkNote = s.p50 > 0.3
        ? 'they said absolutely nothing and just sat there smiling silently at you — creepy vibes'
        : 'they said absolutely nothing and just stared blankly at you — no engagement whatsoever';
    } else if (talkingSec < 2.5) {
      talkNote = 'they gave a very brief answer and trailed off almost immediately';
    } else {
      talkNote = `they actually answered at some length (${talkingSec.toFixed(1)}s) before stopping on their own`;
    }

    try {
      const raw = await client.complete(
        SYSTEM_PROMPT,
        FOLLOWUP_USER_PROMPT
          .replace('{vibe}', scoreToVibe(s.p50))
          .replace('{yield_note}', talkNote),
      );
      const followup = cleanSentence(raw);
      if (followup) {
        showBubble(followup);
        await speak(followup, voice);
      }
    } catch (err) {
      console.warn('[game] follow-up failed, skipping', err);
    }
    hideBubble();
    completedRounds += 1;
    await new Promise((r) => setTimeout(r, 600));
  }

  // Only path out of the while(true) is break → fatalError. Show banner.
  if (completedRounds === 0) {
    showScreen('splash');
    showFatalBanner(fatalError || 'Meeting couldn\'t start. Check the console.');
  } else {
    // We ran some rounds then errored — go back to splash but keep the
    // error around. Salary stays in HUD for reference.
    showFatalBanner('Meeting cut short: ' + (fatalError || 'unknown error'));
  }
}

function showFatalBanner(msg) {
  let banner = document.getElementById('fatal-banner');
  if (!banner) {
    banner = document.createElement('div');
    banner.id = 'fatal-banner';
    banner.style.cssText = 'position:absolute;top:80px;left:50%;transform:translateX(-50%);background:#3a0f15;color:#ffd6de;border:1px solid #ff5a7a;border-radius:8px;padding:12px 18px;max-width:640px;font-size:13px;line-height:1.5;z-index:200';
    document.body.appendChild(banner);
  }
  const safe = msg.replace(/</g, '&lt;');
  const backend = state.settings.backend;
  let hint = '';
  if (backend === 'groq') {
    hint = `<br/><br/>Check your Groq key at <a href="https://console.groq.com/keys" target="_blank" rel="noopener" style="color:#ffd6de;text-decoration:underline">console.groq.com/keys</a>. A valid key starts with <code>gsk_</code>.`;
  } else if (backend === 'chrome') {
    hint = `<br/><br/>Chrome's built-in Prompt API isn't enabled here. Try switching <b>LLM backend</b> to <b>WebLLM</b> — runs locally via WebGPU, downloads the model on first visit, then works forever offline.`;
  } else if (backend === 'webllm') {
    hint = `<br/><br/>WebLLM needs WebGPU (Chrome/Edge/Arc) and the model download to succeed. Check devtools console for a specific failure.`;
  } else if (backend === 'ollama') {
    hint = `<br/><br/>Is Ollama running on this machine? Start it with <code>ollama serve</code>, and make sure you ran <code>OLLAMA_ORIGINS=* ollama serve</code> so the browser can reach it cross-origin.`;
  }
  banner.innerHTML = `<b>Meeting couldn't start.</b><br/>${safe}${hint}`;
  banner.style.display = 'block';
  setTimeout(() => { if (banner) banner.style.display = 'none'; }, 45_000);
}

function showLoadingOverlay(title, subtitle) {
  let ov = document.getElementById('loading-overlay');
  if (!ov) {
    ov = document.createElement('div');
    ov.id = 'loading-overlay';
    ov.innerHTML = `
      <div class="lo-card">
        <div class="lo-title"></div>
        <div class="lo-subtitle"></div>
        <div class="lo-bar"><div class="lo-fill"></div></div>
        <div class="lo-pct"></div>
        <div class="lo-msg"></div>
      </div>`;
    ov.style.cssText = 'position:fixed;inset:0;background:rgba(5,7,13,0.94);display:flex;align-items:center;justify-content:center;z-index:300;font-family:inherit';
    ov.querySelector('.lo-card').style.cssText = 'max-width:520px;text-align:center;padding:32px;background:#0f1424;border:1px solid #2a2f3a;border-radius:12px';
    ov.querySelector('.lo-title').style.cssText = 'font-size:22px;font-weight:600;margin-bottom:6px';
    ov.querySelector('.lo-subtitle').style.cssText = 'font-size:13px;color:#8795b8;margin-bottom:22px';
    ov.querySelector('.lo-bar').style.cssText = 'height:8px;background:#1a1530;border-radius:4px;overflow:hidden;margin-bottom:8px';
    ov.querySelector('.lo-fill').style.cssText = 'height:100%;width:0;background:linear-gradient(90deg,#06B6D4,#c084fc);transition:width 200ms linear';
    ov.querySelector('.lo-pct').style.cssText = 'font-size:12px;color:#8795b8;margin-bottom:8px';
    ov.querySelector('.lo-msg').style.cssText = 'font-size:11px;color:#8795b8;line-height:1.4';
    document.body.appendChild(ov);
  }
  ov.querySelector('.lo-title').textContent = title;
  ov.querySelector('.lo-subtitle').textContent = subtitle || '';
  ov.style.display = 'flex';
}
function updateLoadingOverlay(msg, pct) {
  const ov = document.getElementById('loading-overlay');
  if (!ov) return;
  ov.querySelector('.lo-msg').textContent = msg || '';
  ov.querySelector('.lo-fill').style.width = (Math.max(0, Math.min(1, pct || 0)) * 100).toFixed(1) + '%';
  ov.querySelector('.lo-pct').textContent = ((pct || 0) * 100).toFixed(1) + '%';
}
function hideLoadingOverlay() {
  const ov = document.getElementById('loading-overlay');
  if (ov) ov.style.display = 'none';
}

// ---------------- wiring ----------------
document.addEventListener('DOMContentLoaded', () => {
  // Populate initial UI from settings.
  $('#round-total').textContent = state.settings.rounds;

  $('#btn-start').addEventListener('click', () => runGame());
  $('#btn-settings').addEventListener('click', () => openSettings());
  $('#btn-settings-save').addEventListener('click', () => closeSettingsSaving());
  $('#btn-settings-cancel').addEventListener('click', () => showScreen('splash'));
  $('#cfg-backend').addEventListener('change', syncSettingsFields);
  // End-screen buttons removed (infinite rounds now) — leaving listener
  // guards in case template still has the nodes.
  const again = $('#btn-again'); if (again) again.addEventListener('click', () => runGame());
  const home  = $('#btn-home');  if (home)  home.addEventListener('click', () => showScreen('splash'));

  // Voices may not be ready until an event fires.
  if ('speechSynthesis' in window) {
    speechSynthesis.onvoiceschanged = () => {};
  }

  showScreen('splash');
});
