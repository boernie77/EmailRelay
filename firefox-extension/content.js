// Merkt sich das zuletzt fokussierte Eingabefeld
let lastFocusedInput = null;

document.addEventListener(
  "focusin",
  (e) => {
    const el = e.target;
    if (
      el &&
      el.tagName === "INPUT" &&
      el.type !== "password" &&
      el.type !== "hidden"
    ) {
      lastFocusedInput = el;
    }
  },
  true
);

// Befüllt das zuletzt fokussierte Feld auf Anfrage des Popups
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.type === "fill") {
    const target = lastFocusedInput;
    if (target && document.body.contains(target)) {
      target.value = msg.value;
      target.dispatchEvent(new Event("input", { bubbles: true }));
      target.dispatchEvent(new Event("change", { bubbles: true }));
      target.focus();
      sendResponse({ ok: true });
    } else {
      sendResponse({ ok: false });
    }
  }
});
