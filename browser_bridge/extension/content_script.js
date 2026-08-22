"use strict";

const MAX_TEXT = 100000;
const OPERATIONS = new Set(["inspect", "extract", "click", "type", "select", "scroll", "submit"]);
let documentId = null;
let origin = null;

function currentOrigin() { return new URL(location.href).origin.toLowerCase(); }
function identity(request) {
  if (documentId === null || request.document_id !== documentId || request.origin !== currentOrigin()) throw new Error("document identity changed");
}
function elementFor(target) {
  if (!target) throw new Error("target required");
  let element;
  try { element = document.querySelector(target); } catch (_) { throw new Error("invalid selector"); }
  if (!element || !element.isConnected) throw new Error("target not found");
  return element;
}
function visibleText() { return String(document.body ? document.body.innerText : "").slice(0, MAX_TEXT); }

function apply(request) {
  identity(request);
  if (!OPERATIONS.has(request.operation)) throw new Error("unsupported operation");
  if (request.operation === "inspect" || request.operation === "extract") {
    return {tab_id: String(request.tab_id), origin: currentOrigin(), document_id: documentId, title: document.title.slice(0, 1000), url: location.href.slice(0, 8192), visible_text: visibleText()};
  }
  if (request.operation === "scroll") {
    const element = request.target ? elementFor(request.target) : document.documentElement;
    const amount = Number(request.value);
    if (!Number.isFinite(amount) || Math.abs(amount) > 100000) throw new Error("invalid scroll amount");
    element.scrollBy ? element.scrollBy({top: amount, behavior: "instant"}) : window.scrollBy(0, amount);
  } else if (request.operation === "click" || request.operation === "submit") {
    elementFor(request.target).click();
  } else if (request.operation === "type") {
    const element = elementFor(request.target);
    if (!(element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement || element.isContentEditable)) throw new Error("target is not editable");
    if (element instanceof HTMLInputElement && element.type.toLowerCase() === "file") throw new Error("file inputs are unavailable");
    if (element.isContentEditable) element.textContent = request.value;
    else element.value = request.value;
    element.dispatchEvent(new Event("input", {bubbles: true})); element.dispatchEvent(new Event("change", {bubbles: true}));
  } else if (request.operation === "select") {
    const element = elementFor(request.target);
    if (!(element instanceof HTMLSelectElement)) throw new Error("target is not a select");
    element.value = request.value; element.dispatchEvent(new Event("change", {bubbles: true}));
  }
  return {tab_id: String(request.tab_id), origin: currentOrigin(), document_id: documentId, ok: true};
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!message || message.kind !== "atlas.content") return false;
  try { sendResponse(apply(message)); } catch (_) { sendResponse({ok: false, error: "content operation rejected"}); }
  return false;
});

chrome.runtime.sendMessage({kind: "atlas.hello"}).then((response) => {
  if (response && response.ok) { documentId = response.document_id; origin = response.origin; }
}).catch(() => { documentId = null; origin = null; });
