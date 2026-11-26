"""补传演练测试：切块、规划器（紧额度）、漏帧补传、Flask 全流程。"""
import hashlib
import io

import pytest

import app as web
from planner import (CHUNK_SIZE, MAX_CHUNKS, apply_round, plan_round,
                     reassemble, split_chunks)


# ---------- 切块 ----------

def test_split_and_reassemble_roundtrip():
    data = bytes(range(256)) * 35 + b"tail"  # 8964 字节 → 3 块
    chunks, size = split_chunks(data)
    assert len(chunks) == 3
    assert all(len(c) == CHUNK_SIZE for c in chunks)
    assert reassemble(chunks, size) == data


def test_split_limits():
    chunks, _ = split_chunks(b"x" * (CHUNK_SIZE * MAX_CHUNKS))
    assert len(chunks) == MAX_CHUNKS
    with pytest.raises(ValueError):
        split_chunks(b"x" * (CHUNK_SIZE * MAX_CHUNKS + 1))
    with pytest.raises(ValueError):
        split_chunks(b"")


# ---------- 规划器 ----------

def test_plan_xor_saves_frame_when_quota_allows():
    # T0 缺块3，T1 缺块2：一帧异或即可双双解回，但各需 1 次解码额度
    owned = [0b011, 0b101]
    plan = plan_round(owned, [1, 1], frame_limit=1, n_chunks=3)
    assert plan == [("xor", 1, 2)]


def test_plan_tight_quota_no_solution_then_exact():
    owned = [0b011, 0b101]
    # 解码额度为 0、只许 1 帧：无解
    assert plan_round(owned, [0, 0], frame_limit=1, n_chunks=3) is None
    # 额度为 0、放宽到 2 帧：退化为两帧单块
    plan = plan_round(owned, [0, 0], frame_limit=2, n_chunks=3)
    assert plan is not None and len(plan) == 2
    assert all(fr[0] == "single" for fr in plan)


def test_plan_tight_frame_limit():
    # 三台各缺两块，最少 3 帧（三块单播）
    owned = [0b001, 0b010, 0b100]
    assert plan_round(owned, [8, 8, 8], frame_limit=2, n_chunks=3) is None
    plan = plan_round(owned, [8, 8, 8], frame_limit=3, n_chunks=3)
    assert plan is not None and len(plan) == 3


def test_plan_already_complete():
    assert plan_round([0b111, 0b111], [0, 0], frame_limit=4, n_chunks=3) == []


# ---------- 轮次执行 ----------

def test_apply_round_fault_injection():
    frames = [("xor", 1, 2)]
    res = apply_round([0b011, 0b101], [1, 1], frames, fault=(0, 1))
    assert res[0]["mask"] == 0b011 and res[0]["decodes"] == 0  # 漏收，原地踏步
    assert res[1]["mask"] == 0b111 and res[1]["decodes"] == 1  # 解回块2
    assert res[1]["decoded"] == [(1, 1)]


def test_apply_round_quota_exhausted_no_decode():
    frames = [("xor", 1, 2)]
    res = apply_round([0b011, 0b101], [0, 0], frames)
    assert res[0]["mask"] == 0b011 and res[1]["mask"] == 0b101


# ---------- Flask 全流程 ----------

ORIGINAL = (b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 34 + b"demo")  # 3 块


@pytest.fixture
def client():
    with web.LOCK:
        gen = web.GENERATION
        web.STATE.clear()
        web.STATE.update(web._fresh_state())
        web.GENERATION = gen + 1  # 每个测试用例独立代次，避免历史链接串扰
    web.app.config.update(TESTING=True)
    return web.app.test_client()


def _upload(client, data=ORIGINAL, name="new.png"):
    return client.post("/upload", data={"image": (io.BytesIO(data), name)},
                       content_type="multipart/form-data", follow_redirects=True)


