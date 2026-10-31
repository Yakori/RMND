const TOKEN_MSG = "faceit-panel-token";
let headerSeen = false;

function validJwt(token) {
  return typeof token === "string"
    && token.startsWith("eyJ")
    && token.length >= 80
    && token.length < 8000
    && /^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/.test(token);
}

function remember(token, via) {
  if (!validJwt(token)) return;
  if (via !== "header" && headerSeen) return;
  if (via === "header") headerSeen = true;
  chrome.storage.local.set({
    faceit_token: token,
    faceit_token_at: Date.now()
  });
}

function extractToken(obj, depth) {
  if (depth > 5 || obj == null) return null;
  if (typeof obj === "string") return validJwt(obj) ? obj : null;
  if (typeof obj !== "object") return null;
  const preferred = ["access_token", "accessToken", "token", "bearer", "jwt", "id_token"];
  for (const key of preferred) {
    if (typeof obj[key] === "string" && validJwt(obj[key])) return obj[key];
  }
  for (const key of Object.keys(obj)) {
    const found = extractToken(obj[key], depth + 1);
    if (found) return found;
  }
  return null;
}

function scanStore(store) {
  let fallback = null;
  const keys = Object.keys(store);
  const preferred = keys.filter(key => /access|token|bearer|jwt/i.test(key));
  for (const key of preferred.concat(keys)) {
    try {
      const val = store.getItem(key);
      if (!val) continue;
      if (validJwt(val)) return val;
      if (val.startsWith("{") || val.startsWith("[")) {
        const found = extractToken(JSON.parse(val), 0);
        if (found && preferred.includes(key)) return found;
        if (found && !fallback) fallback = found;
      }
    } catch (e) {}
  }
  return fallback;
}

function scanPage() {
  const token = scanStore(localStorage) || scanStore(sessionStorage);
  if (token) remember(token, "storage");
}

window.addEventListener("message", (ev) => {
  if (ev.source !== window || ev.origin !== location.origin) return;
  const data = ev.data;
  if (!data || data.source !== TOKEN_MSG) return;
  remember(data.token, data.via === "header" ? "header" : "storage");
});

scanPage();
setTimeout(scanPage, 2000);
setTimeout(scanPage, 8000);
