"""核心业务：时间轴换算、成片行重算、单行校时、CSV 换段（两阶段切换）。

区间约定：所有区间含起点、不含终点 [start, end)。
选段在成片里首尾相接：第 i 段成片区间 = [offset_i, offset_i + 时长_i)。
原字幕与选段的可见交集 = [max(字幕起, 段起), min(字幕止, 段止))，非空才生成成片行。

换段（重传 CSV）采用两阶段，避免长重算占住写锁、阻塞另一窗口的校时保存：
1. 场外构建（compute）：在自动提交的只读连接上算好新选段与新成片行，全程不持有写锁，
   期间剪辑师对当前成片的校时保存可以正常提交（即使该句在新选段里已被剪掉）。
2. 短事务切换（swap）：把构建结果与重算期间发生的校时一起对账后原子写入；
   任一步失败整体回滚，继续使用原选段，期间已保存的校时仍然有效。
"""
from __future__ import annotations

import sqlite3
import threading
from typing import Optional

from . import db


class ValidationError(Exception):
    """输入不合法（越界、起止颠倒、行不存在等），保存被拒绝。"""


class Conflict(Exception):
    """原字幕在该行读出后被改过，或整批数据已换段，拒绝保存并提示刷新。"""


# 同一进程内只允许一个换段任务；不阻塞校时保存（compute 阶段不放锁，只有很短的 swap 持锁）。
_rebuild_lock = threading.Lock()

# 测试/自测用钩子：默认空操作，测试可 monkeypatch 来制造重算期间的并发与失败。
def _hook_phase(phase: str) -> None:
    """phase: 'compute'（场外重算开始后）/ 'swap'（拿到写锁、切换前）。"""


def _hook_fail_swap() -> None:
    """默认不失败；测试可替换为抛异常，验证换段失败回滚。"""


# ---------- 查询 ----------

