# Одна разовая привязка инфраструктуры

Это пакет подготовки. Применение на production и изменение общего Caddy выполняются
только после отдельного явного согласования владельца. Разработка и проверки dev продолжаются независимо.

## Публичный dev

Желаемый адрес: https://platforma-dev.akmetron.ru. A-запись должна указывать на согласованный
публичный reverse proxy; кандидат — 45.84.85.63, где уже обслуживается platforma.akmetron.ru.
Проверить маршрут и назначение перед изменением DNS. Для первого этапа dev слушает только
127.0.0.1:18081 внутри kromin. Публичного обхода авторизации не создаётся.

Наблюдение 06.11: внутри kromin интерфейс eth0 имеет 10.200.0.146; SSH приходит через
aiport-jump на 10.53.13.2:2211. Последний адрес не является адресом интерфейса гостя.
Не подставлять его в bind без проверки. Оператор выбирает закрытый маршрут от общего Caddy
до dev: защищённый private bind/forwarding либо другой утверждённый вариант. Не открывать порт на 0.0.0.0.
Общий Caddy, соседние сайты и production-маршрут сохранить. Проверить config до reload,
коды и содержимое стабильных контрольных URL соседей — до и после.

Проверки после привязки: /healthz=200; OAuth issuer ровно https://platforma-dev.akmetron.ru;
/mcp без токена=401; сертификат валиден; отдельное dev-приложение Bitrix24 ведёт только на dev.
Реальные ключи Битрикс24 вводятся напрямую в серверный TTY, не в переписку или Git.

## Управляемый production release

Привязать main.py, release_control/, unit и dispatcher-integration.py к фактической установке ai-srv.
Заполнить config.production.example.json, проверить права root-owned путей и единственного писателя.
Фрагмент dispatcher не заменяет существующий файл. Все старые mutating handlers должны
использовать тот же lock на ВЕСЬ вызов; старые deploy/rollback/restart и прямой restore не обходят процесс.
Отдельно проверить production-ветку Caddy/HTTPS: копия проверяла контейнерный edge.

Выявленная совместимость: нынешний files-интерфейс требует image вида platforma-gateway:<тег>,
а контроллер закрепляет фактический Docker image ID sha256:... в Compose. Нужно согласовать
валидацию управляемого pinned image и исходящего Compose; не снимать проверку области проекта.
До решения этого пункта пакет не считать drop-in заменой существующего deploy.

Для подготовки точного diff можно один раз дать агенту read-only копии dispatcher и валидатора
files/config без секретов, например runtime/workflow-dispatcher-source.py и
runtime/workflow-config-validator-source.py. Env, закрытые ключи и значения реквизитов не копировать.
Агент сам прочитает их через текущий files get; владельцу не потребуется пересказывать код.

## Машиночитаемая передача результата

После согласованного действия оператор размещает небольшой несекретный
runtime/workflow-handoff.json. Агент читает его через:

    ssh platforma@45.84.85.63 files get runtime/workflow-handoff.json

Пример полей:

    {"schema":1,"status":"prepared","package_sha256":"FROM_SHA256SUMS",
     "development":{"public_url":"https://platforma-dev.akmetron.ru","https_route_verified":false},
     "production":{"release_commands_installed":false,"legacy_mutators_guarded":false,
       "pinned_image_validator_checked":false,"caddy_probes_verified":false},
     "source_copies":[],"notes":[]}

Пути и статус менять по факту, не по плану. Значения ключей и содержимое .env не включать.
Размещение информационного файла оператором не требует пересоздания Gateway: приложение
его не использует. Не применять runtime-файл через files apply ради квитанции — текущая
реализация пересоздаёт Gateway для любого runtime apply.

Когда квитанция и исходники доступны через SSH, обычные технические уточнения агент читает
сам. Владелец согласует объём и выпуск; оператор обслуживает host-инфраструктуру и аварии.
