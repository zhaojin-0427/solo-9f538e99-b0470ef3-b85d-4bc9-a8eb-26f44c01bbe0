"""校时换算、批量写入、重算与冲突检测的核心逻辑。

观测有两种编号，同一批提交可混用：
  全局号 obs_id        全库唯一；同号重复提交覆盖原文，可跨开机段更正
  段内号 local_obs_id  仅在开机段内唯一；记录器重启后新段同号另存一条，
                       只覆盖本段同号观测，与同名全局号互不干扰

两类观测共用一条 UTC 毫秒时间线，一同参与跨段冲突判定。

状态机（observations.status / segment_observations.status）：
  ok                   已校时且无冲突，供分析
  pending_calibration  计数落在本段校时区间外，保留原文、待校时
  conflict             不同开机段落在同一 UTC 毫秒，冲突观测均不供分析

所有函数都假定运行在 db.transaction 包裹的同一事务里，
因此“原文 + 校时依据 + 分析供数”同次生效。
"""

from __future__ import annotations

import bisect
import sqlite3

OK = "ok"
PENDING = "pending_calibration"
CONFLICT = "conflict"

# 段内号观测的主键：(开机段, 段内号)
SegmentKey = tuple[str, str]


# ---------------------------------------------------------------- 校时换算

def load_points(conn: sqlite3.Connection, segment: str) -> list[tuple[int, int]]:
    """某开机段的全部校时点，按计数升序：[(counter, utc_ms), ...]"""
    rows = conn.execute(
        "SELECT counter, utc_ms FROM calibration_points "
        "WHERE boot_segment = ? ORDER BY counter",
        (segment,),
    ).fetchall()
    return [(r["counter"], r["utc_ms"]) for r in rows]


def compute_utc(
    points: list[tuple[int, int]], counter: int
) -> tuple[int, tuple[int, int], tuple[int, int]] | None:
    """相邻校时点间线性换算并向下取整。

    返回 (utc_ms, 下界点, 上界点)；计数落在校时区间外返回 None（待校时）。
    恰好命中某个校时点时直接取该点 UTC（上下界相同）。
    """
    if not points:
        return None
    counters = [p[0] for p in points]
    i = bisect.bisect_left(counters, counter)
    if i < len(points) and counters[i] == counter:
        p = points[i]
        return p[1], p, p
    if i == 0 or i == len(points):
        return None
    (c1, t1), (c2, t2) = points[i - 1], points[i]
    # 段内 UTC 单调不倒退已由写入方校验，故各项均非负，// 即向下取整
    utc_ms = t1 + (counter - c1) * (t2 - t1) // (c2 - c1)
    return utc_ms, (c1, t1), (c2, t2)


def _pending_reason(segment: str, counter: int, points: list[tuple[int, int]]) -> str:
    if not points:
        return f"开机段 '{segment}' 尚无校时点，待校时"
    return (
        f"计数 {counter} 超出开机段 '{segment}' 的校时区间 "
        f"[{points[0][0]}, {points[-1][0]}]，待校时"
    )


def _computed_fields(
    segment: str, counter: int, points: list[tuple[int, int]]
) -> tuple:
    """一条观测换算后的 (utc_ms, status, reason, 校时依据四列)；待校时 utc_ms 为 None。"""
    result = compute_utc(points, counter)
    if result is None:
        return (None, PENDING, _pending_reason(segment, counter, points),
                None, None, None, None)
    utc_ms, (c_lo, t_lo), (c_hi, t_hi) = result
    return (utc_ms, OK, None, c_lo, t_lo, c_hi, t_hi)


# ---------------------------------------------------------------- 重算与冲突

_UPDATE_GLOBAL_SQL = (
    "UPDATE observations SET utc_ms = ?, status = ?, reason = ?, "
    "cal_lo_counter = ?, cal_lo_utc_ms = ?, "
    "cal_hi_counter = ?, cal_hi_utc_ms = ? WHERE obs_id = ?"
)
_UPDATE_SEGMENT_SQL = (
    "UPDATE segment_observations SET utc_ms = ?, status = ?, reason = ?, "
    "cal_lo_counter = ?, cal_lo_utc_ms = ?, "
    "cal_hi_counter = ?, cal_hi_utc_ms = ? "
    "WHERE boot_segment = ? AND local_obs_id = ?"
)


