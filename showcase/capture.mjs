#!/usr/bin/env node
// Rebuild the showcase: screenshots of every main screen, and a walkthrough
// video of the core story (a request, a plan, the clients agreeing, the diary).
//
//   node showcase/capture.mjs            screenshots and video
//   node showcase/capture.mjs --shots    screenshots only (faster)
//
// Needs Google Chrome and ffmpeg; nothing is installed. Chrome is driven
// through its own DevTools protocol (Node 22+ has WebSocket built in).
//
// The app runs on its own port with a throwaway session file, loaded with the
// "practice" example, so neither the real session nor a running demo is
// touched. A step whose button has gone fails loudly: when the app changes,
// this script is what tells you the showcase needs updating.

import { spawn } from "node:child_process";
import { mkdtempSync, mkdirSync, rmSync, writeFileSync, existsSync, readdirSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = join(HERE, "..");
const SHOTS = join(HERE, "screens");
const PORT = 8020, DEBUG_PORT = 9333;
const BASE = `http://127.0.0.1:${PORT}`;
const CHROME = process.env.CHROME
  || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const PHONE = { width: 390, height: 844, deviceScaleFactor: 2, mobile: true };
const onlyShots = process.argv.includes("--shots");

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const scratch = mkdtempSync(join(tmpdir(), "showcase-"));
const children = [];

function cleanUp() {
  for (const child of children) { try { child.kill("SIGTERM"); } catch {} }
  try { rmSync(scratch, { recursive: true, force: true }); } catch {}
}
process.on("exit", cleanUp);
process.on("SIGINT", () => process.exit(130));

async function waitFor(check, what, ms = 30000) {
  const until = Date.now() + ms;
  while (Date.now() < until) {
    try { const value = await check(); if (value) return value; } catch {}
    await sleep(200);
  }
  throw new Error(`timed out waiting for ${what}`);
}

// -- the app ------------------------------------------------------------

async function startApp() {
  const app = spawn(join(REPO, "mock_ui", "run.sh"), ["--port", String(PORT)], {
    env: { ...process.env, MOCK_UI_SNAPSHOT: join(scratch, "session.json") },
    stdio: "ignore",
  });
  children.push(app);
  await waitFor(async () => (await fetch(`${BASE}/api/scenarios`)).ok, "the app to start");
}

async function api(path, body) {
  const res = await fetch(`${BASE}${path}`, {
    method: body === undefined ? "GET" : "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`${path}: ${res.status} ${await res.text()}`);
  return res.json();
}

const loadPractice = () => api("/api/reset?scenario=practice&as=provider", {});

// -- Chrome over the DevTools protocol -------------------------------------

async function startChrome() {
  const chrome = spawn(CHROME, [
    "--headless=new", `--remote-debugging-port=${DEBUG_PORT}`,
    `--user-data-dir=${join(scratch, "chrome")}`, "--no-first-run",
    "--hide-scrollbars", "--mute-audio", "about:blank",
  ], { stdio: "ignore" });
  children.push(chrome);
  const target = await waitFor(async () => {
    const list = await (await fetch(`http://127.0.0.1:${DEBUG_PORT}/json/list`)).json();
    return list.find((t) => t.type === "page");
  }, "Chrome to start");
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject; });
  let nextId = 0;
  const pending = new Map(), listeners = new Map();
  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    if (msg.id !== undefined) {
      const p = pending.get(msg.id); pending.delete(msg.id);
      msg.error ? p.reject(new Error(msg.error.message)) : p.resolve(msg.result);
    } else (listeners.get(msg.method) || []).forEach((f) => f(msg.params));
  };
  const send = (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++nextId; pending.set(id, { resolve, reject });
    ws.send(JSON.stringify({ id, method, params }));
  });
  const on = (method, f) => listeners.set(method, [...(listeners.get(method) || []), f]);
  await send("Page.enable");
  await send("Runtime.enable");
  await send("Emulation.setDeviceMetricsOverride", PHONE);
  await send("Emulation.setTouchEmulationEnabled", { enabled: true });
  return { send, on };
}

