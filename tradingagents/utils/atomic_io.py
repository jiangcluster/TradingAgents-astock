"""共享落盘工具：原子替换 + 跨进程文件锁。

这套实现原先只存在于 `dataflows/a_stock.py`（缓存写入），而记忆日志、账本、报告
各写各的——同一个仓库里出现两套并发标准：一边原子替换 + 加锁，另一边直接
``write_text`` 或固定 `.tmp` 路径。两个进程同时跑不同票时，后者会丢更新或写出
半截文件，且**不报错**。

两条规则：
- 整份覆盖写一律走 `atomic_write`（临时文件 + ``os.replace``），读者只会看到旧版
  或新版，不会读到写了一半的内容。
- ``读-改-写`` 除了原子替换还必须持 `file_lock`：两个进程各自读到同一份旧内容，
  后写的会把先写的改动整体覆盖，原子替换拦不住这个。
"""

from __future__ import annotations

import contextlib
import logging
import os
import tempfile
import time
from typing import IO, Callable

logger = logging.getLogger(__name__)

# 抢不到锁时的等待上限（秒）。超过即退化为无锁执行——缓存/日志争用不该把主流程
# 打断，但会留下一条 warning。
DEFAULT_LOCK_TIMEOUT = 10.0

# 残留锁的接管阈值系数：锁文件 mtime 超过 ``timeout * _STALE_FACTOR`` 即视为
# 持锁进程已被 kill，直接接管（否则后续每次取数都要等满超时）。
_STALE_FACTOR = 6


def atomic_write(path: str, write_fn: Callable[[IO[str]], None]) -> None:
    """把 ``write_fn`` 的内容原子地写到 ``path``。

    ``write_fn`` 接收一个已打开的文本文件句柄。写失败时原文件保持不动，临时文件
    被清理——不会留下"半截新内容"。
    """
    dirname = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{os.path.basename(path)}.", suffix=".tmp", dir=dirname
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            write_fn(f)
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


@contextlib.contextmanager
def file_lock(path: str, timeout: float = DEFAULT_LOCK_TIMEOUT):
    """以 ``path + ".lock"`` 为锁的跨进程互斥。

    实现为 ``O_CREAT|O_EXCL`` 锁文件 + 超时。**抢不到锁不抛异常**：yield 的布尔值
    表示是否真的持锁（False = 退化为无锁执行），调用方据此决定是否记录告警，但
    不应因此中断主流程。
    """
    lock_path = f"{path}.lock"
    deadline = time.monotonic() + timeout
    acquired = False
    fd = None
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii", "ignore"))
            acquired = True
            break
        except FileExistsError:
            with contextlib.suppress(OSError):
                if time.time() - os.path.getmtime(lock_path) > timeout * _STALE_FACTOR:
                    os.unlink(lock_path)   # 残留锁（持锁进程已被杀）
                    continue
            if time.monotonic() >= deadline:
                logger.warning("file lock timeout, proceeding unlocked: %s", lock_path)
                break
            time.sleep(0.05)
        except OSError:
            break
    try:
        yield acquired
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        if acquired:
            with contextlib.suppress(OSError):
                os.unlink(lock_path)