def _setup_two_terminals(client):
    # T1 首播收到 #1#2（缺#3），T2 收到 #1#3（缺#2）
    return client.post("/terminals", data={
        "enable_0": "on", "name_0": "T1", "recv_0": ["1", "2"],
        "enable_1": "on", "name_1": "T2", "recv_1": ["1", "3"],
    }, follow_redirects=True)


def _gen():
    with web.LOCK:
        return web.GENERATION


def _dl(client, tid, generation=None):
    g = _gen() if generation is None else generation
    return client.get(f"/terminal/{tid}/download?generation={g}")


def test_full_flow_xor_round_completes_all(client):
    assert b"3" in _upload(client).data  # 切为 3 块
    _setup_two_terminals(client)
    r = client.post("/round", data={"frame_limit": "1", "quota_0": "1", "quota_1": "1"},
                    follow_redirects=True)
    assert "已发送 1 帧".encode() in r.data
    with web.LOCK:
        terms = list(web.STATE["terminals"])
        rounds = list(web.STATE["rounds"])
    assert all(t["completed"] for t in terms)
    assert rounds[0]["frames"] == ["异或 #2⊕#3"]
    # 两台都通过校验并替换旧图，可下载且与新图一致（链接须带当前代次）
    for tid in (0, 1):
        dl = _dl(client, tid)
        assert dl.status_code == 200
        assert dl.data == ORIGINAL
        assert hashlib.sha256(dl.data).hexdigest() == web.STATE["image"]["sha256"]


def test_no_solution_keeps_state(client):
    _upload(client)
    _setup_two_terminals(client)
    r = client.post("/round", data={"frame_limit": "1", "quota_0": "0", "quota_1": "0"},
                    follow_redirects=True)
    assert "无方案".encode() in r.data
    with web.LOCK:
        assert web.STATE["rounds"] == []
        assert web.STATE["terminals"][0]["mask"] == 0b011
        assert web.STATE["terminals"][1]["mask"] == 0b101


def test_missed_frame_then_next_round_repair(client):
    _upload(client)
    _setup_two_terminals(client)
    # 第 1 轮：T1 漏收唯一的异或帧
    r = client.post("/round", data={
        "frame_limit": "1", "quota_0": "1", "quota_1": "1",
        "fault_terminal": "0", "fault_frame": "1",
    }, follow_redirects=True)
    assert "漏收第1帧".encode() in r.data
    with web.LOCK:
        t1, t2 = web.STATE["terminals"]
        assert not t1["completed"] and t1["mask"] == 0b011  # 仍缺 #3
        assert t2["completed"]
    dl = _dl(client, 0)
    assert dl.data != ORIGINAL  # 旧图未被替换
    # 第 2 轮：按实收状态补传，单块 #3 即可，无需解码额度
    r = client.post("/round", data={"frame_limit": "2", "quota_0": "0", "quota_1": "0"},
                    follow_redirects=True)
    assert "已发送 1 帧".encode() in r.data
    with web.LOCK:
        assert web.STATE["rounds"][1]["frames"] == ["单块 #3"]
        assert web.STATE["terminals"][0]["completed"]
    assert _dl(client, 0).data == ORIGINAL


def test_upload_oversize_rejected(client):
    big = b"y" * (CHUNK_SIZE * MAX_CHUNKS + 1)
    r = _upload(client, data=big, name="big.bin")
    assert "超过".encode() in r.data or "上限".encode() in r.data
    with web.LOCK:
        assert web.STATE["image"] is None


# ---------- 值班员监看页：条件请求、分页、fencing、陈旧下载、并发隔离 ----------

def _send_one_round(client):
    return client.post("/round", data={"frame_limit": "1", "quota_0": "1", "quota_1": "1"},
                       follow_redirects=True)


def test_monitor_page_renders(client):
    _upload(client)
    page = client.get("/monitor")
    assert page.status_code == 200
    assert b"DrillPoller" in page.data  # 轮询控制器已挂载
    assert b"/api/state" in page.data and b"/api/rounds" in page.data


