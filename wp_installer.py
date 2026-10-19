#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wp_installer.py
===============
Download and prepare the Persian WordPress (fa_IR) on Windows and Linux
(Python standard library only, except the database steps which need PyMySQL).

Steps:
  1) Read and validate the site name           (test  ->  folder test.ir)
  2) Resolve the install path, create the target folder
     (never writes into an existing folder)
  3) Download latest-fa_IR.zip into a temporary folder
  4) Safely extract the ZIP (no path traversal) directly into the target folder
  5) Fetch the security keys and salts from api.wordpress.org
  6) Create wp-config.php
  7) Connect to the database server
  8) Check for the site database and create it (an existing one is never touched)
  Linux only (run as root); skipped on Windows and with --skip-server-setup:
  9) Create the DNS A record <site>.smabf.ir in Arvan Cloud (an exact existing record is reused)
  10) Create the Apache virtual host config
  11) Enable the site, run configtest and reload Apache (only if the test passes)
  12) Check DNS, ping and HTTP access
  13) Obtain the SSL certificate with Certbot (Production)
  14) Verify the SSL config (port 443 virtual host) and the certificate
  15) Check HTTPS on the origin and the HTTP to HTTPS redirect
  16) Enable the Arvan Cloud proxy (only after a valid Production certificate)
  17) Check the final answer (Server: ArvanCloud)
  Last) Print the final report

Database user creation, privilege changes and the final WordPress installation
are intentionally NOT part of this script.

Steps 9-17 need the environment variables ARVAN_API_TOKEN (Arvan API key) and
CERTBOT_EMAIL (contact email for Let's Encrypt), root privileges, and the commands
a2ensite, apache2ctl, certbot, curl and openssl (dig and ping are optional). All of it
is checked before anything is created. Nothing created by steps 9-17 is rolled back
or deleted when a step fails; the final report lists what was left in place.

Database user creation, privilege changes, the final WordPress installation,
Apache configuration and SSL are intentionally NOT part of this script.

Requirements (database steps only):
  pip install PyMySQL          (pure-Python MySQL / MariaDB driver)
  MySQL 8 accounts that use caching_sha2_password may also need:
  pip install cryptography

Console output:
  Every message is printed through log_info / log_success / log_warning /
  log_error / log_step (plus log_debug with -v) in this format:

      [2026-10-15 21:45:32] [INFO] Downloading WordPress...

  Colors are plain ANSI escape codes (no Colorama needed). They are used only
  when the output is an interactive terminal that supports them. They are
  switched off automatically when the output is redirected to a file or a pipe.
  Environment variables:
      NO_COLOR=1     never use colors
      FORCE_COLOR=1  always use colors (e.g. when piping into `less -R`)

Usage:
  Linux  :  sudo ARVAN_API_TOKEN="$ARVAN_API_TOKEN" CERTBOT_EMAIL="$CERTBOT_EMAIL" python3 wp_installer.py
            (sudo drops environment variables, so pass them explicitly as shown)
  Linux, WordPress and database only:  sudo python3 wp_installer.py --skip-server-setup
  Windows:  py -3 wp_installer.py      (steps 9-17 are skipped)

Minimum Python version: 3.8
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterator, List, NoReturn, Optional, Sequence, Tuple

# ======================================================================
# Settings - in the future, only this section needs to change
# ======================================================================

WP_DOWNLOAD_URL = "https://fa.wordpress.org/latest-fa_IR.zip"
WP_SALT_API_URL = "https://api.wordpress.org/secret-key/1.1/salt/"

# Suffix of the site folder: input "test"  ->  folder "test.ir"
SITE_FOLDER_SUFFIX = ".smabf.ir"

# Base install directory
LINUX_BASE_DIR = Path("/var/www/html")
WINDOWS_DEFAULT_BASE_DIR = Path(r"C:\xampp\htdocs")  # can be changed at run time on Windows
BASE_DIR_ENV_VAR = "WP_INSTALLER_BASE_DIR"  # override the base directory via environment variable

# ----------------------------------------------------------------------
# Database settings (written to wp-config.php and used to connect to the server)
# ----------------------------------------------------------------------
# WARNING: DB_USER and DB_PASSWORD are TEMPORARY TEST values that only exist for
# the current phase of the project. Change them before any real use
# (especially on a public server). DB_PASSWORD is never printed to the console.
DB_NAME_SUFFIX = "_wp"  # database name = <site name without .ir> + this suffix  (test -> test_wp)
DB_USER = "root"  # temporary test value
DB_PASSWORD = "password"  # temporary test value
DB_HOST = "localhost"  # host name only; the port is DB_PORT
DB_PORT = 3306
DB_CHARSET = "utf8mb4"
DB_COLLATE = ""  # empty = pick the best collation the server supports (see DB_COLLATE_PREFERENCE)
DB_COLLATE_PREFERENCE = ("utf8mb4_unicode_520_ci", "utf8mb4_unicode_ci", "utf8mb4_general_ci")
TABLE_PREFIX = "wp_"

# ----------------------------------------------------------------------
# Arvan Cloud DNS settings (DNS / Apache / SSL phase)
# ----------------------------------------------------------------------
# The API token is NEVER stored in this file: it is read from the environment variable below.
ARVAN_API_BASE_URL = "https://napi.arvancloud.ir/cdn/4.0"
ARVAN_TOKEN_ENV_VAR = "ARVAN_API_TOKEN"
ARVAN_PLACEHOLDER_TOKENS = frozenset({"YOUR_ARVAN_API_TOKEN", "YOUR_FAKE_ARVAN_API_KEY_REPLACE_ME"})
ARVAN_HTTP_TIMEOUT_SECONDS = 30
DNS_ZONE = "smabf.ir"  # sites are published as <site name>.smabf.ir
SERVER_IP = "109.122.250.181"  # the origin server (value of the DNS A record)
DNS_RECORD_TTL = 120
ARVAN_ALLOWED_METHODS = ("GET", "POST", "PUT")  # DELETE is deliberately not supported
MAX_ARVAN_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_ARVAN_ERROR_BODY_BYTES = 16 * 1024
ARVAN_DNS_PAGE_SIZE = 50
ARVAN_MAX_DNS_PAGES = 100
DNS_POST_MAX_ATTEMPTS = 2  # at most one repeat, and only after the DNS state was re-checked
DNS_RECHECK_DELAY_SECONDS = 3  # short pause before re-checking after an unclear POST
ARVAN_UPSTREAM_PORT = 443  # origin port Arvan connects to once the cloud proxy is on
ARVAN_UPDATE_MAX_ATTEMPTS = 2  # at most one repeat of a PUT, and only after the record was re-read
ARVAN_CLOUD_WAIT_SECONDS = 300  # how long to wait until the site is served through Arvan Cloud
ARVAN_CLOUD_POLL_INTERVAL_SECONDS = 10

# ----------------------------------------------------------------------
# Apache settings (Linux only)
# ----------------------------------------------------------------------
APACHE_SITES_AVAILABLE_DIR = Path("/etc/apache2/sites-available")
APACHE_SITES_ENABLED_DIR = Path("/etc/apache2/sites-enabled")
COMMAND_TIMEOUT_SECONDS = 60  # limit for every external command (a2ensite, apache2ctl, ...)
# @FQDN@ and @DOCROOT@ are replaced by the script; ${APACHE_LOG_DIR} is an Apache variable and stays as is.
APACHE_VHOST_TEMPLATE = """\
<VirtualHost *:80>
    ServerName @FQDN@
    ServerAdmin webmaster@localhost
    DocumentRoot @DOCROOT@
    <Directory @DOCROOT@>
        AllowOverride All
        Require all granted
    </Directory>
    ErrorLog ${APACHE_LOG_DIR}/@FQDN@-error.log
    CustomLog ${APACHE_LOG_DIR}/@FQDN@-access.log combined
</VirtualHost>
"""

# ----------------------------------------------------------------------
# Site reachability checks (DNS, ping, HTTP)
# ----------------------------------------------------------------------
DNS_WAIT_SECONDS = 300  # how long to wait for the new name to resolve (DNS propagation / cached answers)
DNS_POLL_INTERVAL_SECONDS = 5
PING_COUNT = 3
PING_WAIT_SECONDS = 2  # per-packet wait of ping -W
HTTP_REQUEST_TIMEOUT_SECONDS = 20
HTTP_CHECK_ATTEMPTS = 3  # only repeated when curl itself failed (e.g. connection refused right after a reload)
HTTP_CHECK_INTERVAL_SECONDS = 3

# ----------------------------------------------------------------------
# Certbot / Let's Encrypt settings (Linux only)
# ----------------------------------------------------------------------
CERTBOT_EMAIL_ENV_VAR = "CERTBOT_EMAIL"
CERTBOT_TIMEOUT_SECONDS = 300  # domain validation and installation can take a while
LETSENCRYPT_LIVE_DIR = Path("/etc/letsencrypt/live")
SSL_MIN_VALID_DAYS = 7  # a certificate that expires sooner than this is refused

# ----------------------------------------------------------------------
# Limits and technical constants
# ----------------------------------------------------------------------
HTTP_TIMEOUT_SECONDS = 30
DOWNLOAD_CHUNK_SIZE = 64 * 1024
EXTRACT_CHUNK_SIZE = 64 * 1024
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024  # maximum size of the downloaded file
MAX_SALT_RESPONSE_BYTES = 16 * 1024  # the real API response is about 600 bytes
MAX_ZIP_ENTRIES = 20_000  # WordPress has roughly 4-5 thousand entries
MAX_UNCOMPRESSED_BYTES = 500 * 1024 * 1024  # protection against ZIP bombs
USER_AGENT = "wp-installer/1.0 (python-urllib)"

DB_CONNECT_TIMEOUT_SECONDS = 10
DB_OPERATION_TIMEOUT_SECONDS = 30
DB_DRIVER_PIP_NAME = "PyMySQL"

MYSQL_MAX_DB_NAME_LENGTH = 64
MAX_SITE_NAME_LENGTH = MYSQL_MAX_DB_NAME_LENGTH - len(DB_NAME_SUFFIX)

ZIP_ROOT_DIR = "wordpress"
REQUIRED_WP_ITEMS = (
    "index.php",
    "wp-admin",
    "wp-content",
    "wp-includes",
    "wp-load.php",
    "wp-settings.php",
)
SALT_KEYS = (
    "AUTH_KEY",
    "SECURE_AUTH_KEY",
    "LOGGED_IN_KEY",
    "NONCE_KEY",
    "AUTH_SALT",
    "SECURE_AUTH_SALT",
    "LOGGED_IN_SALT",
    "NONCE_SALT",
)

# Names reserved on Windows (invalid even with an extension, e.g. con.ir)
WINDOWS_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)
# Lowercase English letters, digits and hyphens; must not start or end with a hyphen
SITE_NAME_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?")
# Character set / collation names cannot be sent as SQL parameters, so they are whitelisted
_SQL_WORD_RE = re.compile(r"[a-z0-9_]+")
# The database name cannot be a parameter either: strict whitelist, then quoted as an identifier
_DB_NAME_RE = re.compile(r"[a-z0-9](?:[a-z0-9_-]*[a-z0-9])?")

# Every line of the API response must look exactly like: define('AUTH_KEY', '...');
_SALT_LINE_RE = re.compile(
    r"define\(\s*'(?P<name>[A-Z_]+)'\s*,\s*'(?P<value>[^'\\\r\n]{32,})'\s*\)\s*;"
)

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_INTERRUPT = 130

# Short titles used by the final report
STEP_TITLES: Dict[int, str] = {
    1: "Read and validate the site name",
    2: "Resolve install path and create target folder",
    3: "Download Persian WordPress",
    4: "Extract files",
    5: "Fetch WordPress security keys",
    6: "Create wp-config.php",
    7: "Connect to the database server",
    8: "Check and create the database",
    9: "Create the DNS A record (Arvan Cloud)",
    10: "Create the Apache virtual host config",
    11: "Enable the site, test and reload Apache",
    12: "Check DNS, ping and HTTP access",
    13: "Obtain the SSL certificate (Certbot)",
    14: "Verify the SSL config and certificate",
    15: "Check HTTPS and the HTTP to HTTPS redirect",
    16: "Enable the Arvan Cloud proxy",
    17: "Check the final response through Arvan Cloud",
}

# Console messages of every step: (when it starts, when it succeeded, when it failed).
# The success text is only ever printed AFTER the step really finished without errors.
STEP_MESSAGES: Dict[int, Tuple[str, str, str]] = {
    1: (
        "Validating site name...",
        "Site name is valid.",
        "Failed to validate the site name.",
    ),
    2: (
        "Checking destination directory...",
        "Destination directory created successfully.",
        "Failed to prepare the destination directory.",
    ),
    3: (
        "Downloading Persian WordPress...",
        "WordPress downloaded successfully.",
        "Failed to download WordPress.",
    ),
    4: (
        "Extracting WordPress files...",
        "WordPress files extracted successfully.",
        "Failed to extract WordPress files.",
    ),
    5: (
        "Retrieving WordPress security keys...",
        "WordPress security keys retrieved successfully.",
        "Failed to retrieve WordPress security keys.",
    ),
    6: (
        "Creating wp-config.php...",
        "wp-config.php created successfully.",
        "Failed to create wp-config.php.",
    ),
    7: (
        "Connecting to database server...",
        "Database connection established.",
        "Failed to connect to the database server.",
    ),
    8: (
        "Checking database...",
        "Database created successfully.",
        "Failed to check or create the database.",
    ),
    9: (
        "Creating the DNS A record in Arvan Cloud...",
        "DNS A record is ready.",
        "Failed to create the DNS A record.",
    ),
    10: (
        "Creating the Apache virtual host config...",
        "Apache virtual host config is ready.",
        "Failed to create the Apache virtual host config.",
    ),
    11: (
        "Enabling the site and reloading Apache...",
        "Apache site enabled and reloaded.",
        "Failed to enable the site or reload Apache.",
    ),
    12: (
        "Checking DNS, ping and HTTP access...",
        "DNS and HTTP checks passed.",
        "DNS or HTTP check failed.",
    ),
    13: (
        "Requesting the SSL certificate with Certbot...",
        "SSL certificate obtained.",
        "Failed to obtain the SSL certificate.",
    ),
    14: (
        "Verifying the SSL configuration and certificate...",
        "SSL configuration and certificate are valid.",
        "SSL verification failed.",
    ),
    15: (
        "Checking HTTPS and the HTTP to HTTPS redirect...",
        "HTTPS works and HTTP redirects to it.",
        "HTTPS or redirect check failed.",
    ),
    16: (
        "Enabling the Arvan Cloud proxy...",
        "Arvan Cloud proxy enabled.",
        "Failed to enable the Arvan Cloud proxy.",
    ),
    17: (
        "Checking the final response through Arvan Cloud...",
        "Site is served through Arvan Cloud.",
        "Final Arvan Cloud check failed.",
    ),
}


def is_windows() -> bool:
    return os.name == "nt"


# ======================================================================
# Console output: timestamps, colors and the log_* helpers
# ======================================================================
_ANSI_RESET = "\x1b[0m"
_LEVEL_COLORS = {
    "DEBUG": "\x1b[90m",  # gray
    "INFO": "\x1b[94m",  # blue
    "SUCCESS": "\x1b[92m",  # green
    "WARNING": "\x1b[93m",  # yellow
    "ERROR": "\x1b[91m",  # red
    "STEP": "\x1b[96m",  # cyan
}
_ANSI_SEQUENCE_RE = re.compile(r"\x1b\[[0-9;]*m")
_ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
_MIN_SECRET_LENGTH = 8  # shorter values would also match ordinary words


