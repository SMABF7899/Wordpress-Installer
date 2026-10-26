#!/usr/bin/env python3
"""Safely remove one WordPress site from an Ubuntu Apache/MySQL server."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


BASE_DOMAIN = "smabf.ir"
SITE_FOLDER_SUFFIX = ".smabf.ir"
WEB_ROOT = Path("/var/www/html")
APACHE_AVAILABLE = Path("/etc/apache2/sites-available")
APACHE_ENABLED = Path("/etc/apache2/sites-enabled")
LE_LIVE = Path("/etc/letsencrypt/live")
LE_ARCHIVE = Path("/etc/letsencrypt/archive")
LE_RENEWAL = Path("/etc/letsencrypt/renewal")

NAME_PATTERN = re.compile(r"\A[a-z0-9](?:[a-z0-9-]{0,59}[a-z0-9])?\Z")
DB_NAME_PATTERN = re.compile(r"\A[a-z0-9](?:[a-z0-9_-]*[a-z0-9])?\Z")
REQUIRED_TOOLS = ("a2dissite", "apache2ctl", "systemctl", "certbot", "rm")

# These database settings match wp_installer.py. Never print DB_PASSWORD.
DB_USER = "root"
DB_PASSWORD = ""
DB_HOST = "localhost"
DB_PORT = 3306
DB_CONNECT_TIMEOUT_SECONDS = 10
DB_OPERATION_TIMEOUT_SECONDS = 30
DB_DRIVER_PIP_NAME = "PyMySQL"

ARVAN_API_BASE_URL = "https://napi.arvancloud.ir/cdn/4.0"
ARVAN_TOKEN_ENV_VAR = "ARVAN_API_TOKEN"
ARVAN_REQUEST_TIMEOUT_SECONDS = 30
ARVAN_MAX_RESPONSE_BYTES = 5 * 1024 * 1024
ARVAN_DNS_PAGE_SIZE = 300
ARVAN_MAX_DNS_PAGES = 100


class Log:
    COLORS = {
        "INFO": "\033[36m",
        "OK": "\033[32m",
        "WARN": "\033[33m",
        "ERROR": "\033[31m",
        "STEP": "\033[35m",
    }

    def __init__(self) -> None:
        self.color = sys.stdout.isatty()

    def write(self, level: str, message: str) -> None:
        timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        label = f"[{level}]"
        if self.color:
            color = self.COLORS.get(level, "")
            label = f"{color}{label}\033[0m"
        print(f"{timestamp} {label} {message}", flush=True)

    def info(self, message: str) -> None:
        self.write("INFO", message)

    def ok(self, message: str) -> None:
        self.write("OK", message)

    def warn(self, message: str) -> None:
        self.write("WARN", message)

    def error(self, message: str) -> None:
        self.write("ERROR", message)

    def step(self, message: str) -> None:
        self.write("STEP", message)


LOG = Log()


class UninstallError(RuntimeError):
    pass


class ArvanAPIError(UninstallError):
    def __init__(self, message: str, *, outcome_uncertain: bool = False) -> None:
        super().__init__(message)
        self.outcome_uncertain = outcome_uncertain


def run_command(
    argv: list[str],
    *,
    privileged: bool = True,
    input_text: Optional[str] = None,
    timeout: int = 180,
) -> subprocess.CompletedProcess[str]:
    """Run a command without a shell, optionally through sudo."""
    command = list(argv)
    if privileged and os.geteuid() != 0:
        command.insert(0, "sudo")
    try:
        result = subprocess.run(
            command,
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UninstallError(f"Command could not be completed: {argv[0]}: {exc}") from exc
    if result.stdout.strip():
        for line in result.stdout.rstrip().splitlines():
            LOG.info(line)
    if result.stderr.strip():
        for line in result.stderr.rstrip().splitlines():
            (LOG.error if result.returncode else LOG.warn)(line)
    return result


def validate_name(name: str) -> str:
    if not NAME_PATTERN.fullmatch(name):
        raise UninstallError(
            "Invalid site name. Use 1-61 lowercase letters, digits, or internal hyphens."
        )
    return name


def resource_paths(name: str) -> dict[str, object]:
    domain = f"{name}.{BASE_DOMAIN}"
    database = f"{name}_wp"
    site_dir = WEB_ROOT / f"{name}{SITE_FOLDER_SUFFIX}"
    apache_files = [
        APACHE_AVAILABLE / f"{domain}.conf",
        APACHE_AVAILABLE / f"{domain}-le-ssl.conf",
    ]
    enabled_links = [APACHE_ENABLED / path.name for path in apache_files]
    return {
        "domain": domain,
        "database": database,
        "site_dir": site_dir,
        "apache_files": apache_files,
        "enabled_links": enabled_links,
        "cert_paths": [
            LE_LIVE / domain,
            LE_ARCHIVE / domain,
            LE_RENEWAL / f"{domain}.conf",
        ],
    }


def lexists(path: Path) -> bool:
    return os.path.lexists(path)


def require_confined_path(path: Path, parent: Path, *, allow_missing: bool = True) -> None:
    """Require an exact child of the expected directory, with no final symlink."""
    if path.parent != parent or path.name in ("", ".", ".."):
        raise UninstallError(f"Refusing an unexpected path: {path}")
    try:
        expected_parent = parent.resolve(strict=True)
    except OSError as exc:
        raise UninstallError(f"Expected parent directory is unavailable: {parent}: {exc}") from exc
    if expected_parent != parent:
        raise UninstallError(f"Refusing a symlinked or redirected parent directory: {parent}")
    if not lexists(path):
        if allow_missing:
            return
        raise UninstallError(f"Expected path does not exist: {path}")
    if path.is_symlink():
        raise UninstallError(f"Refusing to remove a symlink at the target path: {path}")
    try:
        resolved_parent = path.parent.resolve(strict=True)
    except OSError as exc:
        raise UninstallError(f"Could not verify target directory: {path.parent}: {exc}") from exc
    if resolved_parent != expected_parent:
        raise UninstallError(f"Refusing a path outside the expected directory: {path}")


def check_sudo_access() -> None:
    if os.geteuid() == 0:
        LOG.ok("Running as root; privileged commands are available.")
        return
    sudo = shutil.which("sudo")
    if not sudo:
        raise UninstallError("This operation requires root or sudo access, but sudo was not found.")
    result = run_command([sudo, "-v"], privileged=False, timeout=300)
    if result.returncode != 0:
        raise UninstallError("Sudo authentication failed or sudo access is unavailable.")
    LOG.ok("Sudo access verified.")


def load_db_driver() -> Any:
    try:
        import pymysql
    except ImportError as exc:
        raise UninstallError(
            f"The database driver {DB_DRIVER_PIP_NAME} is required. "
            f"Install it with: {sys.executable} -m pip install {DB_DRIVER_PIP_NAME}"
        ) from exc
    return pymysql


def check_environment() -> Any:
    if platform.system() != "Linux":
        raise UninstallError("This script can only run on Linux.")
    LOG.ok("Linux platform verified.")
    missing = [tool for tool in REQUIRED_TOOLS if shutil.which(tool) is None]
    if missing:
        raise UninstallError("Required tools are missing: " + ", ".join(missing))
    LOG.ok("Required tools are available: " + ", ".join(REQUIRED_TOOLS))
    driver = load_db_driver()
    LOG.ok(f"Database driver {DB_DRIVER_PIP_NAME} is available.")
    check_sudo_access()
    return driver


def show_targets(paths: dict[str, object]) -> None:
    LOG.info(f"Domain: {paths['domain']}")
    LOG.info(f"Site path: {paths['site_dir']}")
    LOG.info(f"Database: {paths['database']}")


def show_plan(paths: dict[str, object]) -> None:
    LOG.step("Planned operations:")
    LOG.info(f"Disable Apache sites: {paths['domain']}.conf and {paths['domain']}-le-ssl.conf")
    LOG.info("Run apache2ctl configtest, then reload Apache only if it succeeds.")
    LOG.info(f"Delete the Certbot certificate named {paths['domain']}.")
    LOG.info("Remove only the two matching Apache configuration files.")
    LOG.info(f"Remove only the site directory {paths['site_dir']}.")
    LOG.info(f"Drop only the MySQL database {paths['database']}.")
    LOG.info("Delete the exact matching Arvan Cloud A record as the final operation.")


def get_arvan_api_token() -> str:
    token = os.environ.get(ARVAN_TOKEN_ENV_VAR, "").strip()
    if not token:
        raise UninstallError(
            f"Arvan Cloud API token is missing. Set the {ARVAN_TOKEN_ENV_VAR} environment variable."
        )
    if not token or any(not 33 <= ord(character) <= 126 for character in token):
        raise UninstallError(f"{ARVAN_TOKEN_ENV_VAR} contains invalid characters for an API header.")
    return token


def arvan_api_request(method: str, url: str, token: str) -> tuple[int, bytes]:
    request = Request(
        url,
        headers={
            "Authorization": f"Apikey {token}",
            "Accept": "application/json",
        },
        method=method,
    )
    try:
        with urlopen(request, timeout=ARVAN_REQUEST_TIMEOUT_SECONDS) as response:
            status = response.status
            body = response.read(ARVAN_MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        if exc.code == 401:
            raise ArvanAPIError("Arvan Cloud authentication failed (HTTP 401). Check the API token.") from None
        if exc.code == 403:
            raise ArvanAPIError("Arvan Cloud denied the API request (HTTP 403). Check token permissions.") from None
        raise ArvanAPIError(
            f"Arvan Cloud API returned HTTP {exc.code} for {method}.",
            outcome_uncertain=(method == "DELETE"),
        ) from None
    except (URLError, TimeoutError, OSError) as exc:
        if method == "DELETE":
            raise ArvanAPIError(
                "The Arvan Cloud DELETE request did not return a definite result.",
                outcome_uncertain=True,
            ) from None
        reason = getattr(exc, "reason", exc)
        raise ArvanAPIError(f"Arvan Cloud {method} request failed: {reason}.") from None
    if len(body) > ARVAN_MAX_RESPONSE_BYTES:
        raise ArvanAPIError(
            f"Arvan Cloud response exceeded {ARVAN_MAX_RESPONSE_BYTES} bytes.",
            outcome_uncertain=(method == "DELETE"),
        )
    if not 200 <= status < 300:
        raise ArvanAPIError(
            f"Arvan Cloud API returned unexpected HTTP {status} for {method}.",
            outcome_uncertain=(method == "DELETE"),
        )
    return status, body


def fetch_arvan_dns_records(token: str) -> list[dict[str, Any]]:
    endpoint = f"{ARVAN_API_BASE_URL}/domains/{quote(BASE_DOMAIN, safe='')}/dns-records"
    all_records: list[dict[str, Any]] = []
    last_page: Optional[int] = None

    for page in range(1, ARVAN_MAX_DNS_PAGES + 1):
        query = urlencode({"page": page, "per_page": ARVAN_DNS_PAGE_SIZE})
        _, body = arvan_api_request("GET", f"{endpoint}?{query}", token)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UninstallError(f"Arvan Cloud returned invalid JSON: {exc}") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise UninstallError("Arvan Cloud returned an invalid DNS records response (expected a data list).")
        records = payload["data"]
        if any(not isinstance(record, dict) for record in records):
            raise UninstallError("Arvan Cloud returned an invalid DNS records response (record is not an object).")
        all_records.extend(records)

        metadata = payload.get("meta")
        if metadata is None:
            if len(records) >= ARVAN_DNS_PAGE_SIZE:
                raise UninstallError("Arvan Cloud response may be paginated but contains no pagination metadata.")
            return all_records
        if not isinstance(metadata, dict):
            raise UninstallError("Arvan Cloud returned invalid pagination metadata.")
        last_page = metadata.get("last_page")
        if type(last_page) is not int or last_page < 0:
            raise UninstallError("Arvan Cloud returned invalid pagination metadata (last_page).")
        if last_page == 0 or page >= last_page:
            return all_records
        if last_page > ARVAN_MAX_DNS_PAGES:
            raise UninstallError(
                f"Arvan Cloud DNS list has too many pages to inspect safely ({last_page})."
            )

    raise UninstallError(
        f"Arvan Cloud DNS listing exceeded the {ARVAN_MAX_DNS_PAGES}-page safety limit."
    )


def matching_arvan_a_records(records: list[dict[str, Any]], site_name: str) -> list[dict[str, Any]]:
    return [
        record
        for record in records
        if record.get("name") == site_name
        and isinstance(record.get("type"), str)
        and record["type"].upper() == "A"
    ]


def arvan_record_ip(record: dict[str, Any]) -> Optional[str]:
    value = record.get("value")
    if isinstance(value, dict):
        values = [value]
    elif isinstance(value, list):
        values = value
    elif isinstance(value, str) and value.strip():
        return value.strip()
    else:
        return None

    ips: list[str] = []
    for item in values:
        if not isinstance(item, dict) or not isinstance(item.get("ip"), str) or not item["ip"].strip():
            return None
        ips.append(item["ip"].strip())
    return ", ".join(ips) if ips else None


def show_arvan_record(record: dict[str, Any]) -> None:
    record_id = record.get("id", "<missing>")
    name = record.get("name", "<missing>")
    record_type = record.get("type", "<missing>")
    ip = arvan_record_ip(record) or "<unavailable>"
    LOG.info(f"DNS record: name={name}, type={record_type}, IP={ip}, id={record_id}")


def validate_arvan_record_id(record: dict[str, Any]) -> str:
    record_id = record.get("id")
    if not isinstance(record_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", record_id):
        raise UninstallError("Arvan Cloud returned a DNS record with an invalid or missing record ID.")
    return record_id


def ensure_prior_stages_succeeded(stages: dict[str, str]) -> None:
    incomplete = [
        f"{label}={state}"
        for label, state in stages.items()
        if label != "Delete Arvan Cloud DNS record"
        and (state == "in progress" or state.startswith(("not run", "FAILED", "cancelled")))
    ]
    if incomplete:
        raise UninstallError(
            "Arvan Cloud DNS deletion is blocked because earlier uninstall stages did not complete: "
            + "; ".join(incomplete)
        )


def arvan_record_id_is_present(token: str, record_id: str) -> bool:
    records = fetch_arvan_dns_records(token)
    return any(record.get("id") == record_id for record in records)


def delete_arvan_dns_record(
    site_name: str,
    token: str,
    stages: dict[str, str],
    *,
    dry_run: bool,
) -> None:
    label = "Delete Arvan Cloud DNS record"
    stages[label] = "in progress"
    if not dry_run:
        ensure_prior_stages_succeeded(stages)

    matches = matching_arvan_a_records(fetch_arvan_dns_records(token), site_name)
    if not matches:
        stages[label] = "skipped (no exact matching A record found)"
        LOG.warn(f"No exact A record named {site_name} was found in {BASE_DOMAIN}.")
        return
    if len(matches) > 1:
        for record in matches:
            show_arvan_record(record)
        stages[label] = "FAILED (multiple exact A records; none deleted)"
        raise UninstallError(
            f"Found {len(matches)} A records named exactly {site_name}; none were deleted. Resolve the ambiguity first."
        )

    record = matches[0]
    show_arvan_record(record)
    record_id = validate_arvan_record_id(record)
    selected_ip = arvan_record_ip(record)
    if not selected_ip:
        raise UninstallError("The matching A record has no valid IP value; refusing to delete it.")
    if record.get("is_protected") is True or record.get("can_delete") is False:
        raise UninstallError("The matching Arvan Cloud DNS record is protected and cannot be deleted safely.")

    if dry_run:
        stages[label] = "would delete (dry-run; no DELETE request sent)"
        LOG.info("Dry run: this exact A record is the only matching candidate; no DNS changes were made.")
        return

    confirmation = f"DELETE {site_name}.{BASE_DOMAIN}"
    LOG.warn(f"Type exactly '{confirmation}' to delete this DNS record:")
    try:
        typed_confirmation = input("Confirm DNS deletion: ").strip()
    except EOFError:
        typed_confirmation = ""
    if typed_confirmation != confirmation:
        stages[label] = "cancelled (confirmation did not match; record left in place)"
        raise UninstallError("Arvan Cloud DNS deletion was cancelled; the DNS record was left in place.")

    latest_records = fetch_arvan_dns_records(token)
    same_id = [record for record in latest_records if record.get("id") == record_id]
    latest_matches = matching_arvan_a_records(latest_records, site_name)
    if not same_id:
        stages[label] = "completed (record was already absent before DELETE)"
        LOG.warn("The selected DNS record disappeared before DELETE; no DELETE request was sent.")
        return
    if (
        len(same_id) != 1
        or len(latest_matches) != 1
        or latest_matches[0].get("id") != record_id
        or arvan_record_ip(latest_matches[0]) != selected_ip
    ):
        raise UninstallError("The DNS record changed after confirmation; no DELETE request was sent.")
    if latest_matches[0].get("is_protected") is True or latest_matches[0].get("can_delete") is False:
        raise UninstallError("The selected DNS record is now protected; no DELETE request was sent.")

    delete_url = (
        f"{ARVAN_API_BASE_URL}/domains/{quote(BASE_DOMAIN, safe='')}/dns-records/"
        f"{quote(record_id, safe='')}"
    )
    try:
        arvan_api_request("DELETE", delete_url, token)
    except ArvanAPIError as exc:
        if not exc.outcome_uncertain:
            raise
        LOG.warn("DELETE outcome is uncertain; checking the record by ID before deciding what happened.")
        try:
            still_present = arvan_record_id_is_present(token, record_id)
        except UninstallError as verify_error:
            raise UninstallError(
                f"DELETE outcome is uncertain and verification failed ({verify_error}); no retry was sent."
            ) from None
        if still_present:
            raise UninstallError(
                "The DNS record is still present after an uncertain DELETE; no automatic retry was sent."
            ) from None
        stages[label] = "completed (record absent after uncertain DELETE; verified by GET)"
        LOG.ok("The selected DNS record is absent; deletion was verified by GET.")
        return

    try:
        still_present = arvan_record_id_is_present(token, record_id)
    except UninstallError as exc:
        raise UninstallError(
            f"DELETE returned success, but the follow-up GET could not verify the result: {exc}"
        ) from None
    if still_present:
        raise UninstallError("DELETE returned success, but the selected DNS record is still present.")
    stages[label] = "completed (deleted and verified by GET)"
    LOG.ok("The selected Arvan Cloud A record was deleted and verified by GET.")


def connect_to_database(driver: Any) -> Any:
    try:
        connection = driver.connect(
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USER,
            password=DB_PASSWORD,
            connect_timeout=DB_CONNECT_TIMEOUT_SECONDS,
            read_timeout=DB_OPERATION_TIMEOUT_SECONDS,
            write_timeout=DB_OPERATION_TIMEOUT_SECONDS,
            autocommit=True,
        )
    except driver.err.MySQLError as exc:
        code = exc.args[0] if exc.args and isinstance(exc.args[0], int) else "unknown"
        if code == 1045:
            reason = f"Authentication failed for database user '{DB_USER}'. Check the values entered in the runtime prompts."
        else:
            reason = f"MySQL returned error {code}. Check the database server and DB_HOST / DB_PORT settings."
        raise UninstallError(f"Could not connect to MySQL at {DB_HOST}:{DB_PORT}. {reason}") from None
    except OSError as exc:
        raise UninstallError(f"Network error connecting to MySQL at {DB_HOST}:{DB_PORT}: {exc}") from exc
    LOG.ok(f"Connected to MySQL at {DB_HOST}:{DB_PORT} as '{DB_USER}'.")
    return connection


def configure_runtime_settings() -> None:
    global BASE_DOMAIN, SITE_FOLDER_SUFFIX, DB_USER, DB_PASSWORD

    try:
        base_domain = input(f"Base domain [{BASE_DOMAIN}]: ").strip()
        site_folder_suffix = input(f"Site folder suffix [{SITE_FOLDER_SUFFIX}]: ").strip()
        db_user = input(f"Database user [{DB_USER}]: ").strip()
        db_password = getpass.getpass("Database password (input hidden): ")
    except (EOFError, KeyboardInterrupt):
        raise UninstallError("Runtime configuration input was cancelled or unavailable.") from None

    if base_domain:
        BASE_DOMAIN = base_domain.rstrip(".").lower()
    if not re.fullmatch(r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", BASE_DOMAIN):
        raise UninstallError("Base domain must be a valid DNS domain name, such as smabf.ir.")

    if site_folder_suffix:
        SITE_FOLDER_SUFFIX = site_folder_suffix.lower()
    if not SITE_FOLDER_SUFFIX.startswith(".") or not re.fullmatch(
        r"\.(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?",
        SITE_FOLDER_SUFFIX,
    ):
        raise UninstallError("Site folder suffix must be a dot followed by a valid domain suffix, such as .smabf.ir.")

    if db_user:
        DB_USER = db_user
    if not re.fullmatch(r"[A-Za-z0-9_.@-]{1,32}", DB_USER):
        raise UninstallError("Database user must be 1-32 characters using letters, digits, '.', '_', '@', or '-'.")
    if not db_password:
        raise UninstallError("Database password cannot be empty.")
    DB_PASSWORD = db_password
    LOG.info(f"Runtime settings: base domain={BASE_DOMAIN}, folder suffix={SITE_FOLDER_SUFFIX}, database user={DB_USER}.")
    LOG.info("Database password was received and will not be logged.")


def validate_database_name(database: str) -> str:
    if not database.endswith("_wp") or len(database) > 64 or not DB_NAME_PATTERN.fullmatch(database):
        raise UninstallError(f"Refusing an unexpected database name: {database!r}")
    return database


def database_exists(connection: Any, driver: Any, database: str) -> bool:
    validate_database_name(database)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = %s",
                (database,),
            )
            return cursor.fetchone() is not None
    except driver.err.MySQLError as exc:
        code = exc.args[0] if exc.args and isinstance(exc.args[0], int) else "unknown"
        raise UninstallError(f"Could not inspect MySQL database state (error {code}).") from None


def site_state(path: Path) -> str:
    if not lexists(path):
        return "absent"
    if path.is_symlink():
        return "symlink (preserved; unsafe target)"
    if path.is_dir():
        return "present (directory)"
    return "present (non-directory)"


def entry_state(path: Path) -> str:
    if not lexists(path):
        return "absent / disabled"
    if path.is_symlink():
        return "enabled (symlink)"
    if path.is_dir():
        return "present (directory; unexpected)"
    return "present (non-symlink; unexpected)"


def certificate_state(paths: list[Path]) -> str:
    present = [str(path) for path in paths if lexists(path)]
    return "present at " + ", ".join(present) if present else "absent"


def final_report(
    paths: dict[str, object],
    stages: dict[str, str],
    *,
    db_connection: Any,
    db_driver: Any,
) -> None:
    LOG.step("Final status report:")
    for label, state in stages.items():
        LOG.info(f"{label}: {state}")
    LOG.info("Resources:")
    for link in paths["enabled_links"]:  # type: ignore[union-attr]
        LOG.info(f"VirtualHost {link.name}: {entry_state(link)}")
    for conf in paths["apache_files"]:  # type: ignore[union-attr]
        LOG.info(f"Apache file {conf}: {'present' if lexists(conf) else 'absent'}")
    LOG.info(f"SSL certificate {paths['domain']}: {certificate_state(paths['cert_paths'])}")  # type: ignore[arg-type]
    LOG.info(f"Site files {paths['site_dir']}: {site_state(paths['site_dir'])}")  # type: ignore[arg-type]
    if db_connection is not None and db_driver is not None:
        try:
            exists = database_exists(db_connection, db_driver, str(paths["database"]))
            LOG.info(f"Database {paths['database']}: {'present' if exists else 'absent'}")
        except UninstallError as exc:
            LOG.warn(f"Database {paths['database']}: unknown ({exc})")
    else:
        LOG.warn(f"Database {paths['database']}: unknown (no authenticated MySQL connection)")


def checked_action(label: str, argv: list[str], stages: dict[str, str], *, input_text: Optional[str] = None) -> None:
    LOG.step(label)
    stages[label] = "in progress"
    result = run_command(argv, input_text=input_text)
    if result.returncode != 0:
        stages[label] = f"FAILED (exit code {result.returncode})"
        raise UninstallError(f"{label} failed with exit code {result.returncode}.")
    stages[label] = "completed"
    LOG.ok(f"{label} completed.")


def remove_apache_configs(paths: dict[str, object], stages: dict[str, str]) -> None:
    label = "Remove matching Apache configuration files"
    LOG.step(label)
    stages[label] = "in progress"
    files: list[Path] = paths["apache_files"]  # type: ignore[assignment]
    removed = 0
    for conf in files:
        require_confined_path(conf, APACHE_AVAILABLE)
        if not lexists(conf):
            LOG.info(f"Already absent: {conf}")
            continue
        if not conf.is_file():
            raise UninstallError(f"Refusing to remove a non-file Apache target: {conf}")
        result = run_command(["rm", "--", str(conf)])
        if result.returncode != 0:
            stages["Remove matching Apache configuration files"] = "FAILED (partial removal possible)"
            raise UninstallError(f"Could not remove Apache configuration file: {conf}")
        removed += 1
    stages["Remove matching Apache configuration files"] = f"completed ({removed} removed)"
    LOG.ok("Matching Apache configuration file cleanup completed.")


def remove_site_directory(path: Path, stages: dict[str, str]) -> None:
    label = "Remove site directory"
    LOG.step(label)
    stages[label] = "in progress"
    require_confined_path(path, WEB_ROOT)
    if not lexists(path):
        stages[label] = "skipped (already absent)"
        LOG.info(f"Already absent: {path}")
        return
    if not path.is_dir():
        raise UninstallError(f"Refusing to recursively remove a non-directory target: {path}")
    if os.path.ismount(path):
        raise UninstallError(f"Refusing to recursively remove a mounted site directory: {path}")
    result = run_command(["rm", "-r", "--one-file-system", "--", str(path)], timeout=600)
    if result.returncode != 0:
        stages[label] = "FAILED (partial removal possible)"
        raise UninstallError(f"Could not completely remove site directory: {path}")
    stages[label] = "completed"
    LOG.ok(f"Removed site directory: {path}")


def remove_database(database: str, stages: dict[str, str], connection: Any, driver: Any) -> None:
    label = "Drop MySQL database"
    LOG.step(label)
    stages[label] = "in progress"
    if not database_exists(connection, driver, database):
        stages[label] = "skipped (already absent)"
        LOG.info(f"Database already absent: {database}")
        return
    validate_database_name(database)
    try:
        with connection.cursor() as cursor:
            cursor.execute(f"DROP DATABASE `{database}`")
    except driver.err.MySQLError as exc:
        code = exc.args[0] if exc.args and isinstance(exc.args[0], int) else "unknown"
        stages[label] = f"FAILED (MySQL error {code})"
        raise UninstallError(f"Could not drop MySQL database {database} (MySQL error {code}).") from None
    stages[label] = "completed"
    LOG.ok(f"Dropped MySQL database: {database}")


def run_uninstall(name: str, *, dry_run: bool) -> int:
    paths = resource_paths(name)
    stage_names = (
        "Preflight",
        "Database connection",
        "Arvan API token",
        "Confirmation",
        "Disable Apache VirtualHosts",
        "Apache configtest",
        "Reload Apache",
        "Delete Certbot certificate",
        "Remove matching Apache configuration files",
        "Remove site directory",
        "Drop MySQL database",
        "Final Apache configuration check",
        "Delete Arvan Cloud DNS record",
    )
    stages: dict[str, str] = {label: "not run" for label in stage_names}
    db_driver: Any = None
    db_connection: Any = None
    arvan_token: Optional[str] = None
    show_targets(paths)

    try:
        LOG.step("Preflight checks")
        stages["Preflight"] = "in progress"
        db_driver = check_environment()
        stages["Preflight"] = "passed"
        stages["Database connection"] = "in progress"
        db_connection = connect_to_database(db_driver)
        stages["Database connection"] = "passed"
        stages["Arvan API token"] = "in progress"
        arvan_token = get_arvan_api_token()
        stages["Arvan API token"] = "passed"
        LOG.ok(f"{ARVAN_TOKEN_ENV_VAR} is set; the token value is not logged.")
        show_plan(paths)

        if dry_run:
            stages["Confirmation"] = "skipped (--dry-run)"
            for label in stage_names:
                if label not in ("Preflight", "Database connection", "Arvan API token", "Confirmation", "Delete Arvan Cloud DNS record"):
                    stages[label] = "not run (--dry-run)"
            LOG.warn("Dry run enabled; no system changes will be made.")
            delete_arvan_dns_record(name, arvan_token, stages, dry_run=True)
            final_report(paths, stages, db_connection=db_connection, db_driver=db_driver)
            return 0

        expected = str(paths["domain"])
        stages["Confirmation"] = "in progress"
        LOG.warn(f"Type the full domain {expected} to confirm permanent deletion:")
        try:
            confirmation = input("Confirm domain: ").strip()
        except EOFError:
            confirmation = ""
        if confirmation != expected:
            stages["Confirmation"] = "cancelled (domain did not match)"
            for label, state in stages.items():
                if state == "not run":
                    stages[label] = "not run (cancelled; no changes made)"
            LOG.warn("Confirmation did not match. No resources were changed.")
            final_report(paths, stages, db_connection=db_connection, db_driver=db_driver)
            return 0
        stages["Confirmation"] = "passed"

        enabled_links: list[Path] = paths["enabled_links"]  # type: ignore[assignment]
        enabled_names = [link.name for link in enabled_links if lexists(link)]
        if enabled_names:
            checked_action(
                "Disable Apache VirtualHosts",
                ["a2dissite", *enabled_names],
                stages,
            )
        else:
            stages["Disable Apache VirtualHosts"] = "skipped (target VirtualHosts already disabled)"
            LOG.info("Target VirtualHosts are already disabled.")

        checked_action("Apache configtest", ["apache2ctl", "configtest"], stages)
        checked_action("Reload Apache", ["systemctl", "reload", "apache2"], stages)
        cert_paths: list[Path] = paths["cert_paths"]  # type: ignore[assignment]
        if any(lexists(cert_path) for cert_path in cert_paths):
            checked_action(
                "Delete Certbot certificate",
                ["certbot", "delete", "--cert-name", expected],
                stages,
                input_text="Y\n",
            )
        else:
            stages["Delete Certbot certificate"] = "skipped (certificate files already absent)"
            LOG.info(f"Certificate files for {expected} are already absent.")

        remove_apache_configs(paths, stages)
        remove_site_directory(paths["site_dir"], stages)  # type: ignore[arg-type]
        remove_database(str(paths["database"]), stages, db_connection, db_driver)

        LOG.step("Final Apache configuration check")
        stages["Final Apache configuration check"] = "in progress"
        result = run_command(["apache2ctl", "configtest"])
        if result.returncode != 0:
            stages["Final Apache configuration check"] = f"FAILED (exit code {result.returncode})"
            raise UninstallError("Final Apache configuration check failed.")
        stages["Final Apache configuration check"] = "passed"
        LOG.ok("Final Apache configuration check passed.")
        delete_arvan_dns_record(name, arvan_token, stages, dry_run=False)
        final_report(paths, stages, db_connection=db_connection, db_driver=db_driver)
        return 0
    except UninstallError as exc:
        LOG.error(str(exc))
        if stages["Preflight"] in ("not run", "in progress"):
            stages["Preflight"] = f"FAILED ({exc})"
        for label, state in stages.items():
            if state == "in progress":
                stages[label] = "FAILED (see error above)"
        for label, state in stages.items():
            if state == "not run":
                stages[label] = "not run (stopped before this stage)"
        final_report(paths, stages, db_connection=db_connection, db_driver=db_driver)
        return 1
    finally:
        if db_connection is not None:
            try:
                db_connection.close()
                LOG.info("MySQL connection closed.")
            except Exception as exc:  # A close failure must not hide the operation result.
                LOG.warn(f"Could not close the MySQL connection cleanly ({type(exc).__name__}).")


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Safely remove one WordPress site from an Ubuntu Apache/MySQL server.",
        epilog=(
            f"This script prompts for the domain, folder suffix, database user, and hidden database password. "
            f"PyMySQL is required (python3 -m pip install {DB_DRIVER_PIP_NAME})."
        ),
    )
    parser.add_argument("--name", help="Site name, for example zz-first")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the planned operations without changing system resources.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    name = args.name
    if name is None:
        try:
            name = input("Site name (for example zz-first): ").strip()
        except EOFError:
            LOG.error("No site name was provided.")
            return 2
    try:
        validate_name(name)
    except UninstallError as exc:
        LOG.error(str(exc))
        return 2
    try:
        configure_runtime_settings()
    except UninstallError as exc:
        LOG.error(str(exc))
        return 2
    return run_uninstall(name, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
