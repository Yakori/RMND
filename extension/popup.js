const dot        = document.getElementById("dot");
const statusText = document.getElementById("status-text");
const tokenAge   = document.getElementById("token-age");
const btnSend    = document.getElementById("btn-send");
const btnOpen    = document.getElementById("btn-open-faceit");
const msg        = document.getElementById("msg");
const urlInput   = document.getElementById("panel-url");

function fmtAge(ms) {
  const sec = Math.floor((Date.now() - ms) / 1000);
  if (sec < 60)  return `обновлён ${sec} сек назад`;
  if (sec < 3600) return `обновлён ${Math.floor(sec/60)} мин назад`;
  return `обновлён ${Math.floor(sec/3600)} ч назад`;
}

// Загружаем сохранённый URL и токен
chrome.storage.local.get(["faceit_token", "faceit_token_at", "panel_url"], (data) => {
  if (data.panel_url) urlInput.value = data.panel_url;

  if (data.faceit_token) {
    dot.className = "dot dot-ok";
    statusText.textContent = "Токен найден ✓";
    tokenAge.textContent = data.faceit_token_at ? fmtAge(data.faceit_token_at) : "";
    btnSend.disabled = false;
  } else {
    dot.className = "dot dot-no";
    statusText.textContent = "Токен не найден";
    tokenAge.textContent = "Зайди на faceit.com чтобы получить токен";
    btnSend.disabled = true;
  }
});

// Сохраняем URL при изменении
urlInput.addEventListener("input", () => {
  chrome.storage.local.set({ panel_url: urlInput.value.trim() });
});

// Кнопка: открыть faceit.com
btnOpen.addEventListener("click", () => {
  chrome.tabs.create({ url: "https://www.faceit.com" });
});

// Кнопка: отправить токен в панель
btnSend.addEventListener("click", async () => {
  const panelUrl = urlInput.value.trim().replace(/\/$/, "");
  if (!panelUrl) {
    showMsg("Укажи адрес панели", "err");
    return;
  }

  chrome.storage.local.set({ panel_url: panelUrl });

  chrome.storage.local.get(["faceit_token"], async (data) => {
    if (!data.faceit_token) {
      showMsg("Токен не найден — зайди на faceit.com", "err");
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
        showMsg("Токен успешно отправлен ✓", "ok");
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
});

function showMsg(text, type) {
  msg.textContent = text;
  msg.className = "msg" + (type ? ` msg-${type}` : "");
}
