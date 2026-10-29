"""纠错结果缓存：相同文本重复处理零成本。

键为「纠错档位 + 全文 SHA-256」，磁盘 JSON 持久化 + 进程内 LRU 内存缓存。
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from pathlib import Path
from typing import Optional

from ..corrector.rule_engine import Correction, CorrectionResult

CACHE_VERSION = "v1"


class CorrectionCache:
    """纠错结果缓存。"""

    def __init__(self, cache_dir: str, *, enabled: bool = True, max_memory: int = 32) -> None:
        self.dir = Path(cache_dir)
        self.enabled = enabled
        self.max_memory = max_memory
        self._mem: "OrderedDict[str, CorrectionResult]" = OrderedDict()
        if self.enabled:
            self.dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def make_key(text: str, level: int) -> str:
        h = hashlib.sha256()
        h.update(f"{CACHE_VERSION}|{level}|".encode("utf-8"))
        h.update(text.encode("utf-8"))
        return h.hexdigest()

    def _path(self, key: str) -> Path:
        return self.dir / f"{key}.json"

    def get(self, key: str) -> Optional[CorrectionResult]:
        if not self.enabled:
            return None
        if key in self._mem:
            self._mem.move_to_end(key)
            return self._mem[key]
        path = self._path(key)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            result = CorrectionResult(
                data.get("text", ""),
                [Correction(**c) for c in data.get("corrections", [])],
            )
        except Exception:
            return None
        self._remember(key, result)
        return result

    def set(self, key: str, result: CorrectionResult) -> None:
        if not self.enabled:
            return
        self._remember(key, result)
        try:
            payload = {
                "text": result.text,
                "corrections": [c.as_dict() for c in result.corrections],
            }
            self._path(key).write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            pass

    def _remember(self, key: str, result: CorrectionResult) -> None:
        self._mem[key] = result
        self._mem.move_to_end(key)
        while len(self._mem) > self.max_memory:
            self._mem.popitem(last=False)

    def clear_memory(self) -> None:
        self._mem.clear()

    def stats(self) -> dict:
        files = len(list(self.dir.glob("*.json"))) if self.dir.exists() else 0
        return {"enabled": self.enabled, "memory_entries": len(self._mem), "disk_entries": files}
