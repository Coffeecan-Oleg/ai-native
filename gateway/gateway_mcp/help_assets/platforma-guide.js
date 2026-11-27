/* Local-only intake: no HTTP submission, browser storage or external scripts. */
(() => {
  'use strict';
  const fields = Array.from(document.querySelectorAll('[data-card-answer]'));
  const output = document.getElementById('card-output');
  const copy = document.getElementById('card-copy');
  const download = document.getElementById('card-download');
  const status = document.getElementById('card-status');
  if (!output || !copy || !download || !status || fields.length !== 10) return;

  function update() {
    const answers = fields.map(field => {
      const question = field.closest('label').querySelector('span').textContent;
      return question + '\nОтвет: ' + (field.value.trim() || 'не указано');
    });
    output.value = output.dataset.intro + '\n\n' + answers.join('\n\n') + '\n\n' + output.dataset.footer;
    status.textContent = 'Изменения только в этом окне. Сохраните карточку перед обновлением или закрытием страницы.';
  }
  fields.forEach(field => field.addEventListener('input', update));
  copy.disabled = false;
  download.disabled = false;
  copy.addEventListener('click', async () => {
    try {
      if (!navigator.clipboard || !navigator.clipboard.writeText) throw new Error('clipboard unavailable');
      await navigator.clipboard.writeText(output.value);
      status.textContent = 'Карточка скопирована. Передайте её Олегу привычным рабочим способом.';
    } catch (_) {
      output.focus();
      output.select();
      status.textContent = 'Текст выделен. Нажмите Ctrl+C или выберите «Копировать» в меню устройства.';
    }
  });
  download.addEventListener('click', () => {
    const blob = new Blob(['\uFEFF', output.value], {type: 'text/plain;charset=utf-8'});
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = 'Карточка-подключения-сервиса.txt';
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    status.textContent = 'Скачивание карточки запрошено. Проверьте папку загрузок и передайте файл Олегу.';
  });
})();
