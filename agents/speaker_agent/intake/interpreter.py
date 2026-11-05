from __future__ import annotations

import json
from pathlib import Path

from agents.speaker_agent.domain.intake import IntakeDecision, IntakeError, parse_intake_decision
from shared.llm.contracts import JsonCompletionClient


class ModelDemandInterpreter:
    def __init__(self, client: JsonCompletionClient, *, max_chars: int = 12000) -> None:
        self.client = client
        self.max_chars = max_chars
        self.prompt = (Path(__file__).resolve().parents[1] / "templates" / "intake_prompt.md").read_text(encoding="utf-8-sig")

    def interpret(self, text: str) -> IntakeDecision:
        if not text.strip() or len(text) > self.max_chars:
            raise IntakeError("聊天内容为空或超过输入字符上限，请拆分后提交", retryable=False)
        content = self.client.complete(self.prompt, text)
        try:
            value = json.loads(content)
        except (ValueError, TypeError):
            raise IntakeError("模型未返回有效的 JSON 需求单") from None
        return parse_intake_decision(value)
