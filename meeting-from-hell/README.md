# Touch Base

> A corporate meeting sim about KPIs and synergies.

**Touch Base™** is the industry's most advanced solution for workplace engagement, featuring proprietary facial sentiment analysis and an AI-powered conversational 1-on-1 engine. Margaret is incredibly excited to touch base with you.

**Touch Base™** You have been scheduled for a mandatory 1-on-1 with Margaret from Employee Engagement. Please come prepared to demonstrate authentic alignment with Q3 priorities. Attendance is tracked. Enthusiasm is measured.

**Touch Base™** is the world's first on-device AI platform for workplace engagement optimization. By combining computer-vision-powered facial analytics with an advanced Corporate Dialogue Engine™, Touch Base enables frictionless, data-driven 1-on-1s at scale. Early internal reviews have been overwhelmingly positive.

<p align="center">
  <a href="https://smorchj.github.io/touch-base/">
    <img src="https://img.shields.io/badge/TRY%20IT%20TODAY-touch--base-C084FC?style=for-the-badge&labelColor=0a0420&logo=github&logoColor=white" alt="try it today" />
  </a>
</p>

## Privacy

Everything runs locally in your browser. No exceptions.

- **Your webcam feed** is analyzed on your GPU by MediaPipe's FaceLandmarker. It never leaves the device. No frames, no video, no blendshape output is transmitted anywhere.
- **The LLM** (Llama-3.2-3B via WebLLM) runs locally once the weights are cached in your browser. Your prompts and Margaret's responses never hit a server.
- **No analytics, no accounts, no logins.** If you record a demo, the file lands in your Downloads folder. Only you have it.

The only outbound traffic is the one-time asset fetch on first load (three.js, MediaPipe model, WebLLM weights, character GLB) from their respective CDNs. After that, you can disconnect wifi and keep playing.

## How it's built

- Character from the [`metahuman-to-glb`](https://github.com/smorchj/metahuman-to-glb) pipeline (MetaHuman to web-ready GLB)
- three.js renders
- MediaPipe FaceLandmarker drives the 52 ARKit blendshapes from your webcam
- WebLLM (default) / Chrome built-in / Groq / Ollama generates Margaret's dialogue
- Web Speech API synthesizes her voice; upstream audio-viseme layer lipsyncs the character's mouth

## Run locally

```bash
python build.py
python -m http.server 8001 -d site
# open http://localhost:8001/
```

## This is not a training tool

If you're an HR director reading this and your first thought was "this would be great for our engagement workshops", just "touch grass". Touch Base is a satire of workplace monitoring, not an implementation of it. No employees were retained, evaluated, or psychologically conditioned in the making of this game. Deploying it in actual 1-on-1s is both missing the point and, depending on your jurisdiction, illegal.

## License

MIT.
