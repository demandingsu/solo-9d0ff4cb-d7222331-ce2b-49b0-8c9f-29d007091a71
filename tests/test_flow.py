"""端到端自测：重复选段下的单行校时。

场景：素材 M1 在成片里被选了两次（第 1 段 [0,4000)、第 3 段 [2000,7000)），
同一条原字幕 M1#1 [1000,3000) 因此在成片里出现两行。
"""
import os
import threading

import pytest
from fastapi.testclient import TestClient

from app import logic

os.environ.setdefault("APP_DB", ":memory:")  # 兜底；fixture 里会换成临时文件

from app.main import app  # noqa: E402

SAMPLES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "samples")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB", str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def loaded(client):
    r = client.post("/load_sample", follow_redirects=False)
    assert r.status_code == 303
    return client


def state(client):
    return client.get("/api/state").json()


def find_row(rows, seq, material, no):
    matches = [r for r in rows if r["selection_seq"] == seq
               and r["material_id"] == material and r["subtitle_no"] == no]
    assert len(matches) == 1, f"expected 1 row for seg{seq} {material}#{no}, got {matches}"
    return matches[0]


def sub(state, material, no):
    matches = [s for s in state["subtitles"]
               if s["material_id"] == material and s["subtitle_no"] == no]
    assert len(matches) == 1
    return matches[0]


def save(client, row_id, version, start=None, end=None, data_rev=None):
    if data_rev is None:
        data_rev = state(client)["data_rev"]
    data = {"version": str(version),
            "data_rev": str(data_rev),
            "final_start_ms": "" if start is None else str(start),
            "final_end_ms": "" if end is None else str(end)}
    return client.post(f"/rows/{row_id}/save", data=data, follow_redirects=False)


# ---------- 样例与全量重算 ----------

def test_sample_layout_with_duplicate_selection(loaded):
    s = state(loaded)
    assert [sel["offset_ms"] for sel in s["selections"]] == [0, 4000, 6500]
    rows = s["final_rows"]
    assert len(rows) == 6
    # 第 1 段 M1 [0,4000)：#1 完整可见，#2 尾部被裁
    r1 = find_row(rows, 1, "M1", 1)
    assert (r1["final_start_ms"], r1["final_end_ms"]) == (1000, 3000)
    assert (r1["start_clipped"], r1["end_clipped"]) == (0, 0)
    r2 = find_row(rows, 1, "M1", 2)
    assert (r2["final_start_ms"], r2["final_end_ms"]) == (3500, 4000)
    assert r2["end_clipped"] == 1
    # 第 2 段 M2
    r3 = find_row(rows, 2, "M2", 1)
    assert (r3["final_start_ms"], r3["final_end_ms"]) == (4000, 6000)
    # 第 3 段 M1 [2000,7000)（重复选段）：#1 头部被裁，#3 尾部被裁
    r4 = find_row(rows, 3, "M1", 1)
    assert (r4["final_start_ms"], r4["final_end_ms"]) == (6500, 7500)
    assert r4["start_clipped"] == 1
    r6 = find_row(rows, 3, "M1", 3)
    assert (r6["final_start_ms"], r6["final_end_ms"]) == (10500, 11500)
    assert r6["end_clipped"] == 1


# ---------- 校时：改一处，重复选段同步 ----------

def test_adjust_propagates_to_duplicate_selection(loaded):
    s = state(loaded)
    row = find_row(s["final_rows"], 1, "M1", 1)
    v = sub(s, "M1", 1)["version"]
    # 剪辑师：这句出得太早/太长，把第 1 段里这行终点从 3000 改到 2800
    r = save(loaded, row["id"], v, start=1000, end=2800)
    assert r.status_code == 303, r.text
    s2 = state(loaded)
    # 原字幕被换算回原片时刻
    assert (sub(s2, "M1", 1)["start_ms"], sub(s2, "M1", 1)["end_ms"]) == (1000, 2800)
    assert sub(s2, "M1", 1)["version"] == v + 1
    # 本段行更新
    r1 = find_row(s2["final_rows"], 1, "M1", 1)
    assert (r1["final_start_ms"], r1["final_end_ms"]) == (1000, 2800)
    # 重复选段（第 3 段）里的同一句也跟着更新：6500 + (2800-2000) = 7300
    r4 = find_row(s2["final_rows"], 3, "M1", 1)
    assert (r4["final_start_ms"], r4["final_end_ms"]) == (6500, 7300)
    # 其它字幕的行不受影响
    assert find_row(s2["final_rows"], 1, "M1", 2)["final_end_ms"] == 4000


