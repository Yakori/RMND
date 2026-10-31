// Runs in the page world so it sees FACEIT's own fetch/XHR.
(function () {
  const SOURCE = "faceit-panel-token";

  function publish(token) {
    if (typeof token !== "string") return;
    const value = token.trim();
    if (!value.startsWith("eyJ") || value.length < 80) return;
    window.postMessage({ source: SOURCE, token: value, via: "header" }, window.location.origin);
  }

  function readAuth(input, init) {
    let headers = null;
    if (init && init.headers) headers = init.headers;
    else if (typeof Request !== "undefined" && input instanceof Request) headers = input.headers;
    if (!headers) return;
    let auth = null;
    if (typeof Headers !== "undefined" && headers instanceof Headers) {
      auth = headers.get("authorization");
    } else if (Array.isArray(headers)) {
      for (const pair of headers) {
        if (pair && String(pair[0]).toLowerCase() === "authorization") auth = pair[1];
      }
    } else if (typeof headers === "object") {
      auth = headers.authorization || headers.Authorization;
    }
    if (typeof auth !== "string") return;
    const match = /^bearer\s+(.+)$/i.exec(auth.trim());
    if (match) publish(match[1]);
  }

  const origFetch = window.fetch;
  if (typeof origFetch === "function") {
    window.fetch = function () {
      try { readAuth(arguments[0], arguments[1]); } catch (e) {}
      return origFetch.apply(this, arguments);
    };
  }

  const origSetHeader = XMLHttpRequest.prototype.setRequestHeader;
  XMLHttpRequest.prototype.setRequestHeader = function (name, value) {
    try {
      if (String(name).toLowerCase() === "authorization") {
        const match = /^bearer\s+(.+)$/i.exec(String(value).trim());
        if (match) publish(match[1]);
      }
    } catch (e) {}
    return origSetHeader.apply(this, arguments);
  };
})();

// Профиль открывается с #panel-add. Заявку отправляет сама страница faceit.com,
// в сессии, которая уже есть в этом браузере.
(function () {
  if (location.hash !== "#panel-add") return;
  const nickMatch = location.pathname.match(/\/players\/([^/]+)/);
  if (!nickMatch) return;
  const nick = decodeURIComponent(nickMatch[1]);
  const ADD_LABEL = /^(add friend|добавить в друзья|добавить друга)$/i;

  function note(text) {
    const parent = document.body || document.documentElement;
    if (!parent) return;
    let el = document.getElementById("fp-add-note");
    if (!el) {
      el = document.createElement("div");
      el.id = "fp-add-note";
      el.style.cssText = "position:fixed;z-index:2147483647;right:16px;bottom:16px;background:#11141a;color:#eaeef2;border:1px solid #5f7a96;border-radius:12px;padding:12px 14px;font:14px/1.4 Segoe UI,sans-serif;max-width:300px";
      parent.appendChild(el);
    }
    el.textContent = text;
  }

  function clickAdd() {
    const nodes = document.querySelectorAll("button, a, [role='button']");
    for (const el of nodes) {
      const text = (el.innerText || el.getAttribute("aria-label") || "").replace(/\s+/g, " ").trim();
      if (!ADD_LABEL.test(text)) continue;
      el.click();
      return true;
    }
    return false;
  }

  function finish(text) {
    history.replaceState(null, "", location.pathname + location.search);
    note(text);
  }

  async function sendOnce() {
    const playerRes = await fetch(
      "https://api.faceit.com/users/v1/nicknames/" + encodeURIComponent(nick),
      { credentials: "include" }
    );
    const player = await playerRes.json();
    const targetId = player && player.payload && player.payload.id;
    if (!targetId) return "нет такого игрока";

    const meRes = await fetch("https://api.faceit.com/users/v1/sessions/me", { credentials: "include" });
    if (!meRes.ok) return "войди на faceit.com в этом браузере";
    const me = await meRes.json();
    const myId = (me.payload && (me.payload.id || me.payload.guid)) || me.id || me.guid;
    if (!myId) return "войди на faceit.com в этом браузере";

    const res = await fetch(
      "https://api.faceit.com/friend-requests/v1/users/" + encodeURIComponent(myId) + "/requests",
      {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ users: [targetId], conversionPoint: "Profile" })
      }
    );
    if (res.ok) return "";
    let detail = "";
    try {
      const body = await res.json();
      detail = (body && (body.message || (body.errors && body.errors[0] && body.errors[0].message))) || "";
    } catch (e) {}
    return detail || ("ошибка " + res.status);
  }

  window.setTimeout(async function () {
    note("Отправляю заявку…");
    let reason = "";
    try {
      reason = await sendOnce();
    } catch (e) {
      reason = "не удалось отправить";
    }
    if (!reason) {
      finish("Заявка отправлена");
      return;
    }
    const started = Date.now();
    const timer = window.setInterval(function () {
      if (clickAdd()) {
        window.clearInterval(timer);
        finish("Заявка отправлена");
        return;
      }
      if (Date.now() - started > 12000) {
        window.clearInterval(timer);
        note(reason);
      }
    }, 400);
  }, 1500);
})();