@lru_cache(maxsize=1)
def _enable_windows_ansi() -> bool:
    """Turn on ANSI escape processing for the Windows console (Windows 10 and newer)."""
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        if mode.value & _ENABLE_VIRTUAL_TERMINAL_PROCESSING:
            return True
        return bool(kernel32.SetConsoleMode(handle, mode.value | _ENABLE_VIRTUAL_TERMINAL_PROCESSING))
    except Exception:  # noqa: BLE001 - no ctypes or no real console: simply no colors
        return False


def supports_color(stream: Any) -> bool:
    """Decide whether ANSI colors may be written to `stream`."""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR", "0") not in ("", "0"):
        if is_windows():
            _enable_windows_ansi()  # best effort
        return True
    isatty = getattr(stream, "isatty", None)
    if not callable(isatty) or not isatty():
        return False  # redirected to a file or a pipe
    if os.environ.get("TERM") == "dumb":
        return False
    if is_windows():
        return _enable_windows_ansi()
    return True


class Console:
    """Timestamped console output, colored when the terminal supports it."""

    def __init__(self) -> None:
        self.verbose = False
        self._secrets: List[str] = []

    # -- secret protection ---------------------------------------------
    def add_secret(self, value: str) -> None:
        """Never print `value`: it is replaced by *** in every message."""
        if len(value) >= _MIN_SECRET_LENGTH and value not in self._secrets:
            self._secrets.append(value)

    def clear_secrets(self) -> None:
        self._secrets.clear()

    def _redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return text

    # -- formatting ----------------------------------------------------
    @staticmethod
    def _timestamp() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def format_line(self, level: str, text: str, color: bool, timestamp: Optional[str] = None) -> str:
        """One output line: [timestamp] [LEVEL] text, wrapped in color + reset if `color`."""
        line = f"[{timestamp or self._timestamp()}] [{level}] {text}"
        return f"{_LEVEL_COLORS[level]}{line}{_ANSI_RESET}" if color else line

    # -- output --------------------------------------------------------
    def emit(self, level: str, message: Any) -> None:
        if level == "DEBUG" and not self.verbose:
            return
        stream = sys.stdout  # looked up on every call so redirection always works
        color = supports_color(stream)
        stamp = self._timestamp()
        # Multi-line messages (e.g. tracebacks): every line gets its own timestamp and tag
        lines = self._redact(str(message)).splitlines() or [""]
        output = "\n".join(self.format_line(level, line, color, stamp) for line in lines) + "\n"
        try:
            stream.write(output)
        except UnicodeEncodeError:
            encoding = getattr(stream, "encoding", None) or "ascii"
            stream.write(output.encode(encoding, errors="replace").decode(encoding, errors="replace"))
        stream.flush()

    def ask(self, message: str) -> str:
        """Prompt the user, using the same [timestamp] [INFO] format as every other message."""
        color = supports_color(sys.stdout)
        prompt = self.format_line("INFO", self._redact(message), color) + " "
        if color and "readline" in sys.modules:
            # readline must be told which characters take no screen width
            prompt = _ANSI_SEQUENCE_RE.sub(lambda m: f"\x01{m.group(0)}\x02", prompt)
        return input(prompt)


_console = Console()


def log_info(message: Any) -> None:
    """General information (blue)."""
    _console.emit("INFO", message)


def log_success(message: Any) -> None:
    """An operation finished successfully (green). Call it only after success is confirmed."""
    _console.emit("SUCCESS", message)


def log_warning(message: Any) -> None:
    """Something needs attention but did not stop the run (yellow)."""
    _console.emit("WARNING", message)


def log_error(message: Any) -> None:
    """An operation failed (red)."""
    _console.emit("ERROR", message)


def log_step(message: Any) -> None:
    """A new step is starting (cyan)."""
    _console.emit("STEP", message)


def log_debug(message: Any) -> None:
    """Troubleshooting details (gray); printed only when running with -v."""
    _console.emit("DEBUG", message)


def ask_input(message: str) -> str:
    return _console.ask(message)


# ======================================================================
# Errors - each one has its own exit code
# ======================================================================
class InstallerError(Exception):
    """Base installer error; its message is safe to show to the user."""

    exit_code = 1


class InputValidationError(InstallerError):
    exit_code = 2


class BaseDirError(InstallerError):
    exit_code = 2


class TargetExistsError(InstallerError):
    exit_code = 3


class PathPermissionError(InstallerError):
    exit_code = 4


class DownloadError(InstallerError):
    exit_code = 5


class ExtractionError(InstallerError):
    exit_code = 6


class SecurityKeysError(InstallerError):
    exit_code = 7


class ConfigError(InstallerError):
    exit_code = 8


class DatabaseDriverError(InstallerError):
    exit_code = 9


class DatabaseConnectionError(InstallerError):
    exit_code = 10


class DatabaseExistsError(InstallerError):
    exit_code = 11


class DatabaseCreateError(InstallerError):
    exit_code = 12

class ArvanConfigError(InstallerError):
    """The Arvan API token is missing or unusable (the message never contains the token)."""

    exit_code = 13

class ArvanApiError(InstallerError):
    """An Arvan API call failed (HTTP error reply, invalid reply or network error)."""

    exit_code = 14

    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code  # HTTP status of the reply, if there was one


class ArvanOutcomeUnknownError(ArvanApiError):
    """
    The request may or may not have been processed (timeout, interrupted connection,
    HTTP 5xx or an unreadable 2xx reply). Check the real state before repeating a POST.
    """

class DnsRecordError(InstallerError):
    """The DNS A record can not be created or conflicts with an existing record."""

    exit_code = 15

class ApacheConfigError(InstallerError):
    """The Apache virtual host config can not be created or checked."""

    exit_code = 16

class CommandError(InstallerError):
    """An external command could not be started or did not finish in time."""

    exit_code = 17

class SiteCheckError(InstallerError):
    """A DNS or HTTP check of the new site failed."""

    exit_code = 18

class CertbotError(InstallerError):
    """The SSL certificate could not be obtained or installed with Certbot."""

    exit_code = 19

class SslCheckError(InstallerError):
    """The installed SSL certificate or the port 443 virtual host is not correct."""

    exit_code = 20

# ======================================================================
# Data model
# ======================================================================
@dataclass
class InstallContext:
    """State of one run; used to clean up exactly what this run created."""

    site_name: str = ""
    base_dir: Optional[Path] = None
    target_dir: Optional[Path] = None
    temp_dir: Optional[Path] = None
    config_path: Optional[Path] = None
    created_files: List[Path] = field(default_factory=list)
    created_dirs: List[Path] = field(default_factory=list)
    extracted_files: int = 0
    extracted_dirs: int = 0
    db_connection: Optional[Any] = field(default=None, repr=False)  # closed in run_installer's finally
    db_created_name: Optional[str] = None  # set only when THIS run created the database
    server_steps_enabled: bool = False  # True when the DNS / Apache / SSL steps (9-17) are part of this run
    server_phase_started: bool = False  # True once step 9 began: nothing is rolled back after that
    dns_record: Optional[DnsRecordResult] = None
    apache_conf: Optional[ApacheConfigResult] = None
    apache_enabled_now: Optional[bool] = None
    ssl_result: Optional[CertbotResult] = None
    cloud_result: Optional[ArvanCloudResult] = None


@dataclass(frozen=True)
class DbSettings:
    name: str
    user: str = DB_USER
    password: str = field(default=DB_PASSWORD, repr=False)  # never shown in logs / repr
    host: str = DB_HOST
    port: int = DB_PORT
    charset: str = DB_CHARSET
    collate: str = DB_COLLATE
    table_prefix: str = TABLE_PREFIX


@dataclass
class ExtractStats:
    files: int = 0
    dirs: int = 0
    bytes_written: int = 0


# ======================================================================
# Step 1: site name
# ======================================================================
def validate_site_name(raw: str) -> str:
    """Validate and normalize (lowercase) the site name."""
    name = raw.strip().lower()
    if not name:
        raise InputValidationError("Site name must not be empty.")
    if "/" in name or "\\" in name or ".." in name:
        raise InputValidationError(
            "Site name must not contain a path, a path separator (/ or \\) or '..'."
        )
    if "." in name:
        raise InputValidationError(
            f"Site name must not contain a dot; the '{SITE_FOLDER_SUFFIX}' suffix is "
            f"added automatically (e.g. enter 'test')."
        )
    if len(name) > MAX_SITE_NAME_LENGTH:
        raise InputValidationError(
            f"Site name must not be longer than {MAX_SITE_NAME_LENGTH} characters "
            f"(MySQL database name length limit)."
        )
    if not SITE_NAME_RE.fullmatch(name):
        raise InputValidationError(
            "Site name may only contain English letters, digits and hyphens (-), "
            "and must not start or end with a hyphen."
        )
    if name in WINDOWS_RESERVED_NAMES:
        raise InputValidationError(f"'{name}' is a reserved name on Windows; please choose another name.")
    return name


def prompt_site_name(max_attempts: int = 3) -> str:
    """Ask for the site name interactively; ask again a few times if it is invalid."""
    last_error = "No valid input received."
    for _ in range(max_attempts):
        try:
            raw = ask_input("Enter the site name (e.g. test):")
        except EOFError as exc:
            raise InputValidationError(
                "No input received (stdin is closed). Use the --name option instead."
            ) from exc
        try:
            return validate_site_name(raw)
        except InputValidationError as exc:
            last_error = str(exc)
            log_error(f"Invalid input: {exc}")
    raise InputValidationError(f"{last_error} (too many attempts)")


def obtain_site_name(cli_name: Optional[str]) -> str:
    if cli_name is not None:
        name = validate_site_name(cli_name)
        raw = cli_name.strip()
    else:
        name = prompt_site_name()
        raw = name
    if raw != name:
        log_info(f"Site name was converted to lowercase: {name}")
    log_info(f"Site name: {name}  ->  folder: {name}{SITE_FOLDER_SUFFIX}")
    return name


# ======================================================================
# Step 2: path and target folder
# ======================================================================
def resolve_base_dir(cli_value: Optional[str], interactive: Optional[bool] = None) -> Path:
    """
    Base directory selection order:
      --base-dir  >  WP_INSTALLER_BASE_DIR environment variable  >  OS default
    On Windows, if no value was given and the terminal is interactive, the
    path is requested from the user.
    """
    if interactive is None:
        interactive = sys.stdin is not None and sys.stdin.isatty()

    raw = cli_value or os.environ.get(BASE_DIR_ENV_VAR)
    if raw:
        base = Path(raw.strip().strip("\"'"))
    elif is_windows():
        base = WINDOWS_DEFAULT_BASE_DIR
        if interactive:
            try:
                answer = ask_input(f"Base install directory [{base}] (press Enter for the default):")
            except EOFError:
                answer = ""
            answer = answer.strip().strip("\"'")
            if answer:
                base = Path(answer)
    else:
        base = LINUX_BASE_DIR

    base = Path(os.path.expanduser(str(base)))
    if not base.is_absolute():
        raise BaseDirError(f"Base directory must be an absolute path: {base}")
    if not base.exists():
        raise BaseDirError(f"Base directory does not exist: {base}")
    if not base.is_dir():
        raise BaseDirError(f"Base directory is not a folder: {base}")
    return base.resolve()


def build_target_path(base_dir: Path, site_name: str) -> Path:
    folder_name = f"{site_name}{SITE_FOLDER_SUFFIX}"
    target = base_dir / folder_name
    # Second line of defense: the target must be exactly one direct child of the base directory
    if target.parent != base_dir or target.name != folder_name:
        raise InputValidationError(f"Invalid target path: {target}")
    return target


def _permission_hint() -> str:
    if is_windows():
        return "Run the script with \"Run as administrator\" or check the folder permissions."
    return "Run the script with sudo or check the ownership/permissions of the base directory."


def create_target_dir(target: Path, ctx: InstallContext) -> None:
    """Create the target folder; if it already exists, change nothing."""
    if os.path.lexists(target):
        raise TargetExistsError(
            f"Target folder already exists and was left untouched: {target}"
        )
    try:
        target.mkdir(parents=False, exist_ok=False)  # atomic; never overwrites, even in a race
    except FileExistsError as exc:
        raise TargetExistsError(
            f"Target folder already exists and was left untouched: {target}"
        ) from exc
    except PermissionError as exc:
        raise PathPermissionError(
            f"Permission denied while creating the folder: {target}. {_permission_hint()}"
        ) from exc
    except OSError as exc:
        raise InstallerError(f"Could not create the target folder ({target}): {exc}") from exc
    ctx.created_dirs.append(target)


# ======================================================================
# HTTP helpers
# ======================================================================
@contextmanager
def _open_https(url: str, timeout: float, error_cls: type) -> Iterator[Any]:
    """
    Open a safe GET request (timeout, HTTPS only after redirects, HTTP 200 only)
    and convert every network error into `error_cls` with a clear message.
    """
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if not response.geturl().lower().startswith("https://"):
                raise error_cls(
                    "The server redirected to a non-HTTPS URL; aborting for security reasons."
                )
            status = response.getcode()
            if status != 200:
                raise error_cls(f"Unexpected HTTP status: {status}")
            yield response
    except error_cls:
        raise
    except urllib.error.HTTPError as exc:
        raise error_cls(f"Server responded with HTTP {exc.code} ({exc.reason}). URL: {url}") from exc
    except urllib.error.URLError as exc:
        raise error_cls(
            f"Could not connect to the server: {exc.reason}. "
            f"Check your internet connection, DNS, firewall or proxy. URL: {url}"
        ) from exc
    except (socket.timeout, TimeoutError) as exc:
        raise error_cls(f"Timed out after {timeout:g} seconds. URL: {url}") from exc
    except http.client.HTTPException as exc:
        raise error_cls(f"Connection was interrupted or the response was incomplete: {exc!r}") from exc
    except OSError as exc:
        raise error_cls(f"Network error: {exc}") from exc


