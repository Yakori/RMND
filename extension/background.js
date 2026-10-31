// Перехватываем все запросы к faceit.com и ищем Bearer токен
chrome.webRequest.onBeforeSendHeaders.addListener(
  (details) => {
    if (!details.requestHeaders) return;
    for (const header of details.requestHeaders) {
      if (header.name.toLowerCase() === "authorization") {
        const val = header.value || "";
        if (val.startsWith("Bearer ") && val.length > 50) {
          const token = val.slice(7); // убираем "Bearer "
          // Сохраняем токен в storage
          chrome.storage.local.set({ 
            faceit_token: token,
            faceit_token_at: Date.now()
          });
        }
      }
    }
  },
  { urls: ["https://*.faceit.com/*", "https://api.faceit.com/*"] },
  ["requestHeaders"]
);