def recalc_segment(conn: sqlite3.Connection, segment: str) -> None:
    """按当前校时点重算某开机段全部观测（两种编号）的 utc_ms / 状态 / 校时依据。"""
    points = load_points(conn, segment)
    rows = conn.execute(
        "SELECT obs_id, counter FROM observations WHERE boot_segment = ?",
        (segment,),
    ).fetchall()
    for row in rows:
        conn.execute(
            _UPDATE_GLOBAL_SQL,
            (*_computed_fields(segment, row["counter"], points), row["obs_id"]),
        )
    rows = conn.execute(
        "SELECT local_obs_id, counter FROM segment_observations "
        "WHERE boot_segment = ?",
        (segment,),
    ).fetchall()
    for row in rows:
        conn.execute(
            _UPDATE_SEGMENT_SQL,
            (*_computed_fields(segment, row["counter"], points),
             segment, row["local_obs_id"]),
        )


def conflict_ms_set(conn: sqlite3.Connection) -> set[int]:
    """被 >=2 个不同开机段的观测占用的 UTC 毫秒集合（两类编号合并计）。"""
    rows = conn.execute(
        "SELECT utc_ms FROM ("
        "  SELECT utc_ms, boot_segment FROM observations "
        "  WHERE utc_ms IS NOT NULL "
        "  UNION ALL "
        "  SELECT utc_ms, boot_segment FROM segment_observations "
        "  WHERE utc_ms IS NOT NULL"
        ") GROUP BY utc_ms HAVING COUNT(DISTINCT boot_segment) > 1"
    ).fetchall()
    return {r["utc_ms"] for r in rows}


def resolve_conflicts(conn: sqlite3.Connection) -> None:
    """全局刷新冲突状态：新冲突标记 conflict，已解除的恢复 ok（两张表）。"""
    ms_set = conflict_ms_set(conn)
    conflicted_global: set[str] = set()
    conflicted_segment: set[SegmentKey] = set()
    if ms_set:
        ph = ",".join("?" for _ in ms_set)
        params = tuple(sorted(ms_set))
        # 冲突毫秒上的全部观测（两类编号合并分组）
        by_ms: dict[int, list[tuple[str, str, object]]] = {}
        for r in conn.execute(
            f"SELECT obs_id, utc_ms, boot_segment FROM observations "
            f"WHERE utc_ms IN ({ph})",
            params,
        ).fetchall():
            by_ms.setdefault(r["utc_ms"], []).append(("g", r["boot_segment"], r["obs_id"]))
        for r in conn.execute(
            f"SELECT local_obs_id, utc_ms, boot_segment FROM segment_observations "
            f"WHERE utc_ms IN ({ph})",
            params,
        ).fetchall():
            by_ms.setdefault(r["utc_ms"], []).append(
                ("s", r["boot_segment"], (r["boot_segment"], r["local_obs_id"]))
            )
        for ms, group in by_ms.items():
            segments = {g[1] for g in group}
            for kind, seg, key in group:
                others = "、".join(sorted(segments - {seg}))
                reason = f"UTC 毫秒 {ms} 与开机段 {others} 的观测冲突，不供分析"
                if kind == "g":
                    conn.execute(
                        "UPDATE observations SET status = ?, reason = ? "
                        "WHERE obs_id = ?",
                        (CONFLICT, reason, key),
                    )
                    conflicted_global.add(key)
                else:
                    conn.execute(
                        "UPDATE segment_observations SET status = ?, reason = ? "
                        "WHERE boot_segment = ? AND local_obs_id = ?",
                        (CONFLICT, reason, key[0], key[1]),
                    )
                    conflicted_segment.add(key)
    # 解除冲突后恢复：仍是 conflict 但已不在冲突集合里的观测恢复 ok
    for r in conn.execute(
        "SELECT obs_id FROM observations WHERE status = ?", (CONFLICT,)
    ).fetchall():
        if r["obs_id"] not in conflicted_global:
            conn.execute(
                "UPDATE observations SET status = ?, reason = NULL WHERE obs_id = ?",
                (OK, r["obs_id"]),
            )
    for r in conn.execute(
        "SELECT boot_segment, local_obs_id FROM segment_observations "
        "WHERE status = ?",
        (CONFLICT,),
    ).fetchall():
        if (r["boot_segment"], r["local_obs_id"]) not in conflicted_segment:
            conn.execute(
                "UPDATE segment_observations SET status = ?, reason = NULL "
                "WHERE boot_segment = ? AND local_obs_id = ?",
                (OK, r["boot_segment"], r["local_obs_id"]),
            )


