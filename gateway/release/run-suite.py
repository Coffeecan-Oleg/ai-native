"""Run every Gateway test module in a fresh, network-restricted Python process.

Legacy unit fixtures deliberately replace dependencies in sys.modules. They must
not leak into real ASGI tests. PostgreSQL fixtures require a NEW local database;
this runner refuses a populated database and never drops databases or schemas.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
FOLDERS = ("tests", "addon_tests", "corporate_tests", "integration_tests", "onboarding_tests", "release")
ENV_KEYS = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "LANG", "LC_ALL"}


def clean_environment(temporary: Path, database: str = "") -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key.upper() in ENV_KEYS}
    profile = temporary / "profile"
    profile.mkdir()
    env.update(TMP=str(temporary), TEMP=str(temporary), TMPDIR=str(temporary),
               HOME=str(profile), USERPROFILE=str(profile), PYTHONUTF8="1",
               PYTHONIOENCODING="utf-8", GATEWAY_AUDIT_LOG=str(temporary / "audit.jsonl"))
    if database:
        env["ONBOARDING_TEST_DATABASE_URL"] = database
    return env


def deny_external_network(event: str, args: tuple) -> None:
    if event in {"socket.connect", "socket.getaddrinfo", "socket.sendto"}:
        endpoint = args[-1] if event == "socket.sendto" else args[1] if event == "socket.connect" else args[0]
        host = endpoint[0] if isinstance(endpoint, tuple) else endpoint
        if host not in {"127.0.0.1", "::1", "localhost", None}:
            raise PermissionError("ci_external_network_denied")


def test_ids(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from test_ids(item)
        else:
            yield item.id()


def child(folder: str, filename: str, report: Path) -> int:
    sys.addaudithook(deny_external_network)
    sys.path.insert(0, str(ROOT))
    suite = unittest.defaultTestLoader.discover(str(ROOT / folder), pattern=filename)
    identifiers = list(test_ids(suite))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    data = {"tests": result.testsRun, "discovered": len(identifiers), "test_ids": identifiers,
            "failures": len(result.failures), "errors": len(result.errors),
            "skipped": len(result.skipped), "skips": [(case.id(), reason) for case, reason in result.skipped],
            "successful": result.wasSuccessful() and bool(identifiers)}
    report.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return 0 if data["successful"] else 1


def prepare_database(url: str) -> dict:
    # Only this exact disposable database on loopback is accepted, even if a
    # developer accidentally inherited a production GATEWAY_DATABASE_URL.
    from psycopg.conninfo import conninfo_to_dict
    import psycopg
    info = conninfo_to_dict(url)
    if (info.get("dbname") != "platforma_onboarding_test" or
            info.get("host") not in {"127.0.0.1", "::1", "localhost"} or
            info.get("hostaddr") or info.get("service")):
        raise ValueError("A disposable loopback platforma_onboarding_test database is required")
    with psycopg.connect(url) as conn:
        if conn.info.dbname != "platforma_onboarding_test":
            raise ValueError("Unexpected database")
        count = conn.execute("select count(*) from pg_tables where schemaname='public'").fetchone()[0]
        if count:
            raise ValueError("CI requires a NEW empty database; existing tables were left unchanged")
    # Avoid importing a caller's Gateway configuration or secrets.
    with tempfile.TemporaryDirectory(prefix="gwm-") as temp:
        env = clean_environment(Path(temp).resolve())
        env.update(GATEWAY_DATABASE_URL=url, GATEWAY_MIGRATIONS_DIR=str(ROOT / "migrations"))
        run = subprocess.run([sys.executable, "gateway.py", "migrate", "up"], cwd=ROOT,
                             env=env, capture_output=True, timeout=90)
        if run.returncode:
            # Migration diagnostics can contain connection strings: keep them out
            # of logs. This database contains synthetic fixtures only.
            raise RuntimeError("Disposable database migration failed (details withheld)")
    with psycopg.connect(url) as conn:
        versions = [row[0] for row in conn.execute("select version from schema_migrations order by version")]
    expected = [path.stem for path in sorted((ROOT / "migrations").glob("*.sql"))]
    if versions != expected:
        raise RuntimeError("Disposable database migrations are incomplete")
    return {"database": "platforma_onboarding_test", "local_only": True, "migrations": versions}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--require-database", action="store_true")
    parser.add_argument("--allow-platform-skips", action="store_true",
                        help="Allow only the two documented Windows filesystem checks")
    parser.add_argument("--child", nargs=2, metavar=("FOLDER", "FILE"))
    args = parser.parse_args()
    if args.child:
        folder, filename = args.child
        if folder not in FOLDERS or Path(filename).name != filename or not filename.startswith("test") or not filename.endswith(".py"):
            parser.error("Invalid test module")
        return child(folder, filename, args.output)
    if args.timeout < 1 or args.timeout > 600:
        parser.error("Module timeout must be between 1 and 600 seconds")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    database = os.environ.get("ONBOARDING_TEST_DATABASE_URL", "")
    if args.require_database and not database:
        parser.error("ONBOARDING_TEST_DATABASE_URL must name a NEW disposable database")
    migration = prepare_database(database) if database else None
    modules = [(folder, path) for folder in FOLDERS for path in sorted((ROOT / folder).glob("test*.py"))]
    results = []
    for folder, path in modules:
        stem = folder + "-" + path.stem
        receipt = output / (stem + ".json")
        # A previous successful receipt must never conceal a child crash.
        receipt.unlink(missing_ok=True)
        with tempfile.TemporaryDirectory(prefix="gwt-") as temp:
            env = clean_environment(Path(temp).resolve(), database if folder == "onboarding_tests" else "")
            try:
                run = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--child", folder,
                                      path.name, "--output", str(receipt)], cwd=ROOT, env=env,
                                     capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=args.timeout)
                log, code = run.stdout + run.stderr, run.returncode
            except subprocess.TimeoutExpired:
                log, code = "Test module timed out; child process was terminated.\n", 124
        (output / (stem + ".log")).write_text(log, encoding="utf-8")
        item = json.loads(receipt.read_text(encoding="utf-8")) if receipt.is_file() else {}
        item.update(folder=folder, module=path.stem, exit_code=code)
        results.append(item)
        print(json.dumps({"module": stem, "exit_code": code, "tests": item.get("tests"), "skipped": item.get("skipped")}), flush=True)
    skips = [skip for result in results for skip in result.get("skips", [])]
    allowed = {
        "test_metrics_access.MetricsAccessTests.test_world_readable_or_symlink_secret_rejected",
        "test_corporate_stability.CorporateKnowledgeProtocolTests.test_approved_path_cannot_follow_a_symlink_to_other_knowledge",
    } if args.allow_platform_skips and os.name == "nt" else set()
    unexpected_skips = [skip for skip in skips if skip[0] not in allowed]
    identifiers = [case for result in results for case in result.get("test_ids", [])]
    summary = {"successful": bool(modules) and all(result.get("successful") and result["exit_code"] == 0 for result in results) and not unexpected_skips,
               "modules": len(modules), "tests": sum(result.get("tests", 0) for result in results),
               "discovered": len(identifiers), "unique_test_ids": len(set(identifiers)),
               "failures": sum(result.get("failures", 0) for result in results),
               "errors": sum(result.get("errors", 0) for result in results),
               "skipped": len(skips), "skips": skips, "unexpected_skips": unexpected_skips,
               "database": migration, "external_network_denied": True, "inherited_secrets": False,
               "results": results}
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in summary.items() if key not in {"results", "database"}}))
    return 0 if summary["successful"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