def test_clipped_end_is_kept(loaded):
    s = state(loaded)
    row = find_row(s["final_rows"], 3, "M1", 1)  # 起点被裁
    assert row["start_clipped"] == 1
    v = sub(s, "M1", 1)["version"]
    # 即使提交了新的起点 6300，也应被忽略；只应用终点 7600 -> 原片 3100
    r = save(loaded, row["id"], v, start=6300, end=7600)
    assert r.status_code == 303, r.text
    s2 = state(loaded)
    assert (sub(s2, "M1", 1)["start_ms"], sub(s2, "M1", 1)["end_ms"]) == (1000, 3100)
    r4 = find_row(s2["final_rows"], 3, "M1", 1)
    assert (r4["final_start_ms"], r4["final_end_ms"]) == (6500, 7600)
    r1 = find_row(s2["final_rows"], 1, "M1", 1)
    assert (r1["final_start_ms"], r1["final_end_ms"]) == (1000, 3100)


def test_clipped_end_only_submission_rejected(loaded):
    s = state(loaded)
    row = find_row(s["final_rows"], 3, "M1", 1)  # 起点被裁、终点可调
    v = sub(s, "M1", 1)["version"]
    before = state(loaded)
    r = save(loaded, row["id"], v, start=6300, end=None)  # 起点被裁忽略，终点没填
    assert r.status_code == 400
    assert state(loaded) == before  # 保存失败，原字幕和成片行都保留上次结果


# ---------- 校验失败：事务回滚 ----------

def test_out_of_segment_rejected_and_nothing_changes(loaded):
    s = state(loaded)
    row = find_row(s["final_rows"], 1, "M1", 1)
    v = sub(s, "M1", 1)["version"]
    before = state(loaded)
    r = save(loaded, row["id"], v, start=1000, end=4500)  # 本段成片只到 4000
    assert r.status_code == 400
    assert "本段" in r.text
    assert state(loaded) == before


def test_start_after_end_rejected_and_nothing_changes(loaded):
    s = state(loaded)
    row = find_row(s["final_rows"], 1, "M1", 1)
    v = sub(s, "M1", 1)["version"]
    before = state(loaded)
    r = save(loaded, row["id"], v, start=1000, end=500)
    assert r.status_code == 400
    assert state(loaded) == before


# ---------- 行的出现与消失 ----------

def test_row_disappears_and_reappears_in_other_segment(loaded):
    s = state(loaded)
    row = find_row(s["final_rows"], 1, "M1", 1)
    v = sub(s, "M1", 1)["version"]
    # 终点改到 1500：字幕 [1000,1500) 与第 3 段 [2000,7000) 不再相交 -> 该行撤下
    assert save(loaded, row["id"], v, start=1000, end=1500).status_code == 303
    s2 = state(loaded)
    assert len(s2["final_rows"]) == 5
    assert not [r for r in s2["final_rows"] if r["selection_seq"] == 3 and r["subtitle_no"] == 1]
    # 再改回 2500：与第 3 段重新相交 -> 补上新出现的行 [6500, 7000)
    row = find_row(s2["final_rows"], 1, "M1", 1)
    v = sub(s2, "M1", 1)["version"]
    assert save(loaded, row["id"], v, start=1000, end=2500).status_code == 303
    s3 = state(loaded)
    assert len(s3["final_rows"]) == 6
    r4 = find_row(s3["final_rows"], 3, "M1", 1)
    assert (r4["final_start_ms"], r4["final_end_ms"]) == (6500, 7000)