def _parse_content_length(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _fmt_size(num_bytes: int) -> str:
    return f"{num_bytes / (1024 * 1024):.1f} MB"


# ======================================================================
# Step 3: download
# ======================================================================
def create_temp_dir() -> Path:
    try:
        return Path(tempfile.mkdtemp(prefix="wp_installer_"))  # mode 0700 on Linux
    except OSError as exc:
        raise InstallerError(f"Could not create a temporary folder: {exc}") from exc


def download_file(
    url: str,
    dest: Path,
    *,
    timeout: float = HTTP_TIMEOUT_SECONDS,
    max_bytes: int = MAX_DOWNLOAD_BYTES,
) -> int:
    """
    Stream the file into `dest` in chunks and return the number of bytes received.
    An incomplete download (fewer bytes than Content-Length) or an empty one is a failure.
    If the server sends no Content-Length, completeness is checked in the next
    step by validating the ZIP archive.
    """
    received = 0
    with _open_https(url, timeout, DownloadError) as response:
        expected = _parse_content_length(response.headers.get("Content-Length"))
        if expected is not None and expected > max_bytes:
            raise DownloadError(
                f"File size ({_fmt_size(expected)}) exceeds the allowed limit ({_fmt_size(max_bytes)})."
            )
        try:
            output = open(dest, "wb")
        except OSError as exc:
            raise DownloadError(f"Could not create the temporary file ({dest}): {exc}") from exc
        with output:
            while True:
                chunk = response.read(DOWNLOAD_CHUNK_SIZE)  # network errors reach _open_https from here
                if not chunk:
                    break
                received += len(chunk)
                if received > max_bytes:
                    raise DownloadError(f"Received data exceeds the allowed limit ({_fmt_size(max_bytes)}).")
                try:
                    output.write(chunk)
                except OSError as exc:
                    raise DownloadError(
                        f"Could not write to the temporary file (is there enough disk space?): {exc}"
                    ) from exc
        if received == 0:
            raise DownloadError("The downloaded file is empty.")
        if expected is not None and received != expected:
            raise DownloadError(
                f"Incomplete download: received {received} of {expected} bytes."
            )
    return received


# ======================================================================
# Step 4: safe ZIP extraction
# ======================================================================
def validate_zip_file(zip_path: Path) -> None:
    try:
        size = zip_path.stat().st_size
    except OSError as exc:
        raise ExtractionError(f"ZIP file is not accessible: {exc}") from exc
    if size == 0:
        raise ExtractionError("ZIP file is empty.")
    if not zipfile.is_zipfile(zip_path):
        raise ExtractionError("The downloaded file is not a valid ZIP archive (it may be incomplete or corrupted).")


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _split_member_path(name: str) -> List[str]:
    """Split a ZIP member name into safe components; raise if it is unsafe."""
    normalized = name.replace("\\", "/")
    if "\x00" in normalized:
        raise ExtractionError(f"Unsafe name in ZIP (NUL character): {name!r}")
    parts = normalized.split("/")
    if parts and parts[-1] == "":  # directories end with "/"
        parts.pop()
    if not parts:
        raise ExtractionError(f"Empty name in ZIP: {name!r}")
    for part in parts:
        if part in ("", ".", ".."):
            raise ExtractionError(f"Unsafe path in ZIP (path traversal or absolute path): {name!r}")
        if ":" in part:
            raise ExtractionError(f"Unsafe path in ZIP (drive letter or alternate data stream): {name!r}")
    return parts


def _preflight_zip(infos: Sequence[zipfile.ZipInfo]) -> None:
    """Check the whole ZIP listing before a single byte is written."""
    if not infos:
        raise ExtractionError("ZIP file is empty.")
    if len(infos) > MAX_ZIP_ENTRIES:
        raise ExtractionError(f"ZIP entry count ({len(infos)}) exceeds the allowed limit ({MAX_ZIP_ENTRIES}).")
    total = 0
    for info in infos:
        parts = _split_member_path(info.filename)
        if parts[0] != ZIP_ROOT_DIR:
            raise ExtractionError(
                f"Unexpected ZIP layout: every entry must be inside the '{ZIP_ROOT_DIR}' folder: "
                f"{info.filename!r}"
            )
        if not info.is_dir() and len(parts) < 2:
            raise ExtractionError(f"Unexpected ZIP layout: {info.filename!r}")
        if info.create_system == 3 and stat.S_ISLNK(info.external_attr >> 16):
            raise ExtractionError(f"Symbolic links are not allowed in the ZIP: {info.filename!r}")
        total += info.file_size
    if total > MAX_UNCOMPRESSED_BYTES:
        raise ExtractionError(
            f"Uncompressed size ({_fmt_size(total)}) exceeds the allowed limit ({_fmt_size(MAX_UNCOMPRESSED_BYTES)})."
        )


def _ensure_dirs(path: Path, root: Path, ctx: InstallContext) -> None:
    """Create the missing folders of a path and record only the new ones for rollback."""
    if not _is_within(path, root):
        raise ExtractionError(f"Unsafe path during extraction: {path}")
    missing: List[Path] = []
    current = path
    while current != root and not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir()
        ctx.created_dirs.append(directory)


def _extract_member(
    zf: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    root: Path,
    ctx: InstallContext,
    stats: ExtractStats,
) -> None:
    parts = _split_member_path(info.filename)
    relative = parts[1:]  # drop the "wordpress" folder so the content lands directly in the target
    if not relative:
        return
    destination = root.joinpath(*relative)
    if not _is_within(destination, root) or destination == root:
        raise ExtractionError(f"Unsafe path in ZIP: {info.filename!r}")

    if info.is_dir():
        _ensure_dirs(destination, root, ctx)
        stats.dirs += 1
        return

    _ensure_dirs(destination.parent, root, ctx)
    written = 0
    # Mode "xb": fails if the file already exists, so nothing is ever overwritten
    with zf.open(info) as source, open(destination, "xb") as output:
        ctx.created_files.append(destination)
        while True:
            chunk = source.read(EXTRACT_CHUNK_SIZE)
            if not chunk:
                break
            written += len(chunk)
            stats.bytes_written += len(chunk)
            if written > info.file_size or stats.bytes_written > MAX_UNCOMPRESSED_BYTES:
                raise ExtractionError(
                    f"Actual file size does not match the size declared in the ZIP: {info.filename!r}"
                )
            output.write(chunk)
    stats.files += 1


def extract_zip_safely(zip_path: Path, target_dir: Path, ctx: InstallContext) -> ExtractStats:
    """
    Put the content of wordpress/ from the ZIP directly into target_dir.
    Extraction is manual (not extractall) so every path is checked before writing.
    """
    root = target_dir.resolve()
    stats = ExtractStats()
    try:
        with zipfile.ZipFile(zip_path) as zf:
            infos = zf.infolist()
            _preflight_zip(infos)
            corrupt = zf.testzip()
            if corrupt is not None:
                raise ExtractionError(f"Corrupted file inside the ZIP: {corrupt!r}")
            for info in infos:
                _extract_member(zf, info, root, ctx, stats)
    except zipfile.BadZipFile as exc:
        raise ExtractionError(f"ZIP file is corrupted: {exc}") from exc
    except (RuntimeError, NotImplementedError, zipfile.LargeZipFile, zlib.error, EOFError) as exc:
        raise ExtractionError(
            f"Cannot read the ZIP (encrypted, unsupported compression or corrupted data): {exc}"
        ) from exc
    except PermissionError as exc:
        raise PathPermissionError(
            f"Permission denied while writing to the target ({exc.filename}). {_permission_hint()}"
        ) from exc
    except OSError as exc:
        raise ExtractionError(f"File system error during extraction: {exc}") from exc
    ctx.extracted_files = stats.files
    ctx.extracted_dirs = stats.dirs
    return stats


def verify_wordpress_layout(target_dir: Path) -> None:
    missing = [name for name in REQUIRED_WP_ITEMS if not (target_dir / name).exists()]
    if missing:
        raise ExtractionError(
            "WordPress layout is incomplete after extraction; missing: " + ", ".join(missing)
        )


# ======================================================================
# Step 5: security keys
# ======================================================================
def parse_security_keys(text: str) -> str:
    """
    Strictly validate the API response: exactly eight define() lines with the
    known names, each once, with values that contain no quote or backslash.
    Anything else is rejected (prevents PHP code injection). The values are
    re-emitted unchanged in the standard format. The response content never
    appears in error messages or logs.
    """
    found: Dict[str, str] = {}
    for line_no, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        match = _SALT_LINE_RE.fullmatch(line)
        if match is None or match.group("name") not in SALT_KEYS:
            raise SecurityKeysError(f"Invalid security keys API response format (line {line_no}).")
        name = match.group("name")
        if name in found:
            raise SecurityKeysError(f"Key {name} is duplicated in the API response.")
        found[name] = match.group("value")
    missing = [key for key in SALT_KEYS if key not in found]
    if missing:
        raise SecurityKeysError("Incomplete API response; missing keys: " + ", ".join(missing))
    return "\n".join(f"define('{key}', '{found[key]}');" for key in SALT_KEYS)


def fetch_security_keys(
    url: str = WP_SALT_API_URL, timeout: float = HTTP_TIMEOUT_SECONDS
) -> str:
    """Call the API once and return the block of define() lines (values are never logged)."""
    with _open_https(url, timeout, SecurityKeysError) as response:
        raw = response.read(MAX_SALT_RESPONSE_BYTES + 1)
    if not raw:
        raise SecurityKeysError("The security keys API response was empty.")
    if len(raw) > MAX_SALT_RESPONSE_BYTES:
        raise SecurityKeysError("The security keys API response is too large.")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise SecurityKeysError("The security keys API response is not valid UTF-8.") from None
    return parse_security_keys(text)


def _register_key_secrets(keys_block: str) -> None:
    """
    Safety net: even if some future code path tried to print a key or salt,
    the console would replace it with ***. (The database password is not
    registered because the script never prints it, and the word would also be
    masked inside ordinary sentences.)
    """
    for line in keys_block.splitlines():
        match = _SALT_LINE_RE.fullmatch(line.strip())
        if match:
            _console.add_secret(match.group("value"))


# ======================================================================
# Step 6: wp-config.php
# ======================================================================
def build_db_settings(site_name: str) -> DbSettings:
    return DbSettings(name=f"{site_name}{DB_NAME_SUFFIX}")


def php_quote(value: str) -> str:
    """Convert a value into a safe single-quoted PHP string."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def render_wp_config(db: DbSettings, keys_block: str) -> str:
    lines = [
        "<?php",
        "/**",
        " * wp-config.php - generated by wp_installer.py",
        " *",
        " * WARNING: DB_USER and DB_PASSWORD below are TEMPORARY TEST values.",
        " * Change them before using this site in production.",
        " */",
        "",
        "// ** Database settings ** //",
        f"define('DB_NAME', {php_quote(db.name)});",
        f"define('DB_USER', {php_quote(db.user)});",
        f"define('DB_PASSWORD', {php_quote(db.password)});",
        f"define('DB_HOST', {php_quote(db.host)});",
        f"define('DB_CHARSET', {php_quote(db.charset)});",
        f"define('DB_COLLATE', {php_quote(db.collate)});",
        "",
        "// ** Authentication unique keys and salts ** //",
        keys_block,
        "",
        "// ** Table prefix ** //",
        f"$table_prefix = {php_quote(db.table_prefix)};",
        "",
        "// ** Debugging ** //",
        "define('WP_DEBUG', false);",
        "",
        "// ** Absolute path to the WordPress directory ** //",
        "if (!defined('ABSPATH')) {",
        "    define('ABSPATH', __DIR__ . '/');",
        "}",
        "",
        "// ** Sets up WordPress vars and included files ** //",
        "require_once ABSPATH . 'wp-settings.php';",
        "",
    ]
    return "\n".join(lines)


def validate_wp_config(content: str) -> None:
    """Before writing: every definition exactly once and the file must be complete."""
    if not content.startswith("<?php"):
        raise ConfigError("wp-config.php content does not start with <?php.")
    singles = ("DB_NAME", "DB_USER", "DB_PASSWORD", "DB_HOST", "DB_CHARSET", "DB_COLLATE", "WP_DEBUG", "ABSPATH")
    for const in singles + SALT_KEYS:
        count = content.count(f"define('{const}',")
        if count != 1:
            raise ConfigError(f"{const} must be defined exactly once (found: {count}).")
    if content.count("$table_prefix") != 1:
        raise ConfigError("$table_prefix must be defined exactly once.")
    if content.count("require_once ABSPATH . 'wp-settings.php';") != 1:
        raise ConfigError("The require_once line for wp-settings.php is missing or duplicated.")
    if not content.rstrip().endswith("require_once ABSPATH . 'wp-settings.php';"):
        raise ConfigError("wp-config.php must end with the require_once of wp-settings.php.")


def write_wp_config(target_dir: Path, content: str, ctx: InstallContext) -> Path:
    config_path = target_dir / "wp-config.php"
    try:
        # UTF-8 without BOM (a BOM makes PHP emit unwanted output) and LF line endings on both OSes
        with open(config_path, "x", encoding="utf-8", newline="\n") as handle:
            ctx.created_files.append(config_path)
            handle.write(content)
    except FileExistsError as exc:
        raise ConfigError(f"wp-config.php already exists and will not be overwritten: {config_path}") from exc
    except PermissionError as exc:
        raise PathPermissionError(
            f"Permission denied while writing wp-config.php: {config_path}. {_permission_hint()}"
        ) from exc
    except OSError as exc:
        raise ConfigError(f"Could not write wp-config.php: {exc}") from exc
    return config_path


# ======================================================================
# Step 7: database server connection
# ======================================================================
def validate_db_name(name: str) -> str:
    """Strict whitelist check of the database name (<site name>_wp); runs before any SQL is built."""
    if not name.endswith(DB_NAME_SUFFIX):
        raise InputValidationError(f"Database name must end with '{DB_NAME_SUFFIX}': {name!r}")
    if len(name) > MYSQL_MAX_DB_NAME_LENGTH:
        raise InputValidationError(
            f"Database name must not be longer than {MYSQL_MAX_DB_NAME_LENGTH} characters: {name!r}"
        )
    if not _DB_NAME_RE.fullmatch(name):
        raise InputValidationError(
            f"Database name may only contain lowercase English letters, digits, hyphens and underscores: {name!r}"
        )
    return name


def quote_sql_identifier(name: str) -> str:
    """Quote a validated database name with backticks (an identifier cannot be an SQL parameter)."""
    validate_db_name(name)
    return "`" + name.replace("`", "``") + "`"


def _require_sql_word(value: str, label: str) -> str:
    """Character set / collation names are put into the SQL text, so only plain words are accepted."""
    if not _SQL_WORD_RE.fullmatch(value):
        raise InputValidationError(
            f"Invalid {label} setting: {value!r} (only lowercase letters, digits and underscores are allowed)."
        )
    return value


def validate_db_settings(db: DbSettings) -> None:
    """Reject invalid settings BEFORE any connection is made or any SQL is built."""
    validate_db_name(db.name)
    if not db.host.strip():
        raise InputValidationError("Database host must not be empty.")
    if not db.user:
        raise InputValidationError("Database username must not be empty.")
    if not isinstance(db.port, int) or isinstance(db.port, bool) or not 1 <= db.port <= 65535:
        raise InputValidationError(f"Database port must be a number between 1 and 65535 (got: {db.port!r}).")
    _require_sql_word(db.charset, "character set")
    if db.collate:
        _require_sql_word(db.collate, "collation")


def load_db_driver() -> Any:
    """Import PyMySQL only when it is needed, so the earlier steps never depend on it."""
    try:
        import pymysql
    except ImportError as exc:
        raise DatabaseDriverError(
            f"The database driver '{DB_DRIVER_PIP_NAME}' is not installed. "
            f"Install it with:  {sys.executable} -m pip install {DB_DRIVER_PIP_NAME}"
        ) from exc
    return pymysql


def _db_error_code(exc: BaseException) -> Optional[int]:
    """Numeric MySQL / MariaDB error code of a driver exception, if it has one."""
    if exc.args and isinstance(exc.args[0], int):
        return exc.args[0]
    return None


def _db_error_message(exc: BaseException, password: str) -> str:
    """Server message of a driver error with the password masked (the server never sends it, but be safe)."""
    text = str(exc.args[1]) if len(exc.args) > 1 else str(exc)
    if password:
        keep = "using password:"  # the standard "(using password: YES)" wording is not a leak
        text = text.replace(keep, "\0").replace(password, "***").replace("\0", keep)
    return text


def _connection_error(exc: BaseException, db: DbSettings) -> InstallerError:
    """Translate a driver error raised while connecting into a clear, secret-free message."""
    code = _db_error_code(exc)
    text = _db_error_message(exc, db.password)
    log_debug(f"Driver error ({type(exc).__name__}): {text}")
    where = f"{db.host}:{db.port}"
    if code == 1045:
        return DatabaseConnectionError(
            f"Authentication failed for user '{db.user}' on {where} (wrong username or password). "
            f"Check DB_USER and DB_PASSWORD in the settings section."
        )
    if code == 1130:
        return DatabaseConnectionError(
            f"The server at {where} does not allow user '{db.user}' to connect from this machine."
        )
    if code in (1251, 2059):
        return DatabaseDriverError(
            f"The driver cannot use the authentication method required by the server at {where}. "
            f"Try:  {sys.executable} -m pip install --upgrade {DB_DRIVER_PIP_NAME} cryptography"
        )
    if "timed out" in text.lower():
        return DatabaseConnectionError(
            f"Timed out after {DB_CONNECT_TIMEOUT_SECONDS:g} seconds while connecting to {where}. "
            f"Check that the server is running and reachable (firewall, host and port)."
        )
    if code == 2005:
        return DatabaseConnectionError(f"Could not resolve the database host name '{db.host}'. Check DB_HOST.")
    if code in (2002, 2003):
        return DatabaseConnectionError(
            f"Could not connect to the database server at {where}. The server may not be running, "
            f"the port may be wrong (DB_PORT) or a firewall may be blocking the connection."
        )
    if code == 1040:
        return DatabaseConnectionError(f"The database server at {where} has too many open connections.")
    suffix = f" (error {code})" if code is not None else ""
    return DatabaseConnectionError(
        f"Could not connect to the database server at {where}{suffix}: {text}. "
        f"Check DB_HOST and DB_PORT, and make sure a MySQL / MariaDB server is listening there."
    )


def connect_to_database(db: DbSettings, ctx: InstallContext) -> None:
    """
    Connect with the configured settings (with timeouts) and keep the connection in
    ctx.db_connection; run_installer's finally block closes it. Error messages
    never contain the password.
    """
    pymysql = load_db_driver()
    where = f"{db.host}:{db.port}"
    try:
        connection = pymysql.connect(
            host=db.host,
            port=db.port,
            user=db.user,
            password=db.password,
            connect_timeout=DB_CONNECT_TIMEOUT_SECONDS,
            read_timeout=DB_OPERATION_TIMEOUT_SECONDS,
            write_timeout=DB_OPERATION_TIMEOUT_SECONDS,
            autocommit=True,
        )
    except pymysql.err.MySQLError as exc:
        raise _connection_error(exc, db) from None  # the redacted details were logged as DEBUG
    except RuntimeError as exc:
        if "cryptography" not in str(exc):
            raise
        raise DatabaseDriverError(
            "The server requires an authentication method (caching_sha2_password / sha256_password) "
            f"that needs the 'cryptography' package. Install it with:  {sys.executable} -m pip install cryptography"
        ) from exc
    except OSError as exc:  # also covers socket timeouts
        raise DatabaseConnectionError(f"Network error while connecting to {where}: {exc}") from exc
    ctx.db_connection = connection
    log_info(f"Connected to {where} as '{db.user}' (server version: {connection.get_server_info()}).")


def close_database_connection(ctx: InstallContext) -> None:
    """Close the connection if one is open; called from run_installer's finally block."""
    connection, ctx.db_connection = ctx.db_connection, None
    if connection is None:
        return
    try:
        connection.close()
    except Exception as exc:  # noqa: BLE001 - a broken connection must not hide the real result
        log_warning(f"Could not close the database connection cleanly ({type(exc).__name__}).")
    else:
        log_info("Database connection closed.")


# ======================================================================
# Step 8: check for the database and create it
# ======================================================================
def _existing_database_error(name: str) -> DatabaseExistsError:
    """WARNING for an already existing database; the caller raises the returned error to stop the run."""
    log_warning(
        f"Database {name} already exists. It was left untouched: nothing was dropped or overwritten, "
        f"and the steps that depend on creating a new database were stopped."
    )
    return DatabaseExistsError(
        f"Database {name} already exists. Use a different site name, or remove / rename the existing "
        f"database manually if you really want to reuse the name."
    )


def _database_operation_error(exc: BaseException, db: DbSettings, name: str) -> InstallerError:
    """Translate a driver error raised while checking / creating the database."""
    code = _db_error_code(exc)
    text = _db_error_message(exc, db.password)
    log_debug(f"Driver error ({type(exc).__name__}): {text}")
    if code == 1007:  # created by someone else (or not visible to this user) between check and create
        return _existing_database_error(name)
    if code in (1044, 1142, 1227):
        return DatabaseCreateError(
            f"User '{db.user}' is not allowed to create the database {name} (error {code}). "
            f"Grant it the CREATE privilege (on {name} or on *.*), or set another account in DB_USER / "
            f"DB_PASSWORD. Privileges are not changed by this script."
        )
    if code in (2006, 2013) or "timed out" in text.lower():
        return DatabaseCreateError(
            f"Lost the connection to the database server (operation timeout: "
            f"{DB_OPERATION_TIMEOUT_SECONDS:g} seconds). The database may or may not have been created; "
            f"please check the server."
        )
    suffix = f" (error {code})" if code is not None else ""
    return DatabaseCreateError(f"Could not check or create the database {name}{suffix}: {text}")


def database_exists(cursor: Any, name: str) -> bool:
    """
    Exact-name lookup with a bound parameter. (SHOW DATABASES LIKE is avoided on purpose:
    "_" is a wildcard in LIKE, so test_wp would also match testXwp.)
    """
    cursor.execute("SELECT 1 FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = %s", (name,))
    return cursor.fetchone() is not None


def select_collation(cursor: Any, db: DbSettings) -> str:
    """Pick the collation: DB_COLLATE if set, otherwise the first supported entry of DB_COLLATE_PREFERENCE."""
    cursor.execute(
        "SELECT COLLATION_NAME FROM information_schema.COLLATIONS WHERE CHARACTER_SET_NAME = %s",
        (db.charset,),
    )
    available = {str(row[0]).lower() for row in cursor.fetchall()}
    if not available:
        raise DatabaseCreateError(
            f"The database server does not support the '{db.charset}' character set "
            f"(WordPress needs utf8mb4: MySQL 5.5.3+ / MariaDB 5.5+)."
        )
    if db.collate:
        if db.collate.lower() not in available:
            raise DatabaseCreateError(f"The collation '{db.collate}' set in DB_COLLATE is not supported by this server.")
        return _require_sql_word(db.collate.lower(), "collation")
    for candidate in DB_COLLATE_PREFERENCE:
        if candidate in available:
            return _require_sql_word(candidate, "collation")
    raise DatabaseCreateError(
        "None of the preferred collations is supported by this server: " + ", ".join(DB_COLLATE_PREFERENCE)
    )


def check_and_create_database(ctx: InstallContext, db: DbSettings) -> None:
    """
    Create the site database if it does not exist. An existing database is never
    touched (no DROP, no table is changed): a WARNING is logged and the run stops.
    CREATE DATABASE is deliberately sent without IF NOT EXISTS, so a race can never
    be mistaken for success. Whether the account may create databases is decided by
    the server itself (the real CREATE DATABASE), and its refusal is reported clearly.
    """
    connection = ctx.db_connection
    if connection is None:
        raise DatabaseConnectionError("There is no open database connection; the connection step must succeed first.")
    if ctx.db_created_name is not None:
        raise DatabaseCreateError(
            f"Database {ctx.db_created_name} was already created in this run; it will not be created again."
        )
    name = validate_db_name(db.name)
    charset = _require_sql_word(db.charset, "character set")
    pymysql = load_db_driver()
    log_info(f"Database name: {name}")
    try:
        with connection.cursor() as cursor:  # the cursor is always closed, even on errors
            if database_exists(cursor, name):
                raise _existing_database_error(name)
            collation = select_collation(cursor, db)
            log_info("Database does not exist. Creating database...")
            log_info(f"Character set: {charset}, collation: {collation}")
            cursor.execute(f"CREATE DATABASE {quote_sql_identifier(name)} CHARACTER SET {charset} COLLATE {collation}")
            ctx.db_created_name = name  # recorded only after the server accepted the statement
    except InstallerError:
        raise
    except pymysql.err.MySQLError as exc:
        raise _database_operation_error(exc, db, name) from None  # the redacted details were logged as DEBUG
    except OSError as exc:  # also covers socket timeouts
        raise DatabaseCreateError(f"Network error while talking to the database server: {exc}") from exc

# ======================================================================
# Arvan Cloud: token and site domain
# ======================================================================
def build_site_fqdn(site_name: str) -> str:
    """Full domain of a site: 'test' -> 'test.smabf.ir' (site_name is already validated)."""
    return f"{site_name}.{DNS_ZONE}"


def get_arvan_token() -> str:
    """
    Read the Arvan API token from the environment and register it with the console
    so it is replaced by *** in every log message. The token itself is never logged.
    """
    token = os.environ.get(ARVAN_TOKEN_ENV_VAR, "").strip()
    if not token:
        raise ArvanConfigError(
            f"Environment variable {ARVAN_TOKEN_ENV_VAR} is not set (or is empty). "
            f"Example: export {ARVAN_TOKEN_ENV_VAR}='<your Arvan API key>'"
        )
    if token.upper() in ARVAN_PLACEHOLDER_TOKENS:
        raise ArvanConfigError(
            f"{ARVAN_TOKEN_ENV_VAR} still contains the sample placeholder; set your real Arvan API key."
        )
    if any(ch.isspace() for ch in token) or not token.isascii():
        raise ArvanConfigError(
            f"{ARVAN_TOKEN_ENV_VAR} must contain only the key itself: no spaces, "
            f"no 'Apikey ' prefix and no non-ASCII characters."
        )
    if len(token) < _MIN_SECRET_LENGTH:
        raise ArvanConfigError(f"{ARVAN_TOKEN_ENV_VAR} is too short to be a valid API key.")
    _console.add_secret(token)
    return token

# ======================================================================
# Arvan Cloud: API client
# ======================================================================
class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never follow redirects, so the Authorization header can not be sent to another URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


_ARVAN_OPENER = urllib.request.build_opener(_NoRedirectHandler)  # TLS verification stays on (default)

# Errors that happen before any byte of the request was sent: the outcome is NOT unknown
_ARVAN_NOT_SENT_ERRORS = (socket.gaierror, ConnectionRefusedError, ssl.SSLError)

_ARVAN_STATUS_HINTS = {
    401: "authentication failed (the API key is invalid or expired)",
    403: "access denied (the API key has no permission for this operation)",
    404: "not found",
    422: "the request data was rejected as invalid",
    429: "rate limit reached, try again later",
}


def arvan_dns_records_path(record_id: Optional[str] = None) -> str:
    """API path of the zone's DNS records, or of a single record."""
    base = f"/domains/{DNS_ZONE}/dns-records"
    if record_id is None:
        return base
    record_id = str(record_id).strip()
    if not record_id:
        raise ArvanApiError("DNS record id must not be empty.")
    return f"{base}/{urllib.parse.quote(record_id, safe='')}"


def _arvan_error_detail(body: bytes) -> str:
    """Human-readable part of an Arvan error reply: 'message' plus the per-field 'errors'."""
    try:
        info = json.loads(body.decode("utf-8", errors="replace"))
    except ValueError:
        return ""
    if not isinstance(info, dict):
        return ""
    parts: List[str] = []
    message = info.get("message")
    if isinstance(message, str) and message.strip():
        parts.append(message.strip())
    errors = info.get("errors")
    if isinstance(errors, dict):
        for field_name, problems in errors.items():
            texts = problems if isinstance(problems, list) else [problems]
            parts.append(f"{field_name}: " + ", ".join(str(text) for text in texts))
    return "; ".join(parts)[:500]


def _arvan_http_error(method: str, path: str, exc: urllib.error.HTTPError) -> ArvanApiError:
    """Convert an HTTP error reply into an ArvanApiError (5xx: the outcome is unknown)."""
    try:
        body = exc.read(MAX_ARVAN_ERROR_BODY_BYTES)
    except (OSError, http.client.HTTPException):
        body = b""
    hint = _ARVAN_STATUS_HINTS.get(exc.code, "unexpected response")
    message = f"Arvan API {method} {path} failed: HTTP {exc.code} - {hint}"
    if exc.code == 429 and exc.headers is not None and exc.headers.get("Retry-After"):
        message += f" (Retry-After: {exc.headers.get('Retry-After')}s)"
    detail = _arvan_error_detail(body)
    if detail:
        message += f". Details: {detail}"
    error_cls = ArvanOutcomeUnknownError if exc.code >= 500 else ArvanApiError
    return error_cls(message, exc.code)


def arvan_request(
    method: str,
    path: str,
    token: str,
    payload: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, Any]] = None,
    timeout: float = ARVAN_HTTP_TIMEOUT_SECONDS,
) -> Any:
    """
    Call the Arvan CDN API over HTTPS and return the decoded JSON reply (None if empty).

    Only GET, POST and PUT are possible. Redirects are never followed and TLS
    verification is never disabled. The request is never repeated here: a failed
    POST must be re-checked by the caller first. ArvanOutcomeUnknownError means the
    request may have been processed. The token is never logged.
    """
    method = method.upper()
    if method not in ARVAN_ALLOWED_METHODS:
        raise ArvanApiError(f"HTTP method {method} is not allowed (this script never deletes DNS records).")
    if not ARVAN_API_BASE_URL.lower().startswith("https://"):
        raise ArvanApiError("The Arvan API base URL must use HTTPS.")
    if not path.startswith("/"):
        raise ArvanApiError(f"Invalid Arvan API path: {path}")
    url = ARVAN_API_BASE_URL + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {
        "Authorization": f"Apikey {token}",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    where = f"{method} {path}"
    unknown_hint = "The request may or may not have been processed; check the DNS state before repeating it."
    log_debug(f"Arvan API request: {where}")
    try:
        with _ARVAN_OPENER.open(request, timeout=timeout) as response:
            status = response.getcode()
            raw = response.read(MAX_ARVAN_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise _arvan_http_error(method, path, exc) from exc
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, (socket.timeout, TimeoutError)):
            raise ArvanOutcomeUnknownError(
                f"Arvan API {where} timed out after {timeout:g} seconds. {unknown_hint}"
            ) from exc
        if isinstance(reason, _ARVAN_NOT_SENT_ERRORS):
            raise ArvanApiError(f"Could not connect to the Arvan API ({where}): {reason}") from exc
        raise ArvanOutcomeUnknownError(
            f"Connection to the Arvan API failed during {where}: {reason}. {unknown_hint}"
        ) from exc
    except (socket.timeout, TimeoutError) as exc:
        raise ArvanOutcomeUnknownError(
            f"Arvan API {where} timed out after {timeout:g} seconds. {unknown_hint}"
        ) from exc
    except (http.client.HTTPException, ConnectionError) as exc:
        raise ArvanOutcomeUnknownError(
            f"Connection to the Arvan API was interrupted during {where}: {exc!r}. {unknown_hint}"
        ) from exc
    except OSError as exc:
        raise ArvanApiError(f"Network error while calling the Arvan API ({where}): {exc}") from exc

    log_debug(f"Arvan API response: {where} -> HTTP {status}")
    if len(raw) > MAX_ARVAN_RESPONSE_BYTES:
        raise ArvanOutcomeUnknownError(
            f"Reply of the Arvan API is too large ({where}, HTTP {status}). {unknown_hint}", status
        )
    if not raw.strip():
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ArvanOutcomeUnknownError(
            f"Arvan API returned an invalid (non-JSON) reply ({where}, HTTP {status}). {unknown_hint}", status
        ) from exc

# ======================================================================
# Arvan Cloud: DNS A record
# ======================================================================
@dataclass(frozen=True)
class DnsRecordResult:
    record: Dict[str, Any]  # the record as returned by Arvan (it contains the 'id')
    created: bool  # True: created by this run, False: it already existed


def arvan_list_dns_records(token: str) -> List[Dict[str, Any]]:
    """
    Read ALL DNS records of the zone, page by page (read-only). The list must be complete,
    otherwise an existing record could be missed and duplicated, so any doubt is an error.
    """
    records: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for page in range(1, ARVAN_MAX_DNS_PAGES + 1):
        reply = arvan_request(
            "GET",
            arvan_dns_records_path(),
            token,
            params={"page": page, "per_page": ARVAN_DNS_PAGE_SIZE},
        )
        items = reply.get("data") if isinstance(reply, dict) else None
        if not isinstance(items, list):
            raise ArvanApiError("Unexpected reply while listing DNS records (no 'data' list).")
        if not items:
            log_debug(f"DNS records: {len(records)} read in {page - 1} page(s)")
            return records
        fresh = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            key = str(item.get("id") or json.dumps(item, sort_keys=True))
            if key not in seen:
                seen.add(key)
                records.append(item)
                fresh += 1
        if fresh == 0:
            raise ArvanApiError(
                "The Arvan API repeated an earlier page of DNS records; the list can not be read completely."
            )
    raise ArvanApiError(
        f"The zone has more than {ARVAN_MAX_DNS_PAGES} pages of DNS records; "
        f"refusing to continue with an incomplete list."
    )


def _dns_record_ips(record: Dict[str, Any]) -> List[str]:
    value = record.get("value")
    if not isinstance(value, list):
        return []
    return [str(item.get("ip", "")).strip() for item in value if isinstance(item, dict)]


def _same_name_dns_records(records: List[Dict[str, Any]], site_name: str) -> List[Dict[str, Any]]:
    names = {site_name.lower(), build_site_fqdn(site_name).lower()}
    return [r for r in records if str(r.get("name", "")).strip().rstrip(".").lower() in names]


def check_dns_state(token: str, site_name: str) -> Optional[Dict[str, Any]]:
    """
    Read-only DNS check for <site_name>.<zone>. Returns the exact A record (one IP, SERVER_IP)
    if it exists and None if the name is free. A different A/CNAME/ANAME record under the same
    name raises DnsRecordError: such a record is never modified or removed by this script.
    """
    fqdn = build_site_fqdn(site_name)
    exact: List[Dict[str, Any]] = []
    conflicts: List[str] = []
    for record in _same_name_dns_records(arvan_list_dns_records(token), site_name):
        record_type = str(record.get("type", "")).lower()
        if record_type not in ("a", "cname", "aname"):
            continue  # other types (TXT, MX, ...) do not conflict with an A record
        ips = _dns_record_ips(record)
        if record_type == "a" and ips == [SERVER_IP]:
            exact.append(record)
        else:
            value = record.get("value")
            host = value.get("host") if isinstance(value, dict) else None
            shown = ", ".join(ips) or (str(host) if host else "-")
            conflicts.append(f"{record_type.upper()} (id {record.get('id', '?')}, value: {shown})")
    if conflicts:
        raise DnsRecordError(
            f"{fqdn} already has a different DNS record: {'; '.join(conflicts)}. Nothing was changed; "
            f"fix or remove it in the Arvan panel, or choose another site name."
        )
    if len(exact) > 1:
        log_warning(f"{len(exact)} identical A records exist for {fqdn}; using the first one (none are removed).")
    return exact[0] if exact else None


def build_dns_a_record_payload(site_name: str) -> Dict[str, Any]:
    """Body of the POST that creates the A record (the Arvan cloud proxy stays off for now)."""
    return {
        "type": "a",
        "name": site_name,
        "value": [{"ip": SERVER_IP, "port": None, "weight": 100, "country": ""}],
        "ttl": DNS_RECORD_TTL,
        "cloud": False,
        "upstream_https": "default",
        "ip_filter_mode": {"count": "single", "order": "none", "geo_filter": "none"},
    }


def _recheck_dns_state(token: str, site_name: str, problem: str) -> Optional[Dict[str, Any]]:
    """Re-read the DNS state after an unclear POST; if that fails too, stop (the POST is not repeated)."""
    try:
        return check_dns_state(token, site_name)
    except ArvanApiError as exc:
        raise DnsRecordError(
            f"{problem} The DNS state could not be verified ({exc}); the request was not repeated. "
            f"Check the Arvan panel."
        ) from exc


def ensure_dns_a_record(token: str, site_name: str) -> DnsRecordResult:
    """
    Make sure <site_name>.<zone> has the A record -> SERVER_IP, without ever creating a duplicate.
    An existing exact record is reused; a conflicting record stops the run (nothing is changed).
    After a timeout or unclear reply of the POST, the DNS state is re-checked before the POST
    is repeated (at most once). Nothing is deleted or rolled back.
    """
    fqdn = build_site_fqdn(site_name)
    existing = check_dns_state(token, site_name)
    if existing is not None:
        log_info(
            f"DNS A record {fqdn} -> {SERVER_IP} already exists (id {existing.get('id', '?')}); "
            f"not creating a duplicate."
        )
        return DnsRecordResult(existing, created=False)

    payload = build_dns_a_record_payload(site_name)
    reply: Any = None
    for attempt in range(1, DNS_POST_MAX_ATTEMPTS + 1):
        try:
            reply = arvan_request("POST", arvan_dns_records_path(), token, payload=payload)
            break
        except ArvanOutcomeUnknownError as exc:
            log_warning(f"Creating {fqdn} (attempt {attempt}): {exc}")
            time.sleep(DNS_RECHECK_DELAY_SECONDS)
            found = _recheck_dns_state(token, site_name, f"Creating {fqdn}: the request outcome is unclear.")
            if found is not None:
                log_success(f"DNS A record {fqdn} -> {SERVER_IP} exists (confirmed after an unclear reply).")
                return DnsRecordResult(found, created=True)
            if attempt == DNS_POST_MAX_ATTEMPTS:
                raise DnsRecordError(
                    f"Could not create {fqdn}: the request outcome stayed unclear and the record "
                    f"was not found afterwards. Check the Arvan panel."
                ) from exc
            log_warning(f"The record {fqdn} does not exist yet; repeating the request once.")
        except ArvanApiError as exc:
            raise DnsRecordError(f"Could not create DNS record {fqdn}: {exc}") from exc

    data = reply.get("data") if isinstance(reply, dict) else None
    if isinstance(data, dict) and data.get("id"):
        log_success(f"DNS A record created: {fqdn} -> {SERVER_IP} (id {data['id']}, Arvan cloud proxy off).")
        return DnsRecordResult(data, created=True)
    found = _recheck_dns_state(token, site_name, f"Creating {fqdn}: the reply contained no record id.")
    if found is None:
        raise DnsRecordError(f"Creating {fqdn} was accepted but the record was not found afterwards. Check the Arvan panel.")
    log_success(f"DNS A record {fqdn} -> {SERVER_IP} exists (id {found.get('id', '?')}).")
    return DnsRecordResult(found, created=True)

# ======================================================================
# Apache: virtual host config (Linux only)
# ======================================================================
@dataclass(frozen=True)
class ApacheConfigResult:
    conf_path: Path
    created: bool  # True: written by this run, False: an identical file already existed


def is_linux() -> bool:
    return sys.platform.startswith("linux")


def check_apache_prerequisites() -> None:
    """The Apache steps need Linux, root privileges and an Apache sites-available directory."""
    if not is_linux():
        raise ApacheConfigError("The Apache steps are supported on Linux only.")
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise ApacheConfigError(
            "The Apache steps need root privileges (writing to /etc/apache2). Run the script with sudo."
        )
    if not APACHE_SITES_AVAILABLE_DIR.is_dir():
        raise ApacheConfigError(f"{APACHE_SITES_AVAILABLE_DIR} was not found. Is Apache installed?")


def build_apache_vhost_config(site_name: str, document_root: Path) -> str:
    """Content of <site>.smabf.ir.conf. The DocumentRoot folder must be named exactly like the domain."""
    fqdn = build_site_fqdn(site_name)
    docroot = str(document_root)
    if not re.fullmatch(r"/[A-Za-z0-9_./-]+", docroot):
        raise ApacheConfigError(
            f"DocumentRoot must be an absolute path with only letters, digits and _ . / - : {docroot!r}"
        )
    if document_root.name != fqdn:
        raise ApacheConfigError(
            f"The install folder is named {document_root.name!r} but the DocumentRoot folder must be "
            f"named {fqdn!r} (see SITE_FOLDER_SUFFIX)."
        )
    return APACHE_VHOST_TEMPLATE.replace("@FQDN@", fqdn).replace("@DOCROOT@", docroot)


def create_apache_vhost_config(site_name: str, document_root: Path) -> ApacheConfigResult:
    """
    Create <sites-available>/<site>.smabf.ir.conf. An existing file is never overwritten:
    an identical one is reused, a different one stops the run. The file is written to a
    temporary file first and then linked into place, so a half-written config never exists.
    """
    content = build_apache_vhost_config(site_name, document_root)
    if not document_root.is_dir() or not (document_root / "wp-config.php").is_file():
        raise ApacheConfigError(
            f"DocumentRoot {document_root} does not contain an installed WordPress (wp-config.php is missing); "
            f"the Apache config was not created."
        )
    conf_name = f"{build_site_fqdn(site_name)}.conf"
    conf_path = APACHE_SITES_AVAILABLE_DIR / conf_name
    if not APACHE_SITES_AVAILABLE_DIR.is_dir():
        raise ApacheConfigError(f"{APACHE_SITES_AVAILABLE_DIR} was not found. Is Apache installed?")

    if conf_path.exists() or conf_path.is_symlink():
        try:
            existing = conf_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ApacheConfigError(f"{conf_path} already exists and can not be read: {exc}") from exc
        if existing == content:
            log_info(f"Apache config {conf_path} already exists with the expected content; reusing it.")
            return ApacheConfigResult(conf_path, created=False)
        raise ApacheConfigError(
            f"{conf_path} already exists with different content and was NOT modified. "
            f"Review it, or remove it manually if it is not needed."
        )

    fd, tmp_name = tempfile.mkstemp(prefix=f".{conf_name}.", suffix=".tmp", dir=str(APACHE_SITES_AVAILABLE_DIR))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o644)
        os.link(tmp_name, conf_path)  # fails if the name appeared in the meantime
    except FileExistsError as exc:
        raise ApacheConfigError(f"{conf_path} appeared while this script was running; it was not overwritten.") from exc
    except OSError as exc:
        raise ApacheConfigError(f"Could not write {conf_path}: {exc}") from exc
    finally:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
    log_success(f"Apache config created: {conf_path} (DocumentRoot {document_root})")
    return ApacheConfigResult(conf_path, created=True)