def test_state_initial_payload_excludes_round_details(client):
    _upload(client)
    _setup_two_terminals(client)
    _send_one_round(client)
    g = _gen()
    r = client.get(f"/api/state?generation={g}")
    assert r.status_code == 200
    body = r.get_json()
    assert body["generation"] == g
    assert body["revision"] == 1
    assert body["total_rounds"] == 1
    # 进度接口只含各站进度，不含任何轮次明细（窄带首包要小）
    assert "rounds" not in body
    assert body["terminals"] and "missing" in body["terminals"][0]
    assert r.headers["ETag"]


def test_state_conditional_304_no_body_no_progress(client):
    _upload(client)
    _setup_two_terminals(client)
    g = _gen()
    first = client.get(f"/api/state?generation={g}")
    etag = first.headers["ETag"]
    second = client.get(f"/api/state?generation={g}", headers={"If-None-Match": etag})
    assert second.status_code == 304
    assert second.data == b""            # 无变化：不重传进度或轮次
    # 发一轮后同 ETag 条件请求应解除 304
    _send_one_round(client)
    third = client.get(f"/api/state?generation={g}", headers={"If-None-Match": etag})
    assert third.status_code == 200
    assert third.get_json()["revision"] == 1


def test_state_stale_generation_409(client):
    _upload(client)
    _setup_two_terminals(client)
    old_g = _gen()
    # 重新登记终端推进代次：持旧代次的迟到回复必须被拒绝
    _setup_two_terminals(client)
    new_g = _gen()
    assert new_g > old_g
    r = client.get(f"/api/state?generation={old_g}")
    assert r.status_code == 409
    assert r.get_json()["error"] == "generation_mismatch"
    assert r.get_json()["generation"] == new_g
    # 新代次正常
    assert client.get(f"/api/state?generation={new_g}").status_code == 200


def test_stale_generation_beats_conditional_304(client):
    # fencing 优先于条件请求：旧代次即使 ETag 匹配也必须 409，不能被 304 放行
    _upload(client)
    _setup_two_terminals(client)
    old_g = _gen()
    old_etag = client.get(f"/api/state?generation={old_g}").headers["ETag"]
    _setup_two_terminals(client)  # 推进代次
    r = client.get(f"/api/state?generation={old_g}",
                   headers={"If-None-Match": old_etag})
    assert r.status_code == 409
    assert r.get_json()["error"] == "generation_mismatch"


def test_rounds_pagination_max_12_and_indexed_slice(client):
    _upload(client)
    _setup_two_terminals(client)
    g = _gen()
    # 直接构造 25 轮记录，验证只按下标切页、不扫描其他轮
    with web.LOCK:
        base = len(web.STATE["rounds"])
        for k in range(25):
            web.STATE["rounds"].append({
                "no": base + k + 1, "frame_limit": 1, "frames": ["单块 #1"],
                "fault": None, "terminals": [],
            })
    # 首次只取最近 12 轮（轮号 14..25）
    r = client.get(f"/api/rounds?generation={g}").get_json()
    assert r["returned"] == 12 and r["total"] == 25
    assert [x["no"] for x in r["rounds"]] == list(range(14, 26))
    assert r["has_more"] is True and r["next_before"] == 13
    # limit 再大也被夹到 12
    r2 = client.get(f"/api/rounds?generation={g}&limit=999").get_json()
    assert r2["returned"] == 12 and r2["limit"] == 12
    # 用游标向更早翻页：before=13 取轮号 2..13
    r3 = client.get(f"/api/rounds?generation={g}&before=13&limit=12").get_json()
    assert [x["no"] for x in r3["rounds"]] == list(range(2, 14))
    assert r3["next_before"] == 1 and r3["has_more"] is True
    # 最后一页只剩 1 条
    r4 = client.get(f"/api/rounds?generation={g}&before=1&limit=12").get_json()
    assert [x["no"] for x in r4["rounds"]] == [1]
    assert r4["has_more"] is False and r4["next_before"] is None