let cdp;

async function js(expression) {
  const { result, exceptionDetails } = await cdp.send("Runtime.evaluate",
    { expression, awaitPromise: true, returnByValue: true });
  if (exceptionDetails) throw new Error(`${expression}\n${exceptionDetails.exception?.description || exceptionDetails.text}`);
  return result.value;
}

async function scheme(dark) {
  await cdp.send("Emulation.setEmulatedMedia",
    { features: [{ name: "prefers-color-scheme", value: dark ? "dark" : "light" }] });
}

// Open a screen as a person, and wait until it has drawn.
async function open(as, tab) {
  await js(`document.getElementById("showcase-caption")?.remove()`).catch(() => {});
  await cdp.send("Page.navigate", { url: `${BASE}/phone?as=${as}#${tab}` });
  await sleep(300);
  await waitFor(() => js(`!!(document.querySelector("#view") && document.querySelector("#view").children.length)`),
    `${as} ${tab} to draw`);
  await sleep(700);                       // the fade between screens
}

// Click the element a selector names; fail loudly if it is not there.
async function tap(selector, { pause = 900, mark = false } = {}) {
  const found = await js(`(() => {
    const el = document.querySelector(${JSON.stringify(selector)});
    if (!el) return false;
    el.scrollIntoView({ block: "center" });
    if (${mark}) {
      const r = el.getBoundingClientRect(), dot = document.createElement("div");
      dot.className = "showcase-tap";
      dot.style.cssText = "position:fixed;z-index:99;width:44px;height:44px;margin:-22px 0 0 -22px;"
        + "border-radius:50%;background:rgba(30,107,107,.35);border:2px solid rgba(30,107,107,.8);"
        + "pointer-events:none;transition:opacity .5s, transform .5s;left:" + (r.left + r.width / 2)
        + "px;top:" + (r.top + r.height / 2) + "px";
      document.body.appendChild(dot);
      setTimeout(() => { dot.style.opacity = 0; dot.style.transform = "scale(1.6)"; }, 350);
      setTimeout(() => dot.remove(), 900);
    }
    return true;
  })()`);
  if (!found) throw new Error(`nothing to tap: ${selector}`);
  if (mark) await sleep(450);
  await js(`document.querySelector(${JSON.stringify(selector)}).click()`);
  await sleep(pause);
}

// The Wednesday of the example's working week (the days before it are closed).
async function openWorkingWednesday() {
  const ok = await js(`(() => {
    const days = [...document.querySelectorAll(".day")];
    let seenMonday = false;
    for (const [i, b] of days.entries()) {
      if (i > 0 && /mon/i.test(b.textContent)) seenMonday = true;
      if (seenMonday && /wed/i.test(b.textContent)) { b.click(); return true; }
    }
    return false;
  })()`);
  if (!ok) throw new Error("no working Wednesday in the diary strip");
  await sleep(600);
}

async function shot(name) {
  await js(`window.scrollTo(0, 0)`);
  await sleep(250);
  const { data } = await cdp.send("Page.captureScreenshot", { format: "png" });
  writeFileSync(join(SHOTS, `${name}.png`), Buffer.from(data, "base64"));
  console.log(`  ${name}.png`);
}

async function whoIsAsked(kind) {
  const state = await api("/api/state?as=provider");
  const ask = (state.approvals || []).find((a) => a.kind === kind && a.status === "pending");
  if (!ask) throw new Error(`no pending ${kind} to answer`);
  return ask.client_id;
}

// -- the screenshots -------------------------------------------------------

