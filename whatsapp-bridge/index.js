/**
 * Lumina WhatsApp bridge — REPLY ONLY.
 *
 * Links a *dedicated* WhatsApp number (not your personal one — it can be
 * banned) to Lumina's WhatsApp agent using Baileys (unofficial WhatsApp
 * Web protocol, no Meta approval needed).
 *
 * Flow: someone messages the dedicated number
 *       -> this bridge POSTs {from, text} to  LUMINA_URL/whatsapp/bridge
 *       -> Lumina runs the agent (identity, tools, web search, context)
 *       -> bridge sends the returned reply back
 *
 * It never starts a conversation from nothing. Group messages are answered
 * only when the bot is addressed ("lumina ...", an @-mention, or a reply
 * to it) AND Lumina's group allow-list permits that group AND the asker is
 * a known employee — see whatsapp_agent.py. The one deliberate exception:
 * in a group the bot is already active in, a new member joining gets an
 * automatic @-mention greeting + a happy sticker (greetNewMember below) —
 * still triggered by a real WhatsApp event (a join), never spontaneous.
 *
 * Setup:
 *   cd whatsapp-bridge && npm install
 *   set LUMINA_URL=https://lumina.mmga.agency
 *   set WHATSAPP_BRIDGE_TOKEN=<same value you set on Railway>
 *   node index.js           # scan the QR with the dedicated number, once
 *
 * The pairing/session is saved in ./auth (gitignored). Delete that folder
 * to unpair / switch numbers.
 *
 * Keep alive across reboots: Task Scheduler "At log on" ->
 *   node.exe <path>\whatsapp-bridge\index.js
 *
 * Voice calling (place_announcement_call, see whatsapp_agent.py) is a
 * separate, optional layer on top — see voice.js's own header for its
 * one-time setup. Nothing about it is required for the text bridge above
 * to work; a POST to /call just fails cleanly with a clear reason if voice
 * was never set up.
 */

import http from "http";
import fs from "fs";
import path from "path";
import { execFile } from "child_process";
import { fileURLToPath } from "url";
import makeWASocket, {
  useMultiFileAuthState,
  DisconnectReason,
  fetchLatestBaileysVersion,
  downloadMediaMessage,
} from "@whiskeysockets/baileys";
import qrcode from "qrcode-terminal";
import pino from "pino";

const LUMINA_URL = (process.env.LUMINA_URL || "https://lumina.mmga.agency").replace(/\/+$/, "");
const TOKEN =
  process.env.WHATSAPP_BRIDGE_TOKEN ||
  process.env.STORAGE_SYNC_TOKEN ||
  process.env.FLASK_SECRET_KEY ||
  "";
const AUTH_DIR = process.env.AUTH_DIR || "./auth";
// loopback-only endpoint the laptop companion uses to push a proactive
// message (e.g. the noon attendance roll-call). Strangers still only ever
// get a reply — nothing on WhatsApp can trigger an outbound, only something
// on this machine holding TOKEN.
const SEND_PORT = parseInt(process.env.BRIDGE_HTTP_PORT || "8787", 10);
const __dirname = path.dirname(fileURLToPath(import.meta.url));
// generous outer timeout for the isolated voice-call child process -- must
// exceed voice.js's own internal CALL_MAX_MS + Piper's synth timeout so a
// slow-but-legitimate call never gets killed as if it hung.
const VOICE_RUNNER_TIMEOUT_MS =
  Math.max(10000, parseInt(process.env.VOICE_CALL_MAX_MS || "45000", 10)) + 30000;

if (!TOKEN) {
  console.error("bridge: set WHATSAPP_BRIDGE_TOKEN (or STORAGE_SYNC_TOKEN / FLASK_SECRET_KEY).");
  process.exit(1);
}

const log = (...a) => console.log(`[${new Date().toTimeString().slice(0, 8)}]`, ...a);

// Log with a full stack trace and exit promptly and predictably, instead
// of whatever the OS does with an otherwise-uncaught fault (confirmed
// live 2026-09-15: the bridge died with an unusual native-looking exit
// code and ZERO logged reason, which made root-causing that incident far
// harder than it should have been -- see CLAUDE.md). job_bridge's own
// supervisor handles the restart/backoff either way; this only makes sure
// there's something to actually diagnose next time, and that a broken
// process doesn't linger half-alive instead of exiting cleanly.
process.on("uncaughtException", (e) => {
  log(`FATAL uncaughtException: ${e && e.stack ? e.stack : e}`);
  process.exit(1);
});
process.on("unhandledRejection", (e) => {
  log(`FATAL unhandledRejection: ${e && e.stack ? e.stack : e}`);
  process.exit(1);
});

let currentSock = null;   // set on every (re)connect, used by the send endpoint

// ── health state (read by the laptop companion's bridge watchdog) ──────────
let connState = "starting";       // starting | connecting | open | close
let connectedSince = 0;           // epoch ms of the last successful "open"
let loggedOut = false;            // true once WhatsApp says this device was unlinked

// Baileys/WhatsApp needs a few seconds after "open" to finish syncing the
// encryption session -- a proactive send (job_wa_outbox, the morning digest,
// roll-call, etc.) that lands in that window gets accepted here but never
// decrypts on the recipient's side: shows as "Waiting for this message. This
// may take a while." forever. The bridge restarts often (self-update every
// ~10min, health-watchdog restarts), so this window gets hit for real. Gate
// /send on it; callers (the companion outbox, ack-retry up to 3x) already
// handle a 503 by retrying on the next poll -- no in-bridge wait needed.
const SEND_SETTLE_MS = 5000;
function socketReady() {
  return connState === "open" && connectedSince > 0 && (Date.now() - connectedSince) >= SEND_SETTLE_MS;
}

