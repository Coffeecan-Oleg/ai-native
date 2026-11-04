/* Frame transport only: never read OAuth data, call REST or send user content. */
(() => {
  'use strict';
  const status = document.getElementById('bitrix-frame-status');
  const problem = document.getElementById('bitrix-frame-problem');
  if (!status || !problem) return;
  let sdkReady = false;
  let sessionReady = false;
  function fail(state, text) {
    status.dataset.state = state;
    status.textContent = 'Не удалось завершить открытие Платформы.';
    problem.textContent = text;
    problem.hidden = false;
  }
  function ready() {
    if (sdkReady && sessionReady) {
      status.dataset.state = 'ready';
      status.textContent = 'Платформа открыта. Встроенная сессия подтверждена.';
      problem.hidden = true;
    }
  }
  const timer = window.setTimeout(() => {
    if (!sdkReady) fail('sdk-timeout', 'Битрикс24 не подтвердил связь с приложением. Закройте приложение и откройте заново. Если ошибка повторяется, сообщите администратору время проверки.');
  }, 10000);
  const sdk = document.createElement('script');
  // Pinned official SDK ships with Gateway; browsers need no separate CDN access.
  sdk.src = '/auth/bitrix24/frame-sdk.js';
  sdk.async = true;
  sdk.onerror = () => {
    window.clearTimeout(timer);
    fail('sdk-unavailable', 'Не загрузился компонент связи с Битрикс24. Повторите открытие приложения или обратитесь к администратору.');
  };
  sdk.onload = () => {
    try {
      if (!window.BX24 || typeof window.BX24.init !== 'function') throw new Error('sdk-unavailable');
      window.BX24.init(() => {
        sdkReady = true;
        window.clearTimeout(timer);
        ready();
        try {
          if (typeof window.BX24.fitWindow === 'function') window.BX24.fitWindow();
        } catch (_) { /* A resize failure does not revoke a verified session. */ }
      });
    } catch (_) {
      window.clearTimeout(timer);
      fail('sdk-unavailable', 'Не удалось установить связь с окном Битрикс24. Откройте Платформу из меню приложений портала.');
    }
  };
  document.head.appendChild(sdk);
  const controller = new AbortController();
  const sessionTimer = window.setTimeout(() => controller.abort(), 7000);
  fetch('/auth/bitrix24/frame-session', {credentials: 'same-origin', cache: 'no-store', signal: controller.signal})
    .then(response => {
      if (!response.ok) throw new Error('session-unavailable');
      return response.json();
    })
    .then(result => {
      if (result.ok !== true) throw new Error('session-unavailable');
      sessionReady = true;
      ready();
    })
    .catch(() => fail('session-unavailable', 'Браузер не подтвердил встроенную сессию. Закройте приложение и откройте заново. Если ошибка сохраняется, передайте её администратору; повторно вводить ключи не нужно.'))
    .finally(() => window.clearTimeout(sessionTimer));
})();
