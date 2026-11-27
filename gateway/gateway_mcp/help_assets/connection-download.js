/* Fetch in the authenticated page context; never navigate a frame to OAuth. */
(() => {
  'use strict';
  const prepare = document.getElementById('connection-report-prepare');
  const download = document.getElementById('connection-report-download');
  const status = document.getElementById('connection-report-status');
  const preview = document.getElementById('connection-report-preview');
  const reportText = document.getElementById('connection-report-text');
  if (!prepare || !download || !status) return;
  const MAX_BYTES = 65536;
  let objectURL = null;
  let expiry = null;
  let active = null;

  function clearFile() {
    if (objectURL) URL.revokeObjectURL(objectURL);
    objectURL = null;
    clearTimeout(expiry);
    download.hidden = true;
    download.removeAttribute('href');
    if (preview) {preview.hidden = true; preview.open = false;}
    if (reportText) reportText.value = '';
  }

  function messageFor(code) {
    if (code === 401 || code === 403) return 'Сессия завершена или доступ отозван. Откройте Платформу заново из меню Битрикс24 и повторите проверку.';
    if (code === 400) return 'Метка проверки устарела или недействительна. Нажмите «Начать новую проверку».';
    if (code === 503) return 'Журнал проверок временно недоступен. Попробуйте подготовить файл позже.';
    return 'Не удалось подготовить файл. Повторите попытку. Если ошибка сохраняется, откройте Платформу заново из меню Битрикс24.';
  }

  prepare.addEventListener('click', async () => {
    if (active) return;
    clearFile();
    const controller = new AbortController();
    active = controller;
    prepare.disabled = true;
    prepare.setAttribute('aria-busy', 'true');
    status.textContent = 'Подготавливаем файл…';
    const timeout = setTimeout(() => controller.abort(), 15000);
    let errorCode = 0;
    try {
      const url = new URL(prepare.dataset.reportUrl, window.location.href);
      if (url.origin !== window.location.origin || url.pathname !== '/platforma/connection/report') throw new Error('Invalid report URL');
      const response = await fetch(url.href, {
        credentials: 'same-origin', mode: 'same-origin', redirect: 'error',
        cache: 'no-store', referrerPolicy: 'no-referrer', signal: controller.signal
      });
      errorCode = response.status;
      if (!response.ok || response.redirected ||
          response.headers.get('Content-Type')?.split(';')[0].trim().toLowerCase() !== 'text/plain' ||
          response.headers.get('Content-Disposition') !== 'attachment; filename="platforma-connection.txt"') throw new Error('Invalid report response');
      const reader = response.body.getReader();
      const chunks = [];
      let size = 0;
      try {
        while (true) {
          const {done, value} = await reader.read();
          if (done) break;
          size += value.byteLength;
          if (size > MAX_BYTES) throw new Error('Report too large');
          chunks.push(value);
        }
      } catch (error) {
        await reader.cancel();
        throw error;
      } finally {
        reader.releaseLock();
      }
      if (!size || controller.signal.aborted || active !== controller) throw new Error('Report unavailable');
      const blob = new Blob(chunks, {type: 'text/plain;charset=utf-8'});
      const text = await blob.text();
      if (controller.signal.aborted || active !== controller) throw new Error('Report unavailable');
      objectURL = URL.createObjectURL(blob);
      download.href = objectURL;
      download.hidden = false;
      if (reportText && preview) {reportText.value = text; preview.hidden = false;}
      status.textContent = 'Файл подготовлен. Нажмите «Скачать готовый файл (.txt)» или откройте «Показать текст отчёта». Файл и текст доступны 5 минут в этом окне; файл никому не отправлен.';
      download.focus();
      expiry = setTimeout(() => {
        clearFile();
        status.textContent = 'Ссылка скачивания и текст закрыты. При необходимости подготовьте файл заново.';
      }, 300000);
    } catch (_) {
      if (active === controller) {
        clearFile();
        status.textContent = messageFor(errorCode);
      }
    } finally {
      clearTimeout(timeout);
      if (active === controller) {
        active = null;
        prepare.disabled = false;
        prepare.removeAttribute('aria-busy');
      }
    }
  });

  window.addEventListener('pagehide', () => {
    if (active) active.abort();
    active = null;
    clearFile();
    prepare.disabled = false;
    prepare.removeAttribute('aria-busy');
    status.textContent = 'Подготовьте файл заново в этом окне.';
  });
})();
