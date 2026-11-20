"""端到端测试：校时换算、替换重算、冲突与恢复、整批拒绝、变更清单、查询、
段内号与全局号双编号、重启持久化、旧卷兼容。"""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def clean_tables():
    conn = db.get_conn()
    with db.lock():
        conn.execute("DELETE FROM observations")
        conn.execute("DELETE FROM segment_observations")
        conn.execute("DELETE FROM calibration_points")
    yield


# ---------------------------------------------------------------- 工具

def post_obs(items):
    r = client.post("/observations", json={"observations": items})
    assert r.status_code == 200, r.text
    return r.json()


def post_cal(items, expect=200):
    r = client.post("/calibrations", json={"calibrations": items})
    assert r.status_code == expect, r.text
    return r.json()


def get_obs(obs_id):
    return client.get(f"/observations/{obs_id}")


def get_segment_obs(segment, local_id):
    return client.get(f"/segments/{segment}/observations/{local_id}")


def tide(start, end, include_segment_ids=False):
    params = {"start_ms": start, "end_ms": end}
    if include_segment_ids:
        params["include_segment_ids"] = "true"
    r = client.get("/tide-levels", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def changed_ids(resp):
    return {c["obs_id"] for c in resp["changed"]}


def seg_changed_keys(resp):
    return {(c["boot_segment"], c["local_obs_id"]) for c in resp["segment_changed"]}


# ---------------------------------------------------------------- 校时换算

def test_pending_without_calibration_keeps_raw():
    resp = post_obs([
        {"obs_id": "o1", "boot_segment": "A", "counter": 100, "tide_level": 1.5},
    ])
    assert changed_ids(resp) == {"o1"}
    body = get_obs("o1").json()
    assert body["status"] == "pending_calibration"
    assert body["utc_ms"] is None
    assert body["calibration"] is None
    # 原文保留
    assert (body["boot_segment"], body["counter"], body["tide_level"]) == ("A", 100, 1.5)
    assert "尚无校时点" in body["reason"]


def test_linear_interpolation_with_floor_and_basis():
    post_cal([
        {"boot_segment": "A", "counter": 1000, "utc_ms": 10000},
        {"boot_segment": "A", "counter": 2000, "utc_ms": 20050},
    ])
    resp = post_obs([
        {"obs_id": "lo", "boot_segment": "A", "counter": 1000, "tide_level": 1.0},
        {"obs_id": "floor", "boot_segment": "A", "counter": 1001, "tide_level": 2.0},
        {"obs_id": "mid", "boot_segment": "A", "counter": 1500, "tide_level": 3.0},
        {"obs_id": "hi", "boot_segment": "A", "counter": 2000, "tide_level": 4.0},
        {"obs_id": "above", "boot_segment": "A", "counter": 2001, "tide_level": 5.0},
        {"obs_id": "below", "boot_segment": "A", "counter": 999, "tide_level": 6.0},
    ])
    assert changed_ids(resp) == {"lo", "floor", "mid", "hi", "above", "below"}

    assert get_obs("lo").json()["utc_ms"] == 10000
    # 10000 + 1*10050/1000 = 10010.05 -> 向下取整 10010
    assert get_obs("floor").json()["utc_ms"] == 10010
    # 10000 + 500*10050/1000 = 15025
    mid = get_obs("mid").json()
    assert mid["utc_ms"] == 15025 and mid["status"] == "ok"
    assert mid["calibration"] == {
        "lo": {"counter": 1000, "utc_ms": 10000},
        "hi": {"counter": 2000, "utc_ms": 20050},
    }
    assert get_obs("hi").json()["utc_ms"] == 20050

    for oid in ("above", "below"):
        body = get_obs(oid).json()
        assert body["status"] == "pending_calibration"
        assert body["utc_ms"] is None
        assert "超出" in body["reason"]


def test_single_calibration_point_anchors_exact_counter_only():
    post_cal([{"boot_segment": "A", "counter": 500, "utc_ms": 7777}])
    post_obs([
        {"obs_id": "hit", "boot_segment": "A", "counter": 500, "tide_level": 1.0},
        {"obs_id": "miss", "boot_segment": "A", "counter": 501, "tide_level": 2.0},
    ])
    assert get_obs("hit").json()["utc_ms"] == 7777
    assert get_obs("hit").json()["status"] == "ok"
    assert get_obs("miss").json()["status"] == "pending_calibration"


# ---------------------------------------------------------------- 替换与重算

def test_replaced_calibration_recalculates_affected():
    post_cal([
        {"boot_segment": "A", "counter": 1000, "utc_ms": 10000},
        {"boot_segment": "A", "counter": 2000, "utc_ms": 20000},
    ])
    post_obs([{"obs_id": "x", "boot_segment": "A", "counter": 1500, "tide_level": 1.0}])
    assert get_obs("x").json()["utc_ms"] == 15000

    # 同段同计数的新校时点替换旧值 -> 重算
    resp = post_cal([{"boot_segment": "A", "counter": 2000, "utc_ms": 21000}])
    assert changed_ids(resp) == {"x"}
    assert resp["changed"][0]["utc_ms"] == 15500
    assert get_obs("x").json()["utc_ms"] == 15500


def test_late_calibration_extends_range_and_fills_pending():
    post_cal([
        {"boot_segment": "A", "counter": 1000, "utc_ms": 10000},
        {"boot_segment": "A", "counter": 2000, "utc_ms": 20000},
    ])
    post_obs([{"obs_id": "y", "boot_segment": "A", "counter": 3000, "tide_level": 1.0}])
    assert get_obs("y").json()["status"] == "pending_calibration"

    # 补传校时点 -> 区间外观测被重算为可用
    resp = post_cal([{"boot_segment": "A", "counter": 3000, "utc_ms": 30000}])
    assert changed_ids(resp) == {"y"}
    body = get_obs("y").json()
    assert body["status"] == "ok" and body["utc_ms"] == 30000


def test_calibration_batch_without_observations_changes_nothing():
    resp = post_cal([{"boot_segment": "A", "counter": 0, "utc_ms": 100}])
    assert resp["changed_count"] == 0 and resp["changed"] == []


# ---------------------------------------------------------------- 整批拒绝

def test_backwards_utc_rejects_whole_batch_and_keeps_old_data():
    post_cal([
        {"boot_segment": "A", "counter": 1000, "utc_ms": 10000},
        {"boot_segment": "A", "counter": 2000, "utc_ms": 20000},
    ])
    post_obs([{"obs_id": "z", "boot_segment": "A", "counter": 1500, "tide_level": 2.5}])
    assert get_obs("z").json()["utc_ms"] == 15000

    # 同一批里：B 段校时点本身合法，A 段新点使 UTC 倒退 -> 整批拒绝
    resp = post_cal([
        {"boot_segment": "B", "counter": 0, "utc_ms": 5000},
        {"boot_segment": "A", "counter": 1500, "utc_ms": 9000},
    ], expect=409)
    assert "整批拒绝" in resp["detail"]["message"]
    assert resp["detail"]["violations"] == [{
        "boot_segment": "A", "counter": 1500, "utc_ms": 9000,
        "prev_counter": 1000, "prev_utc_ms": 10000,
    }]

    # 旧数据照常供数：A 段观测不变，B 段合法点也未落库
    assert get_obs("z").json()["utc_ms"] == 15000
    assert tide(0, 99999)["count"] == 1
    post_obs([{"obs_id": "b1", "boot_segment": "B", "counter": 0, "tide_level": 1.0}])
    assert get_obs("b1").json()["status"] == "pending_calibration"


def test_equal_utc_within_segment_is_not_backwards():
    post_cal([
        {"boot_segment": "A", "counter": 0, "utc_ms": 1000},
        {"boot_segment": "A", "counter": 10, "utc_ms": 1000},  # 持平不算倒退
    ])
    post_obs([{"obs_id": "e", "boot_segment": "A", "counter": 5, "tide_level": 1.0}])
    assert get_obs("e").json()["utc_ms"] == 1000


# ---------------------------------------------------------------- 冲突与恢复

def test_conflict_across_boot_segments_and_restore():
    post_cal([
        {"boot_segment": "A", "counter": 0, "utc_ms": 1000},
        {"boot_segment": "B", "counter": 0, "utc_ms": 1000},
    ])
    post_obs([{"obs_id": "a1", "boot_segment": "A", "counter": 0, "tide_level": 1.0}])
    assert get_obs("a1").json()["status"] == "ok"

    # 不同开机段落在同一 UTC 毫秒 -> 双方均不供分析
    resp = post_obs([{"obs_id": "b1", "boot_segment": "B", "counter": 0, "tide_level": 2.0}])
    assert changed_ids(resp) == {"a1", "b1"}
    for oid in ("a1", "b1"):
        body = get_obs(oid).json()
        assert body["status"] == "conflict"
        assert "冲突" in body["reason"]
    assert tide(0, 99999)["count"] == 0

    # 更正校时点解除冲突 -> 双方恢复
    resp = post_cal([{"boot_segment": "B", "counter": 0, "utc_ms": 2000}])
    assert changed_ids(resp) == {"a1", "b1"}
    assert get_obs("a1").json()["status"] == "ok"
    b1 = get_obs("b1").json()
    assert b1["status"] == "ok" and b1["utc_ms"] == 2000
    assert tide(0, 99999)["count"] == 2


def test_same_segment_same_ms_is_not_conflict():
    post_cal([{"boot_segment": "A", "counter": 0, "utc_ms": 1000}])
    post_obs([
        {"obs_id": "s1", "boot_segment": "A", "counter": 0, "tide_level": 1.0},
        {"obs_id": "s2", "boot_segment": "A", "counter": 0, "tide_level": 2.0},
    ])
    assert get_obs("s1").json()["status"] == "ok"
    assert get_obs("s2").json()["status"] == "ok"


def test_calibration_change_can_conflict_other_segment():
    post_cal([
        {"boot_segment": "A", "counter": 0, "utc_ms": 1000},
        {"boot_segment": "B", "counter": 0, "utc_ms": 5000},
    ])
    post_obs([
        {"obs_id": "a1", "boot_segment": "A", "counter": 0, "tide_level": 1.0},
        {"obs_id": "b1", "boot_segment": "B", "counter": 0, "tide_level": 2.0},
    ])
    assert get_obs("a1").json()["status"] == "ok"

    # 更正 B 段校时点，使 b1 落到 a1 的毫秒上 -> a1 被卷入冲突并列进变更清单
    resp = post_cal([{"boot_segment": "B", "counter": 0, "utc_ms": 1000}])
    assert changed_ids(resp) == {"a1", "b1"}
    assert get_obs("a1").json()["status"] == "conflict"
    assert get_obs("b1").json()["status"] == "conflict"
    assert tide(0, 99999)["count"] == 0


def test_overwritten_observation_can_resolve_conflict():
    post_cal([
        {"boot_segment": "A", "counter": 0, "utc_ms": 1000},
        {"boot_segment": "B", "counter": 0, "utc_ms": 1000},
        {"boot_segment": "B", "counter": 10, "utc_ms": 5000},
    ])
    post_obs([
        {"obs_id": "a1", "boot_segment": "A", "counter": 0, "tide_level": 1.0},
        {"obs_id": "b1", "boot_segment": "B", "counter": 0, "tide_level": 2.0},
    ])
    assert get_obs("a1").json()["status"] == "conflict"

    # 覆盖 b1 原文使其移走 -> a1 自动恢复
    resp = post_obs([{"obs_id": "b1", "boot_segment": "B", "counter": 10, "tide_level": 2.0}])
    assert changed_ids(resp) == {"a1", "b1"}
    assert get_obs("a1").json()["status"] == "ok"
    assert get_obs("b1").json()["utc_ms"] == 5000


# ---------------------------------------------------------------- 变更清单与查询

def test_identical_resubmission_reports_no_change():
    item = {"obs_id": "r1", "boot_segment": "A", "counter": 0, "tide_level": 1.0}
    post_cal([{"boot_segment": "A", "counter": 0, "utc_ms": 1000}])
    assert changed_ids(post_obs([item])) == {"r1"}
    assert changed_ids(post_obs([item])) == set()  # 无变化不列出


def test_tide_levels_range_query_only_serves_ok():
    post_cal([
        {"boot_segment": "A", "counter": 0, "utc_ms": 1000},
        {"boot_segment": "A", "counter": 100, "utc_ms": 1100},
        {"boot_segment": "B", "counter": 0, "utc_ms": 1050},
        {"boot_segment": "C", "counter": 0, "utc_ms": 1050},
    ])
    post_obs([
        {"obs_id": "in1", "boot_segment": "A", "counter": 0, "tide_level": 1.0},
        {"obs_id": "in2", "boot_segment": "A", "counter": 40, "tide_level": 2.0},
        {"obs_id": "edge", "boot_segment": "A", "counter": 100, "tide_level": 3.0},
        {"obs_id": "cf1", "boot_segment": "B", "counter": 0, "tide_level": 4.0},
        {"obs_id": "cf2", "boot_segment": "C", "counter": 0, "tide_level": 5.0},
        {"obs_id": "pend", "boot_segment": "A", "counter": 500, "tide_level": 6.0},
    ])
    body = tide(1000, 1100)  # 边界含端点；冲突与待校时不供分析
    assert [p["obs_id"] for p in body["items"]] == ["in1", "in2", "edge"]
    assert body["items"][1]["utc_ms"] == 1040
    assert tide(1051, 1051)["count"] == 0
    assert tide(0, 999)["count"] == 0


def test_tide_levels_rejects_inverted_range():
    r = client.get("/tide-levels", params={"start_ms": 2000, "end_ms": 1000})
    assert r.status_code == 422


def test_get_observation_404_and_health():
    assert get_obs("nope").status_code == 404
    assert client.get("/health").json() == {"status": "up"}


def test_docs_and_openapi_available():
    assert client.get("/docs").status_code == 200
    paths = client.get("/openapi.json").json()["paths"]
    for p in ("/observations", "/calibrations", "/tide-levels", "/observations/{obs_id}",
              "/segments/{boot_segment}/observations/{local_obs_id}"):
        assert p in paths


# ---------------------------------------------------------------- 段内号与全局号并存

def test_restarted_recorder_same_local_id_keeps_both():
    """记录器重启后又报 42 号：前后两条都保留，各自可查可供数。"""
    post_cal([
        {"boot_segment": "boot-1", "counter": 0, "utc_ms": 1000},
        {"boot_segment": "boot-2", "counter": 0, "utc_ms": 5000},
    ])
    resp = post_obs([
        {"local_obs_id": "42", "boot_segment": "boot-1", "counter": 0, "tide_level": 1.1},
    ])
    assert resp["changed"] == []
    assert seg_changed_keys(resp) == {("boot-1", "42")}

    # 新记录器重启，新开机段又报来 42 号 -> 另存一条，不覆盖前一条
    resp = post_obs([
        {"local_obs_id": "42", "boot_segment": "boot-2", "counter": 0, "tide_level": 2.2},
    ])
    assert seg_changed_keys(resp) == {("boot-2", "42")}

    first = get_segment_obs("boot-1", "42").json()
    second = get_segment_obs("boot-2", "42").json()
    assert (first["tide_level"], first["utc_ms"], first["status"]) == (1.1, 1000, "ok")
    assert (second["tide_level"], second["utc_ms"], second["status"]) == (2.2, 5000, "ok")

    body = tide(0, 99999, include_segment_ids=True)
    assert [(p["boot_segment"], p["local_obs_id"]) for p in body["items"]] == [
        ("boot-1", "42"), ("boot-2", "42")]


def test_local_id_overwrites_only_within_same_segment():
    """段内号只覆盖本开机段的同号观测，其他段同号不受影响。"""
    post_cal([
        {"boot_segment": "boot-1", "counter": 0, "utc_ms": 1000},
        {"boot_segment": "boot-1", "counter": 100, "utc_ms": 2000},
        {"boot_segment": "boot-2", "counter": 0, "utc_ms": 5000},
    ])
    post_obs([
        {"local_obs_id": "42", "boot_segment": "boot-1", "counter": 0, "tide_level": 1.1},
        {"local_obs_id": "42", "boot_segment": "boot-2", "counter": 0, "tide_level": 2.2},
    ])
    # 同段同号重报 -> 覆盖原文并重算
    resp = post_obs([
        {"local_obs_id": "42", "boot_segment": "boot-1", "counter": 50, "tide_level": 1.5},
    ])
    assert seg_changed_keys(resp) == {("boot-1", "42")}
    body = get_segment_obs("boot-1", "42").json()
    assert (body["counter"], body["tide_level"], body["utc_ms"]) == (50, 1.5, 1500)
    # 另一段的同号观测原样保留
    other = get_segment_obs("boot-2", "42").json()
    assert (other["counter"], other["tide_level"], other["utc_ms"]) == (0, 2.2, 5000)


def test_local_id_coexists_with_same_named_global():
    """段内号可与同名全局号并存，各查各的，互不干扰。"""
    post_cal([{"boot_segment": "boot-1", "counter": 0, "utc_ms": 1000}])
    resp = post_obs([
        {"obs_id": "42", "boot_segment": "boot-1", "counter": 0, "tide_level": 9.9},
        {"local_obs_id": "42", "boot_segment": "boot-1", "counter": 0, "tide_level": 1.1},
    ])
    assert changed_ids(resp) == {"42"}
    assert seg_changed_keys(resp) == {("boot-1", "42")}
    assert get_obs("42").json()["tide_level"] == 9.9
    assert get_segment_obs("boot-1", "42").json()["tide_level"] == 1.1
    # 同段同毫秒不算冲突
    assert get_obs("42").json()["status"] == "ok"
    assert get_segment_obs("boot-1", "42").json()["status"] == "ok"


def test_mixed_batch_and_id_validation():
    """同一批可混用两种编号；每条只填一个号，都填或都不填 -> 422。"""
    post_cal([{"boot_segment": "boot-1", "counter": 0, "utc_ms": 1000}])
    resp = post_obs([
        {"obs_id": "g1", "boot_segment": "boot-1", "counter": 0, "tide_level": 1.0},
        {"local_obs_id": "l1", "boot_segment": "boot-1", "counter": 0, "tide_level": 2.0},
    ])
    assert changed_ids(resp) == {"g1"}
    assert seg_changed_keys(resp) == {("boot-1", "l1")}
    assert resp["changed_count"] == 1 and resp["segment_changed_count"] == 1

    r = client.post("/observations", json={"observations": [
        {"obs_id": "x", "local_obs_id": "y", "boot_segment": "boot-1",
         "counter": 0, "tide_level": 1.0}]})
    assert r.status_code == 422
    r = client.post("/observations", json={"observations": [
        {"boot_segment": "boot-1", "counter": 0, "tide_level": 1.0}]})
    assert r.status_code == 422


def test_global_id_cross_segment_correction():
    """全局号沿用跨段更正：同号在新开机段提交即覆盖原文并搬段。"""
    post_cal([
        {"boot_segment": "boot-1", "counter": 0, "utc_ms": 1000},
        {"boot_segment": "boot-2", "counter": 0, "utc_ms": 5000},
    ])
    post_obs([{"obs_id": "g", "boot_segment": "boot-1", "counter": 0, "tide_level": 1.0}])
    assert get_obs("g").json()["boot_segment"] == "boot-1"

    resp = post_obs([{"obs_id": "g", "boot_segment": "boot-2", "counter": 0, "tide_level": 2.0}])
    assert changed_ids(resp) == {"g"}
    body = get_obs("g").json()
    assert body["boot_segment"] == "boot-2"
    assert (body["tide_level"], body["utc_ms"]) == (2.0, 5000)


def test_segment_observation_detail_endpoint():
    """按开机段和段内号查原文及校时状态：待校时 -> 校时依据齐全。"""
    post_obs([{"local_obs_id": "l1", "boot_segment": "boot-1",
               "counter": 1500, "tide_level": 2.5}])
    body = get_segment_obs("boot-1", "l1").json()
    assert body["status"] == "pending_calibration"
    assert body["utc_ms"] is None and body["calibration"] is None
    assert (body["boot_segment"], body["local_obs_id"],
            body["counter"], body["tide_level"]) == ("boot-1", "l1", 1500, 2.5)
    assert "尚无校时点" in body["reason"]

    post_cal([
        {"boot_segment": "boot-1", "counter": 1000, "utc_ms": 10000},
        {"boot_segment": "boot-1", "counter": 2000, "utc_ms": 20000},
    ])
    body = get_segment_obs("boot-1", "l1").json()
    assert body["status"] == "ok" and body["utc_ms"] == 15000
    assert body["calibration"] == {
        "lo": {"counter": 1000, "utc_ms": 10000},
        "hi": {"counter": 2000, "utc_ms": 20000},
    }
    assert get_segment_obs("boot-1", "nope").status_code == 404
    assert get_segment_obs("nope", "l1").status_code == 404


# ---------------------------------------------------------------- 两类观测共同判定冲突

def test_conflict_between_global_and_segment_local_and_restore():
    """全局号与段内号跨段同毫秒 -> 双方冲突；解除后双方恢复供数。"""
    post_cal([
        {"boot_segment": "boot-1", "counter": 0, "utc_ms": 1000},
        {"boot_segment": "boot-2", "counter": 0, "utc_ms": 1000},
    ])
    post_obs([{"obs_id": "g1", "boot_segment": "boot-1", "counter": 0, "tide_level": 1.0}])
    assert get_obs("g1").json()["status"] == "ok"

    resp = post_obs([
        {"local_obs_id": "l1", "boot_segment": "boot-2", "counter": 0, "tide_level": 2.0},
    ])
    # 全局号回执：g1 被卷入冲突；段内回执：boot-2/l1 新增即冲突
    assert changed_ids(resp) == {"g1"}
    assert seg_changed_keys(resp) == {("boot-2", "l1")}
    entry = resp["segment_changed"][0]
    assert (entry["utc_ms"], entry["status"]) == (1000, "conflict")
    assert get_obs("g1").json()["status"] == "conflict"
    assert get_segment_obs("boot-2", "l1").json()["status"] == "conflict"
    assert tide(0, 99999, include_segment_ids=True)["count"] == 0

    # 更正校时点解除冲突 -> 双方恢复，两类回执分别列出
    resp = post_cal([{"boot_segment": "boot-2", "counter": 0, "utc_ms": 2000}])
    assert changed_ids(resp) == {"g1"}
    assert seg_changed_keys(resp) == {("boot-2", "l1")}
    assert get_obs("g1").json()["status"] == "ok"
    body = get_segment_obs("boot-2", "l1").json()
    assert body["status"] == "ok" and body["utc_ms"] == 2000
    assert tide(0, 99999, include_segment_ids=True)["count"] == 2


def test_conflict_between_segment_locals_across_segments():
    """两条段内号观测跨段同毫秒 -> 冲突；覆盖其中一条解除后双方恢复。"""
    post_cal([
        {"boot_segment": "boot-1", "counter": 0, "utc_ms": 1000},
        {"boot_segment": "boot-2", "counter": 0, "utc_ms": 1000},
        {"boot_segment": "boot-2", "counter": 10, "utc_ms": 9000},
    ])
    resp = post_obs([
        {"local_obs_id": "a", "boot_segment": "boot-1", "counter": 0, "tide_level": 1.0},
        {"local_obs_id": "b", "boot_segment": "boot-2", "counter": 0, "tide_level": 2.0},
    ])
    assert seg_changed_keys(resp) == {("boot-1", "a"), ("boot-2", "b")}
    for seg, lid in (("boot-1", "a"), ("boot-2", "b")):
        assert get_segment_obs(seg, lid).json()["status"] == "conflict"

    # 覆盖 boot-2/b 的原文使其移走 -> 双方恢复
    resp = post_obs([
        {"local_obs_id": "b", "boot_segment": "boot-2", "counter": 10, "tide_level": 2.0},
    ])
    assert seg_changed_keys(resp) == {("boot-1", "a"), ("boot-2", "b")}
    assert get_segment_obs("boot-1", "a").json()["status"] == "ok"
    assert get_segment_obs("boot-2", "b").json()["utc_ms"] == 9000


# ---------------------------------------------------------------- 段内号供数与回执

def test_tide_levels_include_segment_ids():
    """默认仅全局号可用观测；带 flag 时可用段内观测及所属段一并返回。"""
    post_cal([
        {"boot_segment": "boot-1", "counter": 0, "utc_ms": 1000},
        {"boot_segment": "boot-1", "counter": 100, "utc_ms": 1100},
        {"boot_segment": "boot-2", "counter": 0, "utc_ms": 1050},
        {"boot_segment": "boot-3", "counter": 0, "utc_ms": 1050},
    ])
    post_obs([
        {"obs_id": "g1", "boot_segment": "boot-1", "counter": 0, "tide_level": 1.0},
        {"local_obs_id": "l1", "boot_segment": "boot-1", "counter": 40, "tide_level": 2.0},
        {"local_obs_id": "cf", "boot_segment": "boot-2", "counter": 0, "tide_level": 3.0},
        {"obs_id": "cf2", "boot_segment": "boot-3", "counter": 0, "tide_level": 4.0},
        {"local_obs_id": "pend", "boot_segment": "boot-1", "counter": 500, "tide_level": 5.0},
    ])
    # 默认：仅全局号可用观测，响应形状与旧版一致
    body = tide(0, 99999)
    assert [p["obs_id"] for p in body["items"]] == ["g1"]
    assert "local_obs_id" not in body["items"][0]
    # 带 flag：可用段内观测及所属段一并返回；冲突/待校时仍不供数
    body = tide(0, 99999, include_segment_ids=True)
    assert body["count"] == 2
    g, l = body["items"]
    assert g["obs_id"] == "g1" and g["utc_ms"] == 1000
    assert (l["local_obs_id"], l["boot_segment"], l["utc_ms"]) == ("l1", "boot-1", 1040)


def test_calibration_receipt_lists_segment_changes():
    """校时回执另列时刻/状态变化的段内观测：开机段、段内号、UTC 毫秒、状态。"""
    post_cal([
        {"boot_segment": "boot-1", "counter": 0, "utc_ms": 1000},
        {"boot_segment": "boot-1", "counter": 100, "utc_ms": 2000},
    ])
    post_obs([{"local_obs_id": "l1", "boot_segment": "boot-1",
               "counter": 50, "tide_level": 1.0}])
    assert get_segment_obs("boot-1", "l1").json()["utc_ms"] == 1500

    resp = post_cal([{"boot_segment": "boot-1", "counter": 100, "utc_ms": 3000}])
    assert resp["changed"] == [] and resp["changed_count"] == 0
    assert resp["segment_changed_count"] == 1
    assert resp["segment_changed"] == [
        {"boot_segment": "boot-1", "local_obs_id": "l1",
         "utc_ms": 2000, "status": "ok"}]


def test_failed_batch_keeps_segment_data():
    """批次失败（段内 UTC 倒退 -> 409）后原文、校时点与供数保持不变。"""
    post_cal([
        {"boot_segment": "boot-1", "counter": 0, "utc_ms": 1000},
        {"boot_segment": "boot-1", "counter": 100, "utc_ms": 2000},
    ])
    post_obs([
        {"obs_id": "g1", "boot_segment": "boot-1", "counter": 0, "tide_level": 1.0},
        {"local_obs_id": "l1", "boot_segment": "boot-1", "counter": 50, "tide_level": 1.5},
    ])
    post_cal([{"boot_segment": "boot-1", "counter": 50, "utc_ms": 500}], expect=409)

    assert get_obs("g1").json()["utc_ms"] == 1000
    body = get_segment_obs("boot-1", "l1").json()
    assert body["status"] == "ok" and body["utc_ms"] == 1500
    assert tide(0, 99999, include_segment_ids=True)["count"] == 2


# ---------------------------------------------------------------- 重启与旧卷

def test_restart_keeps_both_id_schemes(tmp_path, monkeypatch):
    """批次失败不丢数据；重启后两种编号照常可查、照常供数。"""
    monkeypatch.setenv("BUOY_DB", str(tmp_path / "buoy.db"))
    db.reset_for_tests()
    try:
        post_cal([
            {"boot_segment": "boot-1", "counter": 0, "utc_ms": 1000},
            {"boot_segment": "boot-1", "counter": 100, "utc_ms": 2000},
        ])
        post_obs([
            {"obs_id": "g1", "boot_segment": "boot-1", "counter": 0, "tide_level": 1.0},
            {"local_obs_id": "42", "boot_segment": "boot-1",
             "counter": 50, "tide_level": 1.5},
        ])
        # 批次失败：整批回滚，原文、校时点与供数保留
        post_cal([{"boot_segment": "boot-1", "counter": 50, "utc_ms": 100}], expect=409)

        # 模拟重启：丢弃连接，按同一数据库文件重开
        db.reset_for_tests()

        g = get_obs("g1").json()
        assert g["status"] == "ok" and g["utc_ms"] == 1000
        l = get_segment_obs("boot-1", "42").json()
        assert l["status"] == "ok" and l["utc_ms"] == 1500
        assert tide(0, 99999)["count"] == 1
        assert tide(0, 99999, include_segment_ids=True)["count"] == 2
    finally:
        db.reset_for_tests()


LEGACY_SCHEMA = """
CREATE TABLE calibration_points (
    boot_segment TEXT NOT NULL,
    counter      INTEGER NOT NULL,
    utc_ms       INTEGER NOT NULL,
    PRIMARY KEY (boot_segment, counter)
);
CREATE TABLE observations (
    obs_id         TEXT PRIMARY KEY,
    boot_segment   TEXT NOT NULL,
    counter        INTEGER NOT NULL,
    tide_level     REAL NOT NULL,
    utc_ms         INTEGER,
    status         TEXT NOT NULL,
    reason         TEXT,
    cal_lo_counter INTEGER,
    cal_lo_utc_ms  INTEGER,
    cal_hi_counter INTEGER,
    cal_hi_utc_ms  INTEGER
);
"""


def test_old_sqlite_volume_compatible(tmp_path, monkeypatch):
    """旧版 SQLite 卷（无段内观测表）可直接打开：旧数据可查，新功能可用。"""
    db_file = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_file)
    conn.executescript(LEGACY_SCHEMA)
    conn.execute("INSERT INTO calibration_points VALUES ('boot-1', 0, 1000)")
    conn.execute(
        "INSERT INTO observations (obs_id, boot_segment, counter, tide_level, "
        "utc_ms, status, reason) VALUES ('legacy', 'boot-1', 0, 3.3, 1000, 'ok', NULL)"
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("BUOY_DB", str(db_file))
    db.reset_for_tests()
    try:
        body = get_obs("legacy").json()
        assert body["status"] == "ok" and body["utc_ms"] == 1000
        # 新表自动补建，段内号可写可查
        post_obs([{"local_obs_id": "1", "boot_segment": "boot-1",
                   "counter": 0, "tide_level": 1.0}])
        assert get_segment_obs("boot-1", "1").json()["status"] == "ok"
        assert tide(0, 99999, include_segment_ids=True)["count"] == 2
    finally:
        db.reset_for_tests()
