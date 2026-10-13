#!/usr/bin/env node
// The app as it stood at the end of each day of work, and a timelapse of it.
//
//   node tools/showcase/timeline.mjs [YYYY-MM-DD ...]
//
// For each day on which the app's screens changed (or only the days named),
// the day's last commit on the current branch is checked out into a scratch
// worktree and run from there, with its own code and a throwaway session.
// Each frame is 1280×720: the developer's desktop page until the phone app
// existed, then the phone app, as client and provider. Writes
// showcase/timeline/<date>.jpg and timelapse.gif.
//
// Only committed history is captured, never the working tree.

import { existsSync, mkdirSync, readdirSync } from "node:fs";
import { join, dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { PHONE, api, run, scratch, sleep, startApp, startChrome, waitFor } from "./chrome.mjs";

const REPO = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..");
const PYTHON = join(REPO, "scheduling_engine", ".venv", "bin", "python");
const FONT = "/System/Library/Fonts/Helvetica.ttc";
const onlyDays = process.argv.slice(2);
const OUT = join(REPO, "showcase", "timeline");
mkdirSync(OUT, { recursive: true });

const git = (...args) => run("git", ["-C", REPO, ...args]);

// The last commit of each day that touched the app's screens or server.
async function days() {
  const log = await git("log", "--format=%H %ad", "--date=short", "HEAD", "--", "mock_ui/src");
  const last = new Map();
  for (const line of log.trim().split("\n")) {
    const [sha, day] = line.split(" ");
    if (!last.has(day)) last.set(day, sha);          // newest first: first seen is the day's last
  }
  return [...last.entries()].map(([day, sha]) => ({ day, sha })).reverse()
    .filter(({ day }) => !onlyDays.length || onlyDays.includes(day));
}

const pretty = (day) => new Date(`${day}T12:00:00`).toLocaleDateString("en-GB",
  { day: "numeric", month: "short", year: "numeric" });

async function frame(page, { day, sha }, port) {
  const tree = join(scratch, `tree-${day}`);
  await git("worktree", "add", "--detach", "--force", tree, sha);
  const shots = join(scratch, `shots-${day}`);
  mkdirSync(shots);
  const app = await startApp(tree, port, PYTHON);
  try {
    const hasPhone = existsSync(join(tree, "mock_ui", "src", "mock_ui", "static", "phone.html"));
    // The richest example the version has.
    const names = (await api(app.base, "/api/scenarios").catch(() => ({ scenarios: [] }))).scenarios || [];
    const scenario = ["practice", "busy", "simple"].find((s) => names.includes(s));
    if (scenario) await api(app.base, `/api/reset?scenario=${scenario}&as=provider`, {});

    const drawn = (sel) => waitFor(() => page.js(`!!(document.querySelector(${JSON.stringify(sel)}) && document.querySelector(${JSON.stringify(sel)}).children.length)`), "the page to draw", 30000);
    let caption, file = join(OUT, `${day}.jpg`);
    if (!hasPhone) {
      await page.size({ width: 1280, height: 680, deviceScaleFactor: 1, mobile: false });
      await page.go(`${app.base}/?as=provider`);
      await sleep(2500);
      await page.shot(join(shots, "desktop.jpg"));
      caption = "the developer's test page";
      await run("magick", ["-size", "1280x720", "xc:#f5f3ef",
        join(shots, "desktop.jpg"), "-geometry", "+0+40", "-composite",
        ...label(day, caption), "-quality", "86", file]);
    } else {
      await page.size(PHONE);
      const phone = async (as, tab, name, workingDay = false) => {
        await page.go(`${app.base}/phone?as=${as}#${tab}`);
        await sleep(300);
        await drawn("#view");
        await sleep(900);
        // The diary on the example's Wednesday, not today: the days before
        // the example's week are closed, so today shows nothing.
        if (workingDay) {
          await page.js(`(() => {
            const days = [...document.querySelectorAll(".day")];
            let monday = false;
            for (const [i, b] of days.entries()) {
              if (i > 0 && /mon/i.test(b.textContent)) monday = true;
              if (monday && /wed/i.test(b.textContent)) { b.click(); return; }
            }
          })()`);
          await sleep(700);
        }
        await page.shot(join(shots, `${name}.jpg`));
      };
      const state = await api(app.base, "/api/state?as=provider");
      const client = (state.clients || []).find((c) => c.id !== "provider")?.id || "alice";
      await phone(client, "home", "client");
      await phone("provider", "home", "provider");
      await phone("provider", "diary", "diary", true);
      caption = "the phone app";
      const tile = (n, x) => ["(", join(shots, `${n}.jpg`), "-resize", "x620", ")",
                              "-geometry", `+${x}+70`, "-composite"];
      await run("magick", ["-size", "1280x720", "xc:#f5f3ef",
        ...tile("client", 171), ...tile("provider", 497), ...tile("diary", 823),
        ...label(day, caption), "-quality", "86", file]);
    }
    console.log(`  ${day} (${caption}${scenario ? `, ${scenario} example` : ""})`);
  } finally {
    app.stop();
    await sleep(500);
    await git("worktree", "remove", "--force", tree).catch(() => {});
  }
}

function label(day, caption) {
  return ["-fill", "rgba(18,22,21,0.88)", "-draw", "rectangle 0,0 1280,40",
    "-font", FONT, "-pointsize", "20", "-fill", "#ffffff",
    "-annotate", "+16+27", `${pretty(day)}  ·  ${caption}`];
}

try {
  const list = await days();
  console.log(`${list.length} days`);
  const page = await startChrome(9334);
  let port = 8100;
  for (const d of list) await frame(page, d, port++);
  const all = readdirSync(OUT).filter((f) => /^\d{4}-\d\d-\d\d\.jpg$/.test(f)).sort();
  await run("magick", ["-delay", "120", ...all.map((f) => join(OUT, f)), "-delay", "300",
    join(OUT, all[all.length - 1]), "-resize", "800x", "-layers", "Optimize", "-colors", "128",
    join(OUT, "timelapse.gif")]);
  console.log("  timelapse.gif");
  process.exit(0);
} catch (error) {
  console.error(`\ntimeline failed: ${error.message}`);
  process.exit(1);
}
