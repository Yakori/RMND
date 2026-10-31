const dot        = document.getElementById("dot");
const statusText = document.getElementById("status-text");
const tokenAge   = document.getElementById("token-age");
const btnSend    = document.getElementById("btn-send");
const btnOpen    = document.getElementById("btn-open-faceit");
const btnFind    = document.getElementById("btn-find");
const msg        = document.getElementById("msg");
const urlInput   = document.getElementById("panel-url");

function fmtAge(ms) {
  const sec = Math.floor((Date.now() - ms) / 1000);
  if (sec < 60)   return `обновлён ${sec} сек назад`;
  if (sec < 3600) return `обновлён ${Math.floor(sec / 60)} мин назад`;
  return `обновлён ${Math.floor(sec / 3600)} ч назад`;
}

function updateUI(data) {
  if (data.panel_url) urlInput.value = data.panel_url;

  if (data.faceit_token) {
    dot.className = "dot dot-ok";
    statusText.textContent = "Токен найден ✓";
    tokenAge.textContent = data.faceit_token_at ? fmtAge(data.faceit_token_at) : "";
    btnSend.disabled = false;
  } else {
    dot.className = "dot dot-no";
    statusText.textContent = "Токен не найден";
    tokenAge.textContent = "Нажми «Найти токен» или зайди на faceit.com";
    btnSend.disabled = true;
  }
}

// Загружаем данные при открытии
chrome.storage.local.get(["faceit_token", "faceit_token_at", "panel_url"], updateUI);

// Сохраняем URL при изменении
urlInput.addEventListener("input", () => {
  chrome.storage.local.set({ panel_url: urlInput.value.trim() });
});

// Кнопка: открыть faceit.com
btnOpen.addEventListener("click", () => {
  chrome.tabs.create({ url: "https://www.faceit.com" });
  window.close();
});

// Кнопка: найти токен на текущей вкладке faceit.com
btnFind.addEventListener("click", async () => {
  showMsg("Ищу токен...", "");

  // Находим вкладку с faceit.com
  const tabs = await chrome.tabs.query({ url: "https://www.faceit.com/*" });

  if (tabs.length === 0) {
    showMsg("Открой faceit.com и попробуй снова", "err");
    return;
  }

  const tab = tabs[0];

  // Запускаем скрипт поиска токена на вкладке
  try {
    const results = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: () => {
        function extractToken(obj, depth) {
          if (depth > 5) return null;
          if (typeof obj === "string" && obj.startsWith("eyJ") && obj.length > 100) return obj;
          if (typeof obj !== "object" || obj === null) return null;
          const keys = ["token", "access_token", "accessToken", "bearer", "id_token", "jwt"];
          for (const k of keys) {
            if (obj[k] && typeof obj[k] === "string" && obj[k].startsWith("eyJ")) return obj[k];
          }
          for (const k of Object.keys(obj)) {
            const r = extractToken(obj[k], depth + 1);
            if (r) return r;
          }
          return null;
        }

        // localStorage
        for (const key of Object.keys(localStorage)) {
          try {
            const val = localStorage.getItem(key);
            if (!val) continue;
            if (val.startsWith("eyJ") && val.length > 100) return val;
            if (val.startsWith("{") || val.startsWith("[")) {
              const t = extractToken(JSON.parse(val), 0);
              if (t) return t;
            }
          } catch (e) {}
        }

        // sessionStorage
        for (const key of Object.keys(sessionStorage)) {
          try {
            const val = sessionStorage.getItem(key);
            if (!val) continue;
            if (val.startsWith("eyJ") && val.length > 100) return val;
            if (val.startsWith("{")) {
              const t = extractToken(JSON.parse(val), 0);
              if (t) return t;
            }
          } catch (e) {}
        }

        return null;
      }
    });

    const token = results[0]?.result;
    if (token) {
      await chrome.storage.local.set({ faceit_token: token, faceit_token_at: Date.now() });
      showMsg("Токен найден ✓", "ok");
      chrome.storage.local.get(["faceit_token", "faceit_token_at", "panel_url"], updateUI);
    } else {
      showMsg("Токен не найден в storage. Попробуй обновить faceit.com (F5)", "err");
    }
  } catch (e) {
    showMsg("Ошибка: " + e.message, "err");
  }
});

// Кнопка: отправить токен в панель
btnSend.addEventListener("click", async () => {
  const panelUrl = urlInput.value.trim().replace(/\/$/, "");
  if (!panelUrl) {
    showMsg("Укажи адрес панели", "err");
    return;
  }
  chrome.storage.local.set({ panel_url: panelUrl });

  const data = await chrome.storage.local.get(["faceit_token"]);
  if (!data.faceit_token) {
    showMsg("Токен не найден", "err");
    return;
  }

  btnSend.disabled = true;
  showMsg("Отправляю...", "");

  try {
    const resp = await fetch(`${panelUrl}/api/token`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: data.faceit_token }),
      credentials: "include",
    });

    if (resp.ok) {
      showMsg("Токен успешно отправлен в панель ✓", "ok");
    } else {
      const err = await resp.json().catch(() => ({}));
      showMsg(err.error || `Ошибка ${resp.status}`, "err");
    }
  } catch (e) {
    showMsg("Не удалось подключиться к панели", "err");
  } finally {
    btnSend.disabled = false;
  }
});

function showMsg(text, type) {
  msg.textContent = text;
  msg.className = "msg" + (type ? ` msg-${type}` : "");
}
