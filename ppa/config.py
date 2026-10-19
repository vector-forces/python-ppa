import logging
import os
import re
from pathlib import Path
from threading import Lock
from typing import Any

import yaml
from pymongo import MongoClient
from dotenv import load_dotenv

# Load local .env variables into system memory at the very beginning
load_dotenv()


def _detect_resources_dir() -> Path:
    """
    Implicitly finds the resources directory by checking the execution context (CWD).
    Falls back to environment variables or the file location if not found.
    """
    # 1. Honor explicit override if provided
    if env_root := os.getenv("PROJECT_ROOT"):
        root = Path(env_root)
        return root if _settings_file(root).exists() else root / "resources"

    # 2. Implicit Discovery: Look at where the application is being executed from
    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        for candidate_dir in (parent, parent / "resources"):
            if _settings_file(candidate_dir).exists():
                return candidate_dir

    # 3. Fallback to package-relative directory if all else fails
    return Path(__file__).resolve().parent / "resources"


def _settings_file(directory: Path, stem: str = "settings") -> Path:
    """Prefer .yaml while retaining support for existing .yml applications."""
    yaml_path = directory / f"{stem}.yaml"
    return yaml_path if yaml_path.exists() else directory / f"{stem}.yml"


# Establish dynamic path routing based on runtime context
RESOURCES_DIR = _detect_resources_dir()
BASE_DIR = RESOURCES_DIR.parent
# Also load .env beside the discovered application settings, without overriding exports.
load_dotenv(RESOURCES_DIR / ".env")
load_dotenv(BASE_DIR / ".env")
FRAMEWORK_LOGGER_NAME = "framework"
DEFAULT_FRAMEWORK_LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
DEFAULT_FRAMEWORK_LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Regular expression pattern to match ${VAR_NAME} or ${VAR_NAME:fallback_value}
ENV_VAR_PATTERN = re.compile(r"\$\{(\w+)(?::([^}]+))?}")


def _parse_log_level(level_name: str) -> int:
    normalized_level = str(level_name).upper()
    level = getattr(logging, normalized_level, None)
    if not isinstance(level, int):
        raise ValueError(f"Invalid logging level configured for framework.logging.level: {level_name}")
    return level