# ======================================================================
# External commands
# ======================================================================
@dataclass(frozen=True)
class CommandResult:
    returncode: int
    output: str  # stdout and stderr together, stripped


def _short(text: str, limit: int = 1500) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + " ...[truncated]"


def run_command(args: List[str], timeout: float = COMMAND_TIMEOUT_SECONDS) -> CommandResult:
    """Run an external command without a shell and capture stdout and stderr together."""
    log_debug("Running command: " + " ".join(args))
    try:
        completed = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            env={**os.environ, "LC_ALL": "C"},
        )
    except FileNotFoundError as exc:
        raise CommandError(f"Command not found: {args[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise CommandError(f"Command timed out after {timeout:g} seconds: {' '.join(args)}") from exc
    except OSError as exc:
        raise CommandError(f"Could not run {args[0]}: {exc}") from exc
    return CommandResult(completed.returncode, (completed.stdout or "").strip())


# ======================================================================
# Apache: enable, test and reload (Linux only)
# ======================================================================
def enable_apache_site(conf_path: Path) -> bool:
    """Enable the site with a2ensite. Returns True if this run enabled it, False if it already was."""
    enabled_path = APACHE_SITES_ENABLED_DIR / conf_path.name
    if os.path.lexists(enabled_path):
        try:
            same = enabled_path.resolve() == conf_path.resolve()
        except OSError:
            same = False
        if same:
            log_info(f"Apache site {conf_path.name} is already enabled.")
            return False
        raise ApacheConfigError(f"{enabled_path} already exists but does not point to {conf_path}; it was not touched.")
    result = run_command(["a2ensite", conf_path.name])
    if result.returncode != 0:
        raise ApacheConfigError(
            f"a2ensite {conf_path.name} failed (exit code {result.returncode}): {_short(result.output)}"
        )
    if not os.path.lexists(enabled_path):
        raise ApacheConfigError(f"a2ensite reported success but {enabled_path} does not exist.")
    log_success(f"Apache site enabled: {conf_path.name}")
    return True


def apache_configtest() -> None:
    """apache2ctl configtest; success needs exit code 0 and 'Syntax OK' in the output."""
    result = run_command(["apache2ctl", "configtest"])
    if result.returncode != 0 or "Syntax OK" not in result.output:
        raise ApacheConfigError(
            f"apache2ctl configtest failed (exit code {result.returncode}): {_short(result.output)}"
        )
    log_success("Apache configuration test passed (Syntax OK).")


def reload_apache() -> None:
    """Reload Apache (systemctl, or apache2ctl graceful when systemctl does not exist)."""
    command = ["systemctl", "reload", "apache2"] if shutil.which("systemctl") else ["apache2ctl", "graceful"]
    result = run_command(command)
    if result.returncode != 0:
        raise ApacheConfigError(
            f"{' '.join(command)} failed (exit code {result.returncode}). The configuration test had passed; "
            f"check 'systemctl status apache2'. Output: {_short(result.output)}"
        )
    log_success("Apache reloaded.")


def activate_apache_site(conf_path: Path) -> bool:
    """
    Enable the site, run configtest and reload Apache ONLY if the test passed.
    Returns True if this run enabled the site. Nothing is rolled back on failure; the error
    says exactly what state was left behind.
    """
    enabled_now = enable_apache_site(conf_path)
    try:
        apache_configtest()
    except (ApacheConfigError, CommandError) as exc:
        undo = f" To disable it manually: sudo a2dissite {conf_path.name}" if enabled_now else ""
        raise ApacheConfigError(
            f"{exc} The site {conf_path.name} is enabled but Apache was NOT reloaded "
            f"(the running configuration is unchanged); nothing was rolled back.{undo}"
        ) from exc
    reload_apache()
    return enabled_now

# ======================================================================
# Site reachability checks: DNS, ping, HTTP (Linux only)
# ======================================================================
@dataclass(frozen=True)
class HttpHeadResult:
    status: int
    reason: str
    headers: Dict[str, str]  # header names in lower case (a repeated header keeps its last value)


@dataclass(frozen=True)
class SiteCheckResult:
    dns_ips: List[str]
    ping_ok: Optional[bool]  # None: the ping command is not available
    http_status: int


def resolve_ipv4(fqdn: str) -> List[str]:
    """IPv4 addresses of a name as seen by this server's resolver (empty list if it does not resolve)."""
    try:
        infos = socket.getaddrinfo(fqdn, 80, family=socket.AF_INET, type=socket.SOCK_STREAM)
    except OSError:
        return []
    return sorted({str(info[4][0]) for info in infos})


_IPV4_RE = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}")


