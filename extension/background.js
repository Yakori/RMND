// Слушаем сообщения от content script
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === "token" && msg.token) {
    chrome.storage.local.set({
      faceit_token: msg.token,
      faceit_token_at: Date.now()
    });
  }
});
