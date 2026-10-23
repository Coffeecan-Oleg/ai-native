# Примеры для поиска в репозиториях

Это стартовые кандидаты из документа об осведомлённости, а не универсальные зависимости. Проверено по документации 14.10.2026. Перед установкой перечитай актуальный код, лицензию и требования.

| Потребность | Где искать | Что проверять |
|---|---|---|
| Обнаружение изменений и актуализация индекса документов | https://github.com/onyx-dot-app/onyx/tree/main/backend/onyx/connectors | Poll/Checkpointed connectors, фоновые задания, refresh/prune, Google Docs/Sheets/PDF |
| Каталог метаданных систем | https://github.com/datahub-project/datahub/tree/master/metadata-ingestion и https://github.com/open-metadata/OpenMetadata/tree/main/ingestion | Коннекторы нужных источников, stateful ingestion, расписание, запись метаданных |
| Поиск по документам с первичными ссылками | https://github.com/onyx-dot-app/onyx и https://github.com/infiniflow/ragflow | Индексация, API поиска, ссылки на источник, фильтры доступа |
| Проверка доступа сотрудника к ресурсу | https://github.com/openfga/openfga | Модель отношений, check, пользовательские группы, применение решения в Gateway |
| Сохранение и поиск решений с историей | https://github.com/datahub-project/datahub и https://github.com/gura105/operational-ontology | Найти и подтвердить реальную запись/чтение/версионирование; наличие каталога не доказывает корпоративную память |

Onyx: https://docs.onyx.app/admins/connectors/overview
Google Drive форматы: https://docs.onyx.app/admins/connectors/official/google_drive/overview
У Onyx обновление и удаление имеют разные интервалы. Auto Sync Permissions — Enterprise Edition; для Google Drive нужны service account или Workspace Admin OAuth. Не считать обычную OAuth-индексацию переносом всех прав.
DataHub Google Drive: https://docs.datahub.com/docs/generated/ingestion/sources/google_drive — поддерживает Google Docs, не Sheets/PDF; Google ACL не зеркалируются.
OpenMetadata ingestion: https://docs.open-metadata.org/v2.0.x/deployment/ingestion

Остальные ссылки исходного документа: syzygyhack/open-foundry, typedb/typedb, OpenCTI-Platform/opencti, temporalio/temporal, langflow-ai/langflow, langgenius/dify, FlowiseAI/Flowise/discussions/6727, ushahidi/platform, compdemocracy/polis. Ссылка на обсуждение может описывать пожелание, а не существующий код.