# ---------- 乐观并发 ----------

def test_stale_version_rejected_with_refresh_hint(loaded):
    s = state(loaded)
    row = find_row(s["final_rows"], 1, "M1", 1)
    v = sub(s, "M1", 1)["version"]
    assert save(loaded, row["id"], v, start=1000, end=2800).status_code == 303
    before = state(loaded)
    # 同一行按读出的旧版本再保存一次 -> 拒绝并提示刷新
    r = save(loaded, row["id"], v, start=1000, end=2600)
    assert r.status_code == 409
    assert "刷新" in r.text
    assert state(loaded) == before


# ---------- 上传 ----------

def test_bad_csv_keeps_previous_data(loaded):
    before = state(loaded)
    r = loaded.post("/upload/source",
                    files={"file": ("bad.csv", b"material_id,subtitle_no,start_ms,end_ms,text\nM1,1,abc,2000,x\n")},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "err" in r.headers["location"]
    assert state(loaded) == before


def test_upload_csv_replaces_and_rebuilds(loaded):
    src = b"material_id,subtitle_no,start_ms,end_ms,text\nA,1,0,1000,hello\n"
    sel = b"material_id,start_ms,end_ms\nA,0,500\nA,500,1500\n"
    assert loaded.post("/upload/source", files={"file": ("s.csv", src)},
                       follow_redirects=False).status_code == 303
    assert loaded.post("/upload/selections", files={"file": ("s.csv", sel)},
                       follow_redirects=False).status_code == 303
    s = state(loaded)
    # 字幕 [0,1000) 被两段 [0,500) [500,1500) 各裁一半：成片 [0,500) 与 [500,1000)
    assert [(r["final_start_ms"], r["final_end_ms"]) for r in s["final_rows"]] == [(0, 500), (500, 1000)]
    assert s["final_rows"][0]["end_clipped"] == 1
    assert s["final_rows"][1]["start_clipped"] == 1


def test_pages_render(loaded):
    assert loaded.get("/rows").status_code == 200
    row_id = state(loaded)["final_rows"][0]["id"]
    assert loaded.get(f"/rows/{row_id}").status_code == 200
    assert loaded.get("/upload").status_code == 200


# ---------- 重传选段：两阶段切换，重算期间不阻塞校时 ----------

# 新选段：仍是 M1、M2、M1 重复三段，但切点不同。
# M1 段 1 [0,4000) 不变；M1 段 3 改为 [4000,7000)，
# 这样 M1#1 校到 [1000,2800) 后在第 3 段 [4000,7000) 不再相交（被剪掉）。
NEW_SELECTIONS_CSV = (
    b"material_id,start_ms,end_ms\n"
    b"M1,0,4000\n"
    b"M2,0,2500\n"
    b"M1,4000,7000\n"
)


def _upload_selections_in_thread(client, blob):
    box = {}

    def worker():
        try:
            box["resp"] = client.post(
                "/upload/selections",
                files={"file": ("new_sel.csv", blob)},
                follow_redirects=False,
            )
        except BaseException as exc:  # noqa: BLE001 - 把线程内异常带回主线程断言
            box["exc"] = exc

    t = threading.Thread(target=worker)
    t.start()
    return t, box


def test_save_during_recompute_succeeds_and_is_carried_into_new_segments(loaded, monkeypatch):
    """重算期间另一窗口的校时保存不报错，且换段后成片行反映已保存的校时。"""
    s = state(loaded)
    row = find_row(s["final_rows"], 1, "M1", 1)
    v = sub(s, "M1", 1)["version"]
    old_rev = s["data_rev"]

    entered = threading.Event()
    release = threading.Event()

    def hook(phase):
        if phase == "compute":  # 场外重算进行中：此时不持有任何写锁
            entered.set()
            release.wait(timeout=10)

    monkeypatch.setattr(logic, "_hook_phase", hook)

    t, box = _upload_selections_in_thread(loaded, NEW_SELECTIONS_CSV)
    assert entered.wait(timeout=5), "重算钩子未触发"
    try:
        # 重算进行中，另一窗口在当前成片（旧选段）上校时：终点 3000 -> 2800
        r = save(loaded, row["id"], v, start=1000, end=2800, data_rev=old_rev)
        assert r.status_code == 303, r.text  # 不再等到 database is locked
    finally:
        release.set()
    t.join(timeout=10)
    assert "exc" not in box, box.get("exc")
    assert box["resp"].status_code == 303

    s2 = state(loaded)
    assert s2["data_rev"] == old_rev + 1
    # 已保存的校时落库：原字幕变为 [1000,2800)，版本号 +1
    assert (sub(s2, "M1", 1)["start_ms"], sub(s2, "M1", 1)["end_ms"]) == (1000, 2800)
    assert sub(s2, "M1", 1)["version"] == v + 1
    # 新选段生效：第 3 段切点已是 [4000,7000)，偏移 = 4000+2500 = 6500 不变
    assert [sel["start_ms"] for sel in s2["selections"]] == [0, 0, 4000]
    # 第 1 段里这句的成片行反映切换前保存的校时 [1000,2800)
    r1 = find_row(s2["final_rows"], 1, "M1", 1)
    assert (r1["final_start_ms"], r1["final_end_ms"]) == (1000, 2800)
    # 这句在新选段第 3 段 [4000,7000) 已被剪掉：没有对应成片行
    assert not [r for r in s2["final_rows"] if r["selection_seq"] == 3 and r["subtitle_no"] == 1]


def test_save_during_recompute_kept_even_when_line_cut_in_new_segments(loaded, monkeypatch):
    """即使该句在新选段里被剪掉，重算期间保存的校时仍有效（原字幕保留新时刻）。"""
    s = state(loaded)
    row = find_row(s["final_rows"], 1, "M1", 1)
    v = sub(s, "M1", 1)["version"]
    old_rev = s["data_rev"]

    entered = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(
        logic, "_hook_phase",
        lambda phase: (entered.set(), release.wait(timeout=10)) if phase == "compute" else None,
    )

    t, box = _upload_selections_in_thread(loaded, NEW_SELECTIONS_CSV)
    assert entered.wait(timeout=5)
    try:
        r = save(loaded, row["id"], v, start=1000, end=1500, data_rev=old_rev)
        assert r.status_code == 303, r.text
    finally:
        release.set()
    t.join(timeout=10)
    assert "exc" not in box

    s2 = state(loaded)
    # 校时已保存：原字幕 [1000,1500) 时刻保留
    assert (sub(s2, "M1", 1)["start_ms"], sub(s2, "M1", 1)["end_ms"]) == (1000, 1500)
    # 第 1 段 [0,4000) 仍包含这句，行保留且反映新时刻；第 3 段 [4000,7000) 已剪掉，无行
    r1 = find_row(s2["final_rows"], 1, "M1", 1)
    assert (r1["final_start_ms"], r1["final_end_ms"]) == (1000, 1500)
    assert not [r for r in s2["final_rows"] if r["selection_seq"] == 3 and r["subtitle_no"] == 1]


def test_swap_failure_rolls_back_keeps_old_segments_and_saved_adjustments(loaded, monkeypatch):
    """切换阶段失败：继续用原选段，重算期间已保存的校时仍然有效。"""
    s = state(loaded)
    row = find_row(s["final_rows"], 1, "M1", 1)
    v = sub(s, "M1", 1)["version"]
    old_rev = s["data_rev"]

    entered = threading.Event()
    release = threading.Event()

    def hook(phase):
        if phase == "compute":
            entered.set()
            release.wait(timeout=10)

    monkeypatch.setattr(logic, "_hook_phase", hook)

    def boom():
        raise RuntimeError("模拟切换失败")

    monkeypatch.setattr(logic, "_hook_fail_swap", boom)

    t, box = _upload_selections_in_thread(loaded, NEW_SELECTIONS_CSV)
    assert entered.wait(timeout=5)
    try:
        r = save(loaded, row["id"], v, start=1000, end=2800, data_rev=old_rev)
        assert r.status_code == 303, r.text
    finally:
        release.set()
    t.join(timeout=10)

    # 上传请求被接住并重定向回上传页报错（不是 500）
    resp = box.get("resp")
    assert resp is not None and resp.status_code == 303
    assert "err" in resp.headers["location"]

    s2 = state(loaded)
    # 数据版本号不变：换段未生效
    assert s2["data_rev"] == old_rev
    # 原选段原样保留：第 3 段切点仍是 [2000,7000)
    assert [sel["start_ms"] for sel in s2["selections"]] == [0, 0, 2000]
    # 重算期间保存的校时仍然有效，并按原选段重算出第 3 段成片行 [6500,7300)
    assert (sub(s2, "M1", 1)["start_ms"], sub(s2, "M1", 1)["end_ms"]) == (1000, 2800)
    r4 = find_row(s2["final_rows"], 3, "M1", 1)
    assert (r4["final_start_ms"], r4["final_end_ms"]) == (6500, 7300)


def test_stale_page_after_switch_rejected_with_refresh_hint(loaded, monkeypatch):
    """换段后提交换段前打开的旧校时页面：拒绝保存并提示刷新。"""
    s = state(loaded)
    row = find_row(s["final_rows"], 1, "M1", 1)
    v = sub(s, "M1", 1)["version"]
    old_rev = s["data_rev"]

    # 完成一次换段（新选段里第 1 段 [0,4000) 仍在，旧行 id 可能被新行复用）
    r = loaded.post(
        "/upload/selections",
        files={"file": ("new_sel.csv", NEW_SELECTIONS_CSV)},
        follow_redirects=False,
    )
    assert r.status_code == 303
    s2 = state(loaded)
    assert s2["data_rev"] == old_rev + 1

    # 用换段前页面里的隐藏字段（旧 data_rev、旧 version）提交
    r = save(loaded, row["id"], v, start=1000, end=2700, data_rev=old_rev)
    assert r.status_code == 409
    assert "刷新" in r.text
    # 原字幕未被这次陈旧提交改动
    assert (sub(state(loaded), "M1", 1)["start_ms"],
            sub(state(loaded), "M1", 1)["end_ms"]) == (1000, 3000)


def test_edit_page_carries_current_data_rev(loaded):
    s = state(loaded)
    row_id = s["final_rows"][0]["id"]
    html = loaded.get(f"/rows/{row_id}").text
    assert f'name="data_rev" value="{s["data_rev"]}"' in html


def test_source_upload_also_bumps_rev_and_rebuilds(loaded):
    """只重传原片字幕 CSV：同样两阶段替换，data_rev 递增，成片行按新字幕重算。"""
    old_rev = state(loaded)["data_rev"]
    src = b"material_id,subtitle_no,start_ms,end_ms,text\nA,1,0,1000,hello\n"
    r = loaded.post("/upload/source", files={"file": ("s.csv", src)}, follow_redirects=False)
    assert r.status_code == 303
    s = state(loaded)
    assert s["data_rev"] == old_rev + 1
    # 选段未动（仍为样例 3 段），但只有素材 A 不再与任何选段相交 -> 无成片行
    assert len(s["selections"]) == 3
    assert s["final_rows"] == []
    # 旧页面（旧 data_rev）再保存被拒绝
    r = save(loaded, 1, 1, start=0, end=500, data_rev=old_rev)
    assert r.status_code == 409 and "刷新" in r.text


def test_bad_selections_csv_keeps_rev_and_data(loaded):
    before = state(loaded)
    r = loaded.post(
        "/upload/selections",
        files={"file": ("bad.csv", b"material_id,start_ms,end_ms\nM1,x,1\n")},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "err" in r.headers["location"]
    assert state(loaded) == before  # data_rev 也不变