def dig_ipv4(fqdn: str, server: Optional[str] = None) -> List[str]:
    """IPv4 answers of 'dig +short A <fqdn> [@server]'; an empty list means no answer."""
    command = ["dig", "+short", "+time=3", "+tries=1", "A", fqdn]
    if server:
        command.append(f"@{server}")
    result = run_command(command, timeout=20)
    return sorted({line.strip() for line in result.output.splitlines() if _IPV4_RE.fullmatch(line.strip())})


def get_zone_nameservers() -> List[str]:
    """Name servers of the zone (from 'dig +short NS'); an empty list if they can not be read."""
    result = run_command(["dig", "+short", "+time=3", "+tries=2", "NS", DNS_ZONE], timeout=30)
    names = {line.strip().rstrip(".").lower() for line in result.output.splitlines()}
    return sorted(name for name in names if re.fullmatch(r"[a-z0-9][a-z0-9.-]*[a-z0-9]", name))


def check_dns_resolution(site_name: str, expect_origin: bool = True) -> List[str]:
    """
    Wait until <site>.<zone> is published. The zone's own (authoritative) name servers are asked
    directly with dig, so an old answer cached by this server's resolver can not hide a new record;
    without dig the system resolver is used. With expect_origin (Arvan cloud proxy still off) every
    answer must contain SERVER_IP; pass expect_origin=False when the cloud proxy is already on,
    because Arvan then answers with its own addresses.
    """
    fqdn = build_site_fqdn(site_name)
    nameservers: List[str] = []
    if shutil.which("dig"):
        try:
            nameservers = get_zone_nameservers()
        except CommandError as exc:
            log_warning(f"Could not read the name servers of {DNS_ZONE}: {exc}")
    if nameservers:
        log_info(
            f"Checking DNS for {fqdn} on the name servers {', '.join(nameservers)} "
            f"(waiting up to {DNS_WAIT_SECONDS} seconds)..."
        )
    else:
        log_warning("dig or the name servers are not available: using this server's resolver, which may cache a negative answer.")
        log_info(f"Checking DNS for {fqdn} (waiting up to {DNS_WAIT_SECONDS} seconds)...")

    def acceptable(ips: List[str]) -> bool:
        return bool(ips) and (not expect_origin or SERVER_IP in ips)

    deadline = time.monotonic() + DNS_WAIT_SECONDS
    while True:
        try:
            if nameservers:
                answers = {server: dig_ipv4(fqdn, server) for server in nameservers}
            else:
                answers = {"this server's resolver": resolve_ipv4(fqdn)}
        except CommandError as exc:
            raise SiteCheckError(f"DNS query for {fqdn} failed: {exc}") from exc
        if all(acceptable(ips) for ips in answers.values()):
            all_ips = sorted({ip for ips in answers.values() for ip in ips})
            where = "all name servers" if nameservers else "this server's resolver"
            log_success(f"DNS: {fqdn} resolves to {', '.join(all_ips)} ({where})")
            if nameservers and not acceptable(resolve_ipv4(fqdn)):
                log_warning(
                    "This server's own resolver does not see the record yet (it may have cached 'no such name'). "
                    "This is not an error: Let's Encrypt asks the name servers directly and the HTTP check bypasses it."
                )
            return all_ips
        if time.monotonic() >= deadline:
            break
        log_debug("DNS: " + "; ".join(f"{who}: {', '.join(ips) or 'no answer'}" for who, ips in answers.items()))
        time.sleep(DNS_POLL_INTERVAL_SECONDS)
    state = "; ".join(f"{who}: {', '.join(ips) or 'no answer'}" for who, ips in answers.items())
    if not any(answers.values()):
        raise SiteCheckError(
            f"{fqdn} is not published after {DNS_WAIT_SECONDS} seconds ({state}). If the record was only just "
            f"created, wait and retry; otherwise check that the record exists in Arvan and that {DNS_ZONE} "
            f"uses Arvan's name servers."
        )
    raise SiteCheckError(
        f"{fqdn} did not reach the expected state after {DNS_WAIT_SECONDS} seconds ({state}). Expected: "
        f"{SERVER_IP} (if the Arvan cloud proxy is already on for this record, the addresses differ by design)."
    )


