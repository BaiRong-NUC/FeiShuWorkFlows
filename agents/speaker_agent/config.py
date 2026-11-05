from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from shared.llm.config import DeepSeekConfig
from shared.feishu.config import BitableConfig, FeishuConfig
from shared.feishu.catalog import parse_file_url


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
    source_file_token: str
    source_table_id: str
    source_file_url: str
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
    intake_mode: str = "deepseek"
    deepseek: DeepSeekConfig = field(default_factory=DeepSeekConfig)
    intake_max_chars: int = 12000
    brief_field: str = "需求单"
    ledger_urls: tuple[str, ...] = ()
    chat_target: str = ""
    source_table: str = ""
    source_kind: str = "bitable"
    sheet_id_field: str = "ID"
    sheet_note_field: str = "Agent备注"
    message_mode: str = "personal"
    personal_poll_seconds: int = 10
    personal_include_self: bool = False

    @property
    def feishu(self) -> FeishuConfig:
        return FeishuConfig(self.app_id, self.app_secret)

    @property
    def bitable(self) -> BitableConfig:
        return BitableConfig(self.source_file_token, self.source_table_id, self.source_file_url)

    @classmethod
    def from_env(cls) -> "Settings":
        credentials = FeishuConfig.from_env()
        urls = tuple(part.strip() for part in re.split(r"[,;\n]+", _value("FEISHU_LEDGER_URLS")) if part.strip())
        if not urls:
            raise ConfigError("请配置 FEISHU_LEDGER_URLS：支持一个或多个电子表格、多维表格或知识库文件链接")
        for url in urls:
            parse_file_url(url)

        poll_seconds = int(_value("FEISHU_POLL_SECONDS", "300") or "300")
        if poll_seconds < 30:
            raise ConfigError("FEISHU_POLL_SECONDS must be at least 30")

        runtime_dir = Path(_value("FEISHU_RUNTIME_DIR", "runtime") or "runtime")
        return cls(
            app_id=credentials.app_id,
            app_secret=credentials.app_secret,
            source_file_token="",
            source_table_id="",
            source_file_url="",
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
            intake_mode=_value("SPEAKER_INTAKE_MODE", "deepseek").lower(),
            deepseek=DeepSeekConfig.from_env(),
            intake_max_chars=int(_value("SPEAKER_INTAKE_MAX_CHARS", "12000")),
            brief_field=_value("FEISHU_BRIEF_FIELD", "需求单"),
            ledger_urls=urls,
            chat_target=_value("SPEAKER_CHAT_TARGET"),
            sheet_id_field=_value("FEISHU_SHEETS_ID_FIELD", "ID"),
            sheet_note_field=_value("FEISHU_SHEETS_NOTE_FIELD", "Agent备注"),
            message_mode=_value("FEISHU_MESSAGE_MODE", "personal").lower(),
            personal_poll_seconds=int(_value("FEISHU_PERSONAL_POLL_SECONDS", "10")),
            personal_include_self=_bool("FEISHU_PERSONAL_INCLUDE_SELF", False),
        )

    def validate_for_startup(self) -> None:
        if self.message_mode not in {"personal", "bot"}:
            raise ConfigError("FEISHU_MESSAGE_MODE 必须是 personal 或 bot")
        if not 5 <= self.personal_poll_seconds <= 300:
            raise ConfigError("FEISHU_PERSONAL_POLL_SECONDS 须在 5–300 秒之间")
        if self.message_mode == "personal" and not self.enable_polling:
            raise ConfigError("个人模式通过轮询读取台账，请设置 FEISHU_ENABLE_POLLING=true")
        for url in self.ledger_urls:
            parse_file_url(url)
        if not self.ledger_urls:
            raise ConfigError("请填写台账文件链接")
        if not self.sheet_id_field or not self.sheet_note_field:
            raise ConfigError("电子表格的 ID 列与 Agent 备注列名称不能为空")
        if not self.result_field or not self.raw_field:
            raise ConfigError("FEISHU_RESULT_FIELD and FEISHU_RAW_FIELD are required")
        self.validate_intake()
        # A notification group is optional; the originating chat/requester is sufficient.

    def validate_intake(self) -> None:
        if self.intake_mode not in {"deepseek", "rules"}:
            raise ConfigError("SPEAKER_INTAKE_MODE 必须是 deepseek 或 rules")
        if self.intake_mode == "rules":
            return
        self.deepseek.validate()
        if not 1 <= self.intake_max_chars <= 50000:
            raise ConfigError("SPEAKER_INTAKE_MAX_CHARS 须在 1–50000 之间")
        if not self.brief_field or self.brief_field in {
            self.raw_field, self.result_field, self.status_field, self.title_field,
            self.requester_field, self.error_field, self.proposal_field,
        }:
            raise ConfigError("FEISHU_BRIEF_FIELD 必须是独立的需求单文本列")
