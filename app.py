"""站台终端窄带组播补传演练网页。

流程：上传新图（≤32 KiB，切 4 KiB 块，至多 8 块）→ 登记 2~4 台终端首播
实收块号 → 逐轮规划补传（限帧数、限各终端异或解码次数，可注入某终端漏收
某帧）→ 各终端独立拼图，SHA-256 与新图相符才替换可下载的旧图。

窄带监看（值班员页 /monitor）：
- ``GET /monitor``            监看页（只读，与操作页隔离）。
- ``GET /api/state``          仅当前进度；条件请求无变化回 304，不传进度/轮次。
- ``GET /api/rounds``         轮次按游标分页，每次至多 12 条，只切所需下标。
- ``GET /terminal/<id>/download?generation=g``
  换图、重新登记或重置后旧链接（g 过期）一律 409 拒绝，提示刷新。

状态用两个单调计数器支撑轮询与隔离：
- ``generation`` 演练"代次"：上传新图、重新登记终端、重置都会 +1，
  用于 fencing——客户端持旧代次的迟到回复会被识别并丢弃，旧下载链接会被拒绝。
- ``revision``   当前代次内的内容版本：每发一轮 +1，供 ETag/条件请求去重。
"""
import hashlib
import io
import threading

from flask import (Flask, flash, jsonify, redirect, render_template,
                   request, send_file, url_for)

from planner import (CHUNK_SIZE, MAX_CHUNKS, MAX_FRAME_LIMIT, apply_round,
                     plan_round, reassemble, split_chunks)

app = Flask(__name__)
app.secret_key = "multicast-repair-drill-dev-key"

# 代次为模块级单调计数器，不放进 STATE：重置/换图时 STATE 被清空，
# 但代次仍需持续递增，旧链接与迟到回复才不会在新演练里"复活"。
GENERATION = 0
LOCK = threading.Lock()
MIN_TERMINALS = 2
MAX_TERMINALS = 4

# 监看页每次拉取轮次的硬上限：窄带线路一次最多 12 条。
PAGE_SIZE = 12


def _fresh_state():
    return {
        "image": None,
        "terminals": [],
        "rounds": [],
        # 代次内版本：每发一轮递增，支撑条件请求（304）去重。
        "revision": 0,
    }


STATE = _fresh_state()


def _next_generation():
    """开启新一代：内容全部重开，版本号归零。"""
    global GENERATION
    GENERATION += 1
    STATE["revision"] = 0


def _bump_revision():
    STATE["revision"] += 1


def _mask_of(indices):
    m = 0
    for c in indices:
        m |= 1 << c
    return m


def _missing_list(mask, n_chunks):
    """缺块号，1 基，供页面显示。"""
    return [c + 1 for c in range(n_chunks) if not (mask >> c) & 1]


def _owned_list(mask, n_chunks):
    return [c + 1 for c in range(n_chunks) if (mask >> c) & 1]


def _fmt_frame(fr):
    kind, a, b = fr
    if kind == "single":
        return f"单块 #{a + 1}"
    return f"异或 #{a + 1}⊕#{b + 1}"


def _terminal_view(t, n_chunks):
    return {
        "name": t["name"],
        "owned": _owned_list(t["mask"], n_chunks),
        "missing": _missing_list(t["mask"], n_chunks),
        "completed": t["completed"],
        "total_decodes": t["total_decodes"],
    }


def _view_state():
    img = STATE["image"]
    n = img["n_chunks"] if img else 0
    return {
        "image": img,
        "terminals": [_terminal_view(t, n) for t in STATE["terminals"]],
        "rounds": STATE["rounds"],
        "generation": GENERATION,
        "revision": STATE["revision"],
        "chunk_size": CHUNK_SIZE,
        "max_chunks": MAX_CHUNKS,
        "max_frame_limit": MAX_FRAME_LIMIT,
        "min_terminals": MIN_TERMINALS,
        "max_terminals": MAX_TERMINALS,
        "term_rows": range(MAX_TERMINALS),
    }


