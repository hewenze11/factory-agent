"""
gc.py - Trash GC（垃圾回收）

策略：
- 超7天的 bak 目录/文件直接清理
- trash 目录总体积 > 10GB 时，按时间戳从旧到新清理，直到 < 10GB
- 时机：启动时 + 每日0点（threading.Timer 循环）
- 并发：所有 trash 操作共用一把 threading.Lock()
- FileNotFoundError 在 GC 遍历时直接 skip
"""

import os
import shutil
import threading
import time
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

TRASH_DIR = os.environ.get("FACTORY_TRASH_DIR", "/opt/factory/trash")
MAX_TRASH_BYTES = 10 * 1024 * 1024 * 1024  # 10 GB
MAX_AGE_DAYS = 7

# 全局 trash 锁（所有 trash 操作使用同一把锁）
trash_lock = threading.Lock()

_timer: threading.Timer | None = None


def _get_trash_entries() -> list[tuple[int, str]]:
    """
    扫描 TRASH_DIR，返回 [(timestamp, full_path)] 列表，按时间戳升序排列。
    文件名格式：{project_id}.bak.{unix_ts}
    无法解析 ts 的条目时间戳设为 0（最旧，优先清理）。
    """
    entries = []
    try:
        names = os.listdir(TRASH_DIR)
    except FileNotFoundError:
        return entries
    except OSError as e:
        logger.warning("Cannot list trash dir %s: %s", TRASH_DIR, e)
        return entries

    for name in names:
        full_path = os.path.join(TRASH_DIR, name)
        # 尝试从文件名解析时间戳
        ts = 0
        parts = name.rsplit(".", 2)
        if len(parts) == 3 and parts[1] == "bak":
            try:
                ts = int(parts[2])
            except ValueError:
                pass
        entries.append((ts, full_path))

    entries.sort(key=lambda x: x[0])
    return entries


def _du(path: str) -> int:
    """递归计算 path 占用字节数，FileNotFoundError 返回 0。"""
    total = 0
    try:
        if os.path.isfile(path) or os.path.islink(path):
            return os.lstat(path).st_size
        for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
            for fname in filenames:
                try:
                    total += os.lstat(os.path.join(dirpath, fname)).st_size
                except FileNotFoundError:
                    pass
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning("du error on %s: %s", path, e)
    return total


def _trash_total_size() -> int:
    """计算整个 TRASH_DIR 的大小。"""
    return _du(TRASH_DIR)


def _remove_entry(path: str) -> None:
    """删除单个 trash 条目（目录或文件）。"""
    try:
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        else:
            os.remove(path)
        logger.info("GC removed: %s", path)
    except FileNotFoundError:
        pass  # 已被并发删除，skip
    except OSError as e:
        logger.warning("GC failed to remove %s: %s", path, e)


def run_gc() -> None:
    """
    执行一次 GC。
    锁策略：
    - 大小统计（du 扫描）在锁外执行（IO密集，可能耗时数十秒）
    - 只在实际 remove/move 时短暂持锁，防止阻塞 repo_delete
    """
    logger.info("GC started at %s", datetime.now().isoformat())
    now_ts = time.time()
    cutoff_ts = now_ts - MAX_AGE_DAYS * 86400

    # 阶段1：锁外扫描，确定待清理列表
    entries = _get_trash_entries()
    to_remove_age = []
    for ts, path in entries:
        if ts == 0:
            try:
                mtime = os.lstat(path).st_mtime
            except FileNotFoundError:
                continue
            if mtime < cutoff_ts:
                to_remove_age.append((0, path))
        elif ts < cutoff_ts:
            to_remove_age.append((ts, path))

    # 阶段1：逐条持锁删除（每次持锁时间极短）
    for _, path in to_remove_age:
        with trash_lock:
            _remove_entry(path)

    # 阶段2：锁外统计总体积
    total = _trash_total_size()
    if total > MAX_TRASH_BYTES:
        logger.info(
            "Trash size %d bytes > %d bytes limit, cleaning oldest entries",
            total, MAX_TRASH_BYTES
        )
        entries = _get_trash_entries()
        for ts, path in entries:
            if total <= MAX_TRASH_BYTES:
                break
            try:
                entry_size = _du(path)
            except FileNotFoundError:
                continue
            with trash_lock:  # 只在删除时短暂持锁
                _remove_entry(path)
            total -= entry_size

    logger.info("GC finished. Trash size now: %d bytes", _trash_total_size())


def _schedule_next() -> None:
    """计算距离明日0点的秒数，设置 Timer。"""
    global _timer
    now = datetime.now()
    tomorrow = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    delay = (tomorrow - now).total_seconds()
    logger.info("Next GC scheduled in %.0f seconds (at %s)", delay, tomorrow.isoformat())
    _timer = threading.Timer(delay, _gc_and_reschedule)
    _timer.daemon = True
    _timer.start()


def _gc_and_reschedule() -> None:
    """执行 GC 并安排下一次。"""
    try:
        run_gc()
    except Exception as e:
        logger.error("GC error: %s", e, exc_info=True)
    _schedule_next()


def start_gc_scheduler() -> None:
    """
    启动 GC 调度器：
    1. 立即执行一次启动 GC
    2. 安排每日0点循环
    """
    logger.info("Starting GC scheduler (TRASH_DIR=%s)", TRASH_DIR)
    os.makedirs(TRASH_DIR, exist_ok=True)

    # 启动时立即 GC（后台线程，不阻塞启动）
    t = threading.Thread(target=_safe_run_gc, daemon=True)
    t.start()

    # 安排每日0点
    _schedule_next()


def _safe_run_gc() -> None:
    try:
        run_gc()
    except Exception as e:
        logger.error("Startup GC error: %s", e, exc_info=True)


def stop_gc_scheduler() -> None:
    """停止 GC 调度器（用于测试/优雅退出）。"""
    global _timer
    if _timer is not None:
        _timer.cancel()
        _timer = None
