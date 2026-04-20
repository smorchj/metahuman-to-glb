// Meeting from Hell — game module.
// Ties together:
//   - upstream MetaHuman viewer (character rendering)
//   - MediaPipe FaceLandmarker (player enthusiasm scoring)
//   - swappable LLM backends (Chrome built-in / Groq / WebLLM / Ollama)
//   - Web Speech API (boss dialogue audio)

import { mount as mountViewer } from 'https://smorchj.github.io/metahuman-to-glb/assets/viewer.js';

// ---------------- constants ----------------
const UPSTREAM = 'https://smorchj.github.io/metahuman-to-glb';
const MEDIAPIPE_BUNDLE = 'https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.14/+esm';
const MEDIAPIPE_WASM   = 'https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.14/wasm';
const FACE_MODEL       = 'https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task';

const STARTING_SALARY = 50_000;
const REACTION_WINDOW_MS = 5_000;
const STORAGE_KEY = 'mh_meeting_game_v1';

const SYSTEM_PROMPT = `You are MARGARET, a painfully earnest middle-manager running a weekly employee check-in. You genuinely believe every corporate buzzword you say. Topics: KPIs, synergies, growth mindset, stretch goals, OKRs, "bringing your whole self," psychological safety, mandatory wellness workshops, Q3 pivots, culture deck.

Always output STRICT JSON, no code fences, no preamble. Schema:
{
  "topic": "<short label, 3-6 words>",
  "dialogue": "<Margaret's spoken delivery: 2-3 sentences, 40-80 words. Casual, peppy, uses at least one real corporate buzzword. No stage directions.>",
  "required_expression": "<one of: enthusiastic, thoughtful, engaged, excited, concerned-but-professional>"
}`;

const FOLLOWUP_PROMPT = `Previous topic: {topic}
Player's enthusiasm during reaction (0-1): {score}

Respond AS MARGARET in ONE sentence (max 25 words) based on what you just detected:
- High enthusiasm (>0.6): warm validation, maybe hint at a "growth opportunity"
- Mid (0.35-0.6): mild concern disguised as concern, something like "I appreciate your measured response"
- Low (<0.35): passive-aggressive worry, "noting this for our next one-on-one"

Output STRICT JSON: {"dialogue": "..."}`;

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

class WebLLMClient {
  async init() {
    throw new Error('WebLLM backend not yet wired in this MVP — use Groq or Chrome built-in for now.');
  }
}