def _as_dict(value: Any, *, context: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise TypeError(f"Expected a mapping for {context}, but got {type(value).__name__}.")
    return value


def _load_and_interpolate_yaml(file_path: Path) -> dict[str, Any]:
    """Parse YAML before substitution so environment values cannot change its structure."""
    if not file_path.exists():
        raise FileNotFoundError(f"Configuration file missing at: {file_path}")

    with open(file_path, "r", encoding="utf-8") as f:
        raw_content = f.read()

    def match_replacer(match: re.Match[str]) -> str:
        env_var_name = match.group(1)
        fallback_value = match.group(2) or ""
        # Pull from live environment parameters; defer to fallback if empty
        return os.getenv(env_var_name, fallback_value)

    def interpolate(node: Any) -> Any:
        if isinstance(node, dict):
            return {key: interpolate(value) for key, value in node.items()}
        if isinstance(node, list):
            return [interpolate(value) for value in node]
        if isinstance(node, str):
            result = ENV_VAR_PATTERN.sub(match_replacer, node)
            if ENV_VAR_PATTERN.fullmatch(node):
                if result.lower() in ("true", "false"):
                    return result.lower() == "true"
                if re.fullmatch(r"-?\d+", result):
                    return int(result)
            return result
        return node

    return _as_dict(interpolate(yaml.safe_load(raw_content)), context=str(file_path))


def _merge_config(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_config(merged[key], value)
        else:
            merged[key] = value
    return merged


class ConfigSettings:
    def __init__(self):
        # 1. Bootstrap: Read the master settings.yml to find the active profile
        master_config_path = _settings_file(RESOURCES_DIR)
        master_data = _load_and_interpolate_yaml(master_config_path)
        app_config = _as_dict(master_data.get("app"), context="settings.app")

        # Safely extract app.profile, defaulting to 'dev' if missing
        self.profile = str(app_config.get("profile", "dev"))

        # 2. Profile Loading: Read the target profile file (e.g., settings-dev.yml)
        if not re.fullmatch(r"[\w-]+", self.profile):
            raise ValueError("app.profile must contain only letters, numbers, underscores or hyphens")
        profile_config_path = _settings_file(RESOURCES_DIR, f"settings-{self.profile}")
        profile_filename = profile_config_path.name
        self._config_data = master_data
        if profile_config_path.exists():
            self._config_data = _merge_config(master_data, _load_and_interpolate_yaml(profile_config_path))
        mongodb_config = _as_dict(self._config_data.get("mongodb"), context=f"{profile_filename}.mongodb")
        postgres_config = _as_dict(
            self._config_data.get("postgresql", self._config_data.get("postgres")),
            context=f"{profile_filename}.postgresql",
        )

        # 3. Expose parameters to the framework
        self.mongo_uri = mongodb_config.get("uri") or os.getenv("mongoURI") or os.getenv("MONGO_URI")
        self.database_name = mongodb_config.get("database")
        self.mongo_options = _as_dict(mongodb_config.get("options"), context="mongodb.options")
        self.postgres_dsn = self._resolve_postgres_dsn(postgres_config)
        self.postgres_database_name = postgres_config.get("database") or postgres_config.get("dbname")

        has_mongo_config = bool(self.mongo_uri and self.database_name)
        has_postgres_config = bool(self.postgres_dsn)

        if mongodb_config and not has_mongo_config:
            raise KeyError(
                f"Invalid configuration format in {profile_filename}. Missing 'mongodb.uri' or 'mongodb.database'.")

        if postgres_config and not has_postgres_config:
            raise KeyError(
                f"Invalid configuration format in {profile_filename}. Provide 'postgresql.dsn' or "
                "'postgresql.host', 'postgresql.database', and 'postgresql.user'.")

        if not has_mongo_config and not has_postgres_config:
            raise KeyError(
                f"Invalid configuration format in {profile_filename}. Configure either 'mongodb' or 'postgresql'.")

        framework_config = _as_dict(self._config_data.get("framework"), context=f"{profile_filename}.framework")
        framework_logging_config = _as_dict(framework_config.get("logging"),
                                            context=f"{profile_filename}.framework.logging")
        self.framework_logs_enabled = bool(framework_logging_config.get("enabled", False))
        self.framework_log_level = _parse_log_level(str(framework_logging_config.get("level", "INFO")))
        self.framework_log_format = str(framework_logging_config.get("format", DEFAULT_FRAMEWORK_LOG_FORMAT))
        self.framework_log_date_format = str(
            framework_logging_config.get("date_format", DEFAULT_FRAMEWORK_LOG_DATE_FORMAT))

    @staticmethod
    def _resolve_postgres_dsn(postgres_config: dict[str, Any]) -> str | None:
        dsn = postgres_config.get("dsn") or postgres_config.get("uri") or postgres_config.get("url")
        if isinstance(dsn, str) and dsn:
            return dsn

        host = postgres_config.get("host")
        database = postgres_config.get("database") or postgres_config.get("dbname")
        user = postgres_config.get("user") or postgres_config.get("username")
        if not host or not database or not user:
            return None

        parts = [
            f"host={host}",
            f"dbname={database}",
            f"user={user}",
        ]
        optional_fields = {
            "password": postgres_config.get("password"),
            "port": postgres_config.get("port"),
            "sslmode": postgres_config.get("sslmode"),
            "connect_timeout": postgres_config.get("connect_timeout"),
        }
        parts.extend(f"{key}={value}" for key, value in optional_fields.items() if value not in (None, ""))
        return " ".join(parts)


def _configure_framework_logger() -> logging.Logger:
    logger = logging.getLogger(FRAMEWORK_LOGGER_NAME)
    logger.handlers.clear()
    logger.propagate = False

    if settings.framework_logs_enabled:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(settings.framework_log_format, settings.framework_log_date_format))
        logger.addHandler(handler)
        logger.setLevel(settings.framework_log_level)
    else:
        logger.addHandler(logging.NullHandler())
        logger.setLevel(logging.CRITICAL + 1)

    return logger


def get_framework_logger(name: str | None = None) -> logging.Logger:
    if not name:
        return logging.getLogger(FRAMEWORK_LOGGER_NAME)
    return logging.getLogger(f"{FRAMEWORK_LOGGER_NAME}.{name}")


# --- SINGLETON INITIALIZATION LAYER ---
# This block triggers exactly once when the app imports this file
settings = ConfigSettings()
framework_logger = _configure_framework_logger()
framework_logger.info("Bootstrapping framework with active profile [%s]", settings.profile)
_mongo_client: MongoClient | None = None
_mongo_lock = Lock()
db = None


def _get_mongo_client() -> MongoClient:
    global _mongo_client, db
    if not isinstance(settings.mongo_uri, str) or not settings.mongo_uri:
        raise ValueError("MongoDB URI must be provided before using Mongo repositories")
    if not isinstance(settings.database_name, str) or not settings.database_name:
        raise ValueError("MongoDB database name must be provided before using Mongo repositories")

    with _mongo_lock:
        if _mongo_client is None:
            _mongo_client = MongoClient(settings.mongo_uri, **settings.mongo_options)
            db = _mongo_client[settings.database_name]
            framework_logger.debug("Mongo client initialized for database [%s]", settings.database_name)
        return _mongo_client


def value(place_holder: str) -> Any:
    """
    Spring Boot-inspired property extraction tool.
    Always extracts and returns the raw value immediately from the active configuration sheet.
    """
    # 1. Enforce your validation constraint
    if place_holder.startswith("${") and not place_holder.endswith("}"):
        raise ValueError(f"Invalid place holder: {place_holder}")

    # 2. Extract key path and optional fallback default value
    if place_holder.startswith("${") and place_holder.endswith("}"):
        raw_content = place_holder[2:-1]  # Strip '${' and '}'

        # Support default fallbacks split by a colon (e.g., ${mongodb.uri:mongodb://localhost})
        if ":" in raw_content:
            path, default_val = raw_content.split(":", 1)
            # Basic primitive type-casting
            if default_val.isdigit():
                default_val = int(default_val)
            elif default_val.lower() == "true":
                default_val = True
            elif default_val.lower() == "false":
                default_val = False
        else:
            path = raw_content
            default_val = None
    else:
        path = place_holder
        default_val = None

    # 3. Direct, eager lookup against the live dictionary
    keys = path.split(".")
    current_node = settings._config_data

    for key in keys:
        if isinstance(current_node, dict) and key in current_node:
            current_node = current_node[key]
        else:
            return default_val

    return current_node


def get_collection(collection_name: str):
    """Provides repositories access to the shared single client pool."""
    return _get_mongo_client()[settings.database_name][collection_name]


def ping_mongo() -> None:
    """Check readiness; propagate PyMongo errors for the application to handle."""
    _get_mongo_client().admin.command("ping")


def get_postgres_connection():
    """Creates a PostgreSQL connection using the active profile settings."""
    if not isinstance(settings.postgres_dsn, str) or not settings.postgres_dsn:
        raise ValueError("PostgreSQL DSN must be provided before using Postgres repositories")

    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise ImportError("Install psycopg to use PostgreSQL repositories.") from exc

    return psycopg.connect(settings.postgres_dsn, row_factory=dict_row)


def close_db_connection():
    """Close at application shutdown, after in-flight requests have completed."""
    global _mongo_client, db
    with _mongo_lock:
        if _mongo_client is not None:
            framework_logger.debug("Closing MongoDB client connection pool")
            _mongo_client.close()
            _mongo_client = None
            db = None