def test_rounds_stale_generation_409(client):
    _upload(client)
    _setup_two_terminals(client)
    old_g = _gen()
    _send_one_round(client)
    client.post("/reset")
    r = client.get(f"/api/rounds?generation={old_g}")
    assert r.status_code == 409
    assert r.get_json()["error"] == "generation_mismatch"


def test_download_stale_after_reupload_and_reregister(client):
    _upload(client)
    _setup_two_terminals(client)
    _send_one_round(client)
    g1 = _gen()
    assert _dl(client, 0, g1).status_code == 200
    # 重新登记 → 代次推进，旧链接 409 拒绝并提示刷新
    _setup_two_terminals(client)
    stale = _dl(client, 0, g1)
    assert stale.status_code == 409
    assert "刷新".encode() in stale.data
    # 换图后同样拒绝
    _upload(client, data=ORIGINAL, name="again.png")
    g2 = _gen()
    assert _dl(client, 0, g1).status_code == 409
    # 缺代次参数也拒绝，杜绝无 fencing 的直连
    bare = client.get("/terminal/0/download")
    assert bare.status_code == 409
    assert g2 > g1


def test_download_stale_after_reset(client):
    _upload(client)
    _setup_two_terminals(client)
    g1 = _gen()
    client.post("/reset")
    assert _dl(client, 0, g1).status_code == 409


def test_late_reply_cannot_roll_back_progress(client):
    _upload(client)
    _setup_two_terminals(client)
    old_g = _gen()
    _send_one_round(client)
    # 换图开新一代并发轮，旧代次的迟到 state/rounds 回复一律 409，
    # 客户端据此丢弃，进度不会被旧数据回退
    _upload(client, data=ORIGINAL, name="next.png")
    _setup_two_terminals(client)
    new_g = _gen()
    assert client.get(f"/api/state?generation={old_g}").status_code == 409
    assert client.get(f"/api/rounds?generation={old_g}").status_code == 409
    cur = client.get(f"/api/state?generation={new_g}").get_json()
    assert cur["generation"] == new_g and cur["total_rounds"] == 0


def test_concurrent_monitor_does_not_block_control(client):
    _upload(client)
    _setup_two_terminals(client)
    g = _gen()
    # 预置一轮故障：T1 漏收，保持未收齐，后续每轮都会真正追加（frames 非空）
    client.post("/round", data={
        "frame_limit": "1", "quota_0": "1", "quota_1": "1",
        "fault_terminal": "0", "fault_frame": "1",
    }, follow_redirects=True)
    import concurrent.futures

    def monitor(_):
        return client.get(f"/api/state?generation={g}").status_code

    def control(_):
        # T1 仍缺块且每轮注入漏收：每轮都发单块 #3 但 T1 一直未收齐，确保每轮追加
        r = client.post("/round", data={
            "frame_limit": "2", "quota_0": "0", "quota_1": "0",
            "fault_terminal": "0", "fault_frame": "1",
        }, follow_redirects=True)
        return r.status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(monitor, i) for i in range(8)]
        futs += [ex.submit(control, i) for i in range(4)]
        results = [f.result(timeout=15) for f in futs]
    assert all(200 <= code < 400 for code in results)
    with web.LOCK:
        # 预置 1 轮 + 并发 4 轮全部落库，监看请求未阻塞发帧
        assert len(web.STATE["rounds"]) == 5


def test_download_runs_send_file_outside_lock(client, monkeypatch):
    # 结构性保证：慢下载在锁外传输，不阻塞发帧等其他页面
    import app as webmod
    _upload(client)
    _setup_two_terminals(client)
    g = _gen()
    held = {}

    real_send_file = webmod.send_file

    def spy_send_file(*a, **kw):
        held["locked_during_send"] = webmod.LOCK.locked()
        return real_send_file(*a, **kw)

    monkeypatch.setattr(webmod, "send_file", spy_send_file)
    resp = _dl(client, 0, g)
    assert resp.status_code == 200
    assert held["locked_during_send"] is False
