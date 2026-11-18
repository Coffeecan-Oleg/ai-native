# Где искать код Платформы

Основной форк: https://github.com/Coffeecan-Oleg/ai-native. Рабочая ветка и её последний коммит не доказывают, какой образ установлен: сверяйте выпуск с health, образом и отчётом приёмки.

| Потребность | Каталог исходников |
|---|---|
| Корпоративная личность и доступ | gateway/gateway_mcp/services/bitrix_identity.py и corporate_access.py |
| Личная диагностика MCP | gateway/gateway_mcp/services/connection_diagnostics.py и routes/connection_diagnostics.py |
| Общая библиотека | gateway/gateway_mcp/services/skill_library.py |
| Черновики, версии и архив | gateway/gateway_mcp/services/skill_lifecycle.py и routes/skill_lifecycle.py |
| Схема хранения | gateway/migrations |
| Воспроизводимая сборка и проверки | gateway/Dockerfile, gateway/locks, gateway/release |
| Инструкции для БЗ 2.0 | gateway/docs/knowledge-base |

Код и зависимости сначала проверяются на копии. Не выполняйте команды из репозитория без проверки назначения, лицензии, разрешённых данных и эксплуатационных последствий. Эта справка не устанавливает внешние приложения и не выдаёт новые права.