# ---------------------------------------------------------------- 变更快照

def _snapshot_global(
    conn: sqlite3.Connection, where: str, params: list
) -> dict[str, tuple]:
    """obs_id -> (utc_ms, status)，用于事务内前后对比。"""
    rows = conn.execute(
        f"SELECT obs_id, utc_ms, status FROM observations WHERE {where}",
        tuple(params),
    ).fetchall()
    return {r["obs_id"]: (r["utc_ms"], r["status"]) for r in rows}


def _snapshot_segment(
    conn: sqlite3.Connection, where: str, params: list
) -> dict[SegmentKey, tuple]:
    """(开机段, 段内号) -> (utc_ms, status)，用于事务内前后对比。"""
    rows = conn.execute(
        f"SELECT boot_segment, local_obs_id, utc_ms, status "
        f"FROM segment_observations WHERE {where}",
        tuple(params),
    ).fetchall()
    return {
        (r["boot_segment"], r["local_obs_id"]): (r["utc_ms"], r["status"])
        for r in rows
    }


def _changed_keys(before: dict, after: dict) -> list:
    """时刻或状态发生变化（含新出现）的观测键，排序后返回。"""
    return sorted(k for k, state in after.items() if before.get(k) != state)


def fetch_states(conn: sqlite3.Connection, obs_ids: list[str]) -> list[dict]:
    """读取给定全局号观测的当前 (obs_id, utc_ms, status)，供提交响应使用。"""
    if not obs_ids:
        return []
    ph = ",".join("?" for _ in obs_ids)
    rows = conn.execute(
        f"SELECT obs_id, utc_ms, status FROM observations "
        f"WHERE obs_id IN ({ph}) ORDER BY obs_id",
        obs_ids,
    ).fetchall()
    return [{"obs_id": r["obs_id"], "utc_ms": r["utc_ms"], "status": r["status"]} for r in rows]


def fetch_segment_states(
    conn: sqlite3.Connection, keys: list[SegmentKey]
) -> list[dict]:
    """读取给定段内号观测的当前状态（开机段、段内号、UTC 毫秒、状态）。"""
    if not keys:
        return []
    ph = ",".join("(?,?)" for _ in keys)
    params = [v for key in keys for v in key]
    rows = conn.execute(
        f"SELECT boot_segment, local_obs_id, utc_ms, status "
        f"FROM segment_observations "
        f"WHERE (boot_segment, local_obs_id) IN ({ph}) "
        f"ORDER BY boot_segment, local_obs_id",
        params,
    ).fetchall()
    return [
        {
            "boot_segment": r["boot_segment"],
            "local_obs_id": r["local_obs_id"],
            "utc_ms": r["utc_ms"],
            "status": r["status"],
        }
        for r in rows
    ]


# ---------------------------------------------------------------- 批量写入

def _observation_scope(keys: list, ms_set: set[int], kind: str) -> tuple[str, list]:
    """单表快照范围：本批键 或 受影响 UTC 毫秒上的观测。"""
    parts: list[str] = []
    params: list = []
    if keys:
        if kind == "global":
            parts.append(f"obs_id IN ({','.join('?' for _ in keys)})")
            params.extend(keys)
        else:
            parts.append(
                "(boot_segment, local_obs_id) IN "
                f"({','.join('(?,?)' for _ in keys)})"
            )
            for key in keys:
                params.extend(key)
    if ms_set:
        parts.append(f"utc_ms IN ({','.join('?' for _ in ms_set)})")
        params.extend(sorted(ms_set))
    return (" OR ".join(parts) if parts else "1=0"), params


