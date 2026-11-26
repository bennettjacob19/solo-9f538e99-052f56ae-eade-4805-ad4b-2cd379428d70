/*
 * 窄带监看页轮询控制器（无框架，浏览器全局变量）。
 *
 * 约束（对应演练需求）：
 * - 连接正常、内容在变化：每 activeIntervalMs（2s）轮询一次，
 *   保证发完一轮后两秒内看到实发结果与各站进度。
 * - 连续 idleWindowMs（60s）无变化：才进入空闲态，节奏拉长到 idleIntervalMs（10s），
 *   并用滑动窗口硬性限制每个 60s 窗口最多 idleWindowMaxRequests（6）次自动请求。
 * - 进入空闲前的 60s 仍按 2s 活跃节奏轮询（用于确认"连续无变化"）；
 *   一旦在空闲态收到新版本，立即退出空闲、恢复 2s 节奏并重置窗口。
 * - "无变化"由服务端内容版本（revision/etag）判定；服务端另支持 If-None-Match
 *   回 304 的条件请求通道（空体、不重传进度/轮次），空闲复轮时使用。
 * - 时钟（setTimeout/Date）可注入，便于用 Node 做确定性单测。
 *
 * 两阶段模型：
 *   活跃态 ACTIVE：每 2s 一次；记录"连续无变化起点" _unchangedSince。
 *   空闲态 IDLE  ：当 ACTIVE 下无变化累计满 60s 时切入；每 10s 一次，受 6 次/60s 窗口约束。
 */