/** Places one announcement call in a completely separate OS process (see
 * voice-runner.js's header for why). Any crash, hang, or malformed output
 * from that child comes back as a clean {ok:false, error} here -- it can
 * never throw, hang this handler forever, or touch this process's own
 * WhatsApp connection. */
function runVoiceCallIsolated(to, message) {
  return new Promise((resolve) => {
    const child = execFile(
      process.execPath,
      [path.join(__dirname, "voice-runner.js")],
      { cwd: __dirname, timeout: VOICE_RUNNER_TIMEOUT_MS, windowsHide: true },
      (err, stdout) => {
        if (err && err.killed) {
          resolve({ ok: false, error: "voice call process timed out" });
          return;
        }
        const line = (stdout || "").trim().split("\n").pop() || "";
        try {
          const parsed = JSON.parse(line);
          if (typeof parsed.ok === "boolean") {
            resolve(parsed);
            return;
          }
        } catch { /* fall through */ }
        // the child died/crashed before printing valid JSON -- still a
        // clean, ordinary failure from this process's point of view
        resolve({
          ok: false,
          error: err
            ? `voice call process exited abnormally (${err.code ?? err.message})`
            : "voice call process produced no result",
        });
      }
    );
    try {
      child.stdin.write(JSON.stringify({ to, message }));
      child.stdin.end();
    } catch (e) {
      resolve({ ok: false, error: `couldn't start voice call process: ${e.message}` });
    }
  });
}
const LOGGED_OUT_FLAG = path.join(AUTH_DIR, "..", ".logged_out");

function setLoggedOutFlag(on) {
  try {
    if (on) fs.writeFileSync(LOGGED_OUT_FLAG, new Date().toISOString());
    else if (fs.existsSync(LOGGED_OUT_FLAG)) fs.unlinkSync(LOGGED_OUT_FLAG);
  } catch {
    /* best effort */
  }
}

// ── stickers ────────────────────────────────────────────────────────────────
// A local pool the bot can send at random or by mood. You teach it stickers
// by DMing "!stickers" and then sending it the stickers you want (from your
// Business app favourites, forwarded here). Files + a _tags.json map live in
// ./stickers/ (gitignored). Nothing here ever fetches from the internet.
const STICKER_DIR = process.env.STICKER_DIR || "./stickers";
const STICKER_TAGS_FILE = path.join(STICKER_DIR, "_tags.json");
// chance (1 in N) of tacking a random sticker onto a normal DM reply
const STICKER_RATE = Math.max(0, parseInt(process.env.STICKER_RATE || "4", 10));
let stickerTags = {};                 // { "file.webp": ["smug","bruh"] }
const stickerCapture = new Map();     // jid -> { until: ms, lastFile: string|null, lastAt: ms }

// Burst reactions: when a group the bot operates in goes lively (many messages,
// several people, short window) it reads the recent chat and MAY drop a fitting
// sticker. Heavily throttled: a long cooldown, and Lumina says "none" most of
// the time. Only runs for groups where the bot has already answered something
// (i.e. allow-listed + operational).
const BURST_WINDOW_MS = parseInt(process.env.BURST_WINDOW_MS || "90000", 10);
const BURST_COUNT = parseInt(process.env.BURST_COUNT || "6", 10);
const BURST_COOLDOWN_MS = parseInt(process.env.BURST_COOLDOWN_MS || "1800000", 10); // 30 min
const groupBuf = new Map();       // jid -> [{ name, text, ts }]
const lastBurst = new Map();      // jid -> ms of last burst check
const activeGroups = new Set();   // groups Lumina has replied in at least once

// ── new-member greetings ────────────────────────────────────────────────────
// Only fires in a group already in activeGroups (i.e. allow-listed and the
// bot has spoken there at least once) -- same conservative gate the burst
// reactions use, so the bridge doesn't start proactively messaging some
// random group it happens to sit in but was never meant to be active in.
// Debounced per (group, person) for a short window in case Baileys ever
// re-emits the same participant-update on a reconnect.
const GREETINGS = [
  "Welcome to the group, @{n}! Good to have you here.",
  "Hey @{n}, welcome aboard!",
  "@{n} just joined — welcome!",
  "Everyone say hi to @{n}, glad you're here.",
];
const recentGreets = new Map(); // "groupJid|participantJid" -> ms
const GREET_DEBOUNCE_MS = 5 * 60 * 1000;

async function greetNewMember(sock, groupJid, participantJid) {
  const key = `${groupJid}|${participantJid}`;
  const now = Date.now();
  if (now - (recentGreets.get(key) || 0) < GREET_DEBOUNCE_MS) return;
  recentGreets.set(key, now);
  // The mentions[] entry must be the real jid WhatsApp gave us (lid or pn),
  // but the visible "@digits" text needs the actual phone number to render
  // correctly for a lid-addressed participant -- same lid->pn resolution
  // used for inbound messages above.
  let num = bareJid(participantJid);
  if (participantJid.endsWith("@lid")) {
    try {
      const pn = await sock.signalRepository?.lidMapping?.getPNForLID?.(participantJid);
      if (pn) num = bareJid(pn);
    } catch { /* not available on this Baileys build */ }
  }
  const template = GREETINGS[Math.floor(Math.random() * GREETINGS.length)];
  const text = template.replace("{n}", num);
  try {
    await sock.sendMessage(groupJid, { text, mentions: [participantJid] });
    log(`greeted new member ${num} in ${bareJid(groupJid)}`);
  } catch (e) {
    log(`greeting send failed: ${e.message}`);
  }
  const sp = pickSticker("happy");
  if (sp) await sendSticker(sock, groupJid, sp);
}