def _etag():
    # 合法 HTTP ETag 需带引号；Werkzeug 的 If-None-Match 解析后存裸值。
    return f'"g{GENERATION}-r{STATE["revision"]}"'


def _etag_bare():
    return f'g{GENERATION}-r{STATE["revision"]}'


def _check_generation(g):
    """客户端代次与当前代次不符即视为迟到/陈旧回复。返回是否一致。"""
    try:
        return g is not None and int(g) == GENERATION
    except (TypeError, ValueError):
        return False


# ---------- 操作页（发帧、上传、登记、重置；窄带拥塞不阻塞这些表单） ----------

@app.get("/")
def index():
    with LOCK:
        view = _view_state()
    return render_template("index.html", **view)


@app.post("/upload")
def upload():
    f = request.files.get("image")
    if not f or not f.filename:
        flash("未选择图片文件")
        return redirect(url_for("index"))
    data = f.read()
    try:
        chunks, size = split_chunks(data)
    except ValueError as e:
        flash(f"上传被拒：{e}")
        return redirect(url_for("index"))
    with LOCK:
        STATE.clear()
        STATE.update(_fresh_state())
        STATE["image"] = {
            "name": f.filename,
            "size": size,
            "sha256": hashlib.sha256(data).hexdigest(),
            "n_chunks": len(chunks),
            "chunks": chunks,
            "data": data,
        }
        _next_generation()
    flash(f"已载入新图：{f.filename}，{size} 字节，切为 {len(chunks)} 块。请登记终端首播实收块号。")
    return redirect(url_for("index"))


@app.post("/terminals")
def setup_terminals():
    with LOCK:
        img = STATE["image"]
        if not img:
            flash("请先上传新图")
            return redirect(url_for("index"))
        n = img["n_chunks"]
        terminals = []
        for i in range(MAX_TERMINALS):
            if not request.form.get(f"enable_{i}"):
                continue
            got = set()
            for v in request.form.getlist(f"recv_{i}"):
                c = int(v)
                if 1 <= c <= n:
                    got.add(c - 1)
            name = request.form.get(f"name_{i}", "").strip() or f"T{len(terminals) + 1}"
            terminals.append({
                "name": name,
                "mask": _mask_of(got),
                "completed": False,
                "total_decodes": 0,
                "old": f"旧图占位：终端 {name} 尚未收齐新图。\n".encode(),
                "ext": ".txt",
            })
        if not (MIN_TERMINALS <= len(terminals) <= MAX_TERMINALS):
            flash(f"启用的终端数须为 {MIN_TERMINALS}~{MAX_TERMINALS} 台，当前 {len(terminals)} 台")
            return redirect(url_for("index"))
        STATE["terminals"] = terminals
        STATE["rounds"] = []
        # 重新登记终端 = 新一轮演练代次，旧下载链接与迟到回复全部失效。
        _next_generation()
        # 首播即收齐的终端立即校验替换
        full = (1 << n) - 1
        for t in terminals:
            if t["mask"] == full:
                _try_finalize(t)
    flash(f"已登记 {len(terminals)} 台终端首播实收情况。")
    return redirect(url_for("index"))


def _try_finalize(t):
    """收齐后重组，SHA-256 与新图相符才替换可下载旧图。"""
    img = STATE["image"]
    data = reassemble(img["chunks"], img["size"])
    digest = hashlib.sha256(data).hexdigest()
    if digest == img["sha256"]:
        t["old"] = data
        t["ext"] = _guess_ext(img["name"])
        t["completed"] = True
        return True
    return False


def _guess_ext(filename):
    if "." in filename:
        return "." + filename.rsplit(".", 1)[1]
    return ".bin"