def check_ping(site_name: str) -> Optional[bool]:
    """Ping the site. A failure is only a warning: many servers and firewalls drop ICMP."""
    fqdn = build_site_fqdn(site_name)
    command = ["ping", "-c", str(PING_COUNT), "-W", str(PING_WAIT_SECONDS), fqdn]
    try:
        result = run_command(command, timeout=PING_COUNT * (PING_WAIT_SECONDS + 1) + 10)
    except CommandError as exc:
        log_warning(f"Ping skipped: {exc}")
        return None
    if result.returncode == 0:
        log_success(f"Ping to {fqdn} succeeded.")
        return True
    log_warning(f"Ping to {fqdn} failed. This alone is not an error: many servers and firewalls drop ICMP.")
    return False


def parse_http_head(text: str) -> HttpHeadResult:
    """Parse the output of 'curl -I' (the last response header block is used)."""
    lines = text.replace("\r\n", "\n").split("\n")
    starts = [i for i, line in enumerate(lines) if re.match(r"HTTP/\S+\s+\d{3}", line, re.IGNORECASE)]
    if not starts:
        raise SiteCheckError(f"No HTTP response headers received: {_short(text, 300)}")
    block: List[str] = []
    for line in lines[starts[-1]:]:
        if not line.strip():
            break
        block.append(line)
    match = re.match(r"HTTP/\S+\s+(\d{3})\s*(.*)", block[0].strip(), re.IGNORECASE)
    assert match is not None  # guaranteed by the search above
    headers: Dict[str, str] = {}
    for line in block[1:]:
        name, separator, value = line.partition(":")
        if separator:
            headers[name.strip().lower()] = value.strip()
    return HttpHeadResult(int(match.group(1)), match.group(2).strip(), headers)


def curl_head(url: str, resolve: Optional[str] = None) -> HttpHeadResult:
    """
    'curl -I <url>': redirects are not followed and TLS verification is never disabled.
    resolve ('host:port:ip') makes curl connect to that IP without asking any DNS resolver.
    """
    command = [
        "curl", "-I", "-sS",
        "--connect-timeout", "10",
        "--max-time", str(HTTP_REQUEST_TIMEOUT_SECONDS),
        "--proto", "=http,https",
    ]
    if resolve:
        command += ["--resolve", resolve]
    command.append(url)
    result = run_command(command, timeout=HTTP_REQUEST_TIMEOUT_SECONDS + 10)
    if result.returncode != 0:
        raise SiteCheckError(f"curl -I {url} failed (exit code {result.returncode}): {_short(result.output)}")
    return parse_http_head(result.output)


def check_http_access(site_name: str, via_origin: bool = True) -> HttpHeadResult:
    """
    HEAD request to http://<site>: any 2xx/3xx answer is fine (a fresh WordPress redirects to its installer).
    via_origin: connect straight to SERVER_IP (needs no DNS, so a cached 'no such name' can not interfere);
    use via_origin=False once the Arvan cloud proxy is on and the request must go through the public DNS.
    """
    fqdn = build_site_fqdn(site_name)
    url = f"http://{fqdn}"
    resolve = f"{fqdn}:80:{SERVER_IP}" if via_origin else None
    head: Optional[HttpHeadResult] = None
    for attempt in range(1, HTTP_CHECK_ATTEMPTS + 1):
        try:
            head = curl_head(url, resolve)
            break
        except SiteCheckError as exc:
            if attempt == HTTP_CHECK_ATTEMPTS:
                raise
            log_warning(f"HTTP check attempt {attempt}/{HTTP_CHECK_ATTEMPTS} failed: {exc}")
            time.sleep(HTTP_CHECK_INTERVAL_SECONDS)
    assert head is not None
    if not 200 <= head.status < 400:
        raise SiteCheckError(
            f"{url} answered HTTP {head.status} {head.reason}; a 2xx or 3xx answer was expected. "
            f"Check the Apache vhost, the DocumentRoot and its permissions."
        )
    location = f" -> {head.headers['location']}" if "location" in head.headers else ""
    via = f" (straight from {SERVER_IP})" if via_origin else ""
    log_success(f"HTTP: {url}{via} answered {head.status} {head.reason}{location}")
    return head


def check_site_reachability(site_name: str, expect_origin: bool = True) -> SiteCheckResult:
    """DNS (must pass), ping (warning only) and HTTP (must pass). A failed ping alone is not a failure."""
    dns_ips = check_dns_resolution(site_name, expect_origin=expect_origin)
    ping_ok = check_ping(site_name)
    head = check_http_access(site_name, via_origin=expect_origin)
    return SiteCheckResult(dns_ips, ping_ok, head.status)

# ======================================================================
# SSL certificate with Certbot (Linux only)
# ======================================================================
@dataclass(frozen=True)
class CertbotResult:
    cert_name: str
    live_dir: Path  # <live dir>/<cert name>: holds fullchain.pem and privkey.pem
    dry_run: bool  # True: only a test against the staging server, nothing was saved or installed


# (text to look for in the lower-cased Certbot output, hint for the user)
_CERTBOT_HINTS = [
    ("too many certificates", "Let's Encrypt rate limit reached; wait before trying again."),
    ("too many failed", "Let's Encrypt rate limit for failed validations reached; fix the cause and wait an hour."),
    ("ratelimited", "Let's Encrypt rate limit reached; wait before trying again."),
    ("nxdomain", "The name does not resolve for Let's Encrypt; check the DNS record and wait for propagation."),
    ("dns problem", "Let's Encrypt could not resolve the name; check the DNS record and wait for propagation."),
    ("timeout during connect", "Let's Encrypt could not reach port 80 of this server; check the firewall and the DNS target."),
    ("connection refused", "Let's Encrypt could not connect to port 80; is Apache running and the port open?"),
    ("plugin does not appear to be installed", "Install the Apache plugin: sudo apt install python3-certbot-apache"),
    ("another instance of certbot is already running", "Another Certbot is running; wait for it to finish."),
]


def _tail(text: str, limit: int = 1500) -> str:
    text = text.strip()
    return text if len(text) <= limit else "...[truncated] " + text[-limit:]


def get_certbot_email() -> str:
    """Contact email for Let's Encrypt, read from the CERTBOT_EMAIL environment variable."""
    email = os.environ.get(CERTBOT_EMAIL_ENV_VAR, "").strip()
    if not email:
        raise CertbotError(
            f"Environment variable {CERTBOT_EMAIL_ENV_VAR} is not set (or is empty). "
            f"Example: export {CERTBOT_EMAIL_ENV_VAR}='you@your-domain.com'"
        )
    if email.startswith("-") or not email.isascii() or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise CertbotError(f"{CERTBOT_EMAIL_ENV_VAR} is not a valid email address.")
    domain = email.rsplit("@", 1)[1].lower()
    if domain in ("example.com", "example.org", "example.net"):
        raise CertbotError(f"{CERTBOT_EMAIL_ENV_VAR} still contains a sample address; Let's Encrypt rejects it.")
    return email


def build_certbot_command(site_name: str, email: str, dry_run: bool = False) -> List[str]:
    """
    Real run: 'certbot --apache' (obtain + install + HTTP->HTTPS redirect) against the Production server.
    Dry run: 'certbot certonly --apache --dry-run' (staging server; nothing is saved or installed).
    --keep-until-expiring: an existing valid certificate is reused instead of being issued again.
    """
    fqdn = build_site_fqdn(site_name)
    common = [
        "--non-interactive", "--agree-tos", "--no-eff-email",
        f"--email={email}",
        "--cert-name", fqdn,
        "-d", fqdn,
    ]
    if dry_run:
        return ["certbot", "certonly", "--apache", "--dry-run", *common]
    return ["certbot", "--apache", "--redirect", "--keep-until-expiring", *common]


def obtain_ssl_certificate(site_name: str, dry_run: bool = False) -> CertbotResult:
    """
    Get and install a Let's Encrypt certificate for <site>.<zone> with Certbot. The Arvan cloud proxy
    must still be OFF so that Let's Encrypt reaches this server directly. Nothing is rolled back on
    failure. A real run is checked only for its files here; the Production issuer is verified separately.
    """
    check_apache_prerequisites()
    if shutil.which("certbot") is None:
        raise CertbotError("certbot was not found. Install it, e.g.: sudo apt install certbot python3-certbot-apache")
    email = get_certbot_email()
    fqdn = build_site_fqdn(site_name)
    kind = "a TEST (dry-run, staging)" if dry_run else "a Production"
    log_info(f"Requesting {kind} certificate for {fqdn} with Certbot (this can take a minute)...")
    try:
        result = run_command(build_certbot_command(site_name, email, dry_run), timeout=CERTBOT_TIMEOUT_SECONDS)
    except CommandError as exc:
        raise CertbotError(f"{exc}. Check 'certbot certificates' before trying again; nothing was rolled back.") from exc
    if result.returncode != 0:
        lowered = result.output.lower()
        hints = [hint for needle, hint in _CERTBOT_HINTS if needle in lowered]
        hint_text = (" Hint: " + " ".join(dict.fromkeys(hints))) if hints else ""
        raise CertbotError(
            f"certbot failed (exit code {result.returncode}).{hint_text} Output (end): {_tail(result.output)}"
        )
    live_dir = LETSENCRYPT_LIVE_DIR / fqdn
    if dry_run:
        log_success("Certbot dry-run succeeded: validation works. No certificate was saved or installed.")
        return CertbotResult(fqdn, live_dir, True)
    for file_name in ("fullchain.pem", "privkey.pem"):
        if not (live_dir / file_name).is_file():
            raise CertbotError(f"certbot reported success but {live_dir / file_name} does not exist.")
    log_success(f"Certbot finished: certificate files are in {live_dir}")
    return CertbotResult(fqdn, live_dir, False)

# ======================================================================
# SSL verification: Certbot's -le-ssl.conf and the certificate (Linux only, needs root)
# ======================================================================
@dataclass(frozen=True)
class CertificateInfo:
    issuer: str
    not_after: str  # as printed by openssl, e.g. "Nov 17 12:51:40 2026 GMT"


@dataclass(frozen=True)
class SslCheckResult:
    le_conf_path: Path
    certificate: CertificateInfo


def _openssl(args: List[str]) -> CommandResult:
    try:
        return run_command(["openssl", *args], timeout=30)
    except CommandError as exc:
        raise SslCheckError(f"openssl could not be run: {exc}") from exc


def _parse_vhost_443(text: str) -> Optional[Dict[str, List[str]]]:
    """Directives of the first <VirtualHost ...:443> block (names in lower case), None if there is none."""
    match = re.search(r"<VirtualHost\s+[^>]*:443[^>]*>(.*?)</VirtualHost>", text, re.IGNORECASE | re.DOTALL)
    if match is None:
        return None
    directives: Dict[str, List[str]] = {}
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "<")):
            continue
        parts = line.split(None, 1)
        value = parts[1].strip().strip("\"'") if len(parts) > 1 else ""
        directives.setdefault(parts[0].lower(), []).append(value)
    return directives