function pushGroupMsg(jid, name, text) {
  let buf = groupBuf.get(jid);
  if (!buf) { buf = []; groupBuf.set(jid, buf); }
  const now = Date.now();
  buf.push({ name, text, ts: now });
  while (buf.length > 20 || (buf.length && now - buf[0].ts > 300000)) buf.shift();
}

async function maybeReactToBurst(sock, jid, groupNm) {
  // Owner request 2026-10-26: no stickers in the group when several messages
  // arrive. Set BURST_REACT=on to bring the reaction back.
  if (process.env.BURST_REACT !== "on") return;
  if (STICKER_RATE <= 0|| !activeGroups.has(jid) || !stickerFiles().length) return;
  const now = Date.now();
  const recent = (groupBuf.get(jid) || []).filter((m) => now - m.ts <= BURST_WINDOW_MS);
  const people = new Set(recent.map((m) => m.name));
  if (recent.length < BURST_COUNT || people.size < 2) return;
  if (now - (lastBurst.get(jid) || 0) < BURST_COOLDOWN_MS) return;
  lastBurst.set(jid, now);   // start the cooldown whether or not we react
  const tags = Array.from(
    new Set(Object.values(stickerTags).flat().map((t) => String(t).toLowerCase()))
  );
  try {
    const res = await fetch(`${LUMINA_URL}/whatsapp/bridge`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${TOKEN}` },
      body: JSON.stringify({
        burst: true, group_id: jid, group_name: groupNm, tags,
        messages: recent.map((m) => ({ name: m.name, text: m.text })),
      }),
      signal: AbortSignal.timeout(30000),
    });
    if (!res.ok) return;
    const mood = (await res.json())?.sticker;
    if (mood) {
      const sp = pickSticker(mood);
      if (sp) {
        await sendSticker(sock, jid, sp);
        log(`burst react in ${bareJid(jid)}: ${mood}`);
      }
    }
  } catch (e) {
    log(`burst react error: ${e.message}`);
  }
}

function loadStickerTags() {
  try {
    stickerTags = JSON.parse(fs.readFileSync(STICKER_TAGS_FILE, "utf8"));
  } catch {
    stickerTags = {};
  }
}
function saveStickerTags() {
  try {
    fs.writeFileSync(STICKER_TAGS_FILE, JSON.stringify(stickerTags, null, 2));
  } catch (e) {
    log(`sticker tags save failed: ${e.message}`);
  }
}
function stickerFiles() {
  try {
    return fs.readdirSync(STICKER_DIR).filter((f) => /\.webp$/i.test(f));
  } catch {
    return [];
  }
}
function pickSticker(mood) {
  const files = stickerFiles();
  if (!files.length) return null;
  const m = (mood || "").trim().toLowerCase();
  if (m && m !== "random") {
    const tagged = files.filter((f) =>
      (stickerTags[f] || []).some((t) => t.toLowerCase().includes(m) || m.includes(t.toLowerCase()))
    );
    if (tagged.length) return path.join(STICKER_DIR, tagged[Math.floor(Math.random() * tagged.length)]);
  }
  return path.join(STICKER_DIR, files[Math.floor(Math.random() * files.length)]);
}
async function sendSticker(sock, jid, filePath) {
  try {
    await sock.sendMessage(jid, { sticker: fs.readFileSync(filePath) });
    log(`sticker -> ${bareJid(jid)}: ${path.basename(filePath)}`);
    return true;
  } catch (e) {
    log(`sticker send failed: ${e.message}`);
    return false;
  }
}
try {
  fs.mkdirSync(STICKER_DIR, { recursive: true });
} catch {
  /* ignore */
}
loadStickerTags();

function startSendServer() {
  const srv = http.createServer((req, res) => {
    // unauthenticated health probe (loopback-only server) for the watchdog
    if (req.method === "GET" && (req.url === "/health" || req.url === "/")) {
      const connected = connState === "open" && !!currentSock;
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({
        ok: true,
        connected,
        state: connState,
        loggedOut,
        connectedSince: connectedSince || null,
        uptimeSec: connectedSince ? Math.round((Date.now() - connectedSince) / 1000) : 0,
      }));
      return;
    }
    if ((req.headers["authorization"] || "") !== `Bearer ${TOKEN}`) {
      if (req.method === "POST" && (req.url === "/send" || req.url === "/call" || req.url === "/shutdown")) {
        res.writeHead(401);
        res.end("unauthorized");
        return;
      }
    }

    if (req.method === "POST" && req.url === "/send") {
      let body = "";
      req.on("data", (c) => {
        body += c;
        if (body.length > 6 * 1024 * 1024) req.destroy();  // room for a base64 picture
      });
      req.on("end", async () => {
        try {
          const { to, text, mentionAll, imageB64 } = JSON.parse(body || "{}");
          if (!to || (!text && !imageB64)) {
            res.writeHead(400);
            res.end('{"error":"to and text required"}');
            return;
          }
          if (!currentSock) {
            res.writeHead(503);
            res.end('{"error":"not connected"}');
            return;
          }
          if (!socketReady()) {
            res.writeHead(503);
            res.end('{"error":"bridge just (re)connected, still settling -- retry shortly"}');
            return;
          }
          let outText = String(text);
          let mentions;
          // Tag every group member (e.g. the Tue/Thu meeting-link post). Only
          // meaningful for a group jid -- ignored otherwise. WhatsApp only
          // renders a highlighted tag where the literal "@<number>" text
          // appears, so the mentions[] jid array alone isn't enough -- we
          // build a leading line of @tags the same way greetNewMember does
          // for one person, just for everyone in the group at once.
          if (mentionAll && String(to).endsWith("@g.us")) {
            try {
              const meta = await currentSock.groupMetadata(String(to));
              const selfNum = bareJid(currentSock.user?.id || "");
              const parts = (meta.participants || []).filter((p) => bareJid(p.id) !== selfNum);
              mentions = parts.map((p) => p.id);
              const tags = [];
              for (const p of parts) {
                let num = bareJid(p.id);
                if (p.id.endsWith("@lid")) {
                  try {
                    const pn = await currentSock.signalRepository?.lidMapping?.getPNForLID?.(p.id);
                    if (pn) num = bareJid(pn);
                  } catch { /* not available on this Baileys build */ }
                }
                tags.push(`@${num}`);
              }
              if (tags.length) outText = `${tags.join(" ")}\n\n${outText}`;
            } catch (e) {
              log(`mentionAll: couldn't resolve group members (${e.message}) -- sending untagged`);
            }
          }
          let sent;
          if (imageB64) {
            // Picture with the text as its caption (WhatsApp caps captions
            // near 1024 chars; a longer text goes as a separate message).
            const image = Buffer.from(String(imageB64), "base64");
            const cap = outText.length <= 1000 ? outText : "";
            sent = await currentSock.sendMessage(String(to), cap ? { image, caption: cap } : { image });
            if (!cap && outText) await currentSock.sendMessage(String(to), { text: outText });
          } else {
            sent = await currentSock.sendMessage(
              String(to),
              mentions ? { text: outText, mentions } : { text: outText }
            );
          }
          const waId = sent?.key?.id || null;
          log(`push -> ${to}: ${String(text).slice(0, 80)}`);
          res.writeHead(200, { "Content-Type": "application/json" });
          res.end(JSON.stringify({ ok: true, id: waId }));
        } catch (e) {
          log(`push error: ${e.message}`);
          res.writeHead(500);
          res.end(JSON.stringify({ error: e.message }));
        }
      });
      return;
    }

    // One-way WhatsApp announcement call — see voice.js. Loopback-only,
    // same TOKEN auth as /send, polled by scripts/laptop_agent.py's
    // job_voice_calls. Lazy-loads voice.js so a bridge with no voice setup
    // at all (the common case, at least until voice is set up) still
    // starts and runs the text bridge completely normally.
    if (req.method === "POST" && req.url === "/call") {
      let body = "";
      req.on("data", (c) => {
        body += c;
        if (body.length > 65536) req.destroy();
      });
      req.on("end", async () => {
        try {
          const { to, message } = JSON.parse(body || "{}");
          if (!to || !message) {
            res.writeHead(400);
            res.end('{"error":"to and message required"}');
            return;
          }
          // Isolated in a separate OS process on purpose -- see
          // voice-runner.js's header. A crash in the WASM VoIP library
          // (confirmed to happen, 2026-09-15 -- see CLAUDE.md) must NEVER
          // take the text bridge's live WhatsApp session down with it.
          const result = await runVoiceCallIsolated(String(to), String(message));
          log(result.ok
            ? `call -> ${to}: placed`
            : `call -> ${to}: failed (${result.error})`);
          res.writeHead(200, { "Content-Type": "application/json" });
          res.end(JSON.stringify(result));
        } catch (e) {
          log(`call route error: ${e.message}`);
          res.writeHead(500);
          res.end(JSON.stringify({ ok: false, error: e.message }));
        }
      });
      return;
    }

    // Graceful remote shutdown. Needed because the Python companion
    // (scripts/laptop_agent.py) can only hold a killable process handle for
    // a bridge IT spawned -- after a scripts-only companion restart the
    // bridge survives as a deliberate orphan (so the WhatsApp session isn't
    // dropped, see index.js's own docs in laptop_agent.py) and the new
    // companion process only "adopts" it via this health port, never gets
    // its PID. Without this endpoint, a later bridge-code push had no way
    // to actually stop that adopted orphan -- _stop_bridge() silently did
    // nothing, so the OLD code just kept running forever, or worse, some
    // future change could spawn a second live instance alongside it and
    // fork the WhatsApp session (see CLAUDE.md gotcha #118 -- "Waiting for
    // this message" on recipients' phones). Same Bearer-token auth as
    // /send and /call.
    if (req.method === "POST" && req.url === "/shutdown") {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end('{"ok":true,"exiting":true}');
      log("shutdown requested via /shutdown -- exiting");
      setTimeout(() => process.exit(0), 150);
      return;
    }

    res.writeHead(404);
    res.end("not found");
  });
  srv.on("error", (e) => {
    if (e.code === "EADDRINUSE") {
      // a previous instance hasn't released the port yet — retry
      log(`send port ${SEND_PORT} busy, retrying in 3s`);
      setTimeout(() => srv.listen(SEND_PORT, "127.0.0.1"), 3000);
    } else {
      log(`send server error: ${e.message}`);
    }
  });
  srv.listen(SEND_PORT, "127.0.0.1", () => log(`send endpoint on 127.0.0.1:${SEND_PORT}`));
}