@app.post("/round")
def run_round():
    with LOCK:
        img = STATE["image"]
        terms = STATE["terminals"]
        if not img or not terms:
            flash("请先上传新图并登记终端")
            return redirect(url_for("index"))
        n = img["n_chunks"]
        try:
            frame_limit = int(request.form.get("frame_limit", ""))
        except ValueError:
            flash("帧数限额须为整数")
            return redirect(url_for("index"))
        if not (1 <= frame_limit <= MAX_FRAME_LIMIT):
            flash(f"帧数限额须在 1~{MAX_FRAME_LIMIT} 之间")
            return redirect(url_for("index"))
        quotas = []
        for i in range(len(terms)):
            try:
                q = int(request.form.get(f"quota_{i}", "0"))
            except ValueError:
                q = -1
            if not (0 <= q <= MAX_CHUNKS):
                flash(f"终端 {terms[i]['name']} 的解码额度须为 0~{MAX_CHUNKS} 的整数")
                return redirect(url_for("index"))
            quotas.append(q)
        fault = None
        ft = request.form.get("fault_terminal", "")
        ff = request.form.get("fault_frame", "").strip()
        if ft != "" and ff:
            try:
                fi, fn = int(ft), int(ff)
            except ValueError:
                fi, fn = -1, 0
            if 0 <= fi < len(terms) and fn >= 1:
                fault = (fi, fn)

        masks = [t["mask"] for t in terms]
        frames = plan_round(masks, quotas, frame_limit, n)
        if frames is None:
            flash("无方案：本轮限额内无法让各站收齐，已保留各站已收块，未发送任何帧。")
            return redirect(url_for("index"))
        if frames == []:
            flash("各站均已收齐，无需补传。")
            return redirect(url_for("index"))
        if fault and fault[1] > len(frames):
            fault = None  # 指定的漏收帧号超出实发帧数，忽略

        results = apply_round(masks, quotas, frames, fault)
        full = (1 << n) - 1
        rec_terminals = []
        for i, (t, r) in enumerate(zip(terms, results)):
            before = _missing_list(t["mask"], n)
            t["mask"] = r["mask"]
            t["total_decodes"] += r["decodes"]
            just_completed = False
            if t["mask"] == full and not t["completed"]:
                just_completed = _try_finalize(t)
            rec_terminals.append({
                "name": t["name"],
                "before_missing": before,
                "after_missing": _missing_list(t["mask"], n),
                "received": r["received"],
                "missed_frame": r["missed_frame"],
                "decoded": [(no, c + 1) for no, c in r["decoded"]],
                "decodes": r["decodes"],
                "quota": quotas[i],
                "completed": t["completed"],
                "just_completed": just_completed,
            })
        STATE["rounds"].append({
            "no": len(STATE["rounds"]) + 1,
            "frame_limit": frame_limit,
            "frames": [_fmt_frame(fr) for fr in frames],
            "fault": (terms[fault[0]]["name"], fault[1]) if fault else None,
            "terminals": rec_terminals,
        })
        # 同代次内新增一轮：推进 revision，监看条件请求随之解除 304。
        _bump_revision()
        done = sum(1 for t in terms if t["completed"])
        flash(f"第 {len(STATE['rounds'])} 轮已发送 {len(frames)} 帧；{done}/{len(terms)} 台已收齐并校验替换。")
    return redirect(url_for("index"))


@app.post("/reset")
def reset():
    with LOCK:
        STATE.clear()
        STATE.update(_fresh_state())
        # 重置即开启新代次：旧下载链接与监看页持有的代次全部失效。
        _next_generation()
    flash("演练已重置。")
    return redirect(url_for("index"))


# ---------- 下载：链接绑定代次，换图/重登记/重置后旧链接 409 ----------

@app.get("/terminal/<int:tid>/download")
def download(tid):
    generation = request.args.get("generation")
    with LOCK:
        if not _check_generation(generation):
            # 该站已换图或重新登记（或链接缺代次）：拒绝并提示刷新。
            return _stale_response()
        if not (0 <= tid < len(STATE["terminals"])):
            return _stale_response()
        t = STATE["terminals"][tid]
        payload = t["old"]
        name = f"{t['name']}_current{t['ext']}"
    # send_file 在锁外进行，慢客户端/窄带不会拖住发帧等其他页面操作。
    return send_file(io.BytesIO(payload), as_attachment=True, download_name=name)


