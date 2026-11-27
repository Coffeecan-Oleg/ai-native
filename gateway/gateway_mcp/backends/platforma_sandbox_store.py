"""Persistent fictional request overlay; the bundled snapshot is never rewritten."""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import time
from uuid import uuid4

from gateway_mcp.backends.platforma_demo_store import DATA_FILE, DemoStore, QueryError, valid_date

MAX_RECORDS = 2000
MAX_BYTES = 4 * 1024 * 1024
PRIORITIES = {"Обычный", "Высокий", "Критичный"}


def enabled():
    return os.getenv("GATEWAY_PLATFORMA_SANDBOX_WRITE_ENABLED", "").lower() == "true"


def overlay_path():
    configured = os.getenv("GATEWAY_PLATFORMA_SANDBOX_DATA_DIR", "")
    path = Path(configured)
    if not enabled() or not configured or not path.is_absolute():
        raise QueryError("SANDBOX_WRITE_DISABLED: учебная запись не настроена.")
    return path / "requests.json"


def _fresh():
    return {"schema": 1, "dataset_sha256": hashlib.sha256(DATA_FILE.read_bytes()).hexdigest(), "records": {}}


def _load():
    path = overlay_path()
    if not path.exists():
        return _fresh()
    if path.is_symlink() or path.stat().st_size > MAX_BYTES:
        raise QueryError("SANDBOX_STORE_INVALID: хранилище недоступно.")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if (data["schema"] != 1 or data["dataset_sha256"] != _fresh()["dataset_sha256"]
                or not isinstance(data["records"], dict) or len(data["records"]) > MAX_RECORDS):
            raise ValueError
        for key, row in data["records"].items():
            if row["id"] != key or type(row["_sandbox_revision"]) is not int or row["_sandbox_revision"] < 1:
                raise ValueError
        return data
    except (KeyError, TypeError, ValueError, OSError):
        raise QueryError("SANDBOX_STORE_INVALID: хранилище недоступно.") from None