// Baileys redelivers messages on reconnect — remember the last N ids so a
// redelivery doesn't trigger a second reply.
const seen = new Set();

// ---- permanent local archive (this laptop only, never Railway) ----------
// Every message the bot reads (allow-listed groups + DMs to the bot) and every
// reply it sends is appended to archive/YYYY-MM-DD.jsonl (IST day); voice
// notes and images of allow-listed groups are saved under archive/media/.
const ARCHIVE_DIR = path.join(path.dirname(fileURLToPath(import.meta.url)), "archive");
function istDay(ms) { return new Date(ms + 330 * 60000).toISOString().slice(0, 10); }
function archive(rec) {
  try {
    const ts = Date.now();
    fs.mkdirSync(ARCHIVE_DIR, { recursive: true });
    fs.appendFileSync(path.join(ARCHIVE_DIR, `${istDay(ts)}.jsonl`),
      JSON.stringify({ ts, at: new Date(ts + 330 * 60000).toISOString().replace("Z", "+05:30"), ...rec }) + "\n");
  } catch (e) { /* the archive must never break messaging */ }
}
function archiveMedia(id, ext, buf) {
  try {
    const dir = path.join(ARCHIVE_DIR, "media");
    fs.mkdirSync(dir, { recursive: true });
    const name = `${istDay(Date.now())}_${String(id || Date.now()).replace(/[^\w-]/g, "")}.${ext}`;
    fs.writeFileSync(path.join(dir, name), buf);
    return name;
  } catch { return ""; }
}
function alreadyHandled(id) {
  if (!id) return false;
  if (seen.has(id)) return true;
  seen.add(id);
  if (seen.size > 500) seen.delete(seen.values().next().value);
  return false;
}

