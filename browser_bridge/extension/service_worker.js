"use strict";

const MAX_TARGET = 1000;
const MAX_VALUE = 20000;
const MAX_TEXT = 100000;
const MAX_MESSAGE_BYTES = 64 * 1024;
const OPERATIONS = new Set(["inspect", "navigate", "extract", "click", "type", "select", "scroll", "upload", "download", "submit"]);
const states = new Map();
const seenRequestIds = new Set();
let lastSequence = 0;

function fail(message) { throw new Error(message); }

function canonicalOrigin(value) {
  if (typeof value !== "string" || !value || value.length > 2048 || /[\u0000-\u0020]/.test(value)) fail("invalid origin");
  let parsed;
  try { parsed = new URL(value); } catch (_) { fail("invalid origin"); }
  if (!/^https?:$/.test(parsed.protocol) || parsed.username || parsed.password || parsed.search || parsed.hash || (parsed.pathname !== "/" && parsed.pathname !== "")) fail("invalid origin");
  const port = parsed.port && !((parsed.protocol === "https:" && parsed.port === "443") || (parsed.protocol === "http:" && parsed.port === "80")) ? `:${parsed.port}` : "";
  return `${parsed.protocol.toLowerCase()}//${parsed.hostname.toLowerCase()}${port}`;
}

function originFromUrl(value) {
  if (typeof value !== "string" || value.length > 8192 || /[\u0000-\u0020]/.test(value)) fail("invalid navigation URL");
  let parsed;
  try { parsed = new URL(value); } catch (_) { fail("invalid navigation URL"); }
  if (!/^https?:$/.test(parsed.protocol) || parsed.username || parsed.password) fail("invalid navigation URL");
  return canonicalOrigin(parsed.origin);
}

async function allowedOrigins() {
  const config = await chrome.storage.local.get({allowed_origins: []});
  if (!Array.isArray(config.allowed_origins)) return new Set();
  const result = new Set();
  for (const value of config.allowed_origins) {
    try { result.add(canonicalOrigin(value)); } catch (_) { /* malformed config fails closed */ }
  }
  return result;
}

function newDocumentId() { return crypto.randomUUID(); }

function rotate(tabId, url) {
  states.set(tabId, {documentId: newDocumentId(), origin: null, url: url || "", valid: false});
}

function stateFor(tabId, url) {
  let state = states.get(tabId);
  if (!state) { rotate(tabId, url); state = states.get(tabId); }
  return state;
}

function validateMessage(message) {
  const keys = message && typeof message === "object" ? Object.keys(message) : [];
  const allowedKeys = new Set(["kind", "request_id", "sequence", "operation", "tab_id", "origin", "document_id", "target", "value"]);
  if (!message || typeof message !== "object" || keys.some((key) => !allowedKeys.has(key)) || message.kind !== "atlas.operation" || typeof message.operation !== "string" || !OPERATIONS.has(message.operation)) fail("unsupported operation");
  if (typeof message.request_id !== "string" || !/^[A-Za-z0-9._:-]{1,200}$/.test(message.request_id) || !Number.isInteger(message.sequence) || message.sequence < 1 || seenRequestIds.has(message.request_id) || message.sequence <= lastSequence) fail("replayed request");
  seenRequestIds.add(message.request_id); lastSequence = message.sequence;
  if (!Number.isInteger(message.tab_id) || message.tab_id < 0) fail("invalid tab");
  if (typeof message.document_id !== "string" || !/^[A-Za-z0-9._:-]{1,200}$/.test(message.document_id)) fail("invalid document");
  if (typeof message.origin !== "string") fail("invalid origin");
  const origin = canonicalOrigin(message.origin);
  const target = message.target === undefined ? "" : message.target;
  const value = message.value === undefined ? "" : message.value;
  if (typeof target !== "string" || target.length > MAX_TARGET || /[\u0000-\u001f]/.test(target)) fail("invalid target");
  if (typeof value !== "string" || value.length > MAX_VALUE || /[\u0000-\u001f]/.test(value)) fail("invalid value");
  if (["click", "type", "select", "upload", "download", "submit"].includes(message.operation) && !target) fail("target required");
  if (message.operation === "navigate" && originFromUrl(value) !== origin) fail("navigation origin not allowlisted");
  if (message.operation === "navigate" && target) fail("navigate does not accept target");
  if (message.operation === "inspect" && (target || value)) fail("inspect does not accept target or value");
  const encoded = new TextEncoder().encode(JSON.stringify({kind: "atlas.operation", request_id: message.request_id, sequence: message.sequence, operation: message.operation, tab_id: message.tab_id, document_id: message.document_id, origin, target, value}));
  if (encoded.length > MAX_MESSAGE_BYTES) fail("message too large");
  return {request_id: message.request_id, sequence: message.sequence, operation: message.operation, tab_id: message.tab_id, document_id: message.document_id, origin, target, value};
}

