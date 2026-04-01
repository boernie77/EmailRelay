document.addEventListener("DOMContentLoaded", async () => {
  const { apiUrl, apiSecret, username, password } =
    await chrome.storage.sync.get([
      "apiUrl",
      "apiSecret",
      "username",
      "password",
    ]);
  document.getElementById("api-url").value =
    apiUrl || "https://api.byboernie.de";
  document.getElementById("api-secret").value = apiSecret || "";
  document.getElementById("username").value = username || "";
  document.getElementById("password").value = password || "";
});

document.getElementById("save").addEventListener("click", async () => {
  const apiUrl = document
    .getElementById("api-url")
    .value.trim()
    .replace(/\/$/, "");
  const apiSecret = document.getElementById("api-secret").value.trim();
  const username = document.getElementById("username").value.trim();
  const password = document.getElementById("password").value.trim();
  await chrome.storage.sync.set({ apiUrl, apiSecret, username, password });
  setStatus("Gespeichert ✓", "ok");
});

document.getElementById("test").addEventListener("click", async () => {
  const apiUrl = document
    .getElementById("api-url")
    .value.trim()
    .replace(/\/$/, "");
  const apiSecret = document.getElementById("api-secret").value.trim();
  setStatus("Teste…", "");
  try {
    const username = document.getElementById("username").value.trim();
    const password = document.getElementById("password").value.trim();
    const resp = await fetch(`${apiUrl}/api/addresses`, {
      headers: {
        "x-api-secret": apiSecret,
        "x-username": username,
        "x-password": password,
      },
    });
    if (resp.status === 403) {
      setStatus("✗ Falsches API-Secret", "err");
      return;
    }
    if (!resp.ok) {
      setStatus(`✗ HTTP ${resp.status}`, "err");
      return;
    }
    const data = await resp.json();
    setStatus(`✓ Verbindung OK — ${data.length} Adresse(n)`, "ok");
  } catch (e) {
    setStatus(`✗ ${e.message}`, "err");
  }
});

document.getElementById("toggle-secret").addEventListener("click", () => {
  const input = document.getElementById("api-secret");
  input.type = input.type === "password" ? "text" : "password";
});

function setStatus(msg, cls) {
  const el = document.getElementById("status");
  el.textContent = msg;
  el.className = cls;
}
