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
    bot_open_id: str = ""
    result_field: str = "处理结论"
    raw_field: str = "需求原文"
    reply_template: Path | None = None
    create_documents: bool = False
    status_field_id: str = ""

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
            trigger_status=_value("FEISHU_TRIGGER_STATUS", "待生成方案"),
            processing_status=_value("FEISHU_PROCESSING_STATUS", "生成中"),
            completed_status=_value("FEISHU_COMPLETED_STATUS", "已生成"),
            failed_status=_value("FEISHU_FAILED_STATUS", "生成失败"),
            status_field=_value("FEISHU_STATUS_FIELD", "状态"),
            title_field=_value("FEISHU_TITLE_FIELD", "需求标题"),
            requester_field=_value("FEISHU_REQUESTER_FIELD", "提出人"),
            proposal_field=_value("FEISHU_PROPOSAL_FIELD", "方案链接"),
            error_field=_value("FEISHU_ERROR_FIELD", "处理备注"),
            runtime_dir=runtime_dir,
            poll_seconds=poll_seconds,
            enable_polling=_bool("FEISHU_ENABLE_POLLING", True),
            enable_chat_commands=_bool("FEISHU_ENABLE_CHAT_COMMANDS", True),
            log_level=_value("FEISHU_LOG_LEVEL", "INFO").upper() or "INFO",
            bot_open_id=_value("FEISHU_BOT_OPEN_ID"),
            result_field=_value("FEISHU_RESULT_FIELD", "处理结论"),
            raw_field=_value("FEISHU_RAW_FIELD", "需求原文"),
            reply_template=Path(_value("SPEAKER_REPLY_TEMPLATE")) if _value("SPEAKER_REPLY_TEMPLATE") else None,
            create_documents=_bool("FEISHU_CREATE_DOCUMENTS", False),
            status_field_id=_value("FEISHU_STATUS_FIELD_ID"),
        )

    def validate_for_startup(self) -> None:
        if not self.result_field or not self.raw_field:
            raise ConfigError("FEISHU_RESULT_FIELD and FEISHU_RAW_FIELD are required")
        # A notification group is optional; the originating chat/requester is sufficient.
