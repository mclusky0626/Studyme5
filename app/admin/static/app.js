/* Memory Console — vanilla JS (빌드 도구 없음) */
(() => {
  "use strict";

  // ---------------------------------------------------------------- utils
  const $ = (sel, el = document) => el.querySelector(sel);
  const $$ = (sel, el = document) => Array.from(el.querySelectorAll(sel));
  const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ESC[c]);
  const enc = encodeURIComponent;
  const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  const store = {
    get(k, d = null) { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
  };

  function debounce(fn, ms) {
    let t;
    return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
  }

  function parseDate(v) {
    if (!v) return null;
    const d = new Date(v);
    return isNaN(d) ? null : d;
  }
  function fmtDate(v) {
    const d = parseDate(v);
    return d ? d.toLocaleString("ko-KR", { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "-";
  }
  function fmtDay(v) {
    const d = parseDate(v);
    return d ? d.toLocaleDateString("ko-KR", { year: "numeric", month: "short", day: "numeric" }) : "?";
  }
  function ago(v) {
    const d = parseDate(v);
    if (!d) return "-";
    const s = (Date.now() - d.getTime()) / 1000;
    if (s < 60) return "방금 전";
    if (s < 3600) return `${Math.floor(s / 60)}분 전`;
    if (s < 86400) return `${Math.floor(s / 3600)}시간 전`;
    if (s < 86400 * 30) return `${Math.floor(s / 86400)}일 전`;
    return fmtDay(v);
  }
  function hashHue(str) {
    let h = 0;
    for (const ch of String(str)) h = (h * 31 + ch.codePointAt(0)) >>> 0;
    return h % 360;
  }

  async function api(path, opts = {}) {
    const res = await fetch(path, {
      ...opts,
      headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    });
    if (!res.ok) {
      let msg = `${res.status} ${res.statusText}`;
      try { const j = await res.json(); if (j.detail) msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch { /* ignore */ }
      throw new Error(msg);
    }
    return res.json();
  }

  function toast(msg, kind = "info") {
    const el = document.createElement("div");
    el.className = `toast ${kind}`;
    el.textContent = msg;
    $("#toasts").appendChild(el);
    setTimeout(() => el.remove(), kind === "error" ? 6000 : 3200);
  }

  // ---------------------------------------------------------------- modal
  function modal({ title, html = "", okText = "확인", danger = false, input = null }) {
    return new Promise((resolve) => {
      const m = $("#modal");
      $("#modalTitle").textContent = title;
      const body = $("#modalBody");
      body.innerHTML = html;
      let field = null;
      if (input) {
        field = document.createElement(input.multiline ? "textarea" : "input");
        field.className = "input";
        field.placeholder = input.placeholder || "";
        field.value = input.value || "";
        if (input.multiline) field.rows = 5;
        body.appendChild(field);
      }
      const ok = $("#modalOk");
      ok.textContent = okText;
      ok.className = `btn ${danger ? "danger" : "primary"}`;
      const check = () => { ok.disabled = !!(input && input.mustEqual !== undefined && field.value.trim() !== input.mustEqual); };
      if (field) { field.addEventListener("input", check); check(); } else ok.disabled = false;
      m.classList.remove("hidden");
      setTimeout(() => (field || ok).focus(), 30);
      const close = (val) => {
        m.classList.add("hidden");
        ok.onclick = null; $("#modalCancel").onclick = null; m.onclick = null; document.onkeydown = null;
        resolve(val);
      };
      ok.onclick = () => close(field ? field.value : true);
      $("#modalCancel").onclick = () => close(null);
      m.onclick = (e) => { if (e.target === m) close(null); };
      document.onkeydown = (e) => {
        if (e.key === "Escape") close(null);
        if (e.key === "Enter" && field && !input.multiline && !ok.disabled) close(field.value);
      };
    });
  }

  // ---------------------------------------------------------------- state
  const state = {
    spaces: [],
    gid: null,
    tab: "graph",
    status: null,
    network: null,
    graphData: null,
    facts: { skip: 0, items: [] },
    episodes: { skip: 0, items: [] },
    meta: {},
  };
  const KIND_LABEL = { guild: "서버", dm: "DM", other: "기타" };
  const currentSpace = () => state.spaces.find((s) => s.group_id === state.gid);

  // ---------------------------------------------------------------- theme
  const THEMES = ["system", "dark", "light"];
  function applyTheme(t) {
    if (t === "system") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", t);
    $("#themeBtn").title = `테마: ${{ system: "시스템", dark: "다크", light: "라이트" }[t]}`;
  }
  applyTheme(store.get("theme", "system"));
  $("#themeBtn").addEventListener("click", () => {
    const cur = store.get("theme", "system");
    const next = THEMES[(THEMES.indexOf(cur) + 1) % THEMES.length];
    store.set("theme", next);
    applyTheme(next);
    if (state.graphData && state.tab === "graph") renderGraph(state.graphData);
  });
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", () => {
    if (state.graphData && state.tab === "graph") renderGraph(state.graphData);
  });

  // ---------------------------------------------------------------- status
  async function loadStatus() {
    try {
      const st = await api("/api/status");
      state.status = st;
      const pill = $("#botStatus");
      const b = st.bot;
      pill.classList.toggle("online", st.online);
      pill.classList.toggle("offline", !st.online);
      $(".label", pill).textContent = st.online
        ? `${b.bot || "봇"} · 대기 ${b.buffered_messages}마디 · 저장 중 ${b.pending_saves}`
        : "봇 오프라인";
      pill.title = st.online ? `모델 ${b.chat_model} / 임베딩 ${b.embedding_model}\n마지막 보고 ${fmtDate(b.updated_at)}` : "최근 90초 동안 봇 상태 보고가 없습니다";
      $("#botStats").innerHTML = st.online
        ? [["누적 저장", b.saved], ["실패", b.failed], ["저장 중", b.pending_saves], ["버퍼", `${b.buffered_messages}마디`]]
            .map(([k, v]) => `<div class="mini-stat"><b>${esc(v)}</b><span>${esc(k)}</span></div>`).join("")
        : `<div class="mini-stat" style="grid-column: span 2"><b>오프라인</b><span>봇 컨테이너 로그를 확인하세요</span></div>`;
      if (state.tab === "activity") renderActivity();
    } catch (e) {
      $(".label", $("#botStatus")).textContent = "상태 확인 실패";
    }
  }

  // ---------------------------------------------------------------- spaces
  async function loadSpaces() {
    try {
      state.spaces = await api("/api/spaces");
    } catch (e) {
      $("#spaceList").innerHTML = `<div class="empty small">불러오기 실패: ${esc(e.message)}</div>`;
      return;
    }
    renderSpaces();
    if (state.gid) {
      if (currentSpace()) renderSpaceHeader();
      else { state.gid = null; showWelcome(); }
    }
  }

  function renderSpaces() {
    const q = $("#spaceFilter").value.trim().toLowerCase();
    const list = state.spaces.filter((s) => !q || s.name.toLowerCase().includes(q) || s.group_id.includes(q));
    $("#spaceCount").textContent = state.spaces.length;
    if (!list.length) {
      $("#spaceList").innerHTML = `<div class="empty small">${state.spaces.length ? "검색 결과가 없어요" : "아직 저장된 기억이 없어요.<br/>봇과 대화를 나눠 보세요!"}</div>`;
      return;
    }
    $("#spaceList").innerHTML = list.map((s) => {
      const hue = hashHue(s.group_id);
      const initial = (s.name.replace(/^DM · /, "")[0] || "?").toUpperCase();
      return `<button class="space-item ${s.group_id === state.gid ? "active" : ""}" data-gid="${esc(s.group_id)}">
        <span class="space-avatar" style="background: hsl(${hue} 62% 52%)">${esc(initial)}</span>
        <span class="space-meta">
          <span class="space-name">${esc(s.name)}</span>
          <span class="space-sub"><span>${esc(KIND_LABEL[s.kind] || s.kind)}</span><span>엔티티 ${s.entities}</span><span>사실 ${s.facts}</span></span>
        </span>
      </button>`;
    }).join("");
  }

  function showWelcome() {
    $("#settingsView").classList.add("hidden");
    $("#welcome").classList.remove("hidden");
    $("#spaceView").classList.add("hidden");
    closeDrawer();
  }

  function renderSpaceHeader() {
    const s = currentSpace();
    if (!s) return;
    $("#spaceName").textContent = s.name;
    $("#spaceGid").textContent = s.group_id;
    const kind = $("#spaceKind");
    kind.textContent = KIND_LABEL[s.kind] || s.kind;
    kind.className = `kind-badge ${s.kind}`;
    $("#spaceStats").innerHTML = [
      ["엔티티", s.entities], ["사실", s.facts], ["에피소드", s.episodes],
    ].map(([k, v]) => `<span class="stat-chip"><b>${v}</b>${k}</span>`).join("") +
      `<span class="stat-chip">최근 저장 <b style="font-size:13px">${esc(ago(s.last_episode_at))}</b></span>`;
  }

  function selectSpace(gid, tab) {
    state.gid = gid;
    state.tab = tab || state.tab || "graph";
    $("#welcome").classList.add("hidden");
    $("#settingsView").classList.add("hidden");
    $("#spaceView").classList.remove("hidden");
    closeDrawer();
    closeSidebar();
    renderSpaces();
    renderSpaceHeader();
    switchTab(state.tab);
  }

  function switchTab(tab) {
    state.tab = tab;
    $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === tab));
    $$(".tab-panel").forEach((p) => p.classList.toggle("hidden", p.dataset.panel !== tab));
    history.replaceState(null, "", `#${enc(state.gid)}/${tab}`);
    if (tab === "graph") loadGraph();
    if (tab === "facts") loadFacts(true);
    if (tab === "episodes") loadEpisodes(true);
    if (tab === "activity") renderActivity();
    if (tab === "search") setTimeout(() => $("#searchQuery").focus(), 30);
  }

  // ---------------------------------------------------------------- graph
  async function loadGraph() {
    if (!state.gid) return;
    const limit = $("#graphLimit").value;
    const eps = $("#graphEpisodes").checked;
    $("#graphInfo").textContent = "불러오는 중…";
    try {
      const data = await api(`/api/spaces/${enc(state.gid)}/graph?limit=${limit}&episodes=${eps}`);
      state.graphData = data;
      renderGraph(data);
    } catch (e) {
      $("#graphInfo").textContent = "";
      toast(`그래프 로드 실패: ${e.message}`, "error");
    }
  }

  function tooltipEl(title, lines) {
    const el = document.createElement("div");
    const h = document.createElement("b");
    h.textContent = title;
    el.appendChild(h);
    for (const ln of lines) {
      if (!ln) continue;
      const p = document.createElement("div");
      p.style.marginTop = "4px";
      p.textContent = ln;
      el.appendChild(p);
    }
    return el;
  }

  function isInvalid(e) { return !!(e.invalid_at || e.expired_at); }

  function renderGraph(data) {
    const container = $("#graph");
    const empty = !data.nodes.length;
    $("#graphEmpty").classList.toggle("hidden", !empty);
    $("#graphInfo").textContent = empty ? "" :
      `엔티티 ${data.nodes.length}/${data.total_entities} · 사실 ${data.edges.length}${data.episodes.length ? ` · 에피소드 ${data.episodes.length}` : ""}`;
    if (state.network) { state.network.destroy(); state.network = null; }
    if (empty || !window.vis) {
      if (!window.vis) toast("그래프 라이브러리(vis-network)를 불러오지 못했습니다. 인터넷 연결을 확인하세요.", "error");
      return;
    }
    const nodeColor = cssVar("--node"), text = cssVar("--node-text"), edgeColor = cssVar("--edge");
    const danger = cssVar("--danger"), epColor = cssVar("--episode"), bg = cssVar("--graph-bg");
    const accent = cssVar("--accent");
    const showLabels = $("#graphLabels").checked;

    const paletteFor = (labels) => {
      const custom = (labels || []).filter((l) => l !== "Entity");
      if (!custom.length) return nodeColor;
      return `hsl(${hashHue(custom[0])} 65% 58%)`;
    };

    const nodes = new vis.DataSet([
      ...data.nodes.map((n) => ({
        id: n.uuid,
        label: n.name,
        value: (n.degree || 0) + 1,
        group: "entity",
        title: tooltipEl(n.name, [(n.summary || "").slice(0, 280), `연결 ${n.degree}개`]),
        color: { background: paletteFor(n.labels), border: bg, highlight: { background: accent, border: text }, hover: { background: accent, border: bg } },
      })),
      ...data.episodes.map((e) => ({
        id: `ep:${e.uuid}`,
        label: "",
        shape: "square",
        size: 7,
        group: "episode",
        title: tooltipEl(e.name, [(e.preview || "") + "…"]),
        color: { background: epColor, border: bg },
      })),
    ]);
    const edges = new vis.DataSet([
      ...data.edges.map((e) => ({
        id: e.uuid,
        from: e.source,
        to: e.target,
        label: showLabels ? (e.name || "").replaceAll("_", " ").toLowerCase() : undefined,
        title: tooltipEl(e.name || "관계", [e.fact, isInvalid(e) ? `⛔ 만료됨 ${fmtDay(e.invalid_at || e.expired_at)}` : (e.valid_at ? `since ${fmtDay(e.valid_at)}` : "")]),
        dashes: isInvalid(e),
        color: { color: isInvalid(e) ? danger : edgeColor, highlight: accent, hover: accent, opacity: isInvalid(e) ? 0.7 : 1 },
        arrows: { to: { enabled: true, scaleFactor: 0.5 } },
      })),
      ...data.mentions.map((m, i) => ({
        id: `m:${i}`,
        from: `ep:${m.source}`,
        to: m.target,
        dashes: [2, 4],
        color: { color: epColor, opacity: 0.45 },
        width: 1,
        smooth: false,
      })),
    ]);

    const physics = $("#graphPhysics").checked;
    state.network = new vis.Network(container, { nodes, edges }, {
      autoResize: true,
      nodes: {
        shape: "dot",
        scaling: { min: 9, max: 34, label: { enabled: true, min: 12, max: 20 } },
        font: { color: text, face: "Pretendard Variable, Pretendard, sans-serif", strokeWidth: 3, strokeColor: bg },
        borderWidth: 2,
      },
      edges: {
        width: 1.4,
        font: { size: 10, color: cssVar("--muted"), strokeWidth: 3, strokeColor: bg, align: "middle" },
        smooth: { enabled: true, type: "dynamic" },
        selectionWidth: 2,
      },
      physics: {
        enabled: physics,
        solver: "forceAtlas2Based",
        forceAtlas2Based: { gravitationalConstant: -60, springLength: 110, springConstant: 0.06, avoidOverlap: 0.4 },
        stabilization: { iterations: 250, fit: true },
      },
      interaction: { hover: true, tooltipDelay: 120, navigationButtons: false, keyboard: false },
    });
    state.network.on("click", (params) => {
      if (params.nodes.length) {
        const id = String(params.nodes[0]);
        if (id.startsWith("ep:")) openEpisode(id.slice(3));
        else openNode(id);
      }
    });
    // 작은 그래프는 생성자 안에서 안정화가 끝나 이벤트를 놓치므로, 다음 프레임에도 맞춤을 건다.
    const net = state.network;
    const fit = () => { if (state.network === net) net.fit({ animation: { duration: 350 } }); };
    net.once("stabilized", fit);
    setTimeout(fit, 80);
  }

  $("#graphLimit").addEventListener("change", loadGraph);
  $("#graphEpisodes").addEventListener("change", loadGraph);
  $("#graphLabels").addEventListener("change", () => state.graphData && renderGraph(state.graphData));
  $("#graphPhysics").addEventListener("change", (e) => state.network && state.network.setOptions({ physics: { enabled: e.target.checked } }));
  $("#graphFit").addEventListener("click", () => state.network && state.network.fit({ animation: { duration: 400 } }));
  $("#graphFind").addEventListener("input", debounce((e) => {
    const q = e.target.value.trim().toLowerCase();
    if (!q || !state.network || !state.graphData) return;
    const hit = state.graphData.nodes.find((n) => (n.name || "").toLowerCase().includes(q));
    if (hit) {
      state.network.selectNodes([hit.uuid]);
      state.network.focus(hit.uuid, { scale: 1.3, animation: { duration: 450 } });
    }
  }, 250));
  $("#graphFind").addEventListener("keydown", (e) => {
    if (e.key !== "Enter" || !state.graphData) return;
    const q = e.target.value.trim().toLowerCase();
    const hit = state.graphData.nodes.find((n) => (n.name || "").toLowerCase().includes(q));
    if (hit) openNode(hit.uuid);
  });

  // ---------------------------------------------------------------- fact card
  function factCard(f, { score = null, compact = false } = {}) {
    const invalid = isInvalid(f);
    const range = invalid
      ? `<span class="tag bad">만료</span> ${esc(fmtDay(f.valid_at))} ~ ${esc(fmtDay(f.invalid_at || f.expired_at))}`
      : f.valid_at ? `<span class="tag good">유효</span> ${esc(fmtDay(f.valid_at))}부터` : `<span class="tag good">유효</span>`;
    return `<div class="card fact-card ${invalid ? "invalid" : ""}" data-fact="${esc(f.uuid)}">
      <div class="triple">
        <button class="ent-link" data-node="${esc(f.source)}">${esc(f.source_name || "?")}</button>
        <span class="rel">${esc(f.name || "RELATES_TO")}</span><span class="arrow">→</span>
        <button class="ent-link" data-node="${esc(f.target)}">${esc(f.target_name || "?")}</button>
      </div>
      <div class="fact-actions">
        <button class="btn sm ghost" data-action="edit-fact" title="사실 문장 수정">수정</button>
        ${invalid
          ? `<button class="btn sm ghost" data-action="restore-fact" title="다시 유효한 사실로">복원</button>`
          : `<button class="btn sm ghost" data-action="invalidate-fact" title="더 이상 사실이 아님으로 표시">만료</button>`}
        <button class="btn sm ghost danger-text" data-action="delete-fact">삭제</button>
      </div>
      <div class="fact-text">${esc(f.fact)}</div>
      <div class="fact-meta"><span>${range}</span>${compact ? "" : `<span>기록 ${esc(ago(f.created_at))}</span>`}${score != null ? `<span class="score">점수 ${Number(score).toFixed(3)}</span>` : ""}</div>
    </div>`;
  }

  function findFact(uuid) {
    const pools = [state.facts.items, state.drawerFacts || [], state.searchFacts || [], state.graphData?.edges || []];
    for (const p of pools) { const f = p.find((x) => x.uuid === uuid); if (f) return f; }
    return null;
  }

  async function afterMutation() {
    await loadSpaces();
    if (state.tab === "facts") loadFacts(true);
    if (state.tab === "graph") loadGraph();
    if (state.tab === "episodes") loadEpisodes(true);
    if (state.drawerNode) openNode(state.drawerNode, true);
  }

  document.addEventListener("click", async (e) => {
    const nodeBtn = e.target.closest("[data-node]");
    if (nodeBtn) { openNode(nodeBtn.dataset.node); return; }
    const btn = e.target.closest("[data-action]");
    if (!btn) return;
    const card = btn.closest("[data-fact]");
    const uuid = card?.dataset.fact;
    const base = `/api/spaces/${enc(state.gid)}`;
    try {
      switch (btn.dataset.action) {
        case "edit-fact": {
          const f = findFact(uuid);
          const val = await modal({ title: "사실 수정", html: "<p>문장을 고치면 임베딩도 다시 계산되어 검색에 반영됩니다.</p>", okText: "저장", input: { multiline: true, value: f?.fact || "" } });
          if (val == null || !val.trim()) return;
          await api(`${base}/facts/${enc(uuid)}`, { method: "PATCH", body: JSON.stringify({ fact: val.trim() }) });
          toast("사실을 수정했습니다", "ok");
          break;
        }
        case "invalidate-fact":
          await api(`${base}/facts/${enc(uuid)}/invalidate`, { method: "POST" });
          toast("만료된 사실로 표시했습니다. 봇은 이제 '과거 사실'로 취급합니다.", "ok");
          break;
        case "restore-fact":
          await api(`${base}/facts/${enc(uuid)}/restore`, { method: "POST" });
          toast("다시 유효한 사실로 복원했습니다", "ok");
          break;
        case "delete-fact": {
          const ok = await modal({ title: "사실 삭제", html: `<p>${esc(findFact(uuid)?.fact || "")}</p><p>완전히 삭제합니다. 되돌릴 수 없어요. (지우기보다 '만료'를 권장)</p>`, okText: "삭제", danger: true });
          if (!ok) return;
          await api(`${base}/facts/${enc(uuid)}`, { method: "DELETE" });
          toast("사실을 삭제했습니다", "ok");
          break;
        }
        case "delete-episode": {
          const id = btn.dataset.episode;
          const ok = await modal({ title: "에피소드 삭제", html: "<p>이 대화 묶음과, 이 대화에서만 생겨난 엔티티/사실을 함께 삭제합니다. 되돌릴 수 없어요.</p>", okText: "삭제", danger: true });
          if (!ok) return;
          await api(`${base}/episodes/${enc(id)}`, { method: "DELETE" });
          toast("에피소드를 삭제했습니다", "ok");
          if (state.drawerEpisode === id) closeDrawer();
          break;
        }
        case "toggle-transcript":
          btn.closest(".card").querySelector(".transcript").classList.toggle("open");
          btn.textContent = btn.textContent === "펼치기" ? "접기" : "펼치기";
          return;
        case "save-summary": {
          const ta = $("#summaryInput");
          await api(`${base}/nodes/${enc(state.drawerNode)}`, { method: "PATCH", body: JSON.stringify({ summary: ta.value }) });
          toast("요약을 저장했습니다", "ok");
          break;
        }
        case "delete-node": {
          const name = $("#drawerTitle").textContent;
          const ok = await modal({ title: "엔티티 삭제", html: `<p><b>${esc(name)}</b> 엔티티와 연결된 모든 사실을 삭제합니다. 되돌릴 수 없어요.</p>`, okText: "삭제", danger: true });
          if (!ok) return;
          await api(`${base}/nodes/${enc(state.drawerNode)}`, { method: "DELETE" });
          toast("엔티티를 삭제했습니다", "ok");
          closeDrawer();
          break;
        }
        default:
          return;
      }
      await afterMutation();
    } catch (err) {
      toast(err.message, "error");
    }
  });

  // ---------------------------------------------------------------- facts tab
  async function loadFacts(reset = false) {
    if (reset) state.facts = { skip: 0, items: [] };
    const q = $("#factQuery").value.trim();
    const inv = $("#factInvalid").checked;
    try {
      const res = await api(`/api/spaces/${enc(state.gid)}/facts?q=${enc(q)}&skip=${state.facts.skip}&limit=50&include_invalid=${inv}`);
      state.facts.items = state.facts.items.concat(res.items);
      state.facts.skip += res.items.length;
      state.facts.total = res.total;
      $("#factList").innerHTML = state.facts.items.length
        ? state.facts.items.map((f) => factCard(f)).join("")
        : `<div class="empty">${q ? "조건에 맞는 사실이 없어요" : "아직 추출된 사실이 없어요"}</div>`;
      $("#factMore").classList.toggle("hidden", state.facts.items.length >= res.total);
      $("#factMore").textContent = `더 보기 (${state.facts.items.length}/${res.total})`;
    } catch (e) { toast(`사실 로드 실패: ${e.message}`, "error"); }
  }
  $("#factQuery").addEventListener("input", debounce(() => loadFacts(true), 300));
  $("#factInvalid").addEventListener("change", () => loadFacts(true));
  $("#factMore").addEventListener("click", () => loadFacts(false));

  // ---------------------------------------------------------------- episodes tab
  function transcriptHtml(content) {
    return String(content || "").split("\n").map((ln) => {
      const i = ln.indexOf(": ");
      if (i > 0 && i < 40) return `<div class="line"><span class="who">${esc(ln.slice(0, i))}</span>${esc(ln.slice(i + 2))}</div>`;
      return `<div class="line">${esc(ln)}</div>`;
    }).join("");
  }

  function episodeCard(ep) {
    return `<div class="card episode-card">
      <div class="ep-head">
        <span class="ep-title">${esc(ep.name)}</span>
        <span class="ep-meta"><span>${esc(fmtDate(ep.valid_at))}</span>${ep.mentions != null ? `<span>엔티티 ${ep.mentions}개 언급</span>` : ""}${ep.source_description ? `<span>${esc(ep.source_description)}</span>` : ""}</span>
        <span class="ep-actions">
          <button class="btn sm ghost" data-action="toggle-transcript">펼치기</button>
          <button class="btn sm ghost danger-text" data-action="delete-episode" data-episode="${esc(ep.uuid)}">삭제</button>
        </span>
      </div>
      <div class="transcript">${transcriptHtml(ep.content)}</div>
    </div>`;
  }

  async function loadEpisodes(reset = false) {
    if (reset) state.episodes = { skip: 0, items: [] };
    const q = $("#episodeQuery").value.trim();
    try {
      const res = await api(`/api/spaces/${enc(state.gid)}/episodes?q=${enc(q)}&skip=${state.episodes.skip}&limit=30`);
      state.episodes.items = state.episodes.items.concat(res.items);
      state.episodes.skip += res.items.length;
      $("#episodeList").innerHTML = state.episodes.items.length
        ? state.episodes.items.map(episodeCard).join("")
        : `<div class="empty">${q ? "검색 결과가 없어요" : "아직 저장된 에피소드가 없어요"}</div>`;
      $("#episodeMore").classList.toggle("hidden", state.episodes.items.length >= res.total);
      $("#episodeMore").textContent = `더 보기 (${state.episodes.items.length}/${res.total})`;
    } catch (e) { toast(`에피소드 로드 실패: ${e.message}`, "error"); }
  }
  $("#episodeQuery").addEventListener("input", debounce(() => loadEpisodes(true), 300));
  $("#episodeMore").addEventListener("click", () => loadEpisodes(false));

  // ---------------------------------------------------------------- search tab
  $("#searchForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const q = $("#searchQuery").value.trim();
    if (!q) return;
    const out = $("#searchResults");
    out.innerHTML = `<div class="empty small">검색 중…</div>`;
    try {
      const res = await api(`/api/spaces/${enc(state.gid)}/search?q=${enc(q)}`);
      state.searchFacts = res.facts;
      let html = `<div class="section-title">관련 사실 ${res.facts.length}개</div>`;
      html += res.facts.length ? `<div class="list">${res.facts.map((f) => factCard(f, { score: f.score })).join("")}</div>` : `<div class="empty small">관련 사실 없음</div>`;
      html += `<div class="section-title">관련 엔티티 ${res.entities.length}개</div>`;
      html += res.entities.length ? `<div class="list">${res.entities.map((n) => `
        <div class="card entity-card" data-node="${esc(n.uuid)}">
          <span class="entity-dot"></span>
          <div><div class="name">${esc(n.name)}</div><div class="summary">${esc((n.summary || "요약 없음").slice(0, 300))}</div></div>
        </div>`).join("")}</div>` : `<div class="empty small">관련 엔티티 없음</div>`;
      out.innerHTML = html;
    } catch (err) {
      out.innerHTML = `<div class="empty small">검색 실패: ${esc(err.message)}</div>`;
    }
  });

  // ---------------------------------------------------------------- activity
  function renderActivity() {
    const all = $("#activityAll").checked;
    const events = (state.status?.events || []).filter((ev) => all || ev.group_id === state.gid);
    const names = Object.fromEntries(state.spaces.map((s) => [s.group_id, s.name]));
    $("#activityList").innerHTML = events.length ? events.map((ev) => {
      const ok = ev.type === "saved";
      return `<div class="card event ${esc(ev.type)}">
        <span class="ico">${ok ? "✅" : "⚠️"}</span>
        <div class="body">
          <div class="title">${ok ? `대화 ${ev.messages}마디 저장 → 엔티티 ${ev.nodes} · 관계 ${ev.edges}` : `저장 실패 (대화 ${ev.messages}마디)`}</div>
          <div class="sub">${esc(names[ev.group_id] || ev.group_id)} · ${esc(ev.episode || "")}${ok ? ` · ${ev.seconds}s` : ""}</div>
          ${ok ? "" : `<div class="sub danger-text">${esc(ev.error || "")}</div>`}
        </div>
        <time title="${esc(fmtDate(ev.time))}">${esc(ago(ev.time))}</time>
      </div>`;
    }).join("") : `<div class="empty">${state.status?.online === false && !state.status?.events?.length ? "봇이 오프라인이거나 아직 기록이 없어요" : "아직 저장 기록이 없어요"}</div>`;
  }
  $("#activityAll").addEventListener("change", renderActivity);

  // ---------------------------------------------------------------- drawer
  function openDrawer() { $("#drawer").classList.remove("hidden"); }
  function closeDrawer() {
    $("#drawer").classList.add("hidden");
    state.drawerNode = null;
    state.drawerEpisode = null;
  }
  $("#drawerClose").addEventListener("click", closeDrawer);

  async function openNode(uuid, silent = false) {
    if (!state.gid) return;
    state.drawerNode = uuid;
    state.drawerEpisode = null;
    if (!silent) {
      $("#drawerTitle").textContent = "불러오는 중…";
      $("#drawerLabels").innerHTML = "";
      $("#drawerBody").innerHTML = "";
      openDrawer();
    }
    try {
      const d = await api(`/api/spaces/${enc(state.gid)}/nodes/${enc(uuid)}`);
      if (state.drawerNode !== uuid) return;
      const p = d.props || {};
      state.drawerFacts = d.facts;
      $("#drawerTitle").textContent = p.name || "(이름 없음)";
      $("#drawerLabels").innerHTML = (d.labels || []).map((l) => `<span class="tag">${esc(l)}</span>`).join("");
      const skip = new Set(["name", "summary", "uuid", "group_id", "created_at", "labels"]);
      const extra = Object.entries(p).filter(([k]) => !skip.has(k));
      $("#drawerBody").innerHTML = `
        <div class="section-title" style="margin-top:4px">요약</div>
        <textarea class="input" id="summaryInput" rows="4" placeholder="요약 없음">${esc(p.summary || "")}</textarea>
        <div class="row-actions"><button class="btn sm primary" data-action="save-summary">요약 저장</button></div>

        <div class="section-title">사실 ${d.facts.length}개</div>
        <div class="list">${d.facts.length ? d.facts.map((f) => factCard(f, { compact: true })).join("") : `<div class="empty small">연결된 사실이 없어요</div>`}</div>

        <div class="section-title">언급된 대화 ${d.episodes.length}개</div>
        <div class="list">${d.episodes.length ? d.episodes.map((ep) => episodeCard(ep)).join("") : `<div class="empty small">없음</div>`}</div>

        <div class="section-title">속성</div>
        <dl class="props">
          <dt>uuid</dt><dd><code>${esc(p.uuid)}</code></dd>
          <dt>생성</dt><dd>${esc(fmtDate(p.created_at))}</dd>
          ${extra.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(typeof v === "object" ? JSON.stringify(v) : v)}</dd>`).join("")}
        </dl>
        <div class="row-actions" style="margin-top:18px"><button class="btn sm danger" data-action="delete-node">엔티티 삭제</button></div>`;
    } catch (e) {
      if (!silent) $("#drawerBody").innerHTML = `<div class="empty small">불러오기 실패: ${esc(e.message)}</div>`;
    }
  }

  async function openEpisode(uuid) {
    state.drawerNode = null;
    state.drawerEpisode = uuid;
    $("#drawerTitle").textContent = "에피소드";
    $("#drawerLabels").innerHTML = `<span class="tag">Episodic</span>`;
    $("#drawerBody").innerHTML = `<div class="empty small">불러오는 중…</div>`;
    openDrawer();
    try {
      // 그래프에 있는 미리보기 대신 전체 내용을 가져온다
      const res = await api(`/api/spaces/${enc(state.gid)}/episodes?limit=100`);
      const ep = res.items.find((x) => x.uuid === uuid);
      if (!ep) { $("#drawerBody").innerHTML = `<div class="empty small">찾을 수 없음</div>`; return; }
      $("#drawerTitle").textContent = ep.name;
      $("#drawerBody").innerHTML = episodeCard(ep);
      const t = $("#drawerBody .transcript");
      if (t) t.classList.add("open");
      const tg = $("#drawerBody [data-action=toggle-transcript]");
      if (tg) tg.textContent = "접기";
    } catch (e) {
      $("#drawerBody").innerHTML = `<div class="empty small">불러오기 실패: ${esc(e.message)}</div>`;
    }
  }

  // ---------------------------------------------------------------- space delete
  $("#deleteSpaceBtn").addEventListener("click", async () => {
    const s = currentSpace();
    if (!s) return;
    const val = await modal({
      title: "기억 공간 삭제",
      html: `<p><b>${esc(s.name)}</b>의 모든 엔티티·사실·에피소드를 영구 삭제합니다. 되돌릴 수 없어요.</p><p>확인을 위해 <code>${esc(s.group_id)}</code> 를 입력하세요.</p>`,
      okText: "영구 삭제", danger: true, input: { placeholder: s.group_id, mustEqual: s.group_id },
    });
    if (val == null) return;
    try {
      await api(`/api/spaces/${enc(s.group_id)}`, { method: "DELETE", body: JSON.stringify({ confirm: val.trim() }) });
      toast("기억 공간을 삭제했습니다", "ok");
      state.gid = null;
      history.replaceState(null, "", "#");
      showWelcome();
      await loadSpaces();
    } catch (e) { toast(e.message, "error"); }
  });

  // ---------------------------------------------------------------- bot settings (persona)
  const PRESETS = {
    friendly: "너는 디스코드 서버에서 함께 지내는 다정한 친구다.\n사람들과 나눈 대화를 오래 기억하고, 적절한 순간에 자연스럽게 떠올려 근황을 챙긴다.\n\n말투:\n- 편안한 존댓말. 상대가 반말을 쓰면 반말로 맞춘다.\n- 답은 1~4문장으로 짧게, 이모지는 가끔.\n\n성격:\n- 공감을 먼저 하고, 조언은 물어볼 때만 한다.\n- 모르는 건 솔직하게 되묻는다.",
    tsundere: "너는 츤데레 성격의 디스코드 친구다.\n겉으로는 퉁명스럽게 굴지만 사실 상대를 많이 챙기고, 예전에 들은 이야기를 은근슬쩍 기억해서 꺼낸다.\n\n말투:\n- 반말. \"딱히 너를 위해서 기억한 건 아니거든?\" 같은 말투.\n- 짧게 1~3문장.\n\n규칙:\n- 진짜로 상처 주는 말은 하지 않는다.\n- 상대가 힘들어 보이면 츤데레를 잠시 내려놓고 진심으로 위로한다.",
    butler: "너는 서버 사람들을 모시는 정중한 집사다.\n주인님들의 취향과 일정, 지난 이야기를 세심하게 기억해 두었다가 필요한 순간에 조용히 상기시켜 드린다.\n\n말투:\n- 매우 정중한 존댓말. 상대를 '주인님' 또는 이름 뒤에 '님'을 붙여 부른다.\n- 차분하고 간결하게 2~4문장.\n\n성격:\n- 과장하지 않고, 모르는 것은 \"확인해 보겠습니다\"라고 솔직하게 말한다.",
    mentor: "너는 친절한 공부 멘토다.\n각 사람이 무엇을 공부하는지, 어디서 막혔는지, 시험·마감 일정을 기억하고 진도를 챙겨 준다.\n\n말투:\n- 편안한 존댓말, 격려를 아끼지 않는다.\n- 설명이 필요할 땐 단계별로, 그 외엔 짧게.\n\n규칙:\n- 정답을 바로 주기보다 힌트로 스스로 풀게 돕는다.\n- 확실하지 않은 지식은 확실하지 않다고 말한다.",
  };
  const SOURCE_LABEL = { web: "관리 웹에서 설정한 값 사용 중", file: "config/persona.md 파일 사용 중", env: ".env 값 사용 중", default: "기본값 사용 중" };

  function updatePersonaCount() {
    const n = $("#personaInput").value.length;
    const el = $("#personaCount");
    el.textContent = `${n} / 4000`;
    el.classList.toggle("over", n > 4000);
  }

  function fillPersona(info) {
    $("#botNameInput").value = info.name || "";
    $("#personaInput").value = info.persona || "";
    $("#personaSource").textContent = SOURCE_LABEL[info.persona_source] || info.persona_source;
    $("#personaFileHint").textContent = info.file_persona
      ? "config/persona.md 에도 페르소나가 있어요. '파일 값으로 되돌리기'를 누르면 그 내용이 쓰입니다."
      : "config/persona.md 가 비어 있어, 되돌리면 기본값이 쓰입니다.";
    updatePersonaCount();
  }

  async function showSettings() {
    state.gid = null;
    renderSpaces();
    closeDrawer();
    closeSidebar();
    $("#welcome").classList.add("hidden");
    $("#spaceView").classList.add("hidden");
    $("#settingsView").classList.remove("hidden");
    history.replaceState(null, "", "#settings");
    try { fillPersona(await api("/api/persona")); }
    catch (e) { toast(`설정 로드 실패: ${e.message}`, "error"); }
  }

  $("#settingsBtn").addEventListener("click", showSettings);
  $("#personaInput").addEventListener("input", updatePersonaCount);
  $$("[data-preset]").forEach((b) => b.addEventListener("click", async () => {
    if ($("#personaInput").value.trim()) {
      const ok = await modal({ title: "예시로 바꾸기", html: "<p>지금 입력된 페르소나를 예시로 덮어씁니다. (저장 전까지는 실제로 바뀌지 않아요)</p>", okText: "덮어쓰기" });
      if (!ok) return;
    }
    $("#personaInput").value = PRESETS[b.dataset.preset];
    updatePersonaCount();
  }));
  $("#personaSave").addEventListener("click", async () => {
    try {
      const info = await api("/api/persona", { method: "PUT", body: JSON.stringify({ name: $("#botNameInput").value, persona: $("#personaInput").value }) });
      fillPersona(info);
      toast("저장했어요. 다음 답변부터 반영됩니다.", "ok");
    } catch (e) { toast(e.message, "error"); }
  });
  $("#personaReset").addEventListener("click", async () => {
    const ok = await modal({ title: "파일 값으로 되돌리기", html: "<p>관리 웹에서 저장한 이름·페르소나를 지우고 <code>config/persona.md</code>(없으면 기본값)로 돌아갑니다.</p>", okText: "되돌리기" });
    if (!ok) return;
    try { fillPersona(await api("/api/persona/reset", { method: "POST" })); toast("되돌렸어요.", "ok"); }
    catch (e) { toast(e.message, "error"); }
  });
  $("#personaTestForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const msg = $("#personaTestMsg").value.trim();
    if (!msg) return;
    const out = $("#personaPreview");
    out.innerHTML = '<span class="muted">생각 중…</span>';
    try {
      const res = await api("/api/persona/test", { method: "POST", body: JSON.stringify({ name: $("#botNameInput").value, persona: $("#personaInput").value, message: msg }) });
      out.innerHTML = `<span class="who">${esc($("#botNameInput").value || "봇")}</span>${esc(res.reply)}`;
    } catch (err) { out.innerHTML = `<span class="danger-text">${esc(err.message)}</span>`; }
  });

  // ---------------------------------------------------------------- sidebar (mobile)
  function closeSidebar() { $("#sidebar").classList.remove("open"); $("#scrim").classList.add("hidden"); }
  $("#menuBtn").addEventListener("click", () => {
    const open = !$("#sidebar").classList.contains("open");
    $("#sidebar").classList.toggle("open", open);
    $("#scrim").classList.toggle("hidden", !open);
  });
  $("#scrim").addEventListener("click", closeSidebar);

  // ---------------------------------------------------------------- wiring
  $("#spaceList").addEventListener("click", (e) => {
    const item = e.target.closest(".space-item");
    if (item) selectSpace(item.dataset.gid);
  });
  $("#spaceFilter").addEventListener("input", renderSpaces);
  $$(".tab").forEach((t) => t.addEventListener("click", () => switchTab(t.dataset.tab)));
  $("#refreshBtn").addEventListener("click", async () => {
    await Promise.all([loadSpaces(), loadStatus()]);
    if (state.gid) switchTab(state.tab);
  });

  async function init() {
    try {
      state.meta = await api("/api/meta");
      $("#authBanner").classList.toggle("hidden", state.meta.auth);
      $("#brandSub").textContent = `Graphiti · FalkorDB · ${state.meta.model}`;
    } catch { /* ignore */ }
    await Promise.all([loadSpaces(), loadStatus()]);
    const [gid, tab] = decodeURIComponent(location.hash.slice(1)).split("/");
    if (gid === "settings") showSettings();
    else if (gid && state.spaces.some((s) => s.group_id === gid)) selectSpace(gid, tab || "graph");
    setInterval(loadStatus, 15000);
    setInterval(() => { if (!document.hidden) loadSpaces(); }, 60000);
  }
  init();
})();
