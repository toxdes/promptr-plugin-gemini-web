const HOST = "com.toxdes.promptr_gemini_web";
const ORIGIN = "https://gemini.google.com";
const jobs = new Map();
let port;

function status(badge, title) {
  chrome.action.setBadgeText({text: badge});
  chrome.action.setTitle({title: `Promptr Gemini Web: ${title}`});
}

function isGemini(url) {
  return url && new URL(url).origin === ORIGIN;
}

async function waitForGemini(job) {
  const deadline = Date.now() + 15000;
  while (!job.cancelled && Date.now() < deadline) {
    const tab = await chrome.tabs.get(job.tabId);
    if (tab.status === "complete" && isGemini(tab.url)) return;
    await new Promise(resolve => setTimeout(resolve, 150));
  }
  throw new Error("Gemini did not load in this Chrome profile.");
}

async function cancel(job) {
  job.cancelled = true;
  if (job.tabId !== undefined) {
    await chrome.tabs.remove(job.tabId).catch(() => {});
  }
}

async function handle(connection, request) {
  if (request.ready === true) {
    status("ON", "Connected to Promptr");
    return;
  }
  if (request.operation === "cancel") {
    const job = jobs.get(request.id);
    if (job) await cancel(job);
    return;
  }
  if (!["models", "submit"].includes(request.operation)) return;

  const job = {connection, cancelled: false};
  jobs.set(request.id, job);
  let result;
  try {
    const tab = await chrome.tabs.create({url: "about:blank", active: false});
    job.tabId = tab.id;
    if (job.cancelled) return;
    await chrome.tabs.update(tab.id, {url: `${ORIGIN}/app?hl=en`});
    await waitForGemini(job);
    if (job.cancelled) return;
    await chrome.scripting.executeScript({target: {tabId: tab.id}, files: ["gemini.js"]});
    result = await chrome.tabs.sendMessage(tab.id, request, {frameId: 0});
  } catch {
    result = {error: "Could not open Gemini in Chrome. Check that Gemini is available and you’re signed in."};
  } finally {
    jobs.delete(request.id);
    if (job.tabId !== undefined) {
      await chrome.tabs.remove(job.tabId).catch(() => {});
    }
  }
  if (!job.cancelled && port === connection) {
    try {
      connection.postMessage({...result, id: request.id});
    } catch {
      // The native host may have closed while the tab was open.
    }
  }
}

function connect() {
  if (port) return;
  const connection = chrome.runtime.connectNative(HOST);
  port = connection;
  status("...", "Connecting");
  connection.onMessage.addListener(request => void handle(connection, request));
  connection.onDisconnect.addListener(() => {
    void chrome.runtime.lastError;
    port = undefined;
    status("!", "Disconnected. Click to connect");
    for (const job of jobs.values()) {
      if (job.connection === connection) void cancel(job);
    }
  });
}

chrome.action.onClicked.addListener(connect);
chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
