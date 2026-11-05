from __future__ import annotations

import logging
import threading

from dotenv import load_dotenv

from agents.speaker_agent.config import ConfigError, Settings
from agents.speaker_agent.adapters.feishu.events import register_handlers
from agents.speaker_agent.adapters.feishu.gateway import FeishuGateway
from agents.speaker_agent.orchestration.service import DemandAgent
from shared.feishu.connection import create_socket_client, new_event_dispatcher


LOGGER = logging.getLogger(__name__)


def main() -> None:
    load_dotenv(encoding="utf-8-sig")
    try:
        settings = Settings.from_env()
        settings.validate_for_startup()
    except (ConfigError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc

    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings.runtime_dir.mkdir(parents=True, exist_ok=True)
    if settings.enable_chat_commands and not settings.bot_open_id:
        LOGGER.warning("FEISHU_BOT_OPEN_ID 未设置：仅接收私聊，群聊不会触发建档")

    gateway = FeishuGateway(settings)
    agent = DemandAgent(settings, gateway)
    dispatcher = register_handlers(
        new_event_dispatcher(),
        agent,
    ).build()

    worker = threading.Thread(target=agent.run, name="demand-agent", daemon=True)
    worker.start()
    if settings.enable_polling:
        poller = threading.Thread(
            target=_poll_loop,
            args=(agent, settings.poll_seconds),
            name="demand-poller",
            daemon=True,
        )
        poller.start()

    LOGGER.info("Feishu demand agent started")
    socket_client = create_socket_client(settings.feishu, dispatcher, log_level=settings.log_level)
    socket_client.start()


def _poll_loop(agent: DemandAgent, interval_seconds: int) -> None:
    while True:
        try:
            accepted = agent.scan_pending()
            if accepted:
                LOGGER.info("queued %s pending ledger records", accepted)
        except Exception:
            LOGGER.exception("pending ledger scan failed")
        if agent._stopped.wait(interval_seconds):
            return


if __name__ == "__main__":
    main()