def check_le_ssl_config(site_name: str, document_root: Path) -> Path:
    """
    Check <site>.<zone>-le-ssl.conf made by Certbot: it exists, it is enabled, and its
    <VirtualHost *:443> has the right ServerName, DocumentRoot and certificate files.
    """
    fqdn = build_site_fqdn(site_name)
    live_dir = LETSENCRYPT_LIVE_DIR / fqdn
    conf_path = APACHE_SITES_AVAILABLE_DIR / f"{fqdn}-le-ssl.conf"
    if not conf_path.is_file():
        raise SslCheckError(f"{conf_path} was not found: Certbot did not install the certificate into Apache.")
    enabled_path = APACHE_SITES_ENABLED_DIR / conf_path.name
    try:
        enabled = os.path.lexists(enabled_path) and enabled_path.resolve() == conf_path.resolve()
    except OSError:
        enabled = False
    if not enabled:
        raise SslCheckError(f"{conf_path.name} is not enabled (no matching link in {APACHE_SITES_ENABLED_DIR}).")
    try:
        directives = _parse_vhost_443(conf_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise SslCheckError(f"{conf_path} can not be read: {exc}") from exc
    if directives is None:
        raise SslCheckError(f"{conf_path} has no <VirtualHost *:443> block.")
    expected = {
        "servername": fqdn,
        "documentroot": str(document_root),
        "sslcertificatefile": str(live_dir / "fullchain.pem"),
        "sslcertificatekeyfile": str(live_dir / "privkey.pem"),
    }
    problems = [
        f"{name} should be {value} (found: {', '.join(directives.get(name, [])) or 'nothing'})"
        for name, value in expected.items()
        if value not in directives.get(name, [])
    ]
    if problems:
        raise SslCheckError(f"The port 443 virtual host in {conf_path.name} is not as expected: " + "; ".join(problems) + ".")
    if not any("options-ssl-apache.conf" in value for value in directives.get("include", [])):
        log_warning(f"{conf_path.name} does not include Certbot's options-ssl-apache.conf (the SSL hardening options).")
    log_success(f"Apache SSL config {conf_path.name} is enabled and its port 443 virtual host is correct.")
    return conf_path


def check_certificate(site_name: str, ca_file: Optional[Path] = None) -> CertificateInfo:
    """
    Check the Let's Encrypt certificate in /etc/letsencrypt/live/<site>.<zone>/: Production issuer
    (a Staging certificate is refused), valid for at least SSL_MIN_VALID_DAYS, matches the host name,
    matches the private key, and its chain is trusted. ca_file is only for tests; normally the
    system trust store is used.
    """
    fqdn = build_site_fqdn(site_name)
    live_dir = LETSENCRYPT_LIVE_DIR / fqdn
    fullchain, privkey = live_dir / "fullchain.pem", live_dir / "privkey.pem"
    for path in (fullchain, privkey):
        if not path.is_file():
            raise SslCheckError(f"{path} does not exist.")

    result = _openssl(["x509", "-in", str(fullchain), "-noout", "-issuer", "-enddate"])
    issuer_match = re.search(r"^issuer\s*=\s*(.+)$", result.output, re.MULTILINE)
    end_match = re.search(r"^notAfter\s*=\s*(.+)$", result.output, re.MULTILINE)
    if result.returncode != 0 or issuer_match is None or end_match is None:
        raise SslCheckError(f"{fullchain} is not a readable certificate: {_short(result.output)}")
    issuer, not_after = issuer_match.group(1).strip(), end_match.group(1).strip()
    if "staging" in issuer.lower():
        raise SslCheckError(
            f"The certificate is a Let's Encrypt STAGING (test) certificate (issuer: {issuer}); browsers and "
            f"Arvan do not trust it. Remove it manually (certbot delete --cert-name {fqdn}) and request a Production one."
        )
    if not re.search(r"let.?s encrypt", issuer, re.IGNORECASE):
        raise SslCheckError(f"The certificate was not issued by Let's Encrypt (issuer: {issuer}).")

    result = _openssl(["x509", "-in", str(fullchain), "-noout", "-checkend", str(SSL_MIN_VALID_DAYS * 86400)])
    if result.returncode != 0:
        raise SslCheckError(
            f"The certificate has expired or expires within {SSL_MIN_VALID_DAYS} days (valid until {not_after})."
        )

    result = _openssl(["x509", "-in", str(fullchain), "-noout", "-checkhost", fqdn])
    if "does match certificate" not in result.output:
        raise SslCheckError(f"The certificate is not valid for {fqdn}: {_short(result.output)}")

    cert_key = _openssl(["x509", "-in", str(fullchain), "-noout", "-pubkey"])
    private_key = _openssl(["pkey", "-in", str(privkey), "-pubout"])
    if cert_key.returncode != 0 or private_key.returncode != 0 or cert_key.output != private_key.output:
        raise SslCheckError(f"The private key {privkey} does not belong to the certificate {fullchain}.")

    verify = ["verify", "-purpose", "sslserver"]
    if ca_file is not None:
        verify += ["-CAfile", str(ca_file)]
    result = _openssl(verify + ["-untrusted", str(fullchain), str(fullchain)])
    if result.returncode != 0:
        raise SslCheckError(f"The certificate chain is not trusted: {_short(result.output)}")

    log_success(f"Certificate for {fqdn}: Production Let's Encrypt, valid until {not_after}, matches the host and the key.")
    return CertificateInfo(issuer, not_after)


def verify_ssl_setup(site_name: str, document_root: Path, ca_file: Optional[Path] = None) -> SslCheckResult:
    """Certbot's -le-ssl.conf (port 443 virtual host) and the certificate must both be correct."""
    conf_path = check_le_ssl_config(site_name, document_root)
    certificate = check_certificate(site_name, ca_file)
    return SslCheckResult(conf_path, certificate)

# ======================================================================
# HTTPS on the origin server and the HTTP -> HTTPS redirect (Linux only)
# ======================================================================
@dataclass(frozen=True)
class HttpsCheckResult:
    https_status: int
    redirect_status: int
    redirect_location: str


# (text in the first line of the curl error, hint for the user); the first match wins
_HTTPS_CURL_HINTS = [
    ("subject name", "Apache served a certificate for a different name: the port 443 virtual host of this site is missing or not used."),
    ("has expired", "The certificate served by Apache has expired."),
    ("exit code 60", "The certificate served by Apache is not trusted (a Staging or self-signed certificate, or this server lacks its CA bundle)."),
    ("exit code 7", "Nothing accepts connections on port 443: check that Certbot enabled mod_ssl and that Apache was reloaded."),
    ("exit code 35", "The TLS handshake failed: check the port 443 virtual host and its certificate files."),
]


def _head_with_retries(url: str, resolve: Optional[str]) -> HttpHeadResult:
    """curl -I with a few repeats when curl itself fails; a certificate problem (exit code 60) is not repeated."""
    for attempt in range(1, HTTP_CHECK_ATTEMPTS + 1):
        try:
            return curl_head(url, resolve)
        except SiteCheckError as exc:
            if attempt == HTTP_CHECK_ATTEMPTS or "exit code 60" in str(exc):
                raise
            log_warning(f"Check of {url}, attempt {attempt}/{HTTP_CHECK_ATTEMPTS} failed: {exc}")
            time.sleep(HTTP_CHECK_INTERVAL_SECONDS)
    raise SiteCheckError(f"Check of {url} failed.")  # not reachable


def check_https_origin(site_name: str, via_origin: bool = True) -> HttpHeadResult:
    """
    HEAD request to https://<site> with full certificate verification (never disabled). via_origin connects
    straight to SERVER_IP, so this proves that Apache itself serves a trusted certificate on port 443.
    """
    fqdn = build_site_fqdn(site_name)
    url = f"https://{fqdn}"
    resolve = f"{fqdn}:443:{SERVER_IP}" if via_origin else None
    try:
        head = _head_with_retries(url, resolve)
    except SiteCheckError as exc:
        text = str(exc).splitlines()[0]  # curl adds a long generic explanation after its first line
        hints = [hint for needle, hint in _HTTPS_CURL_HINTS if needle in text]
        raise SiteCheckError(f"{text} {hints[0]}" if hints else text) from exc
    if not 200 <= head.status < 400:
        raise SiteCheckError(
            f"{url} answered HTTP {head.status} {head.reason}; a 2xx or 3xx answer was expected. "
            f"Check the port 443 virtual host and the DocumentRoot."
        )
    via = f" (straight from {SERVER_IP})" if via_origin else ""
    log_success(f"HTTPS: {url}{via} answered {head.status} {head.reason} with a trusted certificate.")
    return head


def check_https_redirect(site_name: str, via_origin: bool = True) -> HttpHeadResult:
    """http://<site>/ must answer with a redirect (301, 302, 307 or 308) to https://<site>/..."""
    fqdn = build_site_fqdn(site_name)
    url = f"http://{fqdn}/"
    resolve = f"{fqdn}:80:{SERVER_IP}" if via_origin else None
    head = _head_with_retries(url, resolve)
    location = head.headers.get("location", "")
    parsed = urllib.parse.urlsplit(location)
    if head.status not in (301, 302, 307, 308) or parsed.scheme.lower() != "https" or (parsed.hostname or "").lower() != fqdn:
        raise SiteCheckError(
            f"{url} does not redirect to https://{fqdn}/ (answered HTTP {head.status} {head.reason}, "
            f"Location: {location or 'none'}). Certbot's --redirect should have added the rewrite rule to "
            f"{fqdn}.conf; check that file."
        )
    log_success(f"Redirect: {url} -> {location} (HTTP {head.status})")
    return head


def check_https_setup(site_name: str, via_origin: bool = True) -> HttpsCheckResult:
    """HTTPS must work with a trusted certificate and plain HTTP must redirect to it."""
    https_head = check_https_origin(site_name, via_origin=via_origin)
    redirect_head = check_https_redirect(site_name, via_origin=via_origin)
    return HttpsCheckResult(https_head.status, redirect_head.status, redirect_head.headers.get("location", ""))

# ======================================================================
# Arvan Cloud proxy: enable it and check the final answer (Linux only)
# ======================================================================
@dataclass(frozen=True)
class ArvanCloudResult:
    record_id: str
    enabled_now: bool  # False: the record already had the cloud proxy on


def arvan_get_dns_record(token: str, record_id: str) -> Dict[str, Any]:
    """Read one DNS record (GET /dns-records/{id})."""
    reply = arvan_request("GET", arvan_dns_records_path(record_id), token)
    data = reply.get("data") if isinstance(reply, dict) else None
    if not isinstance(data, dict):
        raise ArvanApiError("Unexpected reply while reading the DNS record (no 'data' object).")
    return data


def _arvan_cloud_enabled(record: Dict[str, Any]) -> bool:
    value = record.get("value")
    ports_ok = (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(item, dict) and str(item.get("port")) == str(ARVAN_UPSTREAM_PORT) for item in value)
    )
    return record.get("cloud") is True and record.get("upstream_https") == "https" and ports_ok


def build_cloud_enable_payload(record: Dict[str, Any]) -> Dict[str, Any]:
    """
    Body of the PUT that turns the Arvan cloud proxy on. Every writable field of the current record is
    kept (type, name, value, ttl, ip_filter_mode); only cloud, upstream_https and the origin port change.
    """
    for key in ("type", "name", "value"):
        if key not in record:
            raise DnsRecordError(f"The DNS record has no '{key}' field; it was not modified.")
    value = record["value"]
    if not isinstance(value, list) or not value or not all(isinstance(item, dict) for item in value):
        raise DnsRecordError("The DNS record has no usable 'value' list; it was not modified.")
    ip_filter_mode = record.get("ip_filter_mode")
    return {
        "type": record["type"],
        "name": record["name"],
        "value": [{**item, "port": ARVAN_UPSTREAM_PORT} for item in value],
        "ttl": record.get("ttl", DNS_RECORD_TTL),
        "cloud": True,
        "upstream_https": "https",
        "ip_filter_mode": dict(ip_filter_mode) if isinstance(ip_filter_mode, dict)
        else {"count": "single", "order": "none", "geo_filter": "none"},
    }


def enable_arvan_cloud(token: str, site_name: str, document_root: Path) -> ArvanCloudResult:
    """
    Turn the Arvan cloud proxy on for <site>.<zone> (cloud true, upstream_https https, origin port 443).
    It refuses to do anything unless the certificate is a valid Production one (a Staging certificate
    is refused) and Apache serves HTTPS correctly on the origin. A PUT with an unclear outcome is only
    repeated (once) after the record was re-read. Nothing is rolled back on failure.
    """
    fqdn = build_site_fqdn(site_name)
    verify_ssl_setup(site_name, document_root)
    check_https_origin(site_name, via_origin=True)

    exact = check_dns_state(token, site_name)
    if exact is None or not exact.get("id"):
        raise DnsRecordError(f"The A record of {fqdn} was not found in Arvan; the cloud proxy was not enabled.")
    record_id = str(exact["id"])
    try:
        record = arvan_get_dns_record(token, record_id)
    except ArvanApiError as exc:
        raise DnsRecordError(f"Could not read the DNS record of {fqdn}: {exc}") from exc
    if _arvan_cloud_enabled(record):
        log_info(f"The Arvan cloud proxy is already enabled for {fqdn}.")
        return ArvanCloudResult(record_id, enabled_now=False)

    payload = build_cloud_enable_payload(record)
    for attempt in range(1, ARVAN_UPDATE_MAX_ATTEMPTS + 1):
        try:
            arvan_request("PUT", arvan_dns_records_path(record_id), token, payload=payload)
            break
        except ArvanOutcomeUnknownError as exc:
            log_warning(f"Enabling the Arvan cloud proxy (attempt {attempt}): {exc}")
            time.sleep(DNS_RECHECK_DELAY_SECONDS)
            try:
                current = arvan_get_dns_record(token, record_id)
            except ArvanApiError as check_exc:
                raise DnsRecordError(
                    f"The update outcome is unclear and the record could not be re-read ({check_exc}); "
                    f"the request was not repeated. Check the record in the Arvan panel."
                ) from check_exc
            if _arvan_cloud_enabled(current):
                log_success(f"Arvan cloud proxy enabled for {fqdn} (confirmed after an unclear reply).")
                return ArvanCloudResult(record_id, enabled_now=True)
            if attempt == ARVAN_UPDATE_MAX_ATTEMPTS:
                raise DnsRecordError(
                    f"Could not enable the Arvan cloud proxy for {fqdn}: the outcome stayed unclear and the "
                    f"record does not show it. Check the record in the Arvan panel."
                ) from exc
            log_warning("The record does not show the change yet; repeating the update once.")
        except ArvanApiError as exc:
            raise DnsRecordError(f"Arvan rejected enabling the cloud proxy for {fqdn}: {exc}") from exc

    try:
        current = arvan_get_dns_record(token, record_id)
    except ArvanApiError as exc:
        raise DnsRecordError(f"The update was accepted but the record could not be re-read: {exc}") from exc
    if not _arvan_cloud_enabled(current):
        raise DnsRecordError(
            f"Arvan accepted the update but the record of {fqdn} does not show the cloud proxy as enabled "
            f"(cloud/upstream_https/port). Check the record in the Arvan panel."
        )
    log_success(f"Arvan cloud proxy enabled for {fqdn} (upstream https, origin port {ARVAN_UPSTREAM_PORT}).")
    return ArvanCloudResult(record_id, enabled_now=True)


def check_arvan_cloud_response(site_name: str) -> HttpHeadResult:
    """
    The final check: 'curl -I https://<site>' through the public DNS (certificate verification stays on)
    must answer 2xx/3xx with the header 'Server: ArvanCloud'. Waits while the change propagates.
    """
    fqdn = build_site_fqdn(site_name)
    url = f"https://{fqdn}"
    log_info(f"Waiting for {url} to be served through Arvan Cloud (up to {ARVAN_CLOUD_WAIT_SECONDS} seconds)...")
    deadline = time.monotonic() + ARVAN_CLOUD_WAIT_SECONDS
    last = "no answer yet"
    while True:
        try:
            head = curl_head(url)
        except SiteCheckError as exc:
            last = str(exc).splitlines()[0]
        else:
            server = head.headers.get("server", "")
            if "arvancloud" in server.lower():
                if 200 <= head.status < 400:
                    answer = f"{head.status} {head.reason}".strip()
                    log_success(f"Arvan Cloud: {url} answered {answer} (Server: {server}).")
                    return head
                last = f"Arvan answered HTTP {head.status} {head.reason}".rstrip()
                last += (": it can not get a good answer from the origin (check origin port 443 and the SSL mode)"
                         if head.status >= 500 else "; a 2xx or 3xx answer was expected")
            else:
                last = f"HTTP {head.status} with Server: {server or 'none'}, not served by Arvan yet (DNS may still be changing)"
        if time.monotonic() >= deadline:
            break
        log_debug(f"Arvan check: {last}")
        time.sleep(ARVAN_CLOUD_POLL_INTERVAL_SECONDS)
    raise SiteCheckError(
        f"{url} is not served through Arvan Cloud after {ARVAN_CLOUD_WAIT_SECONDS} seconds. Last result: {last}. "
        f"The cloud proxy was left enabled (nothing was rolled back); check the record and its SSL settings in the Arvan panel."
    )