async function screenshots() {
  rmSync(SHOTS, { recursive: true, force: true });
  mkdirSync(SHOTS, { recursive: true });
  await loadPractice();
  await scheme(false);

  console.log("client screens");
  await open("daniel", "home"); await shot("client-01-home");
  await open("aisyah", "book"); await shot("client-02-book");
  await open("daniel", "bookings"); await shot("client-03-bookings");
  await open("daniel", "hours"); await shot("client-04-my-hours");
  await open("daniel", "messages"); await shot("client-05-messages");

  console.log("provider screens");
  await open("provider", "home"); await shot("provider-01-home");
  await open("provider", "diary"); await openWorkingWednesday(); await shot("provider-02-diary");
  await open("provider", "queue"); await shot("provider-03-requests");
  await tap('[data-act="pick-all"]', { pause: 300 });
  await tap('[data-act="run"]', { pause: 500 });
  await waitFor(() => js(`!!document.querySelector('[data-act="approve"]')`), "a plan", 60000);
  await sleep(800); await shot("provider-04-plan");
  await tap('[data-act="approve"]', { pause: 1200 });
  await open(await whoIsAsked("booking"), "home"); await shot("client-06-a-time-offered");
  await open(await whoIsAsked("reschedule"), "home"); await shot("client-07-asked-to-move");
  await open("provider", "plans"); await shot("provider-05-waiting-for-answers");
  await open("provider", "more/clients"); await shot("provider-06-clients");
  await open("provider", "more/payments"); await shot("provider-07-payments");
  await open("provider", "more/away"); await shot("provider-08-time-off");
  await open("provider", "more/messages"); await shot("provider-09-messages");
  await open("provider", "more/hours"); await shot("provider-10-working-hours");
  await open("provider", "more"); await shot("provider-11-more");

  console.log("dark mode");
  await loadPractice();
  await scheme(true);
  await open("daniel", "home"); await shot("dark-client-home");
  await open("provider", "diary"); await openWorkingWednesday(); await shot("dark-provider-diary");
  await scheme(false);
}

// -- the walkthrough video ------------------------------------------------

async function caption(text) {
  await js(`(() => {
    let box = document.getElementById("showcase-caption");
    if (!box) {
      box = document.createElement("div");
      box.id = "showcase-caption";
      box.style.cssText = "position:fixed;left:12px;right:12px;bottom:84px;z-index:98;padding:12px 16px;"
        + "border-radius:14px;background:rgba(18,22,21,.88);color:#fff;font:600 16px/1.35 -apple-system,"
        + "BlinkMacSystemFont,sans-serif;box-shadow:0 6px 24px rgba(0,0,0,.25);pointer-events:none;"
        + "transition:opacity .25s";
      document.body.appendChild(box);
    }
    box.textContent = ${JSON.stringify(text)};
  })()`);
}

async function scene(as, tab, text, hold = 2600) {
  await open(as, tab);
  await caption(text);
  await sleep(hold);
}

async function video() {
  await loadPractice();
  await scheme(false);
  const frames = join(scratch, "frames");
  mkdirSync(frames);
  // Headless Chrome's screencast records at 1x; screenshots honour the
  // phone's 2x pixels. So the recorder takes screenshots as fast as it can
  // (about ten a second), each held until the next.
  const list = [];
  let recording = true;
  const recorder = (async () => {
    while (recording) {
      const at = Date.now() / 1000;
      try {
        const { data } = await cdp.send("Page.captureScreenshot", { format: "jpeg", quality: 85 });
        const file = join(frames, `${String(list.length).padStart(5, "0")}.jpg`);
        writeFileSync(file, Buffer.from(data, "base64"));
        list.push({ file, at });
      } catch { await sleep(50); }         // mid-navigation: try again
    }
  })();

  await scene("aisyah", "home",
    "Aisyah can only come on Tuesday mornings, and both are taken. She has asked anyway.", 3800);
  await scene("aisyah", "book", "Clients say what they need and when suits them best. It's a wish, not a booking.", 3200);
  await js(`window.scrollTo({ top: 500, behavior: "smooth" })`); await sleep(1800);

  await scene("provider", "home", "The provider sees only what needs them.", 3000);
  await scene("provider", "queue", "Four requests are waiting for this week.", 2400);
  await tap('[data-act="pick-all"]', { mark: true, pause: 600 });
  await caption("One tap plans the whole week.");
  await tap('[data-act="run"]', { mark: true, pause: 500 });
  await waitFor(() => js(`!!document.querySelector('[data-act="approve"]')`), "a plan", 60000);
  await caption("The plan finds times for the requests. To fit Aisyah in, one client is asked to move, only if they agree.");
  await sleep(4200);
  await js(`window.scrollTo({ top: 420, behavior: "smooth" })`); await sleep(2200);
  await caption("Nothing is booked until the clients say yes.");
  await tap('[data-act="approve"]', { mark: true, pause: 1600 });

  await scene("aisyah", "home", "Aisyah is offered her Tuesday morning…", 2000);
  await tap('[data-act="respond"][data-answer="accept"]', { mark: true, pause: 1600 });
  const mover = await whoIsAsked("reschedule");
  await scene(mover, "home", "…and the client asked to move sees exactly what changes.", 2800);
  await tap('[data-act="respond"][data-answer="accept"]', { mark: true, pause: 1600 });

  await scene("provider", "plans", "Back with the provider: the answers are in.", 2600);
  await caption("Confirm what was agreed.");
  await tap('[data-act="settle"][data-how="agreed"]', { mark: true, pause: 1500 });
  await open("provider", "diary");
  await caption("The diary is up to date, and everyone was told.");
  await js(`(() => { const d = [...document.querySelectorAll(".day")]; const t = d.find((b, i) => i > 0 && /tue/i.test(b.textContent) && d.slice(0, i).some((x, j) => j > 0 && /mon/i.test(x.textContent))); t && t.click(); })()`);
  await sleep(3400);

  await scene("hannah", "messages", "Clients can message the practice. Urgent-help numbers are always on screen.", 3400);
  await scene("daniel", "bookings", "Bookings, calendar sync and payments, in one place.", 3200);
  await caption("Quiet Harbour Psychology is a made-up practice, shown with example data.");
  await sleep(2600);

  recording = false;
  await recorder;

  // Frames arrive only when something changes: each one is held until the next.
  const lines = [];
  list.forEach((frame, i) => {
    const next = list[i + 1] ? list[i + 1].at : frame.at + 1.5;
    lines.push(`file '${frame.file}'`, `duration ${Math.max(next - frame.at, 0.01).toFixed(3)}`);
  });
  lines.push(`file '${list[list.length - 1].file}'`);
  const concat = join(scratch, "frames.txt");
  writeFileSync(concat, lines.join("\n"));

  const ffmpeg = (args) => new Promise((resolve, reject) => {
    const p = spawn("ffmpeg", ["-y", "-loglevel", "error", ...args], { stdio: "inherit" });
    p.on("exit", (code) => code === 0 ? resolve() : reject(new Error(`ffmpeg exited ${code}`)));
  });
  const even = "scale=trunc(iw/2)*2:trunc(ih/2)*2";
  await ffmpeg(["-f", "concat", "-safe", "0", "-i", concat, "-vf", `${even},fps=30`,
    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "26", "-movflags", "+faststart",
    join(HERE, "walkthrough.mp4")]);
  // A small animated preview that plays inline on GitHub.
  await ffmpeg(["-f", "concat", "-safe", "0", "-i", concat, "-vf",
    "fps=8,scale=300:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=96[p];[b][p]paletteuse=dither=bayer",
    join(HERE, "walkthrough.gif")]);
  console.log("  walkthrough.mp4, walkthrough.gif");
}

// -- run ----------------------------------------------------------------

try {
  if (!existsSync(CHROME)) throw new Error(`Chrome not found at ${CHROME} (set CHROME=…)`);
  console.log("starting the app and Chrome");
  await startApp();
  cdp = await startChrome();
  await screenshots();
  if (!onlyShots) { console.log("recording the walkthrough"); await video(); }
  const size = readdirSync(HERE).concat(readdirSync(SHOTS).map((f) => join("screens", f)))
    .map((f) => statSync(join(HERE, f))).filter((s) => s.isFile()).reduce((t, s) => t + s.size, 0);
  console.log(`done: ${(size / 1e6).toFixed(1)} MB in showcase/`);
  process.exit(0);
} catch (error) {
  console.error(`\nshowcase failed: ${error.message}`);
  process.exit(1);
}
