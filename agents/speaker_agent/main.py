from __future__ import annotations

import argparse
import logging
import os
import threading
import time
import hashlib
from dataclasses import replace
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
from shared.feishu.auth.oauth import UserOAuth
from shared.feishu.personal import PersonalChatClient
from agents.speaker_agent.adapters.feishu.personal import PersonalPoller
from agents.speaker_agent.persistence.personal import PersonalInbox
from agents.speaker_agent.orchestration.personal import PersonalIntake


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
    if settings.message_mode == "personal":
        PersonalIntake.load_away_reply()
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
    modes.add_argument("--login", action="store_true", help="浏览器授权个人飞书账号")
    modes.add_argument("--check-user", action="store_true", help="只读检查用户身份及会话读取权限，不发送消息")
    args = parser.parse_args(argv)
    os.chdir(ROOT)
    try:
        settings = check_configuration(for_discovery=args.discover or args.login or args.check_user)
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
        if args.login or args.check_user:
            # 与正常运行共用实例锁，禁止运行中切换账号或并发轮换凭据。
            with interrupt_signals(), InstanceLock(settings.runtime_dir / "speaker.lock"):
                oauth = UserOAuth(settings.feishu, settings.runtime_dir)
                if args.login:
                    oauth.login()
                else:
                    profile = oauth.profile()
                    feishu = FeishuClient(settings.feishu, user_tokens=oauth)
                    api = PersonalChatClient(feishu.client, oauth)
                    chats = api.chats()
                    for chat in chats:
                        api.messages(chat, int(time.time()) - 60, int(time.time()))
                    print(f"已授权账号：{profile.get('name', '')}；成功检查 {len(chats)} 个单聊。")
                    print("未输出消息正文、未发消息、未写台账、未调用模型；发送权限需实际联调验证。")
            return 0
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
    except (RuntimeError, ValueError, OSError) as exc:
        LOGGER.error("Speaker 启动失败：%s", exc)
        return 1
    return 0


def run_agent(settings: Settings) -> None:
    if settings.message_mode == "personal":
        run_personal(settings)
        return
    if settings.enable_chat_commands and not settings.bot_open_id:
        LOGGER.warning("机器人模式未设置 FEISHU_BOT_OPEN_ID：仅接收发给机器人的私聊")

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


def run_personal(settings: Settings) -> None:
    oauth = UserOAuth(settings.feishu, settings.runtime_dir)
    profile = oauth.profile()
    namespace = hashlib.sha256((settings.app_id + ":" + profile["open_id"]).encode()).hexdigest()[:20]
    personal_settings = replace(settings, runtime_dir=settings.runtime_dir / "personal" / namespace)
    # 账号隔离业务队列，切换授权账号后不会回复原账号的任务。
    inbox = PersonalInbox(personal_settings.runtime_dir / "inbox.sqlite3", int(time.time() * 1000),
                          own_id=profile["open_id"], include_self=settings.personal_include_self)
    feishu = FeishuClient(settings.feishu, user_tokens=oauth)
    feishu.messages.observer = inbox
    agent = SpeakerWorkspace(personal_settings, feishu=feishu)
    poller = PersonalPoller(PersonalChatClient(feishu.client, oauth, own_id=profile["open_id"],
                                             include_self=settings.personal_include_self),
                            inbox, profile["open_id"], include_self=settings.personal_include_self)
    intake = PersonalIntake(agent, inbox)
    threads: list[threading.Thread] = []
    try:
        for target, args, name in ((agent.run, (), "speaker-worker"),
                                   (_poll_loop, (agent, settings.poll_seconds), "ledger-poller")):
            thread = threading.Thread(target=target, args=args, name=name, daemon=True)
            thread.start()
            threads.append(thread)
        LOGGER.info("个人飞书模式已启动；使用用户授权，无消息长连接；账号：%s", profile.get("name", ""))
        LOGGER.info("下层交接目录：%s", personal_settings.runtime_dir / "handoff")
        LOGGER.info("个人消息接入=%s；接收本人手动消息=%s；自动回复始终排除",
                    settings.enable_chat_commands, settings.personal_include_self)
        while not agent._stopped.is_set():
            if settings.enable_chat_commands:
                try:
                    poller.poll()
                except Exception:
                    LOGGER.exception("个人会话枚举失败，稍后重试；未切换为机器人身份")
                intake.drain()
            agent._stopped.wait(settings.personal_poll_seconds)
    finally:
        agent.stop()
        deadline = time.monotonic() + 5
        for thread in threads:
            thread.join(timeout=max(0, deadline - time.monotonic()))
        if any(thread.is_alive() for thread in threads):
            LOGGER.warning("仍有请求未结束，个人消息与工作流将在下次启动时恢复")


if __name__ == "__main__":
    raise SystemExit(main())