# ======================================================================
# DNS / Apache / SSL phase: requirements check and resource listing
# ======================================================================
def preflight_server_setup(site_name: str) -> str:
    """
    Check everything steps 9-17 need BEFORE any file, database or DNS record is created: Linux, root
    and Apache, the required commands, the Arvan API key (proven by a read-only request), the Certbot
    email, and that the DNS name is free or already has exactly the expected A record.
    Returns the Arvan API token.
    """
    check_apache_prerequisites()
    required = ("a2ensite", "apache2ctl", "certbot", "curl", "openssl")
    missing = [name for name in required if shutil.which(name) is None]
    if missing:
        raise CommandError(
            "Required command(s) not found: " + ", ".join(missing) + ". Install them first, e.g.: "
            "sudo apt install apache2 certbot python3-certbot-apache curl openssl"
        )
    if shutil.which("dig") is None:
        log_warning("dig is not installed: the DNS check will use this server's resolver (sudo apt install dnsutils).")
    if shutil.which("ping") is None:
        log_warning("ping is not installed: the ping check will be skipped.")
    token = get_arvan_token()
    get_certbot_email()  # only validates the CERTBOT_EMAIL variable
    fqdn = build_site_fqdn(site_name)
    existing = check_dns_state(token, site_name)  # reads the whole zone; raises on a conflicting record
    if existing is None:
        log_info(f"The DNS name {fqdn} is free.")
    else:
        log_warning(f"The exact A record for {fqdn} already exists in Arvan (id {existing.get('id', '?')}); it will be reused.")
    return token


def log_server_resources(ctx: InstallContext) -> None:
    """List what the DNS / Apache / SSL steps created or reused so far. None of it is ever rolled back."""
    fqdn = build_site_fqdn(ctx.site_name)
    if ctx.dns_record is not None:
        state = "created" if ctx.dns_record.created else "already existed and was reused"
        log_info(f"DNS A record {fqdn} -> {SERVER_IP}: {state} (id {ctx.dns_record.record.get('id', '?')})")
    if ctx.apache_conf is not None:
        state = "created" if ctx.apache_conf.created else "already existed and was reused"
        enabled = {True: ", enabled by this run", False: ", was already enabled", None: ""}[ctx.apache_enabled_now]
        log_info(f"Apache config {ctx.apache_conf.conf_path}: {state}{enabled}")
    if ctx.ssl_result is not None:
        log_info(f"SSL certificate files: {ctx.ssl_result.live_dir}")
    if ctx.cloud_result is not None:
        log_info("Arvan Cloud proxy: " + ("enabled by this run" if ctx.cloud_result.enabled_now else "was already enabled"))

def server_steps_planned(skip_server_setup: bool) -> bool:
    """Steps 9-17 (DNS, Apache, SSL, Arvan Cloud) run only on Linux and unless --skip-server-setup was given."""
    if skip_server_setup:
        log_info("The DNS / Apache / SSL steps are skipped (--skip-server-setup).")
        return False
    if not is_linux():
        log_warning("The DNS / Apache / SSL steps are supported on Linux only; they are skipped on this system.")
        return False
    return True

# ======================================================================
# Cleanup
# ======================================================================
def cleanup_temp_dir(temp_dir: Optional[Path]) -> None:
    if temp_dir is None:
        return
    try:
        shutil.rmtree(temp_dir)
    except FileNotFoundError:
        pass
    except OSError as exc:
        log_warning(f"Could not remove the temporary folder ({temp_dir}): {exc}")
    else:
        log_info("Temporary files removed.")


def rollback(ctx: InstallContext) -> None:
    """Remove only the files and folders that this run created and recorded."""
    if not ctx.created_files and not ctx.created_dirs:
        return
    log_warning("Cleaning up what this run created ...")
    leftovers = 0
    for file_path in reversed(ctx.created_files):
        try:
            file_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            leftovers += 1
            log_warning(f"Could not remove file ({file_path}): {exc}")
    for directory in sorted(ctx.created_dirs, key=lambda p: len(p.parts), reverse=True):
        try:
            directory.rmdir()  # only succeeds if empty; other people's content is never deleted
        except FileNotFoundError:
            pass
        except OSError as exc:
            leftovers += 1
            log_warning(f"Could not remove folder ({directory}): {exc}")
    if leftovers:
        log_warning(f"{leftovers} item(s) could not be removed; please check them manually.")
    else:
        log_info("Cleanup finished.")


# ======================================================================
# Step reporting
# ======================================================================
class StepReporter:
    def __init__(self) -> None:
        self._results: Dict[int, bool] = {}

    def record(self, number: int, ok: bool) -> None:
        self._results[number] = ok

    def status(self, number: int) -> str:
        if number not in self._results:
            return "skipped"
        return "ok" if self._results[number] else "failed"


@contextmanager
def step(reporter: StepReporter, number: int) -> Iterator[None]:
    """
    Announce a step (STEP), run it, and report the outcome: SUCCESS only after the
    body finished without an exception, ERROR if it raised. The exception itself
    is re-raised so the caller handles it exactly as before.
    """
    start_message, success_message, failure_message = STEP_MESSAGES[number]
    log_step(start_message)
    try:
        yield
    except BaseException as exc:
        reporter.record(number, False)
        if isinstance(exc, Exception):  # Ctrl+C is reported once, by the caller
            log_error(failure_message)
        raise
    reporter.record(number, True)
    log_success(success_message)


def ensure_previous_steps_succeeded(reporter: StepReporter, number: int) -> None:
    """Refuse to start step `number` unless every earlier step finished successfully."""
    for previous in range(1, number):
        if reporter.status(previous) != "ok":
            raise InstallerError(
                f"Cannot start step {number}: step {previous} ({STEP_TITLES[previous]}) did not complete successfully."
            )


def print_report(reporter: StepReporter, ctx: InstallContext, exit_code: int) -> None:
    last_step = max(STEP_TITLES) if ctx.server_steps_enabled else 8  # steps 9-17 only when they were part of this run
    log_step(f"Step {last_step + 1}: Final report")
    for number, title in STEP_TITLES.items():
        if number > last_step:
            break
        status = reporter.status(number)
        if status == "ok":
            log_success(f"Step {number}: {title}")
        elif status == "failed":
            log_error(f"Step {number}: {title} - FAILED")
        else:
            log_warning(f"Step {number}: {title} - skipped")
    if (
        exit_code == EXIT_OK
        and ctx.target_dir is not None
        and ctx.config_path is not None
        and ctx.db_created_name is not None
    ):
        db = build_db_settings(ctx.site_name)
        log_success("Result: SUCCESS")
        log_info(f"Final install path: {ctx.target_dir}")
        log_info(f"WordPress files: created ({ctx.extracted_files} files, {ctx.extracted_dirs} folders)")
        log_info("wp-config.php: created")
        log_info(f"Database: {ctx.db_created_name} created on {db.host}:{db.port}")
        log_warning("The database user and password are TEMPORARY TEST values; change them before real use.")
        if ctx.server_phase_started:
            log_info(f"Site address: https://{build_site_fqdn(ctx.site_name)}")
            log_server_resources(ctx)
            log_info(
                "Note: database user creation, privilege changes and the final WordPress installation "
                "are not performed by this script."
            )
        else:
            log_info(
                "Note: database user creation, privilege changes, the final WordPress installation, "
                "Apache configuration and SSL are not performed by this script."
            )
    else:
        log_error(f"Result: FAILED (exit code {exit_code})")
        if ctx.server_phase_started:
            log_server_resources(ctx)
            log_warning(
                "The WordPress files, the database and everything created by the DNS / Apache / SSL steps "
                "were kept (no rollback after step 8). Remove them manually if they are not needed."
            )


# ======================================================================
# Main flow
# ======================================================================
def run_installer(
    name_arg: Optional[str] = None,
    base_dir_arg: Optional[str] = None,
    verbose: bool = False,
    skip_server_setup: bool = False,
) -> int:
    _console.verbose = verbose
    reporter = StepReporter()
    ctx = InstallContext()
    arvan_token: Optional[str] = None
    exit_code = EXIT_OK
    try:
        with step(reporter, 1):
            ctx.site_name = obtain_site_name(name_arg)

        ctx.server_steps_enabled = server_steps_planned(skip_server_setup)
        if ctx.server_steps_enabled:
            # Everything steps 9-17 need is checked now, before any file, database or DNS record is created.
            log_step("Checking the requirements of the DNS / Apache / SSL steps...")
            arvan_token = preflight_server_setup(ctx.site_name)
            log_success("The requirements of the DNS / Apache / SSL steps are met.")

        with step(reporter, 2):
            ctx.base_dir = resolve_base_dir(base_dir_arg)
            ctx.target_dir = build_target_path(ctx.base_dir, ctx.site_name)
            create_target_dir(ctx.target_dir, ctx)
            log_info(f"Target path: {ctx.target_dir}")

        with step(reporter, 3):
            ctx.temp_dir = create_temp_dir()
            zip_path = ctx.temp_dir / "latest-fa_IR.zip"
            log_info(f"Downloading from the official URL: {WP_DOWNLOAD_URL}")
            size = download_file(WP_DOWNLOAD_URL, zip_path)
            log_info(f"Received {_fmt_size(size)}.")

        with step(reporter, 4):
            validate_zip_file(zip_path)
            stats = extract_zip_safely(zip_path, ctx.target_dir, ctx)
            verify_wordpress_layout(ctx.target_dir)
            log_info(f"Extracted {stats.files} files and {stats.dirs} folders.")

        with step(reporter, 5):
            keys_block = fetch_security_keys()
            _register_key_secrets(keys_block)
            log_info("Security keys received and validated (values are never printed).")

        with step(reporter, 6):
            db = build_db_settings(ctx.site_name)
            content = render_wp_config(db, keys_block)
            validate_wp_config(content)
            ctx.config_path = write_wp_config(ctx.target_dir, content, ctx)
            log_info(f"Config file written: {ctx.config_path}")

        ensure_previous_steps_succeeded(reporter, 7)  # database work only after steps 1-6 succeeded

        with step(reporter, 7):
            validate_db_settings(db)  # invalid settings are rejected before any connection
            log_info(f"Database server: {db.host}:{db.port}, user: {db.user} (password is never printed)")
            connect_to_database(db, ctx)

        with step(reporter, 8):
            check_and_create_database(ctx, db)

        if ctx.server_steps_enabled:
            ensure_previous_steps_succeeded(reporter, 9)  # DNS, Apache and SSL only after steps 1-8 succeeded
            if arvan_token is None or ctx.target_dir is None:  # cannot happen: the preflight and steps 1-8 succeeded
                raise InstallerError("Internal error: the DNS / Apache / SSL steps are missing their prerequisites.")
            ctx.server_phase_started = True  # from here on nothing is rolled back

            with step(reporter, 9):
                dns_result = ensure_dns_a_record(arvan_token, ctx.site_name)
                ctx.dns_record = dns_result

            with step(reporter, 10):
                apache_result = create_apache_vhost_config(ctx.site_name, ctx.target_dir)
                ctx.apache_conf = apache_result

            with step(reporter, 11):
                ctx.apache_enabled_now = activate_apache_site(apache_result.conf_path)

            with step(reporter, 12):
                # With the Arvan cloud proxy still off, DNS must point at this server itself.
                reach = check_site_reachability(ctx.site_name, expect_origin=not dns_result.record.get("cloud"))
                ping_text = {True: "ok", False: "failed (only a warning)", None: "skipped"}[reach.ping_ok]
                log_info(f"DNS: {', '.join(reach.dns_ips)}; ping: {ping_text}; HTTP status: {reach.http_status}")

            with step(reporter, 13):
                ctx.ssl_result = obtain_ssl_certificate(ctx.site_name)

            with step(reporter, 14):
                verify_ssl_setup(ctx.site_name, ctx.target_dir)

            with step(reporter, 15):
                check_https_setup(ctx.site_name, via_origin=True)

            ensure_previous_steps_succeeded(reporter, 16)  # the Arvan cloud proxy only after everything before it

            with step(reporter, 16):
                ctx.cloud_result = enable_arvan_cloud(arvan_token, ctx.site_name, ctx.target_dir)

            with step(reporter, 17):
                check_arvan_cloud_response(ctx.site_name)

    except InstallerError as exc:
        log_error(f"Reason: {exc}")
        log_debug("Technical details:\n" + traceback.format_exc())
        exit_code = exc.exit_code
    except KeyboardInterrupt:
        log_error("Operation cancelled by the user.")
        exit_code = EXIT_INTERRUPT
    except Exception as exc:  # noqa: BLE001 - last line of defense; troubleshooting info is shown
        log_error(f"Unexpected error ({type(exc).__name__}): {exc}")
        log_debug("Technical details:\n" + traceback.format_exc())
        if not verbose:
            log_info("Run with -v to see the full traceback.")
        exit_code = EXIT_FAILURE
    finally:
        close_database_connection(ctx)
        cleanup_temp_dir(ctx.temp_dir)

    if exit_code != EXIT_OK:
        if not ctx.server_phase_started:  # no rollback once the DNS / Apache / SSL steps have begun
            rollback(ctx)
        if ctx.db_created_name is not None:  # the database is never dropped automatically
            log_warning(
                f"Database {ctx.db_created_name} was created by this run and was left in place "
                f"(databases are never dropped automatically)."
            )

    print_report(reporter, ctx, exit_code)
    return exit_code


# ======================================================================
# CLI
# ======================================================================
class _ArgumentParser(argparse.ArgumentParser):
    """ArgumentParser whose usage errors use the same timestamped log format."""

    def error(self, message: str) -> NoReturn:
        log_error(f"{self.prog}: {message}")
        log_info(f"Run '{self.prog} --help' to see the available options.")
        raise SystemExit(2)


def _configure_console_encoding() -> None:
    """Make output UTF-8 so non-ASCII paths do not raise encoding errors on Windows."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = _ArgumentParser(
        prog="wp_installer.py",
        description="Download and prepare Persian WordPress (Windows / Linux).",
        epilog=(
            "Steps 9-17 (Linux, root): DNS record, Apache, SSL certificate and Arvan Cloud. They need the "
            "environment variables ARVAN_API_TOKEN and CERTBOT_EMAIL; sudo drops them, so run: "
            "sudo ARVAN_API_TOKEN=... CERTBOT_EMAIL=... python3 wp_installer.py"
        ),
    )
    parser.add_argument(
        "--name",
        help="Site name without the suffix (e.g. test). If omitted, you will be prompted.",
    )
    parser.add_argument(
        "--base-dir",
        help=(
            f"Base install directory. Linux default: {LINUX_BASE_DIR}. "
            f"On Windows you are prompted at run time (or set the {BASE_DIR_ENV_VAR} environment variable)."
        ),
    )
    parser.add_argument(
        "--skip-server-setup",
        action="store_true",
        help="Only install WordPress and create the database; skip the DNS, Apache and SSL steps (9-17).",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Show troubleshooting details (traceback)")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    _configure_console_encoding()
    args = parse_args(argv)
    return run_installer(args.name, args.base_dir, args.verbose, args.skip_server_setup)


if __name__ == "__main__":
    sys.exit(main())