def _stale_response():
    """陈旧链接统一回应：409 + 简短提示页，引导用户刷新监看页。"""
    body = (
        "<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\">"
        "<title>链接已失效</title>"
        "<body style=\"font-family:system-ui;max-width:640px;margin:3rem auto\">"
        "<h2>下载链接已失效</h2>"
        "<p>该终端已换图或重新登记，旧链接对应的内容不再有效。"
        "请刷新监看页后使用最新链接下载。</p>"
        "<p><a href=\"/monitor\">前往监看页</a></p></body>"
    )
    return body, 409, {"Content-Type": "text/html; charset=utf-8"}


@app.get("/image/download")
def download_new():
    with LOCK:
        img = STATE["image"]
        if not img:
            flash("尚未上传新图")
            return redirect(url_for("index"))
        data, name = img["data"], img["name"]
    return send_file(io.BytesIO(data), as_attachment=True, download_name=name)


# ---------- 值班员监看页（只读；窄带轮询、分页、fencing） ----------

@app.get("/monitor")
def monitor_page():
    return render_template("monitor.html", page_size=PAGE_SIZE)


@app.get("/api/state")
def api_state():
    """当前进度（不含轮次明细）。

    - 支持条件请求：``If-None-Match`` 命中当前 ETag 时回 304，空体，
      不重传任何进度或轮次。
    - 支持 fencing：客户端带 ``generation`` 且与当前代次不符时回 409，
      客户端据此丢弃迟到回复并重新装载。
    """
    with LOCK:
        # 先做代次 fencing：旧代次的迟到回复必须 409，不能被 304 短路放行。
        if not _check_generation(request.args.get("generation")):
            return jsonify({
                "error": "generation_mismatch",
                "generation": GENERATION,
            }), 409
        etag = _etag()
        inm = request.if_none_match
        # Werkzeug 解析后为去引号 ETag；通配 "*" 用 star_tag，contains 同时兼容两者。
        if inm and (inm.star_tag or inm.contains(_etag_bare())):
            resp = app.make_response(("", 304))
            resp.headers["ETag"] = etag
            return resp
        img = STATE["image"]
        n = img["n_chunks"] if img else 0
        payload = {
            "generation": GENERATION,
            "revision": STATE["revision"],
            "etag": etag,
            "image": (
                {"name": img["name"], "size": img["size"],
                 "sha256": img["sha256"], "n_chunks": n}
                if img else None
            ),
            "terminals": [_terminal_view(t, n) for t in STATE["terminals"]],
            "total_rounds": len(STATE["rounds"]),
        }
    resp = jsonify(payload)
    resp.headers["ETag"] = etag
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/api/rounds")
def api_rounds():
    """轮次分页。每次至多 12 条，只读取并切出所需下标，不扫描其他轮。

    参数：
    - ``generation``：fencing，过期回 409。
    - ``before``：只返回轮号 < before 的记录；缺省取最新一批。
    - ``limit``：本次条数，服务端夹在 1..12。
    """
    with LOCK:
        if not _check_generation(request.args.get("generation")):
            return jsonify({
                "error": "generation_mismatch",
                "generation": GENERATION,
            }), 409
        rounds = STATE["rounds"]
        total = len(rounds)
        try:
            limit = int(request.args.get("limit", PAGE_SIZE))
        except ValueError:
            limit = PAGE_SIZE
        limit = max(1, min(limit, PAGE_SIZE))
        before_raw = request.args.get("before")
        if before_raw is None:
            upper = total                      # 缺省：从最新一轮往前
        else:
            try:
                upper = min(int(before_raw), total)
            except ValueError:
                upper = total
        lower = max(0, upper - limit)
        # 轮次以 1 基轮号存储，列表下标 = 轮号-1；只切 [lower, upper) 这一段。
        page = [rounds[i] for i in range(lower, upper)]
        payload = {
            "generation": GENERATION,
            "revision": STATE["revision"],
            "total": total,
            "returned": len(page),
            "limit": limit,
            "next_before": lower if lower > 0 else None,
            "has_more": lower > 0,
            "rounds": page,
        }
    resp = jsonify(payload)
    resp.headers["Cache-Control"] = "no-store"
    return resp


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, threaded=True)