function extractText(message) {
  if (!message) return "";
  return (
    message.conversation ||
    message.extendedTextMessage?.text ||
    message.imageMessage?.caption ||
    message.videoMessage?.caption ||
    ""
  ).trim();
}

// digits of a jid ("<phone>:<device>@s.whatsapp.net" / "<id>@lid" -> "<phone>")
function bareJid(j) {
  return (j || "").split("@")[0].split(":")[0];
}

// In a group we act when the bot was addressed: the word "lumina" anywhere in
// the message, an @-mention of the bot, or a reply to one of its own messages.
// Returns the question to forward, or null to ignore the message.
const GROUP_PREFIX = /^\s*(@?lumina|@?assistant)[\s:,\-]+|^\s*[/!](ask|lumina)\s+/i;
const GROUP_NAME_RE = /\blumina\b/i;
function groupQuery(text, addressed) {
  if (GROUP_PREFIX.test(text)) return text.replace(GROUP_PREFIX, "").trim();
  if (GROUP_NAME_RE.test(text)) return text.trim();          // name mentioned anywhere
  if (addressed) return text.replace(/@\d{5,}/g, "").trim(); // @-mention or reply
  return null;
}

const _groupNames = new Map();
async function groupName(sock, jid) {
  if (_groupNames.has(jid)) return _groupNames.get(jid);
  let name = "";
  try {
    name = (await sock.groupMetadata(jid))?.subject || "";
  } catch {
    /* no metadata access */
  }
  _groupNames.set(jid, name);
  return name;
}

// ── group memory ──────────────────────────────────────────────────────────
// Every text message from an allow-listed (staff-only) group is batched and sent
// to Lumina, which stores it (no AI call) so the assistant can follow the chat.
// The bridge asks Lumina which groups are allowed, so nothing from other groups
// ever leaves this machine.
let _obsAllow = new Set();
let _obsAllowAt = 0;
const _obsQueue = new Map();   // groupJid -> { name, msgs: [] }

async function refreshObserveAllow() {
  if (Date.now() - _obsAllowAt < 5 * 60000) return;
  _obsAllowAt = Date.now();
  try {
    const res = await fetch(`${LUMINA_URL}/api/whatsapp/groups`, {
      headers: { Authorization: `Bearer ${TOKEN}` },
      signal: AbortSignal.timeout(15000),
    });
    if (!res.ok) return;
    const d = await res.json();
    _obsAllow = new Set((d.effective || []).map((x) => String(x).replace(/\D/g, "")));
  } catch { /* keep the previous list */ }
}

// '@<lid>' in a group message -> '@<phone>' when WhatsApp's LID map knows the number,
// so the server can turn it into the person's name.
async function resolveMentionText(sock, msg, text) {
  try {
    if (!text || !text.includes("@")) return text;
    let ctx = null;
    for (const v of Object.values(msg.message || {})) { if (v && v.contextInfo) { ctx = v.contextInfo; break; } }
    const ids = Array.isArray(ctx?.mentionedJid) ? ctx.mentionedJid : [];
    let out = text;
    for (const j of ids) {
      const lidDigits = bareJid(j);
      let pn = j.endsWith("@lid") ? ((await sock.signalRepository?.lidMapping?.getPNForLID?.(j)) || "") : j;
      const phone = bareJid(pn);
      if (phone && phone !== lidDigits) out = out.split("@" + lidDigits).join("@" + phone);
    }
    return out;
  } catch { return text; }
}