def list_final_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT fr.*, s.offset_ms AS seg_final_start,
               s.offset_ms + (s.end_ms - s.start_ms) AS seg_final_end
        FROM final_rows fr
        JOIN selections s ON s.seq = fr.selection_seq
        ORDER BY fr.final_start_ms, fr.id
        """
    ).fetchall()


def get_edit_context(conn: sqlite3.Connection, row_id: int) -> Optional[dict]:
    """编辑页所需的全部现值（裁切标记、可见区间都按当前库内状态实时换算）。"""
    row = conn.execute("SELECT * FROM final_rows WHERE id = ?", (row_id,)).fetchone()
    if row is None:
        return None
    sel = conn.execute("SELECT * FROM selections WHERE seq = ?", (row["selection_seq"],)).fetchone()
    sub = conn.execute(
        "SELECT * FROM source_subtitles WHERE material_id = ? AND subtitle_no = ?",
        (row["material_id"], row["subtitle_no"]),
    ).fetchone()
    if sel is None or sub is None:
        return None
    start_clipped = sub["start_ms"] < sel["start_ms"]
    end_clipped = sub["end_ms"] > sel["end_ms"]
    vis_src_start = max(sub["start_ms"], sel["start_ms"])
    vis_src_end = min(sub["end_ms"], sel["end_ms"])
    return {
        "row": row,
        "sel": sel,
        "sub": sub,
        "version": sub["version"],
        "data_rev": db.get_data_rev(conn),
        "start_clipped": start_clipped,
        "end_clipped": end_clipped,
        "final_start": sel["offset_ms"] + (vis_src_start - sel["start_ms"]),
        "final_end": sel["offset_ms"] + (vis_src_end - sel["start_ms"]),
        "seg_final_start": sel["offset_ms"],
        "seg_final_end": sel["offset_ms"] + (sel["end_ms"] - sel["start_ms"]),
    }


def snapshot(conn: sqlite3.Connection) -> dict:
    """给 API/自测用的只读全量状态。"""
    return {
        "data_rev": db.get_data_rev(conn),
        "subtitles": [dict(r) for r in conn.execute(
            "SELECT * FROM source_subtitles ORDER BY material_id, subtitle_no")],
        "selections": [dict(r) for r in conn.execute(
            "SELECT * FROM selections ORDER BY seq")],
        "final_rows": [dict(r) for r in conn.execute(
            "SELECT * FROM final_rows ORDER BY final_start_ms, id")],
    }


# ---------- 重算 ----------

def _visible_interval(sel: sqlite3.Row, sub: sqlite3.Row) -> Optional[dict]:
    """原字幕在该选段里的可见部分；不可见返回 None。区间含起点不含终点。"""
    overlap_start = max(sub["start_ms"], sel["start_ms"])
    overlap_end = min(sub["end_ms"], sel["end_ms"])
    if overlap_start >= overlap_end:  # 贴边不算可见
        return None
    return {
        "final_start_ms": sel["offset_ms"] + (overlap_start - sel["start_ms"]),
        "final_end_ms": sel["offset_ms"] + (overlap_end - sel["start_ms"]),
        "start_clipped": int(sub["start_ms"] < sel["start_ms"]),
        "end_clipped": int(sub["end_ms"] > sel["end_ms"]),
    }


def recompute_subtitle(conn: sqlite3.Connection, material_id: str, subtitle_no: int) -> None:
    """只重算这一条原字幕在各段中的成片行：补新行、撤下不再可见的行。

    仍然可见的行就地更新（保持行 id 稳定），这样“行读出后原字幕被改”
    能在保存时被版本检查拦下，而不是表现为行丢失。
    """
    existing = {
        r["selection_seq"]: r
        for r in conn.execute(
            "SELECT * FROM final_rows WHERE material_id = ? AND subtitle_no = ?",
            (material_id, subtitle_no),
        )
    }
    sub = conn.execute(
        "SELECT * FROM source_subtitles WHERE material_id = ? AND subtitle_no = ?",
        (material_id, subtitle_no),
    ).fetchone()

    visible_seqs: set[int] = set()
    if sub is not None:
        sels = conn.execute(
            "SELECT * FROM selections WHERE material_id = ? ORDER BY seq", (material_id,)
        ).fetchall()
        for sel in sels:
            vis = _visible_interval(sel, sub)
            if vis is None:
                continue
            visible_seqs.add(sel["seq"])
            if sel["seq"] in existing:  # 仍在可见：就地更新
                conn.execute(
                    """
                    UPDATE final_rows
                       SET final_start_ms = ?, final_end_ms = ?,
                           src_start_ms = ?, src_end_ms = ?,
                           start_clipped = ?, end_clipped = ?, text = ?
                     WHERE id = ?
                    """,
                    (vis["final_start_ms"], vis["final_end_ms"],
                     sub["start_ms"], sub["end_ms"],
                     vis["start_clipped"], vis["end_clipped"], sub["text"],
                     existing[sel["seq"]]["id"]),
                )
            else:  # 新出现的行：补上
                conn.execute(
                    """
                    INSERT INTO final_rows
                        (selection_seq, material_id, subtitle_no,
                         final_start_ms, final_end_ms, src_start_ms, src_end_ms,
                         start_clipped, end_clipped, text)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                    """,
                    (sel["seq"], material_id, subtitle_no,
                     vis["final_start_ms"], vis["final_end_ms"],
                     sub["start_ms"], sub["end_ms"],
                     vis["start_clipped"], vis["end_clipped"], sub["text"]),
                )

    # 撤下不再可见的行
    for seq, row in existing.items():
        if seq not in visible_seqs:
            conn.execute("DELETE FROM final_rows WHERE id = ?", (row["id"],))


# ---------- 换段：两阶段（场外构建 + 短事务切换） ----------

def _build_selections(rows: list[dict]) -> list[dict]:
    """纯计算：选段按给定顺序首尾相接，算出每段在成片中的偏移。不碰数据库。"""
    built, offset = [], 0
    for i, r in enumerate(rows, start=1):
        built.append({
            "seq": i,
            "material_id": r["material_id"],
            "start_ms": r["start_ms"],
            "end_ms": r["end_ms"],
            "offset_ms": offset,
        })
        offset += r["end_ms"] - r["start_ms"]
    return built


def _build_rows(
    subs: list, new_selections: list[dict]
) -> list[dict]:
    """场外重算：按新选段在内存里构建全部成片行（纯计算，不碰库、不持锁）。

    subs 为原片字幕行（sqlite3.Row 或 dict 均可，按键取值）。
    """
    subs_by_material: dict[str, list] = {}
    for sub in subs:
        subs_by_material.setdefault(sub["material_id"], []).append(sub)

    built_rows: list[dict] = []
    for sel in new_selections:
        for sub in subs_by_material.get(sel["material_id"], []):
            vis = _visible_interval(sel, sub)
            if vis is None:
                continue
            built_rows.append({
                "selection_seq": sel["seq"],
                "material_id": sub["material_id"],
                "subtitle_no": sub["subtitle_no"],
                "final_start_ms": vis["final_start_ms"],
                "final_end_ms": vis["final_end_ms"],
                "src_start_ms": sub["start_ms"],
                "src_end_ms": sub["end_ms"],
                "start_clipped": vis["start_clipped"],
                "end_clipped": vis["end_clipped"],
                "text": sub["text"],
            })
    return built_rows


def _replace_source_rows(conn: sqlite3.Connection, rows: list[dict]) -> None:
    conn.execute("DELETE FROM source_subtitles")
    conn.executemany(
        "INSERT INTO source_subtitles (material_id, subtitle_no, start_ms, end_ms, text)"
        " VALUES (?,?,?,?,?)",
        [(r["material_id"], r["subtitle_no"], r["start_ms"], r["end_ms"], r["text"]) for r in rows],
    )


def _replace_selection_rows(conn: sqlite3.Connection, built: list[dict]) -> None:
    conn.execute("DELETE FROM selections")
    conn.executemany(
        "INSERT INTO selections (seq, material_id, start_ms, end_ms, offset_ms)"
        " VALUES (:seq,:material_id,:start_ms,:end_ms,:offset_ms)",
        built,
    )


def _replace_final_rows(conn: sqlite3.Connection, built_rows: list[dict]) -> None:
    conn.execute("DELETE FROM final_rows")
    conn.executemany(
        """
        INSERT INTO final_rows
            (selection_seq, material_id, subtitle_no,
             final_start_ms, final_end_ms, src_start_ms, src_end_ms,
             start_clipped, end_clipped, text)
        VALUES (:selection_seq,:material_id,:subtitle_no,
                :final_start_ms,:final_end_ms,:src_start_ms,:src_end_ms,
                :start_clipped,:end_clipped,:text)
        """,
        built_rows,
    )


def replace_dataset(
    conn_factory,
    *,
    new_source: Optional[list[dict]] = None,
    new_selections: Optional[list[dict]] = None,
) -> int:
    """整体替换原片字幕和/或选段，两阶段提交，返回新的数据版本号。

    阶段一（场外构建）：只读连接上算出新选段偏移与全部新成片行。此阶段不持有写锁，
      其它窗口的校时保存可以正常提交；同时记下构建所依据的原字幕版本快照。
    阶段二（短事务切换）：拿到写锁后，对构建期间被校时改过的原字幕逐条对账
      （用当前库内最新时刻重算其成片行），再原子写入新选段/新成片行并 data_rev+1。
      任一步失败抛异常，事务回滚：原选段继续生效，期间已保存的校时也保留。

    conn_factory: 返回新连接的可调用对象（通常传 db.connect），
    因为场外构建与切换必须使用各自独立的连接。
    """
    if new_source is None and new_selections is None:
        raise ValueError("replace_dataset 至少需要一组新数据")

    with _rebuild_lock:
        # ---- 阶段一：场外构建（只读，不阻塞校时写事务） ----
        read_conn = conn_factory()
        try:
            if new_source is not None:
                # 原片字幕也要整体替换：构建以“新原片”为准，不依赖库里的旧数据
                build_subs = new_source
                baseline_versions = {}
            else:
                build_subs = read_conn.execute(
                    "SELECT * FROM source_subtitles"
                ).fetchall()
                # 构建依据的原字幕版本快照：swap 时用它判断重算期间有没有校时提交
                baseline_versions = {
                    (r["material_id"], r["subtitle_no"]): r["version"]
                    for r in read_conn.execute(
                        "SELECT material_id, subtitle_no, version FROM source_subtitles"
                    )
                }
            if new_selections is not None:
                built_selections = _build_selections(new_selections)
                built_rows = _build_rows(build_subs, built_selections)
            else:
                built_selections = None
                built_rows = None
        finally:
            read_conn.close()

        # 慢重算发生在两阶段之间：此处没有任何写锁/事务，校时保存照常进行。
        _hook_phase("compute")

        # ---- 阶段二：短事务原子切换 ----
        conn = conn_factory()
        try:
            conn.execute("BEGIN IMMEDIATE")

            if new_source is not None:
                # 原片字幕整体替换：旧字幕全部失效，不存在“保留重算期间校时”的对象，
                # 直接以新原片字幕为准重算。
                _replace_source_rows(conn, new_source)
                if built_selections is not None:
                    _replace_selection_rows(conn, built_selections)
                    _replace_final_rows(conn, built_rows)
                else:
                    conn.execute("DELETE FROM final_rows")
                    for sub in conn.execute("SELECT * FROM source_subtitles"):
                        recompute_subtitle(conn, sub["material_id"], sub["subtitle_no"])
            else:
                # 只换选段：先把场外构建结果写入，再对重算期间保存过的校时逐条对账。
                _replace_selection_rows(conn, built_selections)
                _replace_final_rows(conn, built_rows)

                current = conn.execute(
                    "SELECT material_id, subtitle_no, version FROM source_subtitles"
                ).fetchall()
                for r in current:
                    key = (r["material_id"], r["subtitle_no"])
                    if baseline_versions.get(key) != r["version"]:
                        # 这条原字幕在重算期间被校时改过：用最新时刻在新选段上重算，
                        # 使新成片行反映切换前已保存的校时（在新选段里被剪掉则自然撤下）。
                        recompute_subtitle(conn, r["material_id"], r["subtitle_no"])

            _hook_phase("swap")
            _hook_fail_swap()
            new_rev = db.bump_data_rev(conn)
            conn.execute("COMMIT")
            return new_rev
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()


# ---------- 单行校时 ----------

def adjust_row(
    conn: sqlite3.Connection,
    row_id: int,
    new_final_start: Optional[int],
    new_final_end: Optional[int],
    expect_version: int,
    expect_data_rev: int,
) -> dict:
    """把选中行在成片上的新起止换算回原字幕，并只重算这条原字幕的成片行。

    被切点裁掉的一端忽略入参、保留原片时刻；未裁的一端必须落在本段范围内。
    版本不符抛 Conflict；任何校验失败抛 ValidationError。
    调用方需在事务内调用，失败回滚则原字幕与成片行都保留上次结果。

    expect_data_rev：编辑页读出时的数据版本号。若期间发生过整批换段（重传 CSV），
    即使行 id 仍存在、原字幕 version 恰好未变，也拒绝保存并提示刷新。
    """
    if db.get_data_rev(conn) != expect_data_rev:
        raise Conflict("选段已重新上传并完成换段，请刷新页面后在新成片行上校时")

    row = conn.execute("SELECT * FROM final_rows WHERE id = ?", (row_id,)).fetchone()
    if row is None:
        raise ValidationError("这条成片行已不存在（可能被重算移除），请刷新列表")
    sel = conn.execute("SELECT * FROM selections WHERE seq = ?", (row["selection_seq"],)).fetchone()
    sub = conn.execute(
        "SELECT * FROM source_subtitles WHERE material_id = ? AND subtitle_no = ?",
        (row["material_id"], row["subtitle_no"]),
    ).fetchone()
    if sel is None or sub is None:
        raise ValidationError("选段或原字幕已变化，请刷新列表")

    if sub["version"] != expect_version:
        raise Conflict("这条原字幕在您读取后已被修改，请刷新页面再试")

    start_clipped = sub["start_ms"] < sel["start_ms"]
    end_clipped = sub["end_ms"] > sel["end_ms"]
    if start_clipped and end_clipped:
        raise ValidationError("本行两端都被切点裁掉，没有可调整的端点")

    # 成片时刻 -> 原片时刻：src = 段原片起点 + (成片时刻 - 段成片起点)
    if start_clipped:
        new_src_start = sub["start_ms"]  # 被裁的一端不能调整，保留原片时刻
    else:
        if new_final_start is None:
            raise ValidationError("请填写新的成片起点")
        new_src_start = sel["start_ms"] + (new_final_start - sel["offset_ms"])
        if not sel["start_ms"] <= new_src_start <= sel["end_ms"]:
            raise ValidationError(
                f"起点超出本段范围（本段原片 {sel['start_ms']}–{sel['end_ms']} ms）"
            )

    if end_clipped:
        new_src_end = sub["end_ms"]
    else:
        if new_final_end is None:
            raise ValidationError("请填写新的成片终点")
        new_src_end = sel["start_ms"] + (new_final_end - sel["offset_ms"])
        if not sel["start_ms"] <= new_src_end <= sel["end_ms"]:
            raise ValidationError(
                f"终点超出本段范围（本段原片 {sel['start_ms']}–{sel['end_ms']} ms）"
            )

    if new_src_start >= new_src_end:
        raise ValidationError("起点必须小于终点")

    # 乐观并发控制：版本不匹配则更新不到行，拒绝保存
    cur = conn.execute(
        "UPDATE source_subtitles SET start_ms = ?, end_ms = ?, version = version + 1"
        " WHERE material_id = ? AND subtitle_no = ? AND version = ?",
        (new_src_start, new_src_end, sub["material_id"], sub["subtitle_no"], expect_version),
    )
    if cur.rowcount != 1:
        raise Conflict("这条原字幕在您读取后已被修改，请刷新页面再试")

    recompute_subtitle(conn, sub["material_id"], sub["subtitle_no"])
    return {
        "material_id": sub["material_id"],
        "subtitle_no": sub["subtitle_no"],
        "start_ms": new_src_start,
        "end_ms": new_src_end,
    }
