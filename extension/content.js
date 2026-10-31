// Ищем токен в localStorage и cookies
function findToken() {
  // 1. Ищем в localStorage по разным ключам
  const keys = Object.keys(localStorage);
  for (const key of keys) {
    try {
      const val = localStorage.getItem(key);
      if (!val) continue;

      // Если значение само по себе JWT токен
      if (val.startsWith("eyJ") && val.length > 100) {
        return val;
      }

      // Если значение JSON — ищем внутри
      if (val.startsWith("{") || val.startsWith("[")) {
        const parsed = JSON.parse(val);
        const token = extractToken(parsed);
        if (token) return token;
      }
    } catch (e) {}
  }

  // 2. Ищем в sessionStorage
  const skeys = Object.keys(sessionStorage);
  for (const key of skeys) {
    try {
      const val = sessionStorage.getItem(key);
      if (!val) continue;
      if (val.startsWith("eyJ") && val.length > 100) return val;
      if (val.startsWith("{")) {
        const parsed = JSON.parse(val);
        const token = extractToken(parsed);
        if (token) return token;
      }
    } catch (e) {}
  }

  return null;
}

function extractToken(obj, depth = 0) {
  if (depth > 5) return null;
  if (typeof obj === "string" && obj.startsWith("eyJ") && obj.length > 100) return obj;
  if (typeof obj !== "object" || obj === null) return null;

  // Проверяем типичные ключи
  const tokenKeys = ["token", "access_token", "accessToken", "bearer", "id_token", "jwt"];
  for (const k of tokenKeys) {
    if (obj[k] && typeof obj[k] === "string" && obj[k].startsWith("eyJ")) {
      return obj[k];
    }
  }

  // Рекурсивно по всем ключам
  for (const k of Object.keys(obj)) {
    const result = extractToken(obj[k], depth + 1);
    if (result) return result;
  }
  return null;
}

// Перехватываем XMLHttpRequest чтобы поймать Authorization заголовок
const origOpen = XMLHttpRequest.prototype.open;
const origSetHeader = XMLHttpRequest.prototype.setRequestHeader;

XMLHttpRequest.prototype.setRequestHeader = function(name, value) {
  if (name.toLowerCase() === "authorization" && value.startsWith("Bearer ")) {
    const token = value.slice(7);
    if (token.length > 50) {
      chrome.storage.local.set({
        faceit_token: token,
        faceit_token_at: Date.now()
      });
    }
  }
  return origSetHeader.apply(this, arguments);
};

// Перехватываем fetch
const origFetch = window.fetch;
window.fetch = function(...args) {
  const [input, init] = args;
  if (init && init.headers) {
    const headers = init.headers;
    let auth = null;
    if (headers instanceof Headers) {
      auth = headers.get("authorization") || headers.get("Authorization");
    } else if (typeof headers === "object") {
      auth = headers["authorization"] || headers["Authorization"];
    }
    if (auth && auth.startsWith("Bearer ") && auth.length > 57) {
      chrome.storage.local.set({
        faceit_token: auth.slice(7),
        faceit_token_at: Date.now()
      });
    }
  }
  return origFetch.apply(this, arguments);
};

// Сразу проверяем localStorage при загрузке
const token = findToken();
if (token) {
  chrome.storage.local.set({
    faceit_token: token,
    faceit_token_at: Date.now()
  });
}