(function (root) {
  "use strict";

  var ACTIVE = "active";
  var IDLE = "idle";

  function DrillPoller(opts) {
    this.activeIntervalMs = opts.activeIntervalMs || 2000;
    this.idleIntervalMs = opts.idleIntervalMs || 10000;
    this.idleWindowMs = opts.idleWindowMs || 60000;
    this.idleWindowMaxRequests = opts.idleWindowMaxRequests || 6;
    this._request = opts.request;
    this._onState = opts.onState || function () {};
    this._onNotModified = opts.onNotModified || function () {};
    this._onError = opts.onError || function () {};
    this._onIdleCount = opts.onIdleCount || function () {};
    this._onModeChange = opts.onModeChange || function () {};

    this._clock = opts.clock || {
      setTimeout: function (fn, ms) { return root.setTimeout(fn, ms); },
      clearTimeout: function (id) { root.clearTimeout(id); },
      now: function () { return Date.now(); }
    };

    this._timer = null;
    this._paused = false;
    this._running = false;
    this._inFlight = false;
    this._mode = ACTIVE;
    this._lastEtag = null;
    this._lastVersion = null;      // 最近一次已应用的内容版本（etag/revision）
    this._unchangedSince = null;   // 连续无变化的起点（活跃态累计用）
    this._windowStart = null;      // 空闲态当前 60s 窗口起点
    this._windowRequests = [];     // 空闲窗口内各次请求的时间戳
  }

  DrillPoller.prototype._isIdle = function () { return this._mode === IDLE; };
  DrillPoller.prototype.isIdle = function () { return this._mode === IDLE; };
  DrillPoller.prototype.isPaused = function () { return this._paused; };

  DrillPoller.prototype.start = function () {
    if (this._running) return;
    this._running = true;
    this._schedule(0);              // 首次立即拉取
  };

  DrillPoller.prototype.pause = function () {
    this._paused = true;
    if (this._timer !== null) { this._clock.clearTimeout(this._timer); this._timer = null; }
  };

  DrillPoller.prototype.resume = function () {
    if (!this._running || !this._paused) return;
    this._paused = false;
    this._schedule(0);              // 回来先看最新进度
  };

  DrillPoller.prototype._schedule = function (delayMs) {
    var self = this;
    if (!this._running || this._paused) return;
    if (this._timer !== null) this._clock.clearTimeout(this._timer);
    this._timer = this._clock.setTimeout(function () { self._tick(); }, delayMs);
  };

  DrillPoller.prototype._setMode = function (mode, now) {
    if (this._mode === mode) return;
    this._mode = mode;
    if (mode === IDLE) {
      this._windowStart = now;
      this._windowRequests = [];
      this._onIdleCount(0);
    } else {
      this._windowStart = null;
      this._windowRequests = [];
      this._onIdleCount(0);
    }
    this._onModeChange(mode);
  };

  /* 空闲滑动窗口：丢弃 60s 之外的请求时间，返回窗口内请求数。*/
  DrillPoller.prototype._pruneWindow = function (now) {
    while (this._windowRequests.length &&
           now - this._windowRequests[0] >= this.idleWindowMs) {
      this._windowRequests.shift();
    }
    return this._windowRequests.length;
  };

  /* 空闲态下本次是否允许发请求；不允许则返回应延后的毫秒数。*/
  DrillPoller.prototype._idleGateDelay = function (now) {
    if (!this._isIdle()) return 0;
    var count = this._pruneWindow(now);
    if (count >= this.idleWindowMaxRequests) {
      var oldest = this._windowRequests[0];
      return Math.max(0, oldest + this.idleWindowMs - now);
    }
    return 0;
  };

  DrillPoller.prototype._recordIdleRequest = function (now) {
    if (!this._isIdle()) return;
    this._windowRequests.push(now);
    this._onIdleCount(this._pruneWindow(now));
  };

  DrillPoller.prototype._tick = function () {
    if (!this._running || this._paused || this._inFlight) {
      // 上一次还没回来（慢/断线）：按当前节奏稍后再试，不叠加请求。
      this._schedule(this._isIdle() ? this.idleIntervalMs : this.activeIntervalMs);
      return;
    }
    var now = this._clock.now();
    var gate = this._idleGateDelay(now);
    if (gate > 0) { this._schedule(gate); return; }
    this._recordIdleRequest(now);
    this._inFlight = true;
    var self = this;
    // 仅空闲复轮时才发条件请求；活跃期要完整 JSON 以比较 revision。
    var conditionalEtag = this._isIdle() ? this._lastEtag : null;
    this._request(conditionalEtag, function (outcome) {
      self._inFlight = false;
      if (!self._running || self._paused) return;
      var at = self._clock.now();
      if (outcome && outcome.stale) {
        // 代次不符（409）：页面层负责整体重装载；轮询回到活跃态。
        self._setMode(ACTIVE, at);
        self._unchangedSince = null;
        self._schedule(self.activeIntervalMs);
        return;
      }
      if (outcome && outcome.error) {
        self._onError();
        self._schedule(self._isIdle() ? self.idleIntervalMs : self.activeIntervalMs);
        return;
      }
      if (outcome && outcome.notModified) {
        // 条件请求 304：确认无变化。标记无变化起点并据此决定是否进入空闲。
        self._noteUnchanged(at);
        self._onNotModified();
        self._schedule(self._nextDelay(at));
        return;
      }
      var data = outcome && outcome.data;
      var etag = data && data.etag;
      var version = etag != null ? etag : (data && data.revision);
      var hadVersion = self._lastVersion !== null;
      var changed = hadVersion && version !== self._lastVersion;
      self._lastEtag = etag || self._lastEtag;
      self._lastVersion = version != null ? version : self._lastVersion;
      if (changed) {
        // 内容有变化：退出空闲、重新累计无变化时间。
        self._setMode(ACTIVE, at);
        self._unchangedSince = at;
      } else {
        self._noteUnchanged(at);
      }
      self._onState(data, outcome && outcome.seq);
      self._schedule(self._nextDelay(at));
    });
  };

  /* 记录一次"无变化"：首次记录起点，之后保持；满 60s 才切入空闲。*/
  DrillPoller.prototype._noteUnchanged = function (now) {
    if (this._unchangedSince === null) this._unchangedSince = now;
    if (!this._isIdle() && now - this._unchangedSince >= this.idleWindowMs) {
      this._setMode(IDLE, now);
    }
  };

  /* 根据当前模式与无变化时长决定下一次延迟。*/
  DrillPoller.prototype._nextDelay = function (now) {
    if (this._isIdle()) return this.idleIntervalMs;
    return this.activeIntervalMs;
  };

  /* 仅供测试：当前空闲窗口内已发请求数。*/
  DrillPoller.prototype.idleWindowCount = function () {
    return this._pruneWindow(this._clock.now());
  };

  DrillPoller.ACTIVE = ACTIVE;
  DrillPoller.IDLE = IDLE;
  root.DrillPoller = DrillPoller;
})(typeof window !== "undefined" ? window : globalThis);
