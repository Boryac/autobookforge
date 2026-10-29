"""模块 12：目录监听（watchdog）。

监听目录内新增 / 变动的 TXT 文件，自动送入流水线处理，支持批量模式。
"""

from __future__ import annotations

import os
import time
from typing import Callable, Optional


def _is_txt(path: str) -> bool:
    return path.lower().endswith(".txt")


class _TxtHandler:
    """鸭子类型事件处理器（避免强依赖 watchdog 的类继承）。"""

    def __init__(self, callback: Callable[[str], None], debounce: float = 1.5) -> None:
        self.callback = callback
        self.debounce = debounce
        self._seen: dict[str, float] = {}

    def _dispatch(self, path: str, is_directory: bool, event_type: str) -> None:
        if is_directory or not _is_txt(path):
            return
        if event_type not in ("created", "modified"):
            return
        now = time.time()
        last = self._seen.get(path, 0.0)
        if now - last < self.debounce:
            return
        self._seen[path] = now
        try:
            self.callback(path)
        except Exception as exc:  # pragma: no cover
            print(f"[watch] 处理失败 {path}: {exc}")

    # watchdog 会调用这些方法
    def on_created(self, event) -> None:  # pragma: no cover
        self._dispatch(event.src_path, event.is_directory, "created")

    def on_modified(self, event) -> None:  # pragma: no cover
        self._dispatch(event.src_path, event.is_directory, "modified")

    def on_moved(self, event) -> None:  # pragma: no cover
        dest = getattr(event, "dest_path", None)
        if dest:
            self._dispatch(dest, event.is_directory, "created")


def process_existing(directory: str, callback: Callable[[str], None]) -> int:
    """处理目录中已存在的 TXT 文件（批量模式）。"""
    count = 0
    for name in sorted(os.listdir(directory)):
        path = os.path.join(directory, name)
        if os.path.isfile(path) and _is_txt(path):
            callback(path)
            count += 1
    return count


def watch_directory(
    directory: str,
    output_dir: str,
    *,
    style: str = "literary",
    correct_level: int = 2,
    process_existing_files: bool = True,
    on_result: Optional[Callable[[object], None]] = None,
) -> None:
    """阻塞式监听目录并自动处理。"""
    try:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer
    except Exception:
        raise RuntimeError("未安装 watchdog，请先 pip install watchdog")

    from ..pipeline.orchestrator import PipelineOptions, run_pipeline

    def handle(path: str) -> None:
        print(f"[watch] 发现文件: {path}")
        result = run_pipeline(
            path,
            output_dir=output_dir,
            style=style,
            correct_level=correct_level,
            progress=lambda s, c, t, m: print(f"  [{s}] {m}"),
        )
        print(f"[watch] 完成: {result.book_title} -> {result.output_files}")
        if on_result:
            on_result(result)

    # 把鸭子类型处理器适配为 watchdog 事件处理器
    base = type("_Base", (FileSystemEventHandler,), {})
    handler = _TxtHandler(handle)

    class _Adapter(base):  # type: ignore[misc, valid-type]
        def on_created(self, event):  # pragma: no cover
            handler.on_created(event)

        def on_modified(self, event):  # pragma: no cover
            handler.on_modified(event)

        def on_moved(self, event):  # pragma: no cover
            handler.on_moved(event)

    os.makedirs(directory, exist_ok=True)
    if process_existing_files:
        n = process_existing(directory, handle)
        print(f"[watch] 批量处理已有文件: {n} 个")

    observer = Observer()
    observer.schedule(_Adapter(), directory, recursive=False)
    observer.start()
    print(f"[watch] 正在监听: {directory}（Ctrl+C 退出）")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:  # pragma: no cover
        observer.stop()
    observer.join()