function observeGroupMessage(jid, groupNm, from, pushName, text, id) {
  if (!text || !_obsAllow.has(jid.replace(/\D/g, ""))) return;
  let q = _obsQueue.get(jid);
  if (!q) { q = { name: groupNm || "", msgs: [] }; _obsQueue.set(jid, q); }
  q.msgs.push({ from, name: pushName || "", text: text.slice(0, 1000), ts: Date.now(), id: id || "" });
  if (q.msgs.length >= 25) flushObserved().catch(() => {});
}

async function flushObserved() {
  for (const [jid, q] of Array.from(_obsQueue.entries())) {
    if (!q.msgs.length) continue;
    const batch = q.msgs.splice(0, q.msgs.length);
    try {
      const res = await fetch(`${LUMINA_URL}/whatsapp/bridge`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${TOKEN}` },
        body: JSON.stringify({ observe: true, group_id: jid, group_name: q.name, messages: batch }),
        signal: AbortSignal.timeout(20000),
      });
      if (!res.ok) throw new Error(`Lumina ${res.status}`);
    } catch (e) {
      log(`group observe flush failed: ${e.message}`);
      if (q.msgs.length < 200) q.msgs.unshift(...batch);   // retry next tick, bounded
    }
  }
}
setInterval(() => flushObserved().catch(() => {}), 10000);

const MEDIA_MAX = { audio: 4 * 1024 * 1024, image: 5 * 1024 * 1024 };

// WhatsApp voice notes are ogg/opus, which not every speech model accepts.
// If ffmpeg is on this machine (it is, for voice calling), convert to a small
// mono mp3 first; if not, send the original and let the server try it.
function toMp3(buf) {
  return new Promise((resolve) => {
    const base = path.join(process.env.TEMP || process.env.TMPDIR || "/tmp",
      `lumina_vn_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`);
    const inp = `${base}.ogg`, outp = `${base}.mp3`;
    const cleanup = () => { for (const f of [inp, outp]) { try { fs.unlinkSync(f); } catch { /* ignore */ } } };
    try { fs.writeFileSync(inp, buf); } catch { return resolve(null); }
    execFile("ffmpeg", ["-y", "-loglevel", "error", "-i", inp, "-ac", "1", "-ar", "16000", "-b:a", "32k", outp],
      { timeout: 30000 }, (err) => {
        let out = null;
        if (!err) { try { out = fs.readFileSync(outp); } catch { /* ignore */ } }
        cleanup();
        resolve(out);
      });
  });
}

async function observeGroupMedia(sock, msg, jid) {
  const am = msg.message?.audioMessage;
  const im = msg.message?.imageMessage;
  const kind = am ? "audio" : "image";
  const info = am || im;
  const size = Number(info?.fileLength || 0);
  if (size > MEDIA_MAX[kind]) return;
  const buf = await downloadMediaMessage(msg, "buffer", {}, { reuploadRequest: sock.updateMediaMessage });
  if (!buf || buf.length > MEDIA_MAX[kind]) return;
  const rp = msg.key.participant || "";
  let pj = msg.key.participantPn || "";
  if (!pj && rp.endsWith("@lid")) {
    try { pj = (await sock.signalRepository?.lidMapping?.getPNForLID?.(rp)) || ""; } catch { /* n/a */ }
  }
  const file = archiveMedia(msg.key.id, kind === "audio" ? "ogg" : ((info?.mimetype || "").split("/")[1] || "jpg").split(";")[0], buf);
  archive({ dir: "in", chat: "group", group_id: bareJid(jid), group: _groupNames.get(jid) || "", from: bareJid(pj || rp),
            name: msg.pushName || "", id: msg.key.id, type: kind, caption: im?.caption || "", file });
  let sendBuf = buf, sendMime = info?.mimetype || "";
  if (kind === "audio") {
    const mp3 = await toMp3(buf);
    if (mp3 && mp3.length) { sendBuf = mp3; sendMime = "audio/mpeg"; }
  }
  const res = await fetch(`${LUMINA_URL}/whatsapp/bridge`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${TOKEN}` },
    body: JSON.stringify({
      observe_media: true, group_id: jid, group_name: _groupNames.get(jid) || "",
      kind, mime: sendMime, data_b64: sendBuf.toString("base64"),
      caption: im?.caption || "", from: bareJid(pj || rp), name: msg.pushName || "",
      ts: Date.now(), id: msg.key.id,
    }),
    signal: AbortSignal.timeout(90000),
  });
  if (!res.ok) throw new Error(`Lumina ${res.status}`);
}

