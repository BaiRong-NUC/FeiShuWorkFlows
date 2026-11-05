from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(ValueError):
    """Raised when required runtime configuration is missing or invalid."""


def _value(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _bool(name: str, default: bool) -> bool:
    raw = _value(name)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    app_id: str
    app_secret: str
    bitable_app_token: str
    bitable_table_id: str
    bitable_url: str
    notify_chat_id: str
    doc_folder_token: str
    trigger_status: str
    processing_status: str
    completed_status: str
    failed_status: str
    status_field: str
    title_field: str
    requester_field: str
    proposal_field: str
    error_field: str
    runtime_dir: Path
    poll_seconds: int
    enable_polling: bool
    enable_chat_commands: bool
    log_level: str

    @classmethod
    def from_env(cls) -> "Settings":
        app_id = _value("FEISHU_APP_ID")
        app_secret = _value("FEISHU_APP_SECRET")
        bitable_app_token = _value("FEISHU_BITABLE_APP_TOKEN")
        bitable_table_id = _value("FEISHU_BITABLE_TABLE_ID")
        missing = [
            name
            for name, value in (
                ("FEISHU_APP_ID", app_id),
                ("FEISHU_APP_SECRET", app_secret),
                ("FEISHU_BITABLE_APP_TOKEN", bitable_app_token),
                ("FEISHU_BITABLE_TABLE_ID", bitable_table_id),
            )
            if not value
        ]
        if missing:
            raise ConfigError("missing required environment variables: " + ", ".join(missing))

        poll_seconds = int(_value("FEISHU_POLL_SECONDS", "300") or "300")
        if poll_seconds < 30:
            raise ConfigError("FEISHU_POLL_SECONDS must be at least 30")

        runtime_dir = Path(_value("FEISHU_RUNTIME_DIR", "runtime") or "runtime")
        return cls(
            app_id=app_id,
            app_secret=app_secret,
            bitable_app_token=bitable_app_token,
            bitable_table_id=bitable_table_id,
            bitable_url=_value("FEISHU_BITABLE_URL"),
            notify_chat_id=_value("FEISHU_NOTIFY_CHAT_ID"),
            doc_folder_token=_value("FEISHU_DOC_FOLDER_TOKEN"),
            trigger_status=_value("FEISHU_TRIGGER_STATUS", "\u5f85\u751f\u6210\u65b9\u6848"),
            processing_status=_value("FEISHU_PROCESSING_STATUS", "\u751f\u6210\u4e2d"),
            completed_status=_value("FEISHU_COMPLETED_STATUS", "\u5df2\u751f\u6210"),
            failed_status=_value("FEISHU_FAILED_STATUS", "\u751f\u6210\u5931\u8d25"),
            status_field=_value("FEISHU_STATUS_FIELD", "\u72b6\u6001"),
            title_field=_value("FEISHU_TITLE_FIELD", "\u9700\u6c42\u6807\u9898"),
            requester_field=_value("FEISHU_REQUESTER_FIELD", "\u63d0\u51fa\u4eba"),
            proposal_field=_value("FEISHU_PROPOSAL_FIELD", "\u65b9\u6848\u94fe\u63a5"),
            error_field=_value("FEISHU_ERROR_FIELD", "\u5904\u7406\u5907\u6ce8"),
            runtime_dir=runtime_dir,
            poll_seconds=poll_seconds,
            enable_polling=_bool("FEISHU_ENABLE_POLLING", True),
            enable_chat_commands=_bool("FEISHU_ENABLE_CHAT_COMMANDS", True),
            log_level=_value("FEISHU_LOG_LEVEL", "INFO").upper() or "INFO",
        )

    def validate_for_startup(self) -> None:
        if not self.notify_chat_id:
            raise ConfigError(
                "FEISHU_NOTIFY_CHAT_ID is required so generated proposals can be sent back to Feishu"
            )