async function revalidate(tabId, documentId, expectedOrigin) {
  const tab = await chrome.tabs.get(tabId);
  const state = stateFor(tabId, tab.url || "");
  const observed = originFromUrl(tab.url || "");
  const allow = await allowedOrigins();
  if (!allow.has(expectedOrigin) || !allow.has(observed) || state.documentId !== documentId || observed !== expectedOrigin) fail("tab identity or origin changed");
  return {tab, state, origin: observed};
}

async function inspect(tabId, documentId, origin) {
  await revalidate(tabId, documentId, origin);
  return chrome.tabs.sendMessage(tabId, {kind: "atlas.content", operation: "inspect", document_id: documentId, origin});
}

async function navigate(request) {
  const allow = await allowedOrigins();
  if (!allow.has(request.origin) || originFromUrl(request.value) !== request.origin) fail("navigation origin not allowlisted");
  await revalidate(request.tab_id, request.document_id, request.origin);
  rotate(request.tab_id, request.value);
  await chrome.tabs.update(request.tab_id, {url: request.value});
  const finalTab = await new Promise((resolve, reject) => {
    const listener = async (tabId, info, tab) => {
      if (tabId !== request.tab_id || info.status !== "complete") return;
      chrome.tabs.onUpdated.removeListener(listener);
      try {
        const finalOrigin = originFromUrl(tab.url || "");
        if (!allow.has(finalOrigin) || finalOrigin !== request.origin) reject(new Error("redirect origin not allowlisted"));
        else resolve(tab);
      } catch (error) { reject(error); }
    };
    chrome.tabs.onUpdated.addListener(listener);
  });
  const state = stateFor(request.tab_id, finalTab.url || "");
  return {tab_id: String(request.tab_id), url: finalTab.url || "", origin: request.origin, document_id: state.documentId};
}

async function operation(message) {
  const request = validateMessage(message);
  if (request.operation === "navigate") return navigate(request);
  if (request.operation === "upload" || request.operation === "download") fail("file operations are unavailable in this tranche");
  const identity = await revalidate(request.tab_id, request.document_id, request.origin);
  const result = await chrome.tabs.sendMessage(request.tab_id, {kind: "atlas.content", ...request});
  if (!result || result.origin !== identity.origin || result.document_id !== request.document_id) fail("content identity changed");
  return result;
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!sender || sender.id !== chrome.runtime.id) return false;
  if (message && message.kind === "atlas.hello") {
    const tabId = sender.tab && sender.tab.id;
    if (!Number.isInteger(tabId)) return false;
    const state = stateFor(tabId, sender.tab.url || "");
    allowedOrigins().then((allow) => {
      let origin;
      try { origin = originFromUrl(sender.tab.url || ""); } catch (_) { sendResponse({ok: false, error: "unsupported page"}); return; }
      if (!allow.has(origin)) { sendResponse({ok: false, error: "origin not allowlisted"}); return; }
      state.origin = origin; state.url = sender.tab.url || ""; state.valid = true;
      sendResponse({ok: true, tab_id: String(tabId), origin, document_id: state.documentId});
    });
    return true;
  }
  if (message && message.kind === "atlas.operation") {
    operation(message).then((result) => sendResponse({version: 1, request_id: message.request_id, sequence: message.sequence, ok: true, result})).catch(() => sendResponse({version: 1, request_id: message.request_id, sequence: message.sequence, ok: false, error: "browser operation rejected"}));
    return true;
  }
  return false;
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status === "loading") rotate(tabId, tab.url || "");
  if (changeInfo.status === "complete") {
    try {
      const state = stateFor(tabId, tab.url || "");
      state.origin = originFromUrl(tab.url || ""); state.url = tab.url || "";
      allowedOrigins().then((allow) => { state.valid = allow.has(state.origin); });
    } catch (_) { states.delete(tabId); }
  }
});

chrome.tabs.onRemoved.addListener((tabId) => states.delete(tabId));

// Pairing is an explicit user gesture in the dedicated profile. activeTab is granted only for
// this click, avoiding a broad host permission while still allowing the content script to start.
chrome.action.onClicked.addListener(async (tab) => {
  if (!tab || !Number.isInteger(tab.id)) return;
  try {
    await chrome.scripting.executeScript({target: {tabId: tab.id}, files: ["content_script.js"]});
  } catch (_) {
    // Restricted browser pages fail closed and remain unpaired.
  }
});
