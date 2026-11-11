/* 主人端看板：与 APK 共用同一份页面与 API（设计 §7）。
   取舍：静态页、无构建链，手机浏览器 / 笔记本 / 投屏同一份。
   边界：**不出现**任何开门、喊话、远程控制入口。 */
(function () {
  "use strict";

  var settings = {
    server: localStorage.getItem("sentinel.server") || location.origin,
    token: localStorage.getItem("sentinel.token") || ""
  };

  var state = {
    current: null,
    visits: [],
    nextBefore: null,
    ws: null,
    online: false,
    photoKey: null,
    photoUrl: null,
    pollTimer: null
  };

  var $ = function (id) { return document.getElementById(id); };

  function fmtTime(ms) {
    if (!ms) return "—";
    var d = new Date(ms);
    var pad = function (n) { return n < 10 ? "0" + n : "" + n; };
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) +
      " " + pad(d.getHours()) + ":" + pad(d.getMinutes());
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

  /* ------------------------------------------------------------ 当前到访 */

  function renderCurrent() {
    var visit = state.current;
    var card = $("current-card");
    var photo = $("current-photo");
    var empty = $("current-photo-empty");
    var talk = $("talk-entry");

    if (!visit) {
      $("current-name").textContent = "暂无来访";
      $("current-caller").textContent = "—";
      $("current-purpose").textContent = "—";
      $("current-summary").textContent = "—";
      $("current-arrived").textContent = "—";
      $("current-meta").textContent = "";
      $("current-alert").hidden = true;
      talk.hidden = true;
      photo.hidden = true;
      empty.hidden = false;
      card.classList.remove("has-visit");
      state.photoKey = null;
      return;
    }

    $("current-name").textContent = visit.name ? (visit.name + "（熟人）") : "陌生访客";
    $("current-caller").textContent = visit.caller_name || "—";
    $("current-purpose").textContent = visit.purpose || "未说明来意";
    $("current-summary").textContent = visit.summary || "—";
    $("current-arrived").textContent = fmtTime(visit.arrived_at);
    $("current-meta").textContent = "第 " + (visit.visit_count || 1) + " 次到访";
    talk.hidden = false;
    $("current-photo").hidden = false;
    empty.hidden = true;

    if (visit.privileged) {
      $("current-alert").hidden = false;
      $("current-alert").textContent = "越权告警：这次到访里有要求开门/索要信息的请求，已即时上报。";
    } else {
      $("current-alert").hidden = true;
    }
    loadPhoto(visit.visit_id, "current-photo");
  }

  function loadPhoto(visitId, targetId) {
    var key = visitId;
    if (state.photoKey === key && targetId === "current-photo") return;
    fetch(apiPath("/api/visits/" + encodeURIComponent(visitId) + "/photo"),
      { headers: headers() }).then(function (resp) {
      if (!resp.ok) throw new Error("no-photo");
      return resp.blob();
    }).then(function (blob) {
      var img = $(targetId);
      if (!img) return;
      if (img.dataset.url) URL.revokeObjectURL(img.dataset.url);
      img.onerror = function () {
        img.hidden = true;
        if (targetId === "current-photo") {
          $("current-photo-empty").hidden = false;
          $("current-photo-empty").textContent = "缺图 · 采集中";
        }
      };
      var url = URL.createObjectURL(blob);
      img.dataset.url = url;
      img.src = url;
      img.hidden = false;
      if (targetId === "current-photo") $("current-photo-empty").hidden = true;
      state.photoKey = key;
    }).catch(function () {
      var img = $(targetId);
      if (img) { img.hidden = true; }
      if (targetId === "current-photo") {
        $("current-photo-empty").hidden = false;
        $("current-photo-empty").textContent = "缺图 · 采集中";
      }
    });
  }

  /* ------------------------------------------------------------ 状态条 */

  function renderHealth(health) {
    var home = health.home_state === "occupied" ? "家中有人" : "家中无人";
    $("chip-home").textContent = home;
    $("chip-home").className = "chip " + (health.home_state === "occupied" ? "on" : "off");
    $("chip-vision").textContent = "视觉" + (health.vision_online ? "在线" : "离线");
    $("chip-vision").className = "chip " + (health.vision_online ? "on" : "off");
    $("chip-reception").textContent = weightText(health.pending_outbox);
    $("chip-reception").className = "chip " + (health.pending_outbox > 0 ? "busy" : "off");
    var d = new Date();
    var pad = function (n) { return n < 10 ? "0" + n : "" + n; };
    $("chip-time").textContent = pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
  }

  function weightText(pending) {
    if (!pending) return "待发队列 0";
    return "待发 " + pending + " 条";
  }

  /* ------------------------------------------------------------ 历史 */

  function renderVisits() {
    var list = $("visits");
    list.innerHTML = "";
    state.visits.forEach(function (item) {
      var li = document.createElement("li");
      li.innerHTML =
        '<span class="when">' + fmtTime(item.arrived_at) + '</span>' +
        '<span class="who">' + (item.name ? escapeHtml(item.name) : "陌生访客") +
        (item.visit_count ? '<span class="muted small"> · 第 ' + item.visit_count + ' 次</span>' : "") + '</span>' +
        '<span class="what">' + escapeHtml(item.summary || item.purpose || "未说明来意") + '</span>' +
        (item.privileged ? '<span class="flag">越权</span>' : "");
      li.addEventListener("click", function () { openDetail(item.visit_id); });
      list.appendChild(li);
    });
    $("btn-more").hidden = !state.nextBefore;
  }

  function escapeHtml(text) {
    return String(text == null ? "" : text).replace(/[&<>"']/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
    });
  }

  /* ------------------------------------------------------------ 详情 */

  function openDetail(visitId) {
    getJSON("/api/visits/" + encodeURIComponent(visitId)).then(function (detail) {
      $("detail-title").textContent = (detail.name || "陌生访客") + " · " +
        fmtTime(detail.arrived_at);
      var body = $("detail-body");
      body.innerHTML = "";
      if (detail.escalation && detail.escalation.length) {
        var alert = document.createElement("div");
        alert.className = "alert-strip";
        alert.textContent = "越权原话：" + detail.escalation.join("、") +
          (detail.turns.length ? "（系统回应见下方对话）" : "");
        body.appendChild(alert);
      }
      if (detail.photo) {
        var img = document.createElement("img");
        img.className = "detail-photo";
        img.alt = "访客照片";
        fetch(apiPath("/api/visits/" + encodeURIComponent(visitId) + "/photo"),
          { headers: headers() }).then(function (r) { return r.ok ? r.blob() : null; })
          .then(function (blob) { if (blob) img.src = URL.createObjectURL(blob); });
        body.appendChild(img);
      }
      var meta = document.createElement("p");
      meta.className = "muted";
      meta.textContent = "来意：" + (detail.purpose || "未说明") +
        " · 自称：" + (detail.caller_name || "—") +
        " · 完成：" + (detail.completed ? "是" : "否") +
        " · 投递：" + detail.outbox.map(function (row) {
          return row.kind + "=" + row.status;
        }).join(" / ");
      body.appendChild(meta);

      detail.turns.forEach(function (turn) {
        var div = document.createElement("div");
        div.className = "turn " + (turn.role === "user" ? "user" : "assistant");
        div.innerHTML = '<span class="role">' + (turn.role === "user" ? "访客" : "管家") +
          " · " + fmtTime(turn.ts) + '</span>' + escapeHtml(turn.text);
        body.appendChild(div);
      });
      $("detail-dialog").showModal();
    }).catch(function (err) { console.warn(err); });
  }

  /* ------------------------------------------------------------ 轮询与 WS */

  function refresh() {
    var currentPromise = getJSON("/api/current").then(function (data) {
      var visit = data.current_visit;
      if (visit) {
        return getJSON("/api/visits/" + encodeURIComponent(visit.visit_id))
          .then(function (detail) {
            state.current = Object.assign({}, visit, {
              caller_name: detail.caller_name,
              summary: detail.summary,
              privileged: detail.privileged
            });
          });
      }
      state.current = null;
    }).catch(function () { /* 断线时保留旧数据，不假装实时 */ });

    var healthPromise = getJSON("/api/health").then(renderHealth).catch(function () {});

    var historyPromise = getJSON("/api/visits?limit=20").then(function (data) {
      state.visits = data.visits;
      state.nextBefore = data.next_before_ts;
      renderVisits();
    }).catch(function () {});

    return Promise.all([currentPromise, healthPromise, historyPromise])
      .then(renderCurrent);
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
      $("conn").textContent = "在线（局域网直连）";
      $("conn").className = "conn live";
      refresh();
    };
    ws.onmessage = function (event) {
      try {
        var msg = JSON.parse(event.data);
        if (msg.type === "security_alert") {
          refresh();
          return;
        }
        refresh();
        if (msg.msg_id) {
          ws.send(JSON.stringify({ type: "ack", message_key: msg.msg_id }));
        }
      } catch (e) { /* 忽略非 JSON */ }
    };
    ws.onclose = function () {
      state.online = false;
      markOffline();
      setTimeout(connectWS, 4000);   // 断线重连；页面明确显示"离线"
    };
    ws.onerror = function () { try { ws.close(); } catch (e) {} };
  }

  function markOffline() {
    $("conn").textContent = "离线，数据来自本地库";
    $("conn").className = "conn dead";
  }

  /* ------------------------------------------------------------ 设置 */

  function openSettings() {
    $("set-server").value = settings.server;
    $("set-token").value = settings.token;
    $("settings-dialog").showModal();
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

  /* ------------------------------------------------------------ 事件绑定 */

  document.addEventListener("DOMContentLoaded", function () {
    $("btn-settings").addEventListener("click", openSettings);
    $("btn-save-settings").addEventListener("click", function (event) {
      event.preventDefault();
      saveSettings();
    });
    $("btn-refresh").addEventListener("click", refresh);
    $("btn-close-detail").addEventListener("click", function () {
      $("detail-dialog").close();
    });
    $("btn-more").addEventListener("click", function () {
      if (!state.nextBefore) return;
      getJSON("/api/visits?limit=20&before_ts=" + state.nextBefore).then(function (data) {
        state.visits = state.visits.concat(data.visits);
        state.nextBefore = data.next_before_ts;
        renderVisits();
      });
    });
    $("talk-send").addEventListener("click", function () {
      var text = $("talk-text").value.trim();
      var visit = state.current;
      if (!text || !visit) return;
      var utteranceId = (window.crypto && crypto.randomUUID)
        ? crypto.randomUUID()
        : "u" + Date.now() + Math.floor(Math.random() * 1000);
      postJSON("/api/utterance", {
        visit_id: visit.visit_id, text: text, utterance_id: utteranceId,
        source: "keyboard"
      }).then(function (result) {
        $("talk-text").value = "";
        if (result.reply) {
          $("current-summary").textContent = result.reply;
        }
        refresh();
      }).catch(function (err) { alert(err.message); });
    });

    if (!settings.token) {
      openSettings();
      return;
    }
    connectWS();
    refresh();
    state.pollTimer = setInterval(refresh, 3000);
  });
})();