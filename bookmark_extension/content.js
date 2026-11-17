(() => {
  const APP_ORIGIN = "http://127.0.0.1:8765";
  const REQUEST_CHANNEL = "coingecko-archive-bookmarks-request";
  const RESPONSE_CHANNEL = "coingecko-archive-bookmarks-response";

  window.addEventListener("message", (event) => {
    if (
      event.source !== window ||
      event.origin !== APP_ORIGIN ||
      event.data?.channel !== REQUEST_CHANNEL
    ) {
      return;
    }
    const request = event.data;
    chrome.runtime.sendMessage(request, (response) => {
      const error = chrome.runtime.lastError?.message || response?.error;
      window.postMessage(
        {
          channel: RESPONSE_CHANNEL,
          requestId: request.requestId,
          result: response?.result,
          error,
        },
        APP_ORIGIN
      );
    });
  });
})();
