from __future__ import annotations

import argparse
import logging
import os
import threading
import time
from pathlib import Path

from dotenv import load_dotenv

from agents.speaker_agent.config import ConfigError, Settings
from agents.speaker_agent.adapters.feishu.events import on_message
from agents.speaker_agent.orchestration.workspace import SpeakerWorkspace
from shared.feishu.client import FeishuClient
from shared.feishu.connection import create_socket_client, new_event_dispatcher
from agents.speaker_agent.presentation.brief import BriefFormatter
from agents.speaker_agent.presentation.reply import ReplyFormatter
from shared.runtime.lock import InstanceLock
from shared.runtime.signals import interrupt_signals


LOGGER = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]


def check_configuration(*, for_discovery: bool = False) -> Settings:
    load_dotenv(ROOT / ".env", encoding="utf-8-sig")
    settings = Settings.from_env()
    if not for_discovery:
        settings.validate_for_startup()
    placeholders = {
        "FEISHU_APP_ID": (settings.app_id, "cli_xxx"),
        "FEISHU_APP_SECRET": (settings.app_secret, "your_app_secret"),
    }
    missing = [key for key, (value, example) in placeholders.items() if value == example]
    if missing:
        raise ConfigError("请替换 .env 中的示例配置：" + ", ".join(missing))
    if for_discovery:
        return settings
    ReplyFormatter(settings.reply_template)
    BriefFormatter()
    if settings.intake_mode == "deepseek":
        prompt = Path(__file__).parent / "templates" / "intake_prompt.md"
        if not prompt.read_text(encoding="utf-8-sig").strip():
            raise ConfigError("Speaker 需求整理提示词不能为空")
    return settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Speaker Agent")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true", help="只检查本地配置和模板")
    modes.add_argument("--discover", action="store_true", help="只读发现所有文件内的数据表，不启动工作流")
    args = parser.parse_args(argv)
    os.chdir(ROOT)
    try:
        settings = check_configuration(for_discovery=args.discover)
    except (OSError, ValueError, KeyError) as exc:
        print(f"Speaker 配置检查失败：{exc}")
        return 2
    if args.check:
        print("Speaker 本地配置与模板检查通过（未连接外部服务）")
        return 0

    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        if args.discover:
            tables = FeishuClient(settings.feishu).catalog.discover(settings.ledger_urls)
            for ref in tables:
                print(f"{ref.kind} | {ref.file_title or ref.file_token} | {ref.title} | {ref.table_id}\n  {ref.url}")
            print(f"共发现 {len(tables)} 张表；未读取消息、未写入台账、未调用大模型。")
            return 0
        with interrupt_signals(), InstanceLock(settings.runtime_dir / "speaker.lock"):
            run_agent(settings)
    except KeyboardInterrupt:
        LOGGER.info("Speaker 已停止")
    except (RuntimeError, ValueError) as exc:
        LOGGER.error("Speaker 启动失败：%s", exc)
        return 1
    return 0


def run_agent(settings: Settings) -> None:
    if settings.enable_chat_commands and not settings.bot_open_id:
        LOGGER.warning("FEISHU_BOT_OPEN_ID 未设置：仅接收发给机器人的私聊，群聊不会触发建档；个人账号私聊尚未接入")

    agent = SpeakerWorkspace(settings)
    if not settings.enable_polling and any(ref.kind == "sheet" for ref in agent.tables.values()):
        raise ConfigError("普通电子表格通过轮询接入，请设置 FEISHU_ENABLE_POLLING=true")
    builder = new_event_dispatcher().register_p2_drive_file_bitable_record_changed_v1(agent.on_record_event)
    if settings.enable_chat_commands:
        builder.register_p2_im_message_receive_v1(lambda data: on_message(agent, data))
    dispatcher = builder.build()

    threads: list[threading.Thread] = []
    try:
        socket_client = create_socket_client(settings.feishu, dispatcher, log_level=settings.log_level)
        worker = threading.Thread(target=agent.run, name="demand-agent", daemon=True)
        worker.start()
        threads.append(worker)
        if settings.enable_polling:
            poller = threading.Thread(target=_poll_loop, args=(agent, settings.poll_seconds),
                                      name="demand-poller", daemon=True)
            poller.start()
            threads.append(poller)
        LOGGER.info("Speaker 已启动，正在建立飞书长连接")
        socket_client.start()
        raise RuntimeError("飞书长连接已退出")
    finally:
        agent.stop()
        deadline = time.monotonic() + 5
        for thread in threads:
            thread.join(timeout=max(0, deadline - time.monotonic()))
        if any(thread.is_alive() for thread in threads):
            LOGGER.warning("仍有请求未结束，持久化队列将在下次启动时恢复")


def _poll_loop(agent: SpeakerWorkspace, interval_seconds: int) -> None:
    while not agent._stopped.is_set():
        try:
            accepted = agent.scan_pending()
            if accepted:
                LOGGER.info("queued %s pending ledger records", accepted)
        except Exception:
            LOGGER.exception("pending ledger scan failed")
        if agent._stopped.wait(interval_seconds):
            return


if __name__ == "__main__":
    raise SystemExit(main())
