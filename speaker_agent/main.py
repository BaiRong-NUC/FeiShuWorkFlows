from __future__ import annotations

import logging
import threading

import lark_oapi as lark
from dotenv import load_dotenv

from .config import ConfigError, Settings
from .events import register_handlers
from .feishu import FeishuGateway
from .service import DemandAgent


LOGGER = logging.getLogger(__name__)


def main() -> None:
    load_dotenv()
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

    gateway = FeishuGateway(settings)
    agent = DemandAgent(settings, gateway)
    dispatcher = register_handlers(
        lark.EventDispatcherHandler.builder("", ""),
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
    socket_client = lark.ws.Client(
        settings.app_id,
        settings.app_secret,
        event_handler=dispatcher,
        log_level=getattr(lark.LogLevel, settings.log_level, lark.LogLevel.INFO),
    )
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