async function askLumina(from, text, groupId, groupNm) {
  const body = { from, text };
  if (groupId) {
    body.group_id = groupId;
    if (groupNm) body.group_name = groupNm;
  }
  const res = await fetch(`${LUMINA_URL}/whatsapp/bridge`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${TOKEN}` },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(90000), // agent tool-loop + web search can be slow
  });
  if (!res.ok) {
    const bodyTxt = await res.text().catch(() => "");
    throw new Error(`Lumina ${res.status}: ${bodyTxt.slice(0, 200)}`);
  }
  return await res.json();   // { reply, sticker?, sticker_cmd? }
}

async function start() {
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
  const { version } = await fetchLatestBaileysVersion().catch(() => ({ version: undefined }));

  const sock = makeWASocket({
    version,
    auth: state,
    logger: pino({ level: "silent" }),
    browser: ["Lumina Bridge", "Chrome", "1.0.0"],
    markOnlineOnConnect: false,
    syncFullHistory: false,
  });
  currentSock = sock;

  sock.ev.on("creds.update", saveCreds);

  sock.ev.on("connection.update", (u) => {
    const { connection, lastDisconnect, qr } = u;
    if (qr) {
      console.log("\nScan this QR with the DEDICATED WhatsApp number (Linked devices > Link a device):\n");
      qrcode.generate(qr, { small: true });
    }
    if (connection === "connecting") connState = "connecting";
    if (connection === "open") {
      connState = "open";
      connectedSince = Date.now();
      loggedOut = false;
      setLoggedOutFlag(false);
      log(`connected. Bridging  ${LUMINA_URL}  (reply-only).`);
    }
    if (connection === "close") {
      connState = "close";
      connectedSince = 0;
      const code = lastDisconnect?.error?.output?.statusCode;
      if (code === DisconnectReason.loggedOut) {
        loggedOut = true;
        setLoggedOutFlag(true);
        log("logged out on the phone. Delete ./auth and re-pair. Exiting.");
        process.exit(1);
      }
      if (code === DisconnectReason.connectionReplaced) {
        // another bridge instance took over this session — don't fight it,
        // just exit. (If it was a stale orphan, the supervisor restarts us.)
        log("connection replaced by another instance. Exiting.");
        process.exit(1);
      }
      log(`connection closed (${code}); reconnecting in 3s…`);
      setTimeout(start, 3000);
    }
  });

  sock.ev.on("group-participants.update", async (update) => {
    try {
      if (update.action !== "add") return;
      const groupJid = update.id;
      if (!activeGroups.has(groupJid)) return;   // not an active/allow-listed group
      const selfNum = bareJid(sock.user?.id || "");
      for (const p of update.participants || []) {
        if (bareJid(p) === selfNum) continue;    // the bridge itself was added — not a greeting
        await greetNewMember(sock, groupJid, p);
      }
    } catch (e) {
      log(`group-participants.update error: ${e.message}`);
    }
  });

  sock.ev.on("messages.upsert", async ({ messages, type }) => {
    if (type !== "notify") return;
    for (const msg of messages) {
      try {
        if (!msg.message || msg.key.fromMe) continue;
        if (alreadyHandled(msg.key.id)) continue;
        const jid = msg.key.remoteJid || "";   // conversation address (may be a @lid)
        // no status, no channels/newsletters/broadcasts
        if (
          jid === "status@broadcast" ||
          jid.endsWith("@newsletter") ||
          jid.endsWith("@broadcast")
        )
          continue;

        const isGroup = jid.endsWith("@g.us");
        const rawText = extractText(msg.message);

        // ── sticker capture (DM only) ──────────────────────────────────────
        const cap = !isGroup ? stickerCapture.get(jid) : null;
        const capActive = cap && cap.until > Date.now();
        if (!isGroup && msg.message?.stickerMessage) {
          if (!capActive) continue;    // not teaching right now — ignore stray stickers
          try {
            const buf = await downloadMediaMessage(msg, "buffer", {}, {
              reuploadRequest: sock.updateMediaMessage,
            });
            const fname = `s_${Date.now().toString(36)}.webp`;
            fs.writeFileSync(path.join(STICKER_DIR, fname), buf);
            cap.lastFile = fname;
            cap.lastAt = Date.now();
            const n = stickerFiles().length;
            await sock.sendMessage(jid, { text: `Saved (${n} total). Send a one-word tag now to categorise it, or another sticker. "!stickers done" to stop.` });
            log(`sticker saved from ${bareJid(jid)}: ${fname} (${n} total)`);
          } catch (e) {
            log(`sticker capture failed: ${e.message}`);
            await sock.sendMessage(jid, { text: "Couldn't save that one, try again." }).catch(() => {});
          }
          continue;
        }
        // voice notes / images in an allow-listed group: send to Lumina to be
        // turned into text (stored as group memory; never replied to)
        if (isGroup && (msg.message?.audioMessage || msg.message?.imageMessage)) {
          await refreshObserveAllow();
          if (_obsAllow.has(jid.replace(/\D/g, ""))) {
            observeGroupMedia(sock, msg, jid).catch((e) => log(`media observe failed: ${e.message}`));
          }
          if (!rawText) continue;      // an image caption still flows on as text
        }
        if (!rawText) continue;

        // during capture, a short word right after a saved sticker = its tag(s)
        if (capActive && cap.lastFile && Date.now() - (cap.lastAt || 0) < 45000
            && !/^!/.test(rawText) && rawText.trim().split(/\s+/).length <= 3) {
          const tags = rawText.toLowerCase().split(/[\s,]+/).filter(Boolean);
          stickerTags[cap.lastFile] = Array.from(new Set([...(stickerTags[cap.lastFile] || []), ...tags]));
          saveStickerTags();
          cap.lastAt = 0;   // one tag set per sticker
          await sock.sendMessage(jid, { text: `Tagged: ${tags.join(", ")}` });
          continue;
        }

        let text = rawText;
        let groupId = null;
        let groupNm = null;

        if (isGroup) {
          // only respond when the bot was addressed
          const ctx = msg.message?.extendedTextMessage?.contextInfo;
          const mentioned = Array.isArray(ctx?.mentionedJid)
            ? ctx.mentionedJid.map(bareJid)
            : [];
          const botIds = [bareJid(sock.user?.id), bareJid(sock.user?.lid)].filter(Boolean);
          const addressed =
            mentioned.some((m) => botIds.includes(m)) ||
            botIds.includes(bareJid(ctx?.participant)); // reply to the bot's msg

          groupNm = await groupName(sock, jid);
          // observe every message in the group for burst detection
          pushGroupMsg(jid, msg.pushName || bareJid(msg.key.participant || "") || "someone", rawText);
          maybeReactToBurst(sock, jid, groupNm).catch(() => {});

          // remember it (allow-listed groups only): resolve who sent it first
          await refreshObserveAllow();
          if (_obsAllow.has(jid.replace(/\D/g, ""))) {
            const rp = msg.key.participant || "";
            let pj = msg.key.participantPn || "";
            if (!pj && rp.endsWith("@lid")) {
              try { pj = (await sock.signalRepository?.lidMapping?.getPNForLID?.(rp)) || ""; } catch { /* n/a */ }
            }
            const obsText = await resolveMentionText(sock, msg, rawText);
            observeGroupMessage(jid, groupNm, bareJid(pj || rp), msg.pushName || "", obsText, msg.key.id);
            archive({ dir: "in", chat: "group", group_id: bareJid(jid), group: groupNm || "", from: bareJid(pj || rp),
                      name: msg.pushName || "", id: msg.key.id, text: obsText });
          }

          const q = groupQuery(rawText, addressed);
          if (!q) continue;
          text = q;
          groupId = jid;
        }

        // Identity is the person, by phone number. In a DM that's key.senderPn;
        // in a group it's key.participantPn. WhatsApp increasingly addresses
        // people by LID (privacy id) — fall back to the LID->PN map, then raw.
        const pnField = isGroup ? msg.key.participantPn : msg.key.senderPn;
        const rawPart = isGroup ? (msg.key.participant || "") : jid;
        let idJid = pnField || "";
        if (!idJid && rawPart.endsWith("@lid")) {
          try {
            idJid = (await sock.signalRepository?.lidMapping?.getPNForLID?.(rawPart)) || "";
          } catch { /* not available on this Baileys build */ }
        }
        if (!idJid) idJid = rawPart;
        const from = bareJid(idJid);
        if (rawPart.endsWith("@lid") && idJid !== rawPart)
          log(`  (resolved LID ${bareJid(rawPart)} -> ${from})`);
        else if (rawPart.endsWith("@lid"))
          log(`  (WARN: LID ${bareJid(rawPart)} — no phone number available)`);

        if (isGroup)
          log(`grp ${bareJid(jid)}${groupNm ? ` (${groupNm})` : ""} <- ${from}: ${text.slice(0, 80)}`);
        else {
          log(`in  ${from}: ${text.slice(0, 80)}`);
          archive({ dir: "in", chat: "dm", from, name: msg.pushName || "", id: msg.key.id, text: rawText });
        }

        await sock.readMessages([msg.key]).catch(() => {});
        await sock.sendPresenceUpdate("composing", jid).catch(() => {});

        let data;
        try {
          data = await askLumina(from, text, groupId, groupNm);
        } catch (e) {
          log(`Lumina error: ${e.message}`);
          // in a group, stay quiet on error rather than posting noise for all
          data = {
            reply: isGroup
              ? null
              : /timeout|aborted|fetch failed|Lumina 5\d\d/i.test(e.message)
                ? "The server is slow right now, so I may not have finished that. Check in a minute whether it went through before asking again."
                : "Sorry — I couldn't reach the assistant just now. Try again in a moment.",
          };
        }

        // sticker-management directives (DM only, employee-gated by Lumina)
        if (!isGroup && data?.sticker_cmd) {
          const c = data.sticker_cmd;
          if (c === "on") {
            stickerCapture.set(jid, { until: Date.now() + 5 * 60000, lastFile: null, lastAt: 0 });
          } else if (c === "off") {
            stickerCapture.delete(jid);
          } else if (c === "clear") {
            for (const f of stickerFiles()) {
              try { fs.unlinkSync(path.join(STICKER_DIR, f)); } catch { /* ignore */ }
            }
            stickerTags = {};
            saveStickerTags();
          } else if (c === "list") {
            const fl = stickerFiles();
            data.reply = `${fl.length} sticker(s) saved, ${fl.filter((f) => (stickerTags[f] || []).length).length} tagged.`;
          }
        }

        const reply = data?.reply || null;
        await sock.sendPresenceUpdate("paused", jid).catch(() => {});
        if (reply) {
          await sock.sendMessage(jid, { text: reply });
          if (isGroup) activeGroups.add(jid);   // burst detection may run here now
          log(`out ${from}: ${reply.slice(0, 80)}`);
          archive({ dir: "out", chat: isGroup ? "group" : "dm", group_id: isGroup ? bareJid(jid) : undefined,
                    group: isGroup ? groupNm || "" : undefined, to: isGroup ? undefined : from, text: reply });
          // a sticker the agent explicitly asked for, or (DM only) random flair
          let sp = null;
          if (data?.sticker) sp = pickSticker(data.sticker);
          else if (!isGroup && STICKER_RATE > 0 && Math.random() < 1 / STICKER_RATE)
            sp = pickSticker(null);
          if (sp) await sendSticker(sock, jid, sp);
        } else if (isGroup) {
          log(`  (no reply — group ${bareJid(jid)} not on allow-list, or sender not staff)`);
        }
      } catch (e) {
        log(`handler error: ${e.message}`);
      }
    }
  });
}

startSendServer();

start().catch((e) => {
  console.error("bridge failed to start:", e);
  process.exit(1);
});