@contextmanager
def _locked():
    path = overlay_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = path.with_suffix(".lock")
    if path.parent.is_symlink() or lock.is_symlink():
        raise QueryError("SANDBOX_STORE_INVALID: хранилище недоступно.")
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "r+b") as handle:
        if os.name == "nt":
            import msvcrt
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b"0"); handle.flush()
            deadline = time.monotonic() + 5
            while True:
                try:
                    handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise QueryError("SANDBOX_WRITE_BUSY: повторите запрос.") from None
                    time.sleep(0.02)
            try:
                yield
            finally:
                handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            deadline = time.monotonic() + 5
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise QueryError("SANDBOX_WRITE_BUSY: повторите запрос.") from None
                    time.sleep(0.02)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _save(data):
    blob = (json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
    if len(blob) > MAX_BYTES or len(data["records"]) > MAX_RECORDS:
        raise QueryError("SANDBOX_QUOTA: достигнут лимит учебных изменений.")
    path = overlay_path()
    fd, name = tempfile.mkstemp(prefix=".requests-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(blob); handle.flush(); os.fsync(handle.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _text(value, field, maximum=1000):
    if not isinstance(value, str) or len(value) > maximum or "\x00" in value:
        raise QueryError("INVALID_FIELD: " + field)
    return value.strip()


def _validate(store, row):
    for key in ("createdAt", "dueAt", "acceptedAt"):
        valid_date(row[key])
    if row["dueAt"] < row["createdAt"] or row["createdAt"] > store.as_of:
        raise QueryError("INVALID_DATES: используйте учебную дату снимка и корректный срок.")
    if row["acceptedAt"] is not None and not row["createdAt"] <= row["acceptedAt"] <= store.as_of:
        raise QueryError("INVALID_DATES: завершение должно быть между созданием и датой снимка.")
    if row["priority"] not in PRIORITIES:
        raise QueryError("INVALID_PRIORITY: неизвестный приоритет.")
    person = store.people.get(row["personId"])
    if not person or person["unitId"] != row["unitId"]:
        raise QueryError("ASSIGNEE_UNAVAILABLE: ответственный не относится к подразделению.")
    if not row["title"]:
        raise QueryError("INVALID_TITLE: заголовок обязателен.")
    row["state"] = ("active" if row["acceptedAt"] is None else
                    "early" if row["acceptedAt"] < row["dueAt"] else
                    "exact" if row["acceptedAt"] == row["dueAt"] else "late")


class SandboxStore(DemoStore):
    def __init__(self):
        super().__init__()
        data = _load()
        for key, row in data["records"].items():
            self.require_service(row.get("serviceId"))
            if row.get("source") != "demo" or row.get("unitId") != self.services[row["serviceId"]]["unitId"]:
                raise QueryError("SANDBOX_STORE_INVALID: неверная учебная запись.")
            self.requests[key] = deepcopy(row)
        self.periods = sorted({self.period(r) for r in self.requests.values()})

    def meta(self):
        from gateway_mcp.services.platforma_clients import client_identifiers
        return {**super().meta(), "skill_telemetry_clients": client_identifiers(), "read_only": False, "sandbox_write_pilot": True,
                "clock": "fixed_snapshot", "base_snapshot_immutable": True,
                "write_routes": ["platforma.requests.create", "platforma.requests.update"],
                "write_scope_required": "platforma:write",
                "authorization_note": "Backend capability is not an actor permission. "
                    "Writes require platforma:write and an active owner or training participant approval. "
                    "Only the sandbox overlay changes; the base snapshot stays immutable."}

    def request_record(self, row, detail=False):
        record = super().request_record(row, detail=detail)
        revision = row.get("_sandbox_revision", 0)
        record.update(revision=revision, sandbox_modified=bool(revision))
        if revision:
            record["source"] = "platforma-training:sandbox-overlay.json#requests/" + row["id"]
        return record


def create_request(*, template_request_id, title, due_at, priority="Обычный", created_at=None):
    title = _text(title, "title", 300)
    valid_date(due_at)
    if due_at is None:
        raise QueryError("INVALID_DATES: срок обязателен.")
    with _locked():
        store = SandboxStore()
        template = store.requests.get(template_request_id)
        if not template:
            raise QueryError("REQUEST_UNAVAILABLE: шаблон не найден.")
        row = deepcopy(template)
        identifier = store.as_of[:7] + ":REQ-SBX-" + uuid4().hex
        row.update(id=identifier, title=title, dueAt=due_at, priority=priority,
                   createdAt=created_at or store.as_of, acceptedAt=None, state="active",
                   hours=0, reason="", nextAction="", externalId="", _sandbox_revision=1)
        _validate(store, row)
        data = _load(); data["records"][identifier] = row; _save(data)
        return {**store.meta(), "read_only": False, "operation": "created", "record": store.request_record(row, detail=True)}


def update_request(*, request_id, expected_revision, changes):
    allowed = {"title", "priority", "due_at", "assignee_id", "accepted_at", "reason", "next_action", "effort_hours"}
    if type(expected_revision) is not int or expected_revision < 0:
        raise QueryError("INVALID_REVISION: требуется неотрицательная версия.")
    if not isinstance(changes, dict) or not changes or not set(changes) <= allowed:
        raise QueryError("INVALID_CHANGES: неизвестные или пустые поля.")
    mapping = {"due_at":"dueAt", "assignee_id":"personId", "accepted_at":"acceptedAt",
               "next_action":"nextAction", "effort_hours":"hours"}
    with _locked():
        store = SandboxStore()
        row = deepcopy(store.requests.get(request_id))
        if not row:
            raise QueryError("REQUEST_UNAVAILABLE: заявка не найдена.")
        revision = row.get("_sandbox_revision", 0)
        if revision != expected_revision:
            raise QueryError("REVISION_CONFLICT: перечитайте заявку перед изменением.")
        for key, value in changes.items():
            if key in {"title", "reason", "next_action"}:
                value = _text(value, key, 300 if key == "title" else 1000)
            elif key in {"due_at", "accepted_at"}:
                valid_date(value)
                if key == "due_at" and value is None:
                    raise QueryError("INVALID_DATES: срок обязателен.")
            elif key in {"priority", "assignee_id"}:
                value = _text(value, key, 200)
            elif key == "effort_hours":
                if type(value) not in {int, float} or not math.isfinite(value) or not 0 <= value <= 10000:
                    raise QueryError("INVALID_EFFORT: допустимо число от 0 до 10000.")
            row[mapping.get(key, key)] = value
        _validate(store, row)
        row["_sandbox_revision"] = revision + 1
        data = _load(); data["records"][request_id] = row; _save(data)
        return {**store.meta(), "read_only": False, "operation": "updated", "record": store.request_record(row, detail=True)}
