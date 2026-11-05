from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen

from shared.feishu.auth.storage import TokenStore
from shared.feishu.config import FeishuConfig
from shared.runtime.lock import InstanceLock

REDIRECT_URI = "http://127.0.0.1:8765/callback"
USER_SCOPES = "offline_access contact:user.base:readonly im:chat:read im:message im:message.p2p_msg:get_as_user im:message.send_as_user"


class AccessTokenProvider(Protocol):
    def access_token(self) -> str: ...


def request_json(url: str, *, payload: dict | None = None, token: str = "") -> dict:
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = Request(url, data=json.dumps(payload).encode() if payload is not None else None,
                      headers=headers)
    try:
        with urlopen(request, timeout=30) as response:
            value = json.loads(response.read())
    except HTTPError as exc:
        # 不记录授权码、Token、响应正文或 SDK 原始请求。
        raise RuntimeError(f"飞书用户授权请求失败（HTTP {exc.code}），请检查应用权限或重新 --login") from None
    except (URLError, OSError, ValueError):
        raise RuntimeError("飞书用户授权请求未完成；保留原授权，请检查网络，必要时重新 --login") from None
    if not isinstance(value, dict) or value.get("code", 0) != 0 or value.get("error"):
        raise RuntimeError("飞书拒绝用户授权请求，请检查应用权限、授权范围并重新 --login")
    return value


class UserOAuth:
    def __init__(self, config: FeishuConfig, runtime: Path) -> None:
        self.config = config
        self.store = TokenStore(runtime / "auth" / "feishu-user.dat")
        self.lock_path = runtime / "auth" / "token.lock"
        self._lock = threading.RLock()
        self.expected_user = ""

    def _exchange(self, grant: dict) -> dict:
        return request_json("https://open.feishu.cn/open-apis/authen/v2/oauth/token", payload={
            "client_id": self.config.app_id, "client_secret": self.config.app_secret, **grant,
        })

    def _token_record(self, result: dict, *, previous: dict | None = None) -> dict:
        previous = previous or {}
        if not result.get("access_token") or not result.get("refresh_token"):
            raise RuntimeError("授权未返回可刷新的 Token，请确认申请 offline_access 后重新 --login")
        try:
            lifetime = int(result["expires_in"])
            if lifetime <= 0:
                raise ValueError
        except (KeyError, ValueError, TypeError):
            raise RuntimeError("飞书返回的用户 Token 有效期无效，请重新 --login") from None
        return {**previous, "app_id": self.config.app_id, "access_token": result["access_token"],
                "refresh_token": result["refresh_token"], "expires_at": time.time() + lifetime,
                "scope": result.get("scope", previous.get("scope", ""))}

    def status(self) -> dict:
        record = self.store.load()
        if record.get("app_id") != self.config.app_id or not record.get("open_id"):
            raise RuntimeError("用户授权与当前应用不匹配，请重新 --login")
        if (not isinstance(record.get("expires_at"), (int, float)) or
                any(not isinstance(record.get(key), str) or not record[key]
                    for key in ("access_token", "refresh_token", "open_id"))):
            raise RuntimeError("用户授权文件字段无效，请重新 --login")
        if self.expected_user and self.expected_user != record["open_id"]:
            raise RuntimeError("登录账号已变化，请重启项目，防止使用不同账号回复旧需求")
        return record

    def access_token(self) -> str:
        with self._lock, InstanceLock(self.lock_path):
            record = self.status()
            if record.get("refresh_uncertain"):
                raise RuntimeError("上次 Token 刷新结果不确定，请重新 --login；不会重复消费刷新凭据")
            if record["expires_at"] <= time.time() + 120:
                # 刷新令牌只可使用一次。先持久化标记，避免进程崩溃/断网后重放。
                self.store.save({**record, "refresh_uncertain": True})
                result = self._exchange({"grant_type": "refresh_token", "refresh_token": record["refresh_token"]})
                record = self._token_record(result, previous=record)
                record.pop("refresh_uncertain", None)
                self.store.save(record)
            token = record.get("access_token")
            if not isinstance(token, str) or not token:
                raise RuntimeError("用户 Token 无效，请重新 --login")
            return token

    def profile(self) -> dict:
        response = request_json("https://open.feishu.cn/open-apis/authen/v1/user_info", token=self.access_token())
        data = response.get("data", {})
        if not isinstance(data, dict) or data.get("open_id") != self.status()["open_id"]:
            raise RuntimeError("用户身份校验失败，请重新 --login")
        self.expected_user = data["open_id"]
        return data

    def login(self, *, timeout: int = 180) -> None:
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        url = "https://open.feishu.cn/open-apis/authen/v1/authorize?" + urlencode({
            "client_id": self.config.app_id, "redirect_uri": REDIRECT_URI, "response_type": "code",
            "scope": USER_SCOPES, "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
        })
        outcome: dict[str, str] = {}

        class Callback(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                pass

            def do_GET(self) -> None:
                parsed = urlsplit(self.path)
                query = parse_qs(parsed.query)
                valid = parsed.path == "/callback" and secrets.compare_digest(query.get("state", [""])[0], state)
                if not valid:
                    self.send_error(400, "Invalid callback")
                    return
                outcome["code"] = query.get("code", [""])[0]
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write("授权回调已收到，请回到终端查看结果。".encode())

        with self._lock, InstanceLock(self.lock_path), HTTPServer(("127.0.0.1", 8765), Callback) as server:
            server.timeout = 1
            print(f"请先在应用安全设置中添加重定向 URL：{REDIRECT_URI}")
            print(f"请在浏览器使用你的个人飞书账号授权：\n{url}")
            webbrowser.open(url)
            deadline = time.monotonic() + timeout
            while not outcome and time.monotonic() < deadline:
                server.handle_request()
            if not outcome.get("code"):
                raise RuntimeError("授权取消或等待超时，请重新运行 --login")
            result = self._exchange({"grant_type": "authorization_code", "code": outcome["code"],
                                     "redirect_uri": REDIRECT_URI, "code_verifier": verifier})
            record = self._token_record(result)
            profile = request_json("https://open.feishu.cn/open-apis/authen/v1/user_info", token=record["access_token"]).get("data", {})
            if not isinstance(profile, dict) or not profile.get("open_id"):
                raise RuntimeError("无法获取授权账号，请检查用户基本信息权限")
            self.store.save({**record, "open_id": profile["open_id"], "name": profile.get("name", "")})
            print("个人飞书授权已安全保存。运行 python main.py --check-user 只读检查权限。")
