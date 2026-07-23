# JarvisMK2

Personal voice assistant with a desktop UI. Successor to the `Jarvis001` project.

---

## 1. Reference folder — HARD RULES

`_reference/` contains the previous working version of Jarvis.

**It is READ-ONLY reference material. Treat it as documentation, not as code.**

- NEVER import anything from `_reference/`
- NEVER add `_reference/` to `sys.path`
- NEVER edit, move, rename, or delete files inside `_reference/`
- NEVER copy a file wholesale from `_reference/` into the project
- NEVER run any script from `_reference/`
- `_reference/` is excluded from git (`.gitignore`)

**What you MAY do:** read files there to understand how something was solved
(app paths, CDP browser control, memory format, router prompts, Gemini Live config),
then **reimplement** it cleanly in the new architecture.

If reference code looks directly reusable, do not paste it silently.
State which file you looked at, what you are taking from it, and why — then write it fresh.

---

## 2. Architecture

Two processes, talking over a local WebSocket. Never merge them.

```
[ Python backend ]  <-- ws://127.0.0.1:8765 -->  [ HTML/CSS/JS frontend ]
  Gemini Live, router,                             UI, themes, history,
  mic, wake word, tools,                           console panel
  memory
                    [ pywebview shell ] -> native desktop window
```

Rules:

- **All audio stays in the backend.** Microphone, wake word, VAD, Gemini Live.
  The frontend must NEVER touch WebAudio, `getUserMedia`, or any audio API.
- **The frontend holds no state.** It renders what the backend sends.
  The backend is the single source of truth. A page refresh must never break anything.
- **The backend runs without the frontend.** If no client is connected,
  Jarvis keeps working and messages are simply dropped. It must stay debuggable from a bare terminal.
- **This is a desktop app, not a web app.** It ships as a pywebview window.
  No browser chrome, no address bar, no external hosting. Bind to `127.0.0.1` only.

---

## 3. Message protocol

Transport: WebSocket, JSON. Every message has the same envelope:

```json
{ "type": "...", "id": "uuid", "ts": 1721600000.123, "data": {} }
```

`id` links a response to its request. `ts` is a unix timestamp for ordering.

### Frontend -> backend

| type | data | meaning |
|---|---|---|
| `send_text` | `{text}` | Query typed into the input field |
| `run_command` | `{command}` | Action button — calls the command layer directly, bypassing the model |
| `set_mode` | `{mode}` | `"voice"` or `"text"` |
| `start_listening` | `{}` | Manual listen, no wake word |
| `stop` | `{}` | Interrupt playback / close session |
| `get_state` | `{}` | Ask for current state |

### Backend -> frontend

| type | data | meaning |
|---|---|---|
| `ready` | `{state, mode, history}` | First message after connect; frontend restores its view |
| `state` | `{state, mode, model}` | `idle` \| `waking` \| `listening` \| `thinking` \| `speaking` |
| `transcript` | `{role, text, final}` | `role`: `user` \| `jarvis`. `final:false` chunks append to the same card |
| `tool_call` | `{name, args, status}` | `status`: `started` \| `ok` \| `error` |
| `log` | `{level, source, text}` | `level`: `debug` \| `info` \| `warn` \| `error` |
| `error` | `{message, fatal}` | Human-facing error |

**The protocol is the contract.** Do not add, rename, or repurpose message types
without saying so explicitly and explaining why. Everything else may change freely.

---

## 4. Modes

The response modality is fixed per Gemini Live session (AUDIO or TEXT, never both),
so the mode switch selects a **route**, not a format.

- **voice mode** -> Gemini Live. Answer is spoken. Output transcription is also
  sent to the frontend as `transcript`, so text appears too.
- **text mode** -> the router path (Groq / Gemini Flash / Claude). Text only,
  nothing is spoken, no WebSocket to Gemini Live is opened, no mic is touched.

Text mode must work with the microphone fully disconnected. It is the primary debugging path.

---

## 5. Logging

All output goes through a thread-safe log bus:

```
code -> logbus -> { stdout (terminal), WebSocket `log` messages }
```

- Use `logging`, not bare `print`, for project code.
- Also tee `sys.stdout` / `sys.stderr` to capture third-party noise
  (PyAudio warnings, pywinauto, tracebacks).
- Terminal output must keep working exactly as before. The UI console is an
  additional consumer, never a replacement.
- Never touch UI state from a background thread. The frontend drains the queue.

Debug logging is intentional and stays in production. Do not remove debug lines for tidiness.

---

## 6. Code rules

- **No hardcoded values.** Paths, ports, thresholds, model names -> `config.py` or `.env`.
  Use `glob` for versioned directories (e.g. `app-*`).
- **No secrets in code.** `.env` only. Never commit `.env`, `secrets/`, or `*token*.json`.
- **Async for anything slow.** Blocking calls must not stall the event loop or the Live session.
- **Keep the tool list lean.** Fewer registered tools = less model hesitation. This is a proven
  constraint on this project, not a preference. Adding a tool requires a stated reason.
- **No arbitrary code execution.** Never build a path where a model writes Python that is then
  executed on this machine. If sandboxed execution is ever needed, it goes on a remote VM.
- **Themes are data.** Colors live in `frontend/themes/*.json` and are applied through CSS
  variables. Never hardcode a color in a stylesheet or component.
- **Retry audio streams.** PyAudio stream init can fail (`OSError -9999`); wrap it in a retry loop.
- **One model family.** Both modes run on Gemini so Jarvis has a single personality.
  Voice mode: Gemini Live. Text mode: Gemini Flash. Other providers may only be added
  as an explicit fallback, never as the default path.

---

## 7. Working style

- **Discuss architecture before implementing.** For anything non-trivial, propose the approach
  and wait for approval. Do not silently make design decisions.
- **When showing a changed file, show the complete file**, not a fragment.
- **Do not refactor working code that was not part of the task.** If something looks wrong,
  mention it — do not fix it uninvited.
- **Prefer free / low-cost APIs.** This project runs on free tiers by design.
- **Do not propose a simpler workaround in place of a proper implementation.** If the proper
  implementation is expensive, say so and let the decision be made explicitly.
- **Suggest a commit** after every 2-3 meaningful changes, with a descriptive message.
- **Diagnose before fixing.** When something misbehaves, find and report the cause before
  changing code. Never mask a bug with a retry, a reconnect, a timeout bump, or a fallback path.
- **Never modify the project venv.** `venv/` belongs to the user. Do not install into it,
  uninstall from it, delete it, or recreate it. If a throwaway environment is needed,
  create it elsewhere and clean up only what you created.

---

## 8. Build order

Do not skip ahead. Each step must run before the next begins.

1. Log bus + backend facade
2. FastAPI WebSocket server + protocol
3. Minimal frontend (input field + console panel, **text mode only**)
   together with the pywebview shell — see rule below
4. Action buttons -> command layer
5. Voice mode: wake word, persistent Gemini Live session, session resumption
6. Themes

**The UI is never opened in a browser, at any stage — not even for testing.**
The pywebview shell therefore ships together with the first frontend, so there is
never a point in development where a browser is the only way to see the interface.
Serving `frontend/` as static files is an implementation detail of the shell,
not an invitation to open `localhost` in Chrome.

Known constraint for step 5: the previous version reconnected the Live session on every
turn, which caused mic churn and lost speech onsets. The new implementation must hold
**one persistent session** and use session resumption for the connection lifetime limit.