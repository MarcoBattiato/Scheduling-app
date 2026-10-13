#!/usr/bin/env node
// The app as it is now: a screenshot of every main screen, contact sheets of
// them, and a captioned walkthrough video of the core story.
//
//   node tools/showcase/capture.mjs            everything
//   node tools/showcase/capture.mjs --shots    no video
//
// Writes into showcase/ at the top of the repository; the main README shows it. Runs THIS checkout's app on its own
// port with a throwaway session and the "practice" example, so neither the
// real session nor a running demo is touched. A step whose button has gone
// fails loudly, naming it: that is how you learn the showcase needs updating.
// See tools/showcase/README.md.

import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import { join, dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { PHONE, api, run, scratch, sleep, startApp, startChrome, waitFor } from "./chrome.mjs";

const REPO = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..");
const PYTHON = join(REPO, "scheduling_engine", ".venv", "bin", "python");
const flags = process.argv.slice(2);
const MEDIA = join(REPO, "showcase");
const SCREENS = join(MEDIA, "screens"), WALK = join(MEDIA, "walkthrough");
const onlyShots = flags.includes("--shots");

let app, page;
const js = (e) => page.js(e);
const loadPractice = () => api(app.base, "/api/reset?scenario=practice&as=provider", {});

// Open a screen as a person, and wait until it has drawn.
async function open(as, tab) {
  await js(`document.getElementById("showcase-caption")?.remove()`).catch(() => {});
  await page.go(`${app.base}/phone?as=${as}#${tab}`);
  await sleep(300);
  await waitFor(() => js(`!!(document.querySelector("#view") && document.querySelector("#view").children.length)`),
    `${as} ${tab} to draw`);
  await sleep(700);                       // the fade between screens
}

async function tap(selector, pause = 900) {
  const found = await js(`(() => { const el = document.querySelector(${JSON.stringify(selector)});
    if (!el) return false; el.click(); return true; })()`);
  if (!found) throw new Error(`nothing to tap: ${selector}`);
  await sleep(pause);
}

// The Wednesday (or another day) of the example's working week; the days
// before it are closed.
async function openWorkingDay(day = "wed") {
  const ok = await js(`(() => {
    const days = [...document.querySelectorAll(".day")];
    let seenMonday = false;
    for (const [i, b] of days.entries()) {
      if (i > 0 && /mon/i.test(b.textContent)) seenMonday = true;
      if (seenMonday && new RegExp(${JSON.stringify(day)}, "i").test(b.textContent)) { b.click(); return true; }
    }
    return false;
  })()`);
  if (!ok) throw new Error(`no working ${day} in the diary strip`);
  await sleep(600);
}

const shots = [];
async function shot(name) {
  await js(`window.scrollTo(0, 0)`);
  await sleep(250);
  const file = join(SCREENS, `${name}.jpg`);
  await page.shot(file);
  shots.push(file);
  console.log(`  ${name}.jpg`);
}

async function whoIsAsked(kind) {
  const state = await api(app.base, "/api/state?as=provider");
  const ask = (state.approvals || []).find((a) => a.kind === kind && a.status === "pending");
  if (!ask) throw new Error(`no pending ${kind} to answer`);
  return ask.client_id;
}

// A row of screens, side by side, for the README.
async function sheet(names, out) {
  await run("magick", [...names.map((n) => join(SCREENS, `${n}.jpg`)),
    "-resize", "390x", "-bordercolor", "#e9e6e0", "-border", "12", "+append",
    "-quality", "85", join(MEDIA, out)]);
  console.log(`  ${out}`);
}

// -- the screenshots -------------------------------------------------------

async function screenshots() {
  rmSync(SCREENS, { recursive: true, force: true });
  mkdirSync(SCREENS, { recursive: true });
  await loadPractice();
  await page.scheme(false);

  console.log("client screens");
  await open("daniel", "home"); await shot("client-home");
  await open("aisyah", "book"); await shot("client-book");
  await open("daniel", "bookings"); await shot("client-bookings");
  await open("daniel", "hours"); await shot("client-hours");
  await open("daniel", "messages"); await shot("client-messages");

  console.log("provider screens");
  await open("provider", "home"); await shot("provider-home");
  await open("provider", "diary"); await openWorkingDay(); await shot("provider-diary");
  await open("provider", "queue"); await shot("provider-requests");
  await tap('[data-act="pick-all"]', 300);
  await tap('[data-act="run"]', 500);
  await waitFor(() => js(`!!document.querySelector('[data-act="approve"]')`), "a plan", 120000);
  await sleep(800); await shot("provider-plan");
  await tap('[data-act="approve"]', 1200);
  await open(await whoIsAsked("booking"), "home"); await shot("client-offered");
  await open(await whoIsAsked("reschedule"), "home"); await shot("client-asked-to-move");
  await open("provider", "plans"); await shot("provider-answers");
  await open("provider", "more/clients"); await shot("provider-clients");
  await open("provider", "more/payments"); await shot("provider-payments");
  await open("provider", "more/away"); await shot("provider-time-off");
  await open("provider", "more/messages"); await shot("provider-messages");
  await open("provider", "more/hours"); await shot("provider-hours");

  console.log("dark mode");
  await loadPractice();
  await page.scheme(true);
  await open("daniel", "home"); await shot("dark-client-home");
  await open("provider", "diary"); await openWorkingDay(); await shot("dark-provider-diary");
  await page.scheme(false);

  console.log("contact sheets");
  await sheet(["client-home", "client-book", "client-offered", "client-asked-to-move", "client-messages"],
              "clients_sheet.jpg");
  await sheet(["provider-home", "provider-diary", "provider-requests", "provider-plan", "provider-answers"],
              "provider_sheet.jpg");
}

// -- the walkthrough video ------------------------------------------------
// Built from stills, each with the time it should stay on screen, rather than
// recorded in real time: a slow or busy machine changes how long the script
// takes, never how the video plays.

let frames = [], framesDir = "";

async function caption(text) {
  await js(`(() => {
    let box = document.getElementById("showcase-caption");
    if (!box) {
      box = document.createElement("div");
      box.id = "showcase-caption";
      box.style.cssText = "position:fixed;left:12px;right:12px;bottom:84px;z-index:98;padding:12px 16px;"
        + "border-radius:14px;background:rgba(18,22,21,.88);color:#fff;font:600 16px/1.35 -apple-system,"
        + "BlinkMacSystemFont,sans-serif;box-shadow:0 6px 24px rgba(0,0,0,.25);pointer-events:none";
      document.body.appendChild(box);
    }
    box.textContent = ${JSON.stringify(text)};
  })()`);
}

async function still(seconds) {
  const file = join(framesDir, `${String(frames.length).padStart(5, "0")}.jpg`);
  await page.shot(file);
  frames.push({ file, seconds });
}

// A short transition, drawn as evenly spaced steps (24 a second).
async function motion(seconds, step) {
  const count = Math.max(2, Math.round(seconds * 24));
  for (let i = 1; i <= count; i++) { await step(i / count); await still(1 / 24); }
}
const ease = (t) => t < .5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;

async function scrollTo(top, seconds = 0.8) {
  const from = await js(`window.scrollY`);
  const to = await js(`Math.min(${top}, document.documentElement.scrollHeight - innerHeight)`);
  await motion(seconds, (t) => js(`window.scrollTo(0, ${from + (to - from) * ease(t)})`));
}

async function fadeIn() {
  await js(`document.getElementById("view").classList.remove("entering")`);
  await motion(0.25, (t) => js(`document.getElementById("view").style.opacity = ${ease(t)}`));
  await js(`document.getElementById("view").style.opacity = ""`);
}

async function scene(as, tab, text, hold = 2.6) {
  await open(as, tab);
  await js(`document.getElementById("view").style.opacity = 0`);
  await caption(text);
  await fadeIn();
  await still(hold);
}

async function say(text, hold = 2.6) { await caption(text); await still(hold); }

// Show the touch, then do it, then let the result settle before the next still.
async function press(selector, settle = 900, hold = 0.6) {
  const found = await js(`(() => {
    const el = document.querySelector(${JSON.stringify(selector)});
    if (!el) return false;
    el.scrollIntoView({ block: "center" });
    const r = el.getBoundingClientRect(), dot = document.createElement("div");
    dot.id = "showcase-tap";
    dot.style.cssText = "position:fixed;z-index:99;width:44px;height:44px;margin:-22px 0 0 -22px;"
      + "border-radius:50%;background:rgba(30,107,107,.35);border:2px solid rgba(30,107,107,.8);"
      + "pointer-events:none;left:" + (r.left + r.width / 2) + "px;top:" + (r.top + r.height / 2) + "px";
    document.body.appendChild(dot);
    return true;
  })()`);
  if (!found) throw new Error(`nothing to tap: ${selector}`);
  await motion(0.3, (t) => js(`document.getElementById("showcase-tap").style.transform = "scale(${0.6 + 0.6 * t})"`));
  await js(`document.getElementById("showcase-tap")?.remove()`);
  await js(`document.querySelector(${JSON.stringify(selector)}).click()`);
  await sleep(settle);
  await still(hold);
}

async function video() {
  await loadPractice();
  await page.scheme(false);
  framesDir = join(scratch, "frames");
  mkdirSync(framesDir, { recursive: true });
  frames = [];

  await scene("aisyah", "home",
    "Aisyah can only come on Tuesday mornings, and both are taken. She has asked anyway.", 3.8);
  await scene("aisyah", "book", "Clients say what they need and when suits them best. It's a wish, not a booking.", 2.4);
  await scrollTo(500);
  await still(1.6);

  await scene("provider", "home", "The provider sees only what needs them.", 3.0);
  await scene("provider", "queue", "Four requests are waiting for this week.", 2.2);
  await press('[data-act="pick-all"]', 400);
  await caption("One tap plans the whole week.");
  await press('[data-act="run"]', 300, 0.3);
  await waitFor(() => js(`!!document.querySelector('[data-act="approve"]')`), "a plan", 120000);
  await sleep(800);
  await js(`window.scrollTo(0, 0)`);
  await say("The plan finds times for the requests. To fit Aisyah in, one client is asked to move, only if they agree.", 4.2);
  await scrollTo(420, 1.0);
  await still(1.6);
  await say("Nothing is booked until the clients say yes.", 1.6);
  await press('[data-act="approve"]', 1500, 0.8);

  await scene("aisyah", "home", "Aisyah is offered her Tuesday morning…", 2.0);
  await press('[data-act="respond"][data-answer="accept"]', 1500, 1.4);
  const mover = await whoIsAsked("reschedule");
  await scene(mover, "home", "…and the client asked to move sees exactly what changes.", 2.8);
  await press('[data-act="respond"][data-answer="accept"]', 1500, 1.4);

  await scene("provider", "plans", "Back with the provider: the answers are in.", 2.4);
  await say("Confirm what was agreed.", 1.2);
  await press('[data-act="settle"][data-how="agreed"]', 1500, 0.6);
  await open("provider", "diary");
  await openWorkingDay("tue");
  await js(`document.getElementById("view").style.opacity = 0`);
  await caption("The diary is up to date, and everyone was told.");
  await fadeIn();
  await still(3.4);

  await scene("hannah", "messages", "Clients can message the practice. Urgent-help numbers are always on screen.", 3.4);
  await scene("daniel", "bookings", "Bookings, calendar sync and payments, in one place.", 3.2);
  await say("Quiet Harbour Psychology is a made-up practice, shown with example data.", 2.6);

  rmSync(WALK, { recursive: true, force: true });
  mkdirSync(WALK, { recursive: true });
  const lines = [];
  for (const f of frames) lines.push(`file '${f.file}'`, `duration ${f.seconds.toFixed(4)}`);
  lines.push(`file '${frames[frames.length - 1].file}'`);
  const concat = join(scratch, "frames.txt");
  writeFileSync(concat, lines.join("\n"));
  const ffmpeg = (args) => run("ffmpeg", ["-y", "-loglevel", "error", ...args]);
  await ffmpeg(["-f", "concat", "-safe", "0", "-i", concat, "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,fps=30",
    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "26", "-movflags", "+faststart",
    join(WALK, "walkthrough.mp4")]);
  // A small teaser that plays inline on GitHub, and a poster for the link.
  await ffmpeg(["-f", "concat", "-safe", "0", "-i", concat, "-vf",
    "fps=10,scale=320:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=96[p];[b][p]paletteuse=dither=bayer",
    join(WALK, "walkthrough_teaser.gif")]);
  await ffmpeg(["-ss", "21", "-i", join(WALK, "walkthrough.mp4"), "-frames:v", "1", "-vf", "scale=480:-1",
    "-q:v", "3", join(WALK, "walkthrough_poster.jpg")]);
  console.log(`  walkthrough.mp4, walkthrough_teaser.gif, walkthrough_poster.jpg (${frames.length} stills)`);
}

// -- run ----------------------------------------------------------------

try {
  console.log("starting the app and Chrome");
  app = await startApp(REPO, 8020, PYTHON);
  page = await startChrome();
  await page.size(PHONE);
  await screenshots();
  if (!onlyShots) { console.log("recording the walkthrough"); await video(); }
  console.log("done");
  process.exit(0);
} catch (error) {
  console.error(`\nshowcase failed: ${error.message}`);
  process.exit(1);
}
