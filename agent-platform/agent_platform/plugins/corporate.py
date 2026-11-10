"""Corporate OAuth-only distribution; does not alter the server or user grants."""
from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from agent_platform.io import write_json, write_text

VERSION = "0.1.11"
NAME = "gateway-mcp"  # Keep the existing installed plugin identity.
TITLE = "Платформа Акметрон"
DESCRIPTION = "Личное MCP-подключение к корпоративной Платформе: каталог опубликованных навыков, разрешённые инструменты и собственный журнал."


def validate_config(config: dict[str, Any]) -> str:
    if config.get("profile") != "corporate":
        raise ValueError("unknown gateway distribution profile")
    url = config.get("mcpUrl")
    if not isinstance(url, str):
        raise ValueError("corporate mcpUrl is required")
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment or parsed.path != "/mcp"
            or any(char.isspace() for char in url)):
        raise ValueError("corporate mcpUrl must be an HTTPS /mcp URL without credentials")
    return url


def write_corporate_gateway(plugin_root: Path, config: dict[str, Any], platform_release: str) -> None:
    url = validate_config(config)  # Validate before creating any output.
    metadata = {"platformRelease": platform_release, "distribution": "corporate", "authentication": "oauth"}
    author = {"name": "Coffeecan-Oleg", "url": "https://github.com/Coffeecan-Oleg"}
    interface = {
        "displayName": TITLE,
        "shortDescription": "Навыки и личное подключение",
        "longDescription": DESCRIPTION,
        "developerName": "Coffeecan-Oleg",
        "category": "Productivity",
        "capabilities": ["MCP"],
        "defaultPrompt": "Проверь моё подключение: вызови gateway_access_profile и gateway_corporate_skill_list. Покажи фактический результат; ничего не изменяй.",
    }
    identity = {"name": NAME, "version": VERSION, "description": DESCRIPTION, "author": author}
    write_json(plugin_root / "plugin.json", {
        "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
        **identity,
        "extensions": {"com.openai": {"interface": interface}},
    })
    write_json(plugin_root / "mcp.json", {
        "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
        "mcpServers": {"gateway": {
            "type": "streamable-http", "url": url,
            "extensions": {"com.openai": {"auth": {"type": "oauth", "client": {"mode": "dcr"}}}},
        }},
    })
    for name, body in {
        "codex.mcp.json": {"type": "http", "url": url, "timeout": 60000},
        "cursor.mcp.json": {"url": url},
        ".mcp.json": {"type": "http", "url": url},
    }.items():
        write_json(plugin_root / name, {"mcpServers": {"gateway": body}})
    write_json(plugin_root / ".codex-plugin/plugin.json", {
        **identity, "mcpServers": "./codex.mcp.json", "interface": interface, "metadata": metadata,
    })
    write_json(plugin_root / ".claude-plugin/plugin.json", {
        **identity, "mcpServers": "./.mcp.json", "metadata": metadata,
    })
    write_json(plugin_root / ".cursor-plugin/plugin.json", {
        **identity, "displayName": TITLE, "category": "productivity",
        "mcpServers": "./cursor.mcp.json", "metadata": metadata,
    })
    # Compatibility descriptor only: its presence does not prove runtime support.
    write_json(plugin_root / "openclaw.plugin.json", {
        "schema": 1, **identity, "mcpServers": "./mcp.json", "metadata": metadata,
    })
    write_text(plugin_root / "plugin.yaml", f'name: {NAME}\nversion: "{VERSION}"\ndescription: "Corporate MCP connection; configure OAuth in the host application."\n')
    write_text(plugin_root / "__init__.py", '"""Connection metadata only; this module does not acquire or forward tokens."""\n\ndef register(ctx):\n    del ctx\n')
    write_text(plugin_root / "README.md", f"""# {TITLE} — личное MCP-подключение

Пакет `gateway-mcp` {VERSION} подключает к `{url}`. Сервер проверяет личную
учётную запись действующего внутреннего сотрудника через корпоративный Битрикс24.
Название и ID плагина не меняют роль, права и список инструментов сотрудника.

## Начало работы

1. Добавьте пакет или этот адрес в приложение с поддержкой удалённого MCP и OAuth.
2. Пройдите личный вход Битрикс24. Вход в веб-панель сам по себе не подключает чат.
3. В чате с подключением отправьте запрос:

```text
Вызови gateway_access_profile, затем gateway_corporate_skill_list через MCP Платформы.
Покажи фактические имена и постоянные ID навыков. Ничего не изменяй.
Если инструментов в этом сеансе нет, скажи, что запрос до сервера не дошёл.
```

Ожидаемый результат: ответ профиля и каталог с сервера. Наличие установленного
пакета или название приложения не доказывают успешный вызов.

## Форматы подключения

- Portable: `plugin.json` и `mcp.json`; Codex также имеет совместимый overlay.
- Codex: `codex.mcp.json`; Cursor: `cursor.mcp.json`; Claude Code: `.mcp.json`.
- OpenCode/ToolKit, OpenClaw и Hermes: используйте этот же адрес, если конкретная
  версия умеет удалённый MCP с OAuth. Дескриптор не устанавливает транспортный
  адаптер и не подтверждает совместимость. Если OAuth нет, сообщите администратору
  название и версию приложения; не подменяйте имя клиента и не вставляйте общий токен.

Ключей, Bearer-заголовков, клиентских секретов и заранее запрошенных scopes в пакете
нет. Права определяет сервер после личного входа. Не запрашивайте memory:write для
чтения каталога. Не создавайте второй сервер, если подключение уже существует:
проверьте адрес существующего подключения и повторите OAuth.

## Границы

Опубликованный навык — инструкция для ассистента, а не новый API и не фоновый агент.
Доступ к CRM, UniSender, памяти и чужой статистике не появляется от установки.
Учебные заявки и старый вход Яндекса не входят в корпоративный профиль.
Для диагностики используйте раздел «Проверить подключение» и пришлите свой отчёт
без токенов и полной ссылки OAuth. Клиентские версии принимаются отдельной проверкой.

Формат portable-пакета: https://developers.openai.com/plugins/build/plugins
""")
