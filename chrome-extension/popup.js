let currentTab = null;
let apiUrl = '';
let apiSecret = '';
let apiUsername = '';
let apiPassword = '';

document.addEventListener('DOMContentLoaded', async () => {
  // Einstellungen laden
  const settings = await chrome.storage.sync.get(['apiUrl', 'apiSecret']);
  apiUrl = (settings.apiUrl || '').replace(/\/$/, '');
  apiSecret = settings.apiSecret || '';

  // Aktiven Tab ermitteln
  [currentTab] = await chrome.tabs.query({ active: true, currentWindow: true });

  // Domain als Label-Vorschlag
  let domain = '';
  try {
    const hostname = new URL(currentTab.url).hostname.replace(/^www\./, '');
    domain = hostname.split('.')[0];
    domain = domain.charAt(0).toUpperCase() + domain.slice(1);
    document.getElementById('site-domain').textContent = hostname;
  } catch {}
  document.getElementById('label').value = domain;

  // Einstellungen-Link immer registrieren
  document.getElementById('settings-link').addEventListener('click', (e) => {
    e.preventDefault();
    chrome.runtime.openOptionsPage();
  });

  // Einstellungen fehlen
  if (!apiUrl || !apiSecret) {
    showError('Bitte zuerst die Einstellungen konfigurieren.');
    return;
  }

  // E-Mail-Adressen laden
  try {
    const resp = await fetch(`${apiUrl}/api/addresses`, {
      headers: { 'x-api-secret': apiSecret }
    });
    if (resp.status === 403) throw new Error('API-Secret falsch — Einstellungen prüfen.');
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);

    const addresses = await resp.json();
    if (!addresses.length) {
      showError('Keine Adressen konfiguriert. Bitte in EmailRelay eine Adresse anlegen.');
      return;
    }

    const select = document.getElementById('address');
    addresses.forEach(({ address }) => {
      const opt = document.createElement('option');
      opt.value = address;
      opt.textContent = address;
      select.appendChild(opt);
    });

    document.getElementById('loading').style.display = 'none';
    document.getElementById('main').style.display = 'block';
  } catch (e) {
    showError(`Verbindungsfehler: ${e.message}`);
  }

  // Alias erstellen
  document.getElementById('create').addEventListener('click', createAlias);
  document.getElementById('new-alias').addEventListener('click', () => {
    document.getElementById('result').style.display = 'none';
    document.getElementById('main').style.display = 'block';
  });

  // Kopieren
  document.getElementById('copy').addEventListener('click', async () => {
    const alias = document.getElementById('alias-display').textContent;
    await navigator.clipboard.writeText(alias);
    document.getElementById('copy').textContent = '✓ Kopiert!';
    setTimeout(() => { document.getElementById('copy').textContent = 'Nochmal kopieren'; }, 1500);
  });

});

async function createAlias() {
  const realAddress = document.getElementById('address').value;
  const label = document.getElementById('label').value.trim();
  const btn = document.getElementById('create');

  btn.disabled = true;
  btn.textContent = 'Erstelle…';

  try {
    const resp = await fetch(`${apiUrl}/api/alias/create`, {
      method: 'POST',
      headers: {
        'x-api-secret': apiSecret,
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({ real_address: realAddress, label })
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const { alias_address } = await resp.json();

    // Alias in Zwischenablage
    await navigator.clipboard.writeText(alias_address);

    // Zuletzt fokussiertes Feld auf der Seite befüllen (via Content Script)
    try {
      await chrome.tabs.sendMessage(currentTab.id, { type: 'fill', value: alias_address });
    } catch {}

    // Ergebnis anzeigen
    document.getElementById('main').style.display = 'none';
    document.getElementById('alias-display').textContent = alias_address;
    document.getElementById('result').style.display = 'block';

  } catch (e) {
    showError(`Fehler beim Erstellen: ${e.message}`);
    btn.disabled = false;
    btn.textContent = 'Alias erstellen';
  }
}

function showError(msg) {
  document.getElementById('loading').style.display = 'none';
  const el = document.getElementById('error');
  el.textContent = msg;
  el.style.display = 'block';
}
