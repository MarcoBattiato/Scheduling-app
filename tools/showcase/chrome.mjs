// Shared by the showcase scripts: run a version of the app on its own port,
// and drive headless Google Chrome through its DevTools protocol. Nothing is
// installed: Node 22+ has WebSocket built in, and Chrome is the one already on
// the machine (set CHROME= if it lives elsewhere).

import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
export const CHROME = process.env.CHROME
  || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
export const PHONE = { width: 390, height: 844, deviceScaleFactor: 2, mobile: true };

const children = [];
export const scratch = mkdtempSync(join(tmpdir(), "showcase-"));
function cleanUp() {
  for (const child of children) { try { child.kill("SIGTERM"); } catch {} }
  try { rmSync(scratch, { recursive: true, force: true }); } catch {}
}
process.on("exit", cleanUp);
process.on("SIGINT", () => process.exit(130));

export async function waitFor(check, what, ms = 30000) {
  const until = Date.now() + ms;
  while (Date.now() < until) {
    try { const value = await check(); if (value) return value; } catch {}
    await sleep(200);
  }
  throw new Error(`timed out waiting for ${what}`);
}

// Run the app from a checkout (`root`: the repository's top directory) on
// `port`, with a throwaway session file. The checkout's own packages come
// first on the path, so an old checkout runs its own code, not today's.
export async function startApp(root, port, python) {
  const src = ["calendar_store", "scheduling_engine", "scheduling_contracts", "mock_ui"]
    .map((p) => join(root, p, "src")).filter(existsSync);
  const app = spawn(python, ["-m", "uvicorn", "mock_ui.app:app", "--port", String(port),
                             "--host", "127.0.0.1", "--log-level", "warning"], {
    cwd: scratch,
    env: { ...process.env, PYTHONPATH: src.join(":"),
           MOCK_UI_SNAPSHOT: join(scratch, `session-${port}.json`) },
    stdio: "ignore",
  });
  children.push(app);
  const base = `http://127.0.0.1:${port}`;
  await waitFor(async () => (await fetch(`${base}/`)).ok, `the app on port ${port}`, 60000);
  return { base, stop: () => app.kill("SIGTERM") };
}

export async function api(base, path, body) {
  const res = await fetch(`${base}${path}`, {
    method: body === undefined ? "GET" : "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`${path}: ${res.status} ${await res.text()}`);
  return res.json();
}

export async function startChrome(debugPort = 9333) {
  if (!existsSync(CHROME)) throw new Error(`Chrome not found at ${CHROME} (set CHROME=…)`);
  const chrome = spawn(CHROME, [
    "--headless=new", `--remote-debugging-port=${debugPort}`,
    `--user-data-dir=${join(scratch, "chrome")}`, "--no-first-run",
    "--hide-scrollbars", "--mute-audio", "about:blank",
  ], { stdio: "ignore" });
  children.push(chrome);
  const target = await waitFor(async () => {
    const list = await (await fetch(`http://127.0.0.1:${debugPort}/json/list`)).json();
    return list.find((t) => t.type === "page");
  }, "Chrome to start");
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject; });
  let nextId = 0;
  const pending = new Map();
  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    if (msg.id === undefined) return;
    const p = pending.get(msg.id); pending.delete(msg.id);
    msg.error ? p.reject(new Error(msg.error.message)) : p.resolve(msg.result);
  };
  const send = (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++nextId; pending.set(id, { resolve, reject });
    ws.send(JSON.stringify({ id, method, params }));
  });
  await send("Page.enable");
  await send("Runtime.enable");

  const page = {
    send,
    async js(expression) {
      const { result, exceptionDetails } = await send("Runtime.evaluate",
        { expression, awaitPromise: true, returnByValue: true });
      if (exceptionDetails) {
        throw new Error(`${expression.slice(0, 120)}\n${exceptionDetails.exception?.description || exceptionDetails.text}`);
      }
      return result.value;
    },
    async size(metrics) {
      await send("Emulation.setDeviceMetricsOverride", metrics);
      await send("Emulation.setTouchEmulationEnabled", { enabled: !!metrics.mobile });
    },
    async scheme(dark) {
      await send("Emulation.setEmulatedMedia",
        { features: [{ name: "prefers-color-scheme", value: dark ? "dark" : "light" }] });
    },
    async go(url) { await send("Page.navigate", { url }); },
    async shot(file, format = "jpeg") {
      const { data } = await send("Page.captureScreenshot",
        format === "png" ? { format } : { format, quality: 88 });
      writeFileSync(file, Buffer.from(data, "base64"));
    },
  };
  return page;
}

export function run(command, args, opts = {}) {
  return new Promise((resolve, reject) => {
    const p = spawn(command, args, { stdio: ["ignore", "pipe", "inherit"], ...opts });
    let out = "";
    p.stdout.on("data", (d) => { out += d; });
    p.on("exit", (code) => code === 0 ? resolve(out) : reject(new Error(`${command} exited ${code}`)));
  });
}
