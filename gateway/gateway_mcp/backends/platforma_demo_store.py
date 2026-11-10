"""Fixed local fixture, filtering and explicit output fields. No network or writes."""
from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import date
from pathlib import Path
from typing import Any

DATA_FILE = Path(__file__).with_name("platforma_training_data.json")
STATUS_LABELS = {"active": "В работе", "early": "Завершена досрочно", "exact": "Завершена в срок", "late": "Завершена с опозданием"}


class QueryError(ValueError):
    """A safe, user-facing query failure, with no paths or raw data."""


def valid_date(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError
        date.fromisoformat(value)
    except ValueError:
        raise QueryError("INVALID_DATE: ожидается существующая дата YYYY-MM-DD.") from None
    return value


def folded(value: str) -> str:
    return value.casefold().replace("ё", "е")


class DemoStore:
    def __init__(self, scope_units: list[str] | None = None):
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        if data.get("synthetic") is not True or data.get("schema_version") != 1:
            raise ValueError("Only the version 1 synthetic fixture is supported")
        self.as_of = valid_date(data["snapshot_as_of"])
        self.seed = data["seed"]
        self.units = self.index(data["units"])
        self.services = self.index(data["services"])
        self.customers = self.index(data["customers"])
        self.people = self.index(data["people"])
        self.requests = self.index(data["requests"])
        for service in self.services.values():
            if service["unitId"] not in self.units:
                raise ValueError("Service refers to an unknown unit")
        for row in self.requests.values():
            if row.get("source") != "demo" or row["state"] not in STATUS_LABELS:
                raise ValueError("Only synthetic service requests are supported")
            if row["serviceId"] not in self.services or row["unitId"] not in self.units:
                raise ValueError("Request refers to an unknown service or unit")
            if self.services[row["serviceId"]]["unitId"] != row["unitId"]:
                raise ValueError("Request and service units differ")
            if row["personId"] not in self.people or row["customerId"] not in self.customers:
                raise ValueError("Request refers to an unknown person or customer")
            if self.people[row["personId"]]["unitId"] != row["unitId"]:
                raise ValueError("Request and assignee units differ")
            for key in ("createdAt", "dueAt", "acceptedAt"):
                valid_date(row[key])
            if (row["state"] == "active") != (row["acceptedAt"] is None):
                raise ValueError("Request state and acceptance date differ")
        scope = set(self.units) if scope_units is None else set(scope_units)
        if not scope or not scope.issubset(self.units):
            raise ValueError("Scope must contain existing unit IDs only")
        self.scope = frozenset(scope)
        self.periods = sorted({self.period(r) for r in self.requests.values()})

    @staticmethod
    def index(rows: list[dict]) -> dict[str, dict]:
        result = {r["id"]: r for r in rows}
        if not rows or len(result) != len(rows):
            raise ValueError("Empty dataset or duplicate IDs")
        return result

    def period(self, row: dict) -> str:
        prefix = row["id"].split(":", 1)[0]
        return prefix if re.fullmatch(r"\d{4}-\d{2}", prefix) else self.as_of[:7]

    def require_unit(self, unit_id: str | None) -> None:
        if unit_id is not None and unit_id not in self.scope:
            raise QueryError("UNIT_UNAVAILABLE: юнит не найден или недоступен в учебном наборе.")

    def require_service(self, service_id: str | None) -> dict | None:
        if service_id is None:
            return None
        service = self.services.get(service_id)
        if service is None or service["unitId"] not in self.scope:
            raise QueryError("SERVICE_UNAVAILABLE: сервис не найден или недоступен.")
        return service

    def meta(self) -> dict:
        return {"synthetic": True, "read_only": True, "snapshot_as_of": self.as_of,
                "overdue_as_of": self.as_of, "dataset": "platforma-demo", "seed": self.seed}


    def service_record(self, row: dict) -> dict:
        return {"id": row["id"], "name": row["name"], "unit_id": row["unitId"],
                "unit_name": self.units[row["unitId"]]["name"], "description": row["description"],
                "normal_sla": row["normal"], "high_load_sla": row["high"],
                "acceptance_criteria": row["acceptance"], "source": "platforma-training:synthetic.json#services/" + row["id"]}

    def request_record(self, row: dict, detail: bool = False) -> dict:
        result = {"id": row["id"], "title": row["title"], "period": self.period(row),
                  "unit_id": row["unitId"], "unit_name": self.units[row["unitId"]]["name"],
                  "service_id": row["serviceId"], "service_name": self.services[row["serviceId"]]["name"],
                  "customer_id": row["customerId"], "customer_name": self.customers[row["customerId"]]["name"],
                  "assignee_id": row["personId"], "assignee_name": self.people[row["personId"]]["name"],
                  "status": row["state"], "status_label": STATUS_LABELS[row["state"]],
                  "priority": row["priority"], "created_at": row["createdAt"], "due_at": row["dueAt"],
                  "accepted_at": row["acceptedAt"], "is_overdue": row["acceptedAt"] is None and row["dueAt"] < self.as_of,
                  "source": "platforma-training:synthetic.json#requests/" + row["id"]}
        if detail:
            result.update(effort_hours=row["hours"], reason=row["reason"], next_action=row["nextAction"],
                          external_id=row["externalId"])
        return result

    def page(self, records: list[dict], limit: int, offset: int, filters: dict) -> dict:
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 100000:
            raise QueryError("INVALID_PAGE: limit должен быть от 1 до 100, offset от 0 до 100000.")
        selected = records[offset:offset + limit]
        more = offset + len(selected) < len(records)
        return {**self.meta(), "total": len(records), "returned": len(selected), "limit": limit,
                "offset": offset, "has_more": more, "next_offset": offset + len(selected) if more else None,
                "filters": filters, "records": deepcopy(selected)}

    def list_services(self, unit_id=None, query=None, limit=20, offset=0) -> dict:
        self.require_unit(unit_id)
        rows = [self.service_record(s) for s in self.services.values() if s["unitId"] in self.scope and (unit_id is None or s["unitId"] == unit_id)]
        if query and query.strip():
            q = folded(query.strip())
            rows = [s for s in rows if q in folded(" ".join(str(s[k]) for k in ("id", "name", "unit_name", "description")))]
        rows.sort(key=lambda s: (s["unit_id"], s["id"]))
        return self.page(rows, limit, offset, {"unit_id": unit_id, "query": query})

    def get_service(self, service_id: str) -> dict:
        return {**self.meta(), "record": self.service_record(self.require_service(service_id))}

    def list_requests(self, *, unit_id=None, service_id=None, customer_id=None, status=None, priority=None,
                      query=None, period=None, due_from=None, due_to=None, overdue_only=False,
                      limit=20, offset=0, sort="due_asc") -> dict:
        self.require_unit(unit_id)
        service = self.require_service(service_id)
        if service is not None and unit_id is not None and service["unitId"] != unit_id:
            raise QueryError("FILTER_CONFLICT: сервис не относится к выбранному юниту.")
        if customer_id is not None and customer_id not in self.customers:
            raise QueryError("CUSTOMER_UNKNOWN: заказчик не найден.")
        period = self.as_of[:7] if period is None else period
        if period not in self.periods and period != "all":
            raise QueryError("PERIOD_UNAVAILABLE: используйте месяц из platforma.context.get.periods или all.")
        due_from, due_to = valid_date(due_from), valid_date(due_to)
        if due_from and due_to and due_from > due_to:
            raise QueryError("INVALID_RANGE: начало диапазона позже окончания.")
        if overdue_only and status not in (None, "active"):
            raise QueryError("FILTER_CONFLICT: overdue_only применим только к незавершённым заявкам.")
        rows = []
        q = folded(query.strip()) if query else ""
        for row in self.requests.values():
            if row["unitId"] not in self.scope or (unit_id is not None and row["unitId"] != unit_id): continue
            if service_id is not None and row["serviceId"] != service_id: continue
            if customer_id is not None and row["customerId"] != customer_id: continue
            if period != "all" and self.period(row) != period: continue
            if status == "completed" and row["state"] == "active": continue
            if status is not None and status != "completed" and row["state"] != status: continue
            if priority is not None and row["priority"] != priority: continue
            if due_from and row["dueAt"] < due_from or due_to and row["dueAt"] > due_to: continue
            record = self.request_record(row)
            if overdue_only and not record["is_overdue"]: continue
            if q and q not in folded(" ".join(str(record[k]) for k in ("id", "title", "service_name", "unit_name", "customer_name", "assignee_name"))): continue
            rows.append(record)
        if sort == "id": rows.sort(key=lambda r: r["id"])
        else:
            field = "created_at" if sort == "created_desc" else "due_at"
            rows.sort(key=lambda r: (r[field], r["id"]), reverse=sort in ("due_desc", "created_desc"))
        return self.page(rows, limit, offset, {"unit_id": unit_id, "service_id": service_id,
            "customer_id": customer_id, "status": status, "priority": priority, "query": query,
            "period": period, "due_from": due_from, "due_to": due_to, "overdue_only": overdue_only, "sort": sort})

    def get_request(self, request_id: str) -> dict:
        row = self.requests.get(request_id)
        if row is None or row["unitId"] not in self.scope:
            raise QueryError("REQUEST_UNAVAILABLE: заявка не найдена или недоступна.")
        return {**self.meta(), "record": self.request_record(row, detail=True)}