async function buildClient(settings) {
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
  await client.init(settings);
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

// ---------------- face capture ----------------
let landmarker = null;
let videoEl = null;

async function initFaceCapture(statusFn) {
  statusFn('loading MediaPipe…');
  const vision = await import(/* @vite-ignore */ MEDIAPIPE_BUNDLE);
  const { FaceLandmarker, FilesetResolver } = vision;
  const fileset = await FilesetResolver.forVisionTasks(MEDIAPIPE_WASM);
  landmarker = await FaceLandmarker.createFromOptions(fileset, {
    baseOptions: { modelAssetPath: FACE_MODEL, delegate: 'GPU' },
    runningMode: 'VIDEO',
    numFaces: 1,
    outputFaceBlendshapes: true,
    outputFacialTransformationMatrixes: false,
    minFaceDetectionConfidence: 0.2,
    minFacePresenceConfidence: 0.2,
    minTrackingConfidence: 0.2,
  });

  statusFn('opening camera…');
  const stream = await navigator.mediaDevices.getUserMedia({
    video: { facingMode: 'user', width: { ideal: 1280 }, height: { ideal: 720 } },
    audio: false,
  });
  videoEl = document.createElement('video');
  videoEl.autoplay = true; videoEl.playsInline = true; videoEl.muted = true;
  videoEl.srcObject = stream;
  videoEl.style.cssText = 'position:fixed;bottom:12px;right:12px;width:180px;border:1px solid #2a2f3a;border-radius:8px;transform:scaleX(-1);z-index:50;background:#000;pointer-events:none';
  document.body.appendChild(videoEl);
  await new Promise((resolve) => videoEl.addEventListener('loadedmetadata', resolve, { once: true }));
  await videoEl.play();
  statusFn('tracking');
}

function readBlendshapes() {
  if (!landmarker || !videoEl || videoEl.readyState < 2) return null;
  const res = landmarker.detectForVideo(videoEl, performance.now());
  const cats = res?.faceBlendshapes?.[0]?.categories;
  if (!cats) return null;
  const map = Object.create(null);
  for (const c of cats) map[c.categoryName] = c.score;
  return map;
}

function scoreEnthusiasm(bs) {
  if (!bs) return 0;
  const g = (k) => bs[k] || 0;
  const smile = (g('mouthSmileLeft') + g('mouthSmileRight')) / 2;
  const cheek = (g('cheekSquintLeft') + g('cheekSquintRight')) / 2;
  const brow  = g('browInnerUp');
  const wide  = Math.max(0, (g('eyeWideLeft') + g('eyeWideRight')) / 2);
  const frown = (g('mouthFrownLeft') + g('mouthFrownRight')) / 2;
  const down  = (g('browDownLeft') + g('browDownRight')) / 2;
  const raw = 0.50 * smile + 0.20 * cheek + 0.15 * brow + 0.15 * wide - 0.40 * frown - 0.30 * down;
  return Math.max(0, Math.min(1, raw));
}

async function collectReactionWindow(onTick) {
  const samples = [];
  const start = performance.now();
  return new Promise((resolve) => {
    const tick = () => {
      const elapsed = performance.now() - start;
      if (elapsed >= REACTION_WINDOW_MS) return resolve(samples);
      const bs = readBlendshapes();
      const score = scoreEnthusiasm(bs);
      samples.push(score);
      onTick(score, elapsed / REACTION_WINDOW_MS);
      requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  });
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
  for (const id of ['splash', 'settings', 'endscreen']) {
    const el = document.getElementById(id);
    if (el) el.classList.toggle('hidden', id !== name && name !== null);
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
  const remaining = REACTION_WINDOW_MS / 1000 * (1 - progress);
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
    await mountViewer($('#viewer'), {
      glbUrl: `${UPSTREAM}/characters/${cid}/${cid}.glb`,
      mappingUrl: `${UPSTREAM}/characters/${cid}/mh_materials.json`,
      autoRotate: false,
      interactive: false,
    });
  } catch (err) {
    console.error('[game] character mount failed', err);
    setStatus('character mount failed: ' + (err?.message || err));
    showScreen('splash');
    return;
  }

  let voice = null, client = null;
  try {
    voice = await pickVoice();
    setStatus('initialising LLM…');
    client = await buildClient(state.settings);
    setStatus('initialising face capture…');
    await initFaceCapture(setStatus);
  } catch (err) {
    console.error('[game] startup failed', err);
    setStatus('startup failed: ' + (err?.message || err));
    showScreen('splash');
    return;
  }

  state.salary = STARTING_SALARY;
  state.round = 0;
  state.history = [];
  state.totalRounds = state.settings.rounds;
  $('#round-total').textContent = state.totalRounds;
  updateSalary(0);
  setStatus('');

  for (let i = 1; i <= state.totalRounds; i++) {
    state.round = i;
    $('#round-n').textContent = i;

    let topic;
    try {
      const raw = await client.complete(
        SYSTEM_PROMPT,
        `Generate round ${i} of ${state.totalRounds}. Pick a topic we have NOT covered: ${state.history.map(h => h.topic).join('; ') || '(none yet)'}`,
      );
      topic = extractJson(raw);
    } catch (err) {
      console.error('[game] LLM topic gen failed', err);
      setStatus('LLM failed: ' + (err?.message || err));
      break;
    }

    showBubble(topic.dialogue);
    await speak(topic.dialogue, voice);

    showMeter();
    const samples = await collectReactionWindow((score, progress) => updateMeter(score, progress));
    hideMeter();

    const s = summarise(samples);
    const multiplier = 4500 + i * 500; // rounds get higher stakes
    const delta = multiplier * (s.p50 - 0.35);
    updateSalary(delta);
    state.history.push({ topic: topic.topic, score: s.p50, delta });

    // Follow-up reaction.
    try {
      const raw = await client.complete(
        SYSTEM_PROMPT,
        FOLLOWUP_PROMPT.replace('{topic}', topic.topic).replace('{score}', s.p50.toFixed(2)),
      );
      const followup = extractJson(raw);
      showBubble(followup.dialogue);
      await speak(followup.dialogue, voice);
    } catch (err) {
      console.warn('[game] follow-up failed, skipping', err);
    }
    hideBubble();
    await new Promise((r) => setTimeout(r, 600));
  }

  endGame();
}

function endGame() {
  const delta = state.salary - STARTING_SALARY;
  $('#end-salary').textContent = state.salary.toLocaleString();
  $('#end-delta').textContent = (delta >= 0 ? '+ $' : '− $') + Math.abs(delta).toLocaleString();
  $('#end-delta').className = 'end-delta ' + (delta >= 0 ? 'up' : 'down');
  $('#end-blurb').textContent =
    delta > 15000 ? "Margaret called you 'a real culture carrier'. You're doomed." :
    delta > 0     ? 'You cashed a raise. You also cashed a little of your soul.' :
    delta > -8000 ? 'Margaret has scheduled a follow-up one-on-one. No further comment.' :
                    'HR has been notified.';
  showScreen('endscreen');
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
  $('#btn-again').addEventListener('click', () => runGame());
  $('#btn-home').addEventListener('click', () => showScreen('splash'));

  // Voices may not be ready until an event fires.
  if ('speechSynthesis' in window) {
    speechSynthesis.onvoiceschanged = () => {};
  }

  showScreen('splash');
});
