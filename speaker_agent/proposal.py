from __future__ import annotations

from datetime import datetime

from .models import DemandRecord, Proposal


PROMPT_FIELDS = (
    "\u9700\u6c42\u80cc\u666f",
    "\u4e1a\u52a1\u76ee\u6807",
    "\u529f\u80fd\u8981\u6c42",
    "\u975e\u529f\u80fd\u8981\u6c42",
    "\u7ea6\u675f\u6761\u4ef6",
    "\u9a8c\u6536\u6807\u51c6",
    "\u4f18\u5148\u7ea7",
    "\u671f\u671b\u4e0a\u7ebf\u65f6\u95f4",
    "\u76f8\u5173\u94fe\u63a5",
)


class ProposalGenerator:
    """Build a reviewable implementation proposal from one ledger record."""

    def generate(self, record: DemandRecord, *, now: datetime | None = None) -> Proposal:
        generated_at = (now or datetime.now()).strftime("%Y-%m-%d %H:%M")
        title = record.title or f"\u9700\u6c42 {record.record_id}"
        details = self._details(record)
        gaps = self._gaps(record)
        markdown = self._markdown(title, generated_at, record, details, gaps)
        summary = self._summary(title, gaps)
        return Proposal(title=f"{title}\u5b9e\u65bd\u65b9\u6848", markdown=markdown, summary=summary)

    def _details(self, record: DemandRecord) -> list[tuple[str, str]]:
        details: list[tuple[str, str]] = []
        for name in PROMPT_FIELDS:
            value = _display(record.fields.get(name))
            if value:
                details.append((name, value))
        if not details:
            for name, raw in record.fields.items():
                if name in {"\u72b6\u6001", "\u65b9\u6848\u94fe\u63a5", "\u5904\u7406\u5907\u6ce8"}:
                    continue
                value = _display(raw)
                if value:
                    details.append((str(name), value))
        return details[:12]

    def _gaps(self, record: DemandRecord) -> list[str]:
        required = ("\u4e1a\u52a1\u76ee\u6807", "\u529f\u80fd\u8981\u6c42", "\u9a8c\u6536\u6807\u51c6")
        return [name for name in required if not _display(record.fields.get(name))]

    def _markdown(
        self,
        title: str,
        generated_at: str,
        record: DemandRecord,
        details: list[tuple[str, str]],
        gaps: list[str],
    ) -> str:
        lines = [
            f"# {title}\u5b9e\u65bd\u65b9\u6848",
            "",
            f"> \u751f\u6210\u65f6\u95f4\uff1a{generated_at}",
            f"> \u53f0\u8d26\u8bb0\u5f55\uff1a{record.record_id}",
            "",
            "## 1. \u9700\u6c42\u6458\u8981",
            "",
        ]
        if details:
            lines.extend(f"- **{name}\uff1a** {value}" for name, value in details)
        else:
            lines.append("- \u53f0\u8d26\u4e2d\u6682\u65e0\u53ef\u7528\u7684\u9700\u6c42\u63cf\u8ff0\uff0c\u9700\u8981\u5148\u8865\u5145\u80cc\u666f\u3001\u76ee\u6807\u548c\u9a8c\u6536\u6807\u51c6\u3002")
        lines.extend(["", "## 2. \u5efa\u8bae\u65b9\u6848", ""])
        lines.extend([
            "- \u4fdd\u6301\u73b0\u6709\u4e1a\u52a1\u6d41\u7a0b\u4e0d\u53d8\uff0c\u65b0\u589e\u80fd\u529b\u4ee5\u72ec\u7acb\u6a21\u5757\u63a5\u5165\u3002",
            "- \u5148\u5b9a\u4e49\u8f93\u5165\u3001\u8f93\u51fa\u3001\u6743\u9650\u3001\u5931\u8d25\u56de\u9000\u548c\u5ba1\u8ba1\u65e5\u5fd7\uff0c\u518d\u8fdb\u5165\u5f00\u53d1\u3002",
            "- \u5c06\u9700\u6c42\u62c6\u6210\u53ef\u72ec\u7acb\u9a8c\u6536\u7684\u6700\u5c0f\u95ed\u73af\uff1a\u6570\u636e\u8bfb\u53d6\u3001\u6838\u5fc3\u5904\u7406\u3001\u7ed3\u679c\u56de\u5199\u3001\u5f02\u5e38\u901a\u77e5\u3002",
            "- \u5bf9\u5916\u90e8\u4f9d\u8d56\u4f7f\u7528\u663e\u5f0f\u914d\u7f6e\u548c\u8d85\u65f6\u63a7\u5236\uff0c\u907f\u514d\u5931\u8d25\u540e\u91cd\u590d\u6267\u884c\u3002",
            "",
            "## 3. \u5b9e\u65bd\u6b65\u9aa4",
            "",
            "1. \u786e\u8ba4\u5e72\u7cfb\u4eba\u3001\u6570\u636e\u6765\u6e90\u3001\u6743\u9650\u8fb9\u754c\u548c\u4e0a\u7ebf\u7a97\u53e3\u3002",
            "2. \u8865\u9f50\u7f3a\u5931\u5b57\u6bb5\uff0c\u5e76\u7528\u4e00\u6761\u771f\u5b9e\u8bb0\u5f55\u9a8c\u8bc1\u5b57\u6bb5\u89e3\u6790\u3002",
            "3. \u5b9e\u73b0\u6700\u5c0f\u53ef\u7528\u6d41\u7a0b\uff0c\u5148\u8986\u76d6\u6210\u529f\u8def\u5f84\u548c\u5931\u8d25\u56de\u5199\u3002",
            "4. \u589e\u52a0\u91cd\u590d\u63d0\u4ea4\u3001\u6743\u9650\u4e0d\u8db3\u3001\u5916\u90e8\u8d85\u65f6\u548c\u6570\u636e\u7f3a\u5931\u6d4b\u8bd5\u3002",
            "5. \u5c0f\u8303\u56f4\u8bd5\u8fd0\u884c\uff0c\u6838\u5bf9\u7ed3\u679c\u540e\u518d\u6269\u5927\u4f7f\u7528\u8303\u56f4\u3002",
            "",
            "## 4. \u9a8c\u6536\u4e0e\u98ce\u9669",
            "",
            "- \u9a8c\u6536\u65f6\u540c\u65f6\u6838\u5bf9\u529f\u80fd\u7ed3\u679c\u3001\u6743\u9650\u3001\u5931\u8d25\u63d0\u793a\u548c\u6570\u636e\u56de\u5199\u662f\u5426\u4e00\u81f4\u3002",
            "- \u82e5\u9700\u6c42\u53d8\u66f4\uff0c\u4fdd\u7559\u539f\u8bb0\u5f55\u5e76\u65b0\u589e\u7248\u672c\u8bf4\u660e\uff0c\u907f\u514d\u8986\u76d6\u5df2\u786e\u8ba4\u7684\u8303\u56f4\u3002",
        ])
        if gaps:
            lines.extend(["- \u5f53\u524d\u7f3a\u5c11\u5173\u952e\u4fe1\u606f\uff1a" + "\u3001".join(gaps) + "\u3002\u5efa\u8bae\u8865\u9f50\u540e\u518d\u8bc4\u4f30\u5de5\u671f\u3002"])
        else:
            lines.append("- \u5173\u952e\u5b57\u6bb5\u5df2\u9f50\u5168\uff0c\u53ef\u8fdb\u5165\u5de5\u671f\u8bc4\u4f30\u548c\u4efb\u52a1\u62c6\u5206\u3002")
        if record.record_url:
            lines.extend(["", "## 5. \u6765\u6e90", "", record.record_url])
        return "\n".join(lines).strip() + "\n"

    def _summary(self, title: str, gaps: list[str]) -> str:
        if gaps:
            return f"\u300a{title}\u300b\u65b9\u6848\u5df2\u751f\u6210\uff0c\u4f46\u8fd8\u7f3a\u5c11\uff1a" + "\u3001".join(gaps)
        return f"\u300a{title}\u300b\u65b9\u6848\u5df2\u751f\u6210\uff0c\u53ef\u8fdb\u5165\u8bc4\u5ba1\u3002"


def _display(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        parts = [item for item in (_display(item) for item in value) if item]
        return "\u3001".join(parts)
    if isinstance(value, dict):
        for key in ("text", "name", "value", "link", "url"):
            if key in value:
                shown = _display(value.get(key))
                if shown:
                    return shown
        parts = [item for item in (_display(item) for item in value.values()) if item]
        return "\u3001".join(parts)
    return str(value).strip()