def _upsert_global(conn: sqlite3.Connection, it: dict, fields: tuple) -> None:
    """全局号观测：同号覆盖原文（可跨开机段更正）并写入换算结果。"""
    conn.execute(
        "INSERT INTO observations (obs_id, boot_segment, counter, tide_level, "
        "utc_ms, status, reason, cal_lo_counter, cal_lo_utc_ms, "
        "cal_hi_counter, cal_hi_utc_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(obs_id) DO UPDATE SET "
        "boot_segment=excluded.boot_segment, counter=excluded.counter, "
        "tide_level=excluded.tide_level, utc_ms=excluded.utc_ms, "
        "status=excluded.status, reason=excluded.reason, "
        "cal_lo_counter=excluded.cal_lo_counter, "
        "cal_lo_utc_ms=excluded.cal_lo_utc_ms, "
        "cal_hi_counter=excluded.cal_hi_counter, "
        "cal_hi_utc_ms=excluded.cal_hi_utc_ms",
        (it["obs_id"], it["boot_segment"], it["counter"], it["tide_level"], *fields),
    )


def _upsert_segment(conn: sqlite3.Connection, it: dict, fields: tuple) -> None:
    """段内号观测：只覆盖本开机段的同号观测；其他段同号另存。"""
    conn.execute(
        "INSERT INTO segment_observations (boot_segment, local_obs_id, counter, "
        "tide_level, utc_ms, status, reason, cal_lo_counter, cal_lo_utc_ms, "
        "cal_hi_counter, cal_hi_utc_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(boot_segment, local_obs_id) DO UPDATE SET "
        "counter=excluded.counter, tide_level=excluded.tide_level, "
        "utc_ms=excluded.utc_ms, status=excluded.status, "
        "reason=excluded.reason, "
        "cal_lo_counter=excluded.cal_lo_counter, "
        "cal_lo_utc_ms=excluded.cal_lo_utc_ms, "
        "cal_hi_counter=excluded.cal_hi_counter, "
        "cal_hi_utc_ms=excluded.cal_hi_utc_ms",
        (it["boot_segment"], it["local_obs_id"], it["counter"],
         it["tide_level"], *fields),
    )


def upsert_observations(
    conn: sqlite3.Connection, items: list[dict]
) -> tuple[list[str], list[SegmentKey]]:
    """分批写入观测，返回 (全局号变更清单, 段内号变更清单)。

    每条观测恰含 obs_id 或 local_obs_id 之一，同批可混用：
      全局号  同号覆盖原文并重算，可跨开机段更正；
      段内号  只覆盖本开机段的同号观测，其他段同号另存。
    新观测可能与其他开机段落在同一 UTC 毫秒而触发冲突（两类编号一同判定），
    因此写入后做全局冲突刷新，并把受波及的观测一并纳入变更清单。
    """
    if not items:
        return [], []
    segments = {it["boot_segment"] for it in items}
    points_by_seg = {s: load_points(conn, s) for s in segments}

    # 同批同键后者覆盖前者
    global_items: dict[str, tuple[dict, tuple]] = {}
    segment_items: dict[SegmentKey, tuple[dict, tuple]] = {}
    for it in items:
        fields = _computed_fields(
            it["boot_segment"], it["counter"], points_by_seg[it["boot_segment"]]
        )
        if it["obs_id"] is not None:
            global_items[it["obs_id"]] = (it, fields)
        else:
            segment_items[(it["boot_segment"], it["local_obs_id"])] = (it, fields)

    # 被覆盖观测的旧 UTC 毫秒（其上的冲突可能因此解除）
    old_ms: set[int] = set()
    if global_items:
        ids = list(global_items)
        ph = ",".join("?" for _ in ids)
        old_ms |= {
            r["utc_ms"]
            for r in conn.execute(
                f"SELECT utc_ms FROM observations "
                f"WHERE obs_id IN ({ph}) AND utc_ms IS NOT NULL",
                ids,
            ).fetchall()
        }
    if segment_items:
        keys = list(segment_items)
        ph = ",".join("(?,?)" for _ in keys)
        params = [v for key in keys for v in key]
        old_ms |= {
            r["utc_ms"]
            for r in conn.execute(
                f"SELECT utc_ms FROM segment_observations "
                f"WHERE (boot_segment, local_obs_id) IN ({ph}) "
                f"AND utc_ms IS NOT NULL",
                params,
            ).fetchall()
        }

    new_ms = {
        fields[0]
        for _, fields in (*global_items.values(), *segment_items.values())
        if fields[0] is not None
    }
    affected_ms = old_ms | new_ms

    # 快照范围：本批键 + 受影响毫秒上的观测（两张表同范围）
    g_where, g_params = _observation_scope(list(global_items), affected_ms, "global")
    s_where, s_params = _observation_scope(list(segment_items), affected_ms, "segment")
    before_g = _snapshot_global(conn, g_where, g_params)
    before_s = _snapshot_segment(conn, s_where, s_params)

    for _, (it, fields) in global_items.items():
        _upsert_global(conn, it, fields)
    for _, (it, fields) in segment_items.items():
        _upsert_segment(conn, it, fields)

    resolve_conflicts(conn)

    after_g = _snapshot_global(conn, g_where, g_params)
    after_s = _snapshot_segment(conn, s_where, s_params)
    return _changed_keys(before_g, after_g), _changed_keys(before_s, after_s)


