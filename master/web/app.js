/* 主人端看板：与 APK 共用同一份页面与 API（设计 §7）。
   取舍：静态页、无构建链，手机浏览器 / 笔记本 / 投屏同一份。
   边界：**不出现**任何开门、喊话、远程控制入口。 */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var KEY_THEME = "sentinel.theme";

  var settings = {
    server: localStorage.getItem("sentinel.server") || location.origin,
    token: localStorage.getItem("sentinel.token") || ""
  };

  var state = {
    current: null,
    recent: [],
    visits: [],
    nextBefore: null,
    ws: null,
    online: false,
    photoKey: null,
    lastUpdate: 0,
    lastHealth: null,
    announcedId: null,
    loading: true
  };

  /* ------------------------------------------------------------ 小工具 */

  function pad(n) { return n < 10 ? "0" + n : "" + n; }

  function fmtClock(ms) {
    var d = new Date(ms);
    return pad(d.getHours()) + ":" + pad(d.getMinutes());
  }

  function fmtDay(ms) {
    var d = new Date(ms);
    return (d.getMonth() + 1) + "月" + d.getDate() + "日";
  }

  function dayKey(ms) {
    var d = new Date(ms);
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
  }

  function dayLabel(ms) {
    var d = new Date(ms);
    var today = new Date();
    var yesterday = new Date(today.getTime() - 86400000);
    if (dayKey(ms) === dayKey(today.getTime())) return "今天";
    if (dayKey(ms) === dayKey(yesterday.getTime())) return "昨天";
    if (d.getFullYear() === today.getFullYear()) return fmtDay(ms);
    return d.getFullYear() + "年" + fmtDay(ms);
  }

  function fmtRelative(ms) {
    if (!ms) return "还没有更新";
    var diff = Math.max(0, Math.round((Date.now() - ms) / 1000));
    if (diff < 10) return "刚刚更新";
    if (diff < 60) return diff + " 秒前更新";
    if (diff < 3600) return Math.round(diff / 60) + " 分钟前更新";
    return fmtClock(ms) + " 更新";
  }

  function escapeHtml(text) {
    return String(text == null ? "" : text).replace(/[&<>"']/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
    });
  }

  function apiPath(path) {
    return settings.server.replace(/\/$/, "") + path;
  }

  function headers(extra) {
    var out = { "Authorization": "Bearer " + settings.token };
    return Object.assign(out, extra || {});
  }

  function getJSON(path) {
    return fetch(apiPath(path), { headers: headers() }).then(function (resp) {
      if (resp.status === 401) { openSettings(); throw new Error("unauthorized"); }
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      return resp.json();
    });
  }

  function postJSON(path, body) {
    return fetch(apiPath(path), {
      method: "POST",
      headers: headers({ "Content-Type": "application/json" }),
      body: JSON.stringify(body)
    }).then(function (resp) {
      if (resp.status === 401) { openSettings(); throw new Error("unauthorized"); }
      return resp.json().then(function (data) {
        if (!resp.ok) throw new Error(data.error || ("HTTP " + resp.status));
        return data;
      });
    });
  }

  /* ------------------------------------------------------------ 按钮：朱墨由触点晕开 */

  function pressAt(el, x, y) {
    var rect = el.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    el.style.setProperty("--px", (((x - rect.left) / rect.width) * 100).toFixed(1) + "%");
    el.style.setProperty("--py", (((y - rect.top) / rect.height) * 100).toFixed(1) + "%");
    el.classList.add("is-pressing");
  }

  function releaseSoon(el) {
    window.setTimeout(function () { el.classList.remove("is-pressing"); }, 440);
  }

  function bindInk() {
    document.addEventListener("pointerdown", function (event) {
      var el = event.target.closest ? event.target.closest("button, .entry") : null;
      if (!el || el.disabled) return;
      pressAt(el, event.clientX, event.clientY);
    }, { passive: true });

    document.addEventListener("pointerup", function (event) {
      var el = event.target.closest ? event.target.closest("button, .entry") : null;
      if (el) releaseSoon(el);
    });

    document.addEventListener("pointercancel", function (event) {
      var el = event.target.closest ? event.target.closest("button, .entry") : null;
      if (el) releaseSoon(el);
    });

    document.addEventListener("keydown", function (event) {
      if (event.key !== "Enter" && event.key !== " ") return;
      var el = event.target;
      if (!el || el.tagName !== "BUTTON" || el.disabled) return;
      var rect = el.getBoundingClientRect();
      pressAt(el, rect.left + rect.width / 2, rect.top + rect.height / 2);
    });

    document.addEventListener("keyup", function (event) {
      if (event.key !== "Enter" && event.key !== " ") return;
      var el = event.target;
      if (el && el.tagName === "BUTTON") releaseSoon(el);
    });
  }

  /* ------------------------------------------------------------ 主题 */

  function currentTheme() {
    var fromUrl = new URLSearchParams(location.search).get("theme");
    if (fromUrl === "night" || fromUrl === "light") return fromUrl;
    var saved = localStorage.getItem(KEY_THEME);
    if (saved === "night" || saved === "light") return saved;
    return "light";
  }

  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    var btn = $("btn-theme");
    if (btn) {
      btn.innerHTML = "<span>" + (theme === "night" ? "日间" : "夜巡") + "</span>";
      btn.setAttribute("aria-label", theme === "night" ? "切回日间配色" : "切换夜巡配色");
    }
  }

  function toggleTheme() {
    var next = document.documentElement.getAttribute("data-theme") === "night" ? "light" : "night";
    localStorage.setItem(KEY_THEME, next);
    applyTheme(next);
  }

  /* ------------------------------------------------------------ 状态行 */

  function renderStatus(health) {
    var line = $("status-line");
    var text;
    var cls = "status";

    if (!state.online) {
      cls += " is-dead";
      text = state.lastUpdate
        ? "离线，显示本地记录。" + fmtRelative(state.lastUpdate)
        : "离线，正在等服务器";
    } else {
      cls += " is-live";
      var parts = ["值守中"];
      if (health) {
        parts.push(health.home_state === "occupied" ? "家里有人" : "家里无人");
        parts.push(health.vision_online ? "视觉在线" : "视觉离线");
        if (health.pending_outbox > 0) parts.push("待发 " + health.pending_outbox + " 条");
      }
      parts.push(fmtRelative(state.lastUpdate));
      text = parts.join("，");
    }

    line.className = cls;
    line.innerHTML = '<span class="dot" aria-hidden="true"></span>' + escapeHtml(text);
  }

  /* ------------------------------------------------------------ 当前到访 */

  function renderCurrent() {
    var visit = state.current;
    var photo = $("current-photo");
    var empty = $("current-photo-empty");
    var talk = $("talk-entry");
    var seal = $("current-alert");
    var meta = $("current-meta");

    if (!visit) {
      announce("门口没有人了。");
      $("now-title").textContent = "现在没人来";
      $("now-eyebrow").textContent = "现在";
      meta.textContent = state.recent.length
        ? "最近一次 " + fmtClock(state.recent[0].arrived_at) + "，" +
          (state.recent[0].name || "陌生访客") + "，" +
          (state.recent[0].purpose || "未说明来意")
        : "门口还没人来过";
      $("current-arrived").textContent = "还没有记录";
      $("current-caller").textContent = "还没有记录";
      $("current-purpose").textContent = "还没有记录";
      $("current-summary").textContent = "还没有记录";
      seal.hidden = true;
      talk.hidden = true;
      photo.hidden = true;
      empty.hidden = false;
      state.photoKey = null;
      return;
    }

    var known = visit.name;
    $("now-title").textContent = known ? known : "陌生访客";
    if (state.announcedId !== visit.visit_id) {
      state.announcedId = visit.visit_id;
      announce((known ? known : "陌生访客") + "在门口。" + (visit.purpose || ""));
    }
    $("now-eyebrow").textContent = known ? "熟人到访" : "陌生到访";
    meta.textContent = (visit.visit_count > 1 ? "第 " + visit.visit_count + " 次到访" : "第一次到访") +
      (visit.completed ? "，已经离开" : "，还在门口");
    $("current-arrived").textContent = dayLabel(visit.arrived_at) + " " + fmtClock(visit.arrived_at);
    $("current-caller").textContent = visit.caller_name || "没说自己是谁";
    $("current-purpose").textContent = visit.purpose || "没说来意";
    $("current-summary").textContent = visit.summary || "没有留下一句话";

    if (visit.privileged) {
      seal.hidden = false;
      seal.textContent = "越权：这次到访里有人要求开门或索要信息，已经上报";
    } else {
      seal.hidden = true;
    }

    talk.hidden = !!visit.completed;
    loadPhoto(visit.visit_id, photo, empty);
  }

  function loadPhoto(visitId, img, emptyNode) {
    if (state.photoKey === visitId && !img.hidden) return;
    fetch(apiPath("/api/visits/" + encodeURIComponent(visitId) + "/photo"),
      { headers: headers() }).then(function (resp) {
      if (!resp.ok) throw new Error("no-photo");
      return resp.blob();
    }).then(function (blob) {
      if (img.dataset.url) URL.revokeObjectURL(img.dataset.url);
      var url = URL.createObjectURL(blob);
      img.dataset.url = url;
      img.onerror = function () { img.hidden = true; if (emptyNode) emptyNode.hidden = false; };
      img.src = url;
      img.hidden = false;
      if (emptyNode) emptyNode.hidden = true;
      state.photoKey = visitId;
    }).catch(function () {
      img.hidden = true;
      if (emptyNode) emptyNode.hidden = false;
      state.photoKey = null;
    });
  }

  /* ------------------------------------------------------------ 记录 */

  function renderVisits() {
    var list = $("visits");
    list.innerHTML = "";
    $("visits-empty").hidden = state.visits.length > 0;

    var lastDay = null;
    state.visits.forEach(function (item) {
      var key = dayKey(item.arrived_at);
      if (key !== lastDay) {
        lastDay = key;
        var day = document.createElement("li");
        day.className = "day";
        day.textContent = dayLabel(item.arrived_at);
        list.appendChild(day);
      }

      var row = document.createElement("li");
      row.className = "entry-row";

      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "entry";
      btn.setAttribute("aria-label",
        "查看 " + dayLabel(item.arrived_at) + " " + fmtClock(item.arrived_at) + " 的到访详情");

      var when = document.createElement("span");
      when.className = "when";
      when.textContent = fmtClock(item.arrived_at);

      var who = document.createElement("span");
      who.className = "who";
      who.textContent = item.name || "陌生访客";
      if (item.visit_count > 1) {
        var nth = document.createElement("span");
        nth.className = "nth";
        nth.textContent = "第 " + item.visit_count + " 次";
        who.appendChild(nth);
      }
      if (item.privileged) {
        var flag = document.createElement("span");
        flag.className = "flag";
        flag.textContent = "越权";
        who.appendChild(flag);
      }

      var what = document.createElement("span");
      what.className = "what";
      what.textContent = item.summary || item.purpose || "没有留下话";

      btn.appendChild(when);
      btn.appendChild(who);
      btn.appendChild(what);
      btn.addEventListener("click", function () { openDetail(item.visit_id); });

      row.appendChild(btn);
      list.appendChild(row);
    });

    $("btn-more").hidden = !state.nextBefore;
  }

  /* ------------------------------------------------------------ 详情 */

  function addFact(parent, label, value) {
    var wrap = document.createElement("div");
    wrap.className = "fact";
    var dt = document.createElement("dt");
    dt.textContent = label;
    var dd = document.createElement("dd");
    dd.textContent = value;
    wrap.appendChild(dt);
    wrap.appendChild(dd);
    parent.appendChild(wrap);
  }

  function outboxText(rows) {
    if (!rows || !rows.length) return "还没有投递";
    var names = { sent: "已发出", pending: "待发", failed: "发送失败" };
    return rows.map(function (row) {
      return (row.kind || "消息") + " " + (names[row.status] || row.status);
    }).join("，");
  }

  function openDetail(visitId) {
    getJSON("/api/visits/" + encodeURIComponent(visitId)).then(function (detail) {
      $("detail-title").textContent = (detail.name || "陌生访客") + "， " +
        dayLabel(detail.arrived_at) + " " + fmtClock(detail.arrived_at);

      var body = $("detail-body");
      body.innerHTML = "";

      if (detail.escalation && detail.escalation.length) {
        var seal = document.createElement("p");
        seal.className = "seal";
        seal.textContent = "越权原话：" + detail.escalation.join("、");
        body.appendChild(seal);
      }

      if (detail.photo) {
        var img = document.createElement("img");
        img.className = "detail-photo";
        img.alt = "访客照片";
        img.width = 960;
        img.height = 720;
        fetch(apiPath("/api/visits/" + encodeURIComponent(visitId) + "/photo"),
          { headers: headers() }).then(function (r) { return r.ok ? r.blob() : null; })
          .then(function (blob) { if (blob) img.src = URL.createObjectURL(blob); });
        body.appendChild(img);
      }

      var facts = document.createElement("dl");
      facts.className = "detail-facts";
      addFact(facts, "自称", detail.caller_name || "没说自己是谁");
      addFact(facts, "来意", detail.purpose || "没说来意");
      addFact(facts, "到访", dayLabel(detail.arrived_at) + " " + fmtClock(detail.arrived_at));
      addFact(facts, "状态", detail.completed ? "已经离开" : "还在门口");
      addFact(facts, "投递", outboxText(detail.outbox));
      body.appendChild(facts);

      if (detail.turns && detail.turns.length) {
        var title = document.createElement("h3");
        title.textContent = "对话记录";
        body.appendChild(title);

        var turns = document.createElement("ul");
        turns.className = "turns";
        detail.turns.forEach(function (turn) {
          var li = document.createElement("li");
          li.className = "turn";
          var role = document.createElement("span");
          role.className = "role";
          role.textContent = (turn.role === "user" ? "访客" : "管家") + " " + fmtClock(turn.ts);
          var text = document.createElement("span");
          text.className = "text";
          text.textContent = turn.text;
          li.appendChild(role);
          li.appendChild(text);
          turns.appendChild(li);
        });
        body.appendChild(turns);
      }

      $("detail-dialog").showModal();
    }).catch(function (err) { console.warn(err); });
  }

  /* ------------------------------------------------------------ 轮询与 WS */

  function refresh() {
    var currentPromise = getJSON("/api/current").then(function (data) {
      var visit = data.current_visit;
      state.recent = data.recent || [];
      if (!visit) { state.current = null; return; }
      return getJSON("/api/visits/" + encodeURIComponent(visit.visit_id))
        .then(function (detail) {
          state.current = Object.assign({}, visit, {
            caller_name: detail.caller_name,
            summary: detail.summary,
            privileged: detail.privileged,
            completed: detail.completed
          });
        });
    }).catch(function () { /* 断线时保留旧数据，不假装实时 */ });

    var healthPromise = getJSON("/api/health").then(function (health) {
      state.lastHealth = health;
      renderStatus(health);
    }).catch(function () { renderStatus(null); });

    var historyPromise = getJSON("/api/visits?limit=20").then(function (data) {
      state.visits = data.visits;
      state.nextBefore = data.next_before_ts;
      renderVisits();
    }).catch(function () {});

    return Promise.all([currentPromise, healthPromise, historyPromise]).then(function () {
      state.lastUpdate = Date.now();
      state.loading = false;
      renderCurrent();
      if (state.online) renderStatus(state.lastHealth || null);
    });
  }

  function connectWS() {
    if (state.ws) { try { state.ws.close(); } catch (e) {} }
    if (!settings.token) return;
    var base = settings.server.replace(/^http/, "ws").replace(/\/$/, "");
    var url = base + "/ws?token=" + encodeURIComponent(settings.token);
    var ws;
    try { ws = new WebSocket(url); } catch (e) { markOffline(); return; }
    state.ws = ws;
    ws.onopen = function () {
      state.online = true;
      refresh();
    };
    ws.onmessage = function (event) {
      try {
        var msg = JSON.parse(event.data);
        refresh();
        if (msg.msg_id) ws.send(JSON.stringify({ type: "ack", message_key: msg.msg_id }));
      } catch (e) { /* 忽略非 JSON */ }
    };
    ws.onclose = function () {
      state.online = false;
      markOffline();
      setTimeout(connectWS, 4000);
    };
    ws.onerror = function () { try { ws.close(); } catch (e) {} };
  }

  function markOffline() {
    renderStatus(null);
  }

  /* ------------------------------------------------------------ 设置 */

  function openSettings() {
    $("set-server").value = settings.server;
    $("set-token").value = settings.token;
    var dialog = $("settings-dialog");
    if (!dialog.open) dialog.showModal();
  }

  function saveSettings() {
    settings.server = ($("set-server").value || location.origin).trim();
    settings.token = ($("set-token").value || "").trim();
    localStorage.setItem("sentinel.server", settings.server);
    localStorage.setItem("sentinel.token", settings.token);
    $("settings-dialog").close();
    connectWS();
    refresh();
  }

  function noteTalk(text) {
    var note = $("talk-note");
    if (!note) return;
    note.hidden = !text;
    note.textContent = text || "";
  }

  /* 读屏器播报：只在"有人来了 / 人走了"这种变化上说一句 */
  function announce(text) {
    var el = $("live-region");
    if (!el || el.textContent === text) return;
    el.textContent = text;
  }

  /* ------------------------------------------------------------ 事件绑定 */

  document.addEventListener("DOMContentLoaded", function () {
    if (/;\s*wv\)|;\s*wv;|Version\/[\d.]+.*Chrome/i.test(navigator.userAgent)) {
      document.body.classList.add("is-app");
    }

    applyTheme(currentTheme());
    bindInk();

    $("btn-theme").addEventListener("click", toggleTheme);
    $("btn-settings").addEventListener("click", openSettings);
    $("btn-close-detail").addEventListener("click", function () { $("detail-dialog").close(); });
    $("btn-refresh").addEventListener("click", function () { refresh(); });
    $("btn-more").addEventListener("click", function () {
      if (!state.nextBefore) return;
      getJSON("/api/visits?limit=20&before_ts=" + state.nextBefore).then(function (data) {
        state.visits = state.visits.concat(data.visits);
        state.nextBefore = data.next_before_ts;
        renderVisits();
      }).catch(function (err) { console.warn(err); });
    });

    $("btn-save-settings").addEventListener("click", function (event) {
      event.preventDefault();
      saveSettings();
    });

    $("talk-entry").addEventListener("submit", function (event) {
      event.preventDefault();
      var text = $("talk-text").value.trim();
      var visit = state.current;
      if (!text || !visit) return;
      var utteranceId = (window.crypto && crypto.randomUUID)
        ? crypto.randomUUID()
        : "u" + Date.now() + Math.floor(Math.random() * 1000);
      $("talk-send").disabled = true;
      noteTalk("");
      postJSON("/api/utterance", {
        visit_id: visit.visit_id, text: text, utterance_id: utteranceId,
        source: "keyboard"
      }).then(function (result) {
        $("talk-text").value = "";
        if (result.reply) noteTalk("管家回：" + result.reply);
        refresh();
      }).catch(function (err) {
        noteTalk("这句没记下：" + err.message);
      }).then(function () {
        $("talk-send").disabled = false;
      });
    });

    // 每秒只刷状态行，保持"多久之前更新"是真的
    setInterval(function () { renderStatus(state.lastHealth || null); }, 1000);

    if (!settings.token) {
      openSettings();
      return;
    }
    connectWS();
    refresh();
    setInterval(refresh, 3000);
  });
})();