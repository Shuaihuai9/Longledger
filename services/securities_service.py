"""证券名称服务：meta 表本地缓存 + 东财接口补全。

名称只是展示层数据：缓存进全局 meta 表（key = sec_name_<代码>），不进任何
账务结构；补全失败保持缺失，界面回退显示破折号。
"""
from __future__ import annotations

from data.db import WRITE_LOCK
from ingest.quotes_download import fetch_security_name

KEY_PREFIX = "sec_name_"


def cached_names(conn) -> dict[str, str]:
    """读出全部已缓存名称：{代码: 名称}。"""
    rows = conn.execute(
        "SELECT key, value FROM meta WHERE key LIKE ?", (f"{KEY_PREFIX}%",)
    ).fetchall()
    return {row["key"][len(KEY_PREFIX):]: row["value"] for row in rows}


def ensure_names(conn, symbols, timeout: float = 8) -> dict[str, str]:
    """保证给定代码尽量有名称缓存；返回本次可解析的 {代码: 名称}。

    已缓存的直接命中；缺失的逐个请求行情接口，成功即写入缓存。
    网络或代码错误只跳过，不抛异常——名称缺失不应阻断任何账务操作。
    """
    known = cached_names(conn)
    resolved: dict[str, str] = {}
    pending_writes: list[tuple[str, str]] = []
    for symbol in dict.fromkeys(str(s).strip() for s in symbols if str(s).strip()):
        if symbol in known:
            resolved[symbol] = known[symbol]
            continue
        try:
            name = fetch_security_name(symbol, timeout=timeout)
        except Exception:
            continue
        if not name:
            continue
        resolved[symbol] = name
        pending_writes.append((f"{KEY_PREFIX}{symbol}", name))

    if pending_writes:
        with WRITE_LOCK:
            try:
                conn.executemany(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                    pending_writes,
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    return resolved