class MonotonicViolation(Exception):
    """校时点使段内 UTC 倒退；violations 携带明细。"""

    def __init__(self, violations: list[dict]):
        super().__init__("calibration points make UTC go backwards within a segment")
        self.violations = violations


def _calibration_scope(segments: set[str], ms_set: set[int]) -> tuple[str, list]:
    """校时批次的快照范围：受影响开机段 或 受影响 UTC 毫秒上的观测。"""
    parts = [f"boot_segment IN ({','.join('?' for _ in segments)})"]
    params: list = sorted(segments)
    if ms_set:
        parts.append(f"utc_ms IN ({','.join('?' for _ in ms_set)})")
        params.extend(sorted(ms_set))
    return " OR ".join(parts), params


def upsert_calibrations(
    conn: sqlite3.Connection, items: list[dict]
) -> tuple[list[str], list[SegmentKey]]:
    """分批写入校时点（同段同计数替换旧值），重算受影响观测（两种编号）。

    合并新旧校时点后，任一开机段内 UTC 随计数倒退则抛 MonotonicViolation，
    由调用方回滚整批，旧数据照常供数。
    """
    if not items:
        return [], []
    segments = {it["boot_segment"] for it in items}
    for it in items:
        conn.execute(
            "INSERT INTO calibration_points (boot_segment, counter, utc_ms) "
            "VALUES (?,?,?) "
            "ON CONFLICT(boot_segment, counter) DO UPDATE SET utc_ms=excluded.utc_ms",
            (it["boot_segment"], it["counter"], it["utc_ms"]),
        )

    violations: list[dict] = []
    for seg in sorted(segments):
        points = load_points(conn, seg)
        for (c1, t1), (c2, t2) in zip(points, points[1:]):
            if t2 < t1:
                violations.append({
                    "boot_segment": seg,
                    "counter": c2, "utc_ms": t2,
                    "prev_counter": c1, "prev_utc_ms": t1,
                })
    if violations:
        raise MonotonicViolation(violations)

    ms_before = conflict_ms_set(conn)
    where, params = _calibration_scope(segments, ms_before)
    before_g = _snapshot_global(conn, where, params)
    before_s = _snapshot_segment(conn, where, params)

    for seg in segments:
        recalc_segment(conn, seg)
    resolve_conflicts(conn)

    # 冲突集合可能变化，前后两批毫秒上的观测都可能翻转状态
    ms_all = ms_before | conflict_ms_set(conn)
    where, params = _calibration_scope(segments, ms_all)
    after_g = _snapshot_global(conn, where, params)
    after_s = _snapshot_segment(conn, where, params)
    return _changed_keys(before_g, after_g), _changed_keys(before_s, after_s)
