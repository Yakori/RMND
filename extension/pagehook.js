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
