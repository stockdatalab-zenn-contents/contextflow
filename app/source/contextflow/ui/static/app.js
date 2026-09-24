/* contextflow ローカル GUI。フレームワーク不使用の素の JavaScript。
 * サーバの静的配信のみに依存し、外部への通信は一切発生させない。 */

const state = {
  token: "",
  date: "",
  projects: [],
  day: null,
  runningStartedAt: null,   // 実行中作業の開始時刻（ミリ秒）。経過分の再計算に使う
  activityForm: null,       // {mode:'add'} または {mode:'edit', id, startIso, endIso}
  changeEditTs: null,       // 変化を編集中の元 ts（ISO文字列）
  decisionEditTs: null,     // 判断を編集中の元 ts（ISO文字列）
  rawLogs: [],              // 表示中の生ログ（ts 昇順、集約前）
  rawLogGroups: [],         // 生ログを集約した行（groupRawLogs の結果、表示・選択はこちらを使う）
  rawLogSelected: new Set(),// 選択中グループの start_ts
  rawLogAnchor: null,       // Shift+クリックの起点（rawLogGroups の添字）
  settingsDraft: null,      // 設定パネルの編集用ワーキングコピー（{types, projects, taskSuggestions}）
  statusUnknown: false,     // 通信に失敗して帯の状態が分からない間 true（古い値を出し続けないため）
  chartStepMin: 60,         // 俯瞰図の刻み（分）。60 / 30 / 5
  chartSort: "settings",    // 俯瞰図の行の並び。settings（設定の並び順）/ total（合計時間順）
  currentState: null,             // 「現在の状態」（GET/POST /api/state の state）。null なら未作成
  currentStateGeneratedAt: null,  // 上の生成時刻（ISO文字列）。null なら未作成
  currentStatePath: null,         // 上の保存先パス（表示用）
  decideInfo: null,               // 判断・計画エンジンの情報（GET /api/decide/info の結果）。日付に依存しない
  calendarEvents: [],             // カレンダー予定の分類一覧（GET /api/calendar/events の events）。日付に依存しない
};

let bannerTimer = null;
let rawLogRowEls = [];      // 生ログ各行の DOM 参照（添字 → {li, checkbox}）

// ---------------------------------------------------------------------------
// 共通ヘルパ
// ---------------------------------------------------------------------------

function pad2(n) {
  return String(n).padStart(2, "0");
}

function todayStr() {
  const d = new Date();
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
}

function addDaysStr(dateStr, n) {
  const d = new Date(`${dateStr}T00:00:00`);
  d.setDate(d.getDate() + n);
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
}

// ローカルタイムゾーンのオフセットを "+09:00" 形式で返す
function localOffset() {
  const offsetMin = -new Date().getTimezoneOffset();
  const sign = offsetMin >= 0 ? "+" : "-";
  const abs = Math.abs(offsetMin);
  return `${sign}${pad2(Math.floor(abs / 60))}:${pad2(abs % 60)}`;
}

// ISO文字列（例 "2026-09-23T10:45:00+09:00"）から "HH:MM" を取り出す
function hhmm(isoStr) {
  return isoStr.slice(11, 16);
}

// 元の ISO文字列の日付・オフセットは保ったまま、時刻だけ差し替える
function withTime(isoStr, hhmmValue) {
  return isoStr.replace(/T\d{2}:\d{2}:\d{2}/, `T${hhmmValue}:00`);
}

function gapMinutes(startIso, endIso) {
  return Math.round((new Date(endIso) - new Date(startIso)) / 60000);
}

function dayIsoRange(dateStr) {
  const offset = localOffset();
  return {
    start: `${dateStr}T00:00:00${offset}`,
    end: `${addDaysStr(dateStr, 1)}T00:00:00${offset}`,
  };
}

function mkButton(label, cssClass) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.textContent = label;
  btn.className = cssClass || "row-btn";
  return btn;
}

// 分を "Xh Ym" 形式にする（1時間未満は "Nm" のみ、0 は "0m"）
function fmtMinutes(min) {
  const m = Math.max(0, Math.round(min || 0));
  const h = Math.floor(m / 60);
  const rest = m % 60;
  return h > 0 ? `${h}h ${rest}m` : `${rest}m`;
}

// ISO文字列（例 "2026-09-23T10:45:00+09:00"）の日付部分から "M/D" を作る
function mdLabel(isoStr) {
  const [, mo, da] = isoStr.slice(0, 10).split("-");
  return `${parseInt(mo, 10)}/${parseInt(da, 10)}`;
}

// ---------------------------------------------------------------------------
// メッセージ表示
// ---------------------------------------------------------------------------

function showMessage(text, isError) {
  const banner = document.getElementById("banner");
  document.getElementById("banner-text").textContent = text;
  banner.className = "banner " + (isError ? "error" : "success");
  banner.hidden = false;
  if (bannerTimer) clearTimeout(bannerTimer);
  if (!isError) {
    bannerTimer = setTimeout(() => { banner.hidden = true; }, 4000);
  }
}

document.getElementById("banner-close").addEventListener("click", () => {
  document.getElementById("banner").hidden = true;
});

// ---------------------------------------------------------------------------
// API 呼び出し（通信中はボタン無効化、エラーはバナーへ表示）
// ---------------------------------------------------------------------------

async function callApi(path, opts) {
  opts = opts || {};
  const method = opts.method || "GET";
  const body = opts.body !== undefined ? opts.body : null;
  const button = opts.button || null;
  if (button) button.disabled = true;
  try {
    const headers = {};
    if (body !== null) headers["Content-Type"] = "application/json";
    if (method !== "GET") headers["X-CF-Token"] = state.token;
    const res = await fetch(path, {
      method,
      headers,
      body: body !== null ? JSON.stringify(body) : undefined,
    });
    const text = await res.text();
    let data = null;
    if (text) {
      try { data = JSON.parse(text); } catch (e) { data = null; }
    }
    if (!res.ok) {
      showMessage((data && data.error) || `通信失敗（${res.status}）`, true);
      return { ok: false, data: null };
    }
    return { ok: true, data };
  } catch (e) {
    showMessage("サーバに接続できない", true);
    return { ok: false, data: null };
  } finally {
    if (button) button.disabled = false;
  }
}

// ---------------------------------------------------------------------------
// 案件・種別ドロップダウン
//
// 表示は日本語ラベル、保存値（select の value）は英語の種別・案件キーのまま。
// options（GET /api/day の options）が無いサーバでも空にならないよう、
// 既存の生値（activity_types 文字列配列 / state.projects）へ退避する。
// ---------------------------------------------------------------------------

// 種別の {value, label} 一覧。options が無ければ生値をそのままラベルにする
function currentTypeOptions() {
  const opts = state.day && state.day.options && state.day.options.activity_types;
  if (opts && opts.length) return opts;
  return ((state.day && state.day.activity_types) || []).map((v) => ({ value: v, label: v }));
}

// 案件の {key, label, from_repo} 一覧。options が無ければ state.projects をそのままラベルにする
function currentProjectOptions() {
  const opts = state.day && state.day.options && state.day.options.projects;
  if (opts && opts.length) return opts;
  return (state.projects || []).map((p) => ({ key: p.key, label: p.key, from_repo: true }));
}

// 種別の値（英語）→ 日本語ラベル。options に無い値（隠した種別が残っているなど）は値そのものを返す
function typeLabel(value) {
  if (!value) return value;
  const found = currentTypeOptions().find((t) => t.value === value);
  return found ? found.label : value;
}

function populateProjectSelects() {
  document.querySelectorAll(".project-select").forEach((sel) => {
    const current = sel.value;
    sel.innerHTML = "";
    const empty = document.createElement("option");
    empty.value = "";
    empty.textContent = "（未指定）";
    sel.appendChild(empty);
    currentProjectOptions().forEach((p) => {
      const opt = document.createElement("option");
      opt.value = p.key;
      // リポジトリ外（表示用に追加した）案件は、判断・変化の書き出し先が無いと分かるようにする
      opt.textContent = p.from_repo === false ? `${p.label}（リポジトリ外）` : p.label;
      sel.appendChild(opt);
    });
    sel.value = current;
  });
}

function populateTypeSelects(types) {
  document.querySelectorAll(".type-select").forEach((sel) => {
    const current = sel.value;
    sel.innerHTML = "";
    types.forEach((t) => {
      const opt = document.createElement("option");
      opt.value = t.value;
      opt.textContent = t.label;
      sel.appendChild(opt);
    });
    sel.value = current;
  });
}

// タスク入力の候補（datalist）。自由入力は input 側の性質上そのまま残る
function populateTaskDatalist() {
  const list = document.getElementById("task-suggestions");
  if (!list) return;
  list.innerHTML = "";
  const suggestions = (state.day && state.day.options && state.day.options.task_suggestions) || [];
  suggestions.forEach((s) => {
    const opt = document.createElement("option");
    opt.value = s;
    list.appendChild(opt);
  });
}

async function loadProjects() {
  const r = await callApi("/api/projects");
  if (!r.ok) return;
  state.projects = r.data || [];
}

// ---------------------------------------------------------------------------
// 日付ナビゲーション
// ---------------------------------------------------------------------------

function changeDate(dateStr) {
  state.date = dateStr;
  document.getElementById("date-input").value = dateStr;
  hideActivityForm();
  resetChangeForm();
  resetDecisionForm();
  // 前の日の判断・計画の結果を持ち越さない
  document.getElementById("decide-result").hidden = true;
  document.getElementById("plan-result").hidden = true;
  // 生ログ一覧は開いたままにする（loadDay() 側で開いていれば取り直す）
  loadDay();
}

document.getElementById("prev-day").addEventListener("click", () => changeDate(addDaysStr(state.date, -1)));
document.getElementById("next-day").addEventListener("click", () => changeDate(addDaysStr(state.date, 1)));
document.getElementById("today-btn").addEventListener("click", () => changeDate(todayStr()));
document.getElementById("date-input").addEventListener("change", (e) => changeDate(e.target.value));

// ---------------------------------------------------------------------------
// 実行中の作業
// ---------------------------------------------------------------------------

// 操作欄はボタン1つ。記録中かどうかで文言と動作だけが入れ替わり、位置は動かさない。
//
// 種別・案件・タスク・経過は状態表示の帯（renderRecordDetail）が唯一の出どころ。
// 同じ値を2か所に持つと書式・更新漏れでずれるため、ここは操作だけを担う。
function renderRunning() {
  // 状態が分かったので操作欄を出す（通信失敗で隠していた場合の復帰）
  document.getElementById("running-section").hidden = false;
  const typeEl = document.getElementById("start-type");
  const projectEl = document.getElementById("start-project");
  const taskEl = document.getElementById("start-task");
  const button = document.getElementById("record-toggle-btn");
  const running = state.day.running;

  // 記録中は入力欄を非活性にする（隠さない。ボタンの位置も動かない）
  for (const el of [typeEl, projectEl, taskEl]) {
    el.disabled = !!running;
  }

  if (running) {
    state.runningStartedAt = new Date(running.started_at).getTime();
    // 非活性の欄には記録中の内容を入れる。空のまま非活性にすると、再読み込み後に
    // 既定値が残り「帯は会議、入力欄は開発」のように食い違うため
    typeEl.value = running.activity_type;
    projectEl.value = running.project || "";
    taskEl.value = running.task || "";
    button.textContent = "記録を停止";
    // 収集・記録は常に今日に対して動く。過去日を見ているときは、停止すると今日に付くと分かるようにする
    document.getElementById("running-today-hint").hidden = state.date === todayStr();
    updateElapsed();
  } else {
    state.runningStartedAt = null;
    button.textContent = "記録を開始";
    document.getElementById("running-today-hint").hidden = true;
  }
}

// 止め忘れ警告のしきい値（分）。8時間以上つけっぱなしなら知らせる
const RUNNING_WARN_MIN = 480;

// 収集・記録の状態表示（何が動いているのか一目で分かるようにする）
//
// 収集・記録は実体として「今」（＝今日）に対してだけ動く。一方で画面は過去日も表示できるため、
// 過去日を見ているときにそのまま「収集中」「記録中」とだけ出すと、その日が収集・記録されている
// ように誤解する。過去日を見ているときはラベルへ「今日」の注記を添え、件数には表示中の日付を
// 明示し、収集・記録が動いているときだけ帯の下に「今日に記録される」旨の注意書きを出す。
function renderStatusStrip() {
  state.statusUnknown = false;
  const isToday = state.date === todayStr();
  const c = state.day.collector || {};
  const dot = document.getElementById("collector-dot");
  const label = document.getElementById("collector-label");
  const detail = document.getElementById("collector-detail");
  const hint = document.getElementById("collector-hint");

  if (c.running) {
    dot.textContent = "●";
    dot.className = "status-dot on";
    label.textContent = isToday
      ? `生ログ収集：収集中（${c.interval_sec}秒間隔）`
      : `生ログ収集：収集中（${c.interval_sec}秒間隔・今日の状態）`;
    hint.hidden = true;
  } else {
    dot.textContent = "○";
    dot.className = "status-dot off";
    label.textContent = "生ログ収集：停止中";
    // 今日を見ているときだけ、始め方を出す（過去日では意味が無い）
    hint.hidden = !isToday;
  }

  const parts = [];
  // 最終取得は収集全体の最終時刻であって、表示中の日のものとは限らない。日が違えば日付を添える
  if (c.last_event_at) {
    const lastLabel = mdLabel(c.last_event_at) === mdLabel(state.date)
      ? hhmm(c.last_event_at)
      : `${mdLabel(c.last_event_at)} ${hhmm(c.last_event_at)}`;
    parts.push(`最終取得 ${lastLabel}`);
  }
  parts.push(`${mdLabel(state.date)} の生ログ ${c.events_in_range || 0}件`);
  // まとめ書きのため、収集中でも件数はすぐには増えない。誤解しないよう添える
  if (c.running && c.flush_every) parts.push(`${c.flush_every}件ごとに保存`);
  detail.textContent = parts.join("　/　");

  renderCollectorControls(c);

  const rdot = document.getElementById("record-dot");
  const rlabel = document.getElementById("record-label");
  const rwarn = document.getElementById("record-warn");
  const running = state.day.running;
  if (running) {
    rdot.textContent = "●";
    rdot.className = "status-dot on";
    rlabel.textContent = isToday ? "作業記録：記録中" : "作業記録：記録中（今日の状態）";
    renderRecordDetail();
  } else {
    rdot.textContent = "○";
    rdot.className = "status-dot off";
    rlabel.textContent = "作業記録：停止中";
    renderRecordIdleDetail();
    rwarn.hidden = true;
  }

  // 過去日を見ている間は常に出す。何も動いていなくても、開始フォームから始めた作業は
  // 表示中の日ではなく今日に記録されるため
  document.getElementById("today-status-hint").hidden = isToday;
}

// 通信に失敗したときは、帯を「確認中」（＝分からない）へ戻す。
// 取得できた最後の値を出し続けると、実際には止まっているのに「収集中」と表示し続けることになる。
// 状態が分からない間は、開始・停止のボタンや案内も出さない（正しい操作を提示できないため）。
function renderStatusUnknown() {
  state.statusUnknown = true;
  for (const id of ["collector-dot", "record-dot"]) {
    const el = document.getElementById(id);
    el.textContent = "○";
    el.className = "status-dot";
  }
  document.getElementById("collector-label").textContent = "生ログ収集：確認中";
  document.getElementById("record-label").textContent = "作業記録：確認中";
  document.getElementById("collector-detail").textContent = "";
  document.getElementById("record-detail").textContent = "";
  for (const id of ["collector-start-btn", "collector-stop-btn", "collector-hint",
                    "collector-elsewhere-hint", "collector-close-warning",
                    "record-warn", "today-status-hint"]) {
    document.getElementById(id).hidden = true;
  }
  // 記録の操作欄も同じ理由で隠す。記録中かどうかが分からない以上、
  // [記録を停止] と [記録を開始] のどちらを出すべきかも決められない
  document.getElementById("running-section").hidden = true;
  state.runningStartedAt = null;
}

// 作業記録：記録中のときの詳細（種別・案件・タスク・開始・経過）を描く。
// renderStatusStrip() からの初回描画と、updateElapsed() からの定期更新の両方で使う
function renderRecordDetail() {
  // 状態が分からない間は書き戻さない（30秒ごとの更新が「確認中」を上書きしてしまうため）
  if (state.statusUnknown) return;
  const running = state.day.running;
  if (!running) return;
  const elapsedMin = state.runningStartedAt
    ? Math.max(0, Math.floor((Date.now() - state.runningStartedAt) / 60000))
    : 0;
  document.getElementById("record-detail").textContent = [
    running.project || "（未指定）",
    typeLabel(running.activity_type),
    running.task || "-",
    `開始 ${hhmm(running.started_at)}`,
    `経過 ${fmtMinutes(elapsedMin)}`,
  ].join("　/　");
  document.getElementById("record-warn").hidden = elapsedMin < RUNNING_WARN_MIN;
}

// 作業記録：停止中のときの詳細（表示中の日の作業記録の件数・最後の時刻）を描く。
// 生ログ収集が停止中でもこの日の件数を出しているのと同じ考え方で、行を常に意味あるものにする
function renderRecordIdleDetail() {
  const manual = (state.day.activities || []).filter((a) => a.source === "manual" || a.layer === "reported");
  const parts = [`${mdLabel(state.date)} の作業記録 ${manual.length}件`];
  if (manual.length) {
    const lastEnd = manual.map((a) => a.end_at).sort().slice(-1)[0];
    parts.push(`最後 ${hhmm(lastEnd)}`);
  }
  document.getElementById("record-detail").textContent = parts.join("　/　");
}

// 収集の開始・停止ボタンの出し分け。
// running=false                      → [収集を開始]
// running=true かつ owned_by_ui=true → [収集を停止]
// running=true かつ owned_by_ui=false→ ボタンを無効化し、別ウィンドウで収集中の旨を表示
function renderCollectorControls(c) {
  const startBtn = document.getElementById("collector-start-btn");
  const stopBtn = document.getElementById("collector-stop-btn");
  const elsewhereHint = document.getElementById("collector-elsewhere-hint");
  const closeWarning = document.getElementById("collector-close-warning");

  if (!c.running) {
    startBtn.hidden = false;
    stopBtn.hidden = true;
    elsewhereHint.hidden = true;
    closeWarning.hidden = true;
  } else if (c.owned_by_ui) {
    startBtn.hidden = true;
    stopBtn.hidden = false;
    stopBtn.disabled = false;
    elsewhereHint.hidden = true;
    // 収集中は、閉じると止まることを毎回明記する（押し間違いで一日ぶん失わないため）
    closeWarning.hidden = false;
  } else {
    startBtn.hidden = true;
    stopBtn.hidden = false;
    stopBtn.disabled = true;
    elsewhereHint.hidden = false;
    closeWarning.hidden = true;
  }
}

document.getElementById("collector-start-btn").addEventListener("click", async (e) => {
  const r = await callApi("/api/collector/start", { method: "POST", body: {}, button: e.currentTarget });
  if (r.ok) {
    showMessage((r.data && r.data.message) || "収集を開始した", false);
    await loadDay();
  }
});

document.getElementById("collector-stop-btn").addEventListener("click", async (e) => {
  const r = await callApi("/api/collector/stop", { method: "POST", body: {}, button: e.currentTarget });
  if (r.ok) {
    showMessage((r.data && r.data.message) || "収集を停止した", false);
    await loadDay();
  }
});

// 経過の表示先は状態表示の帯だけ。30秒ごとに呼ばれ、再読み込みなしで進む
function updateElapsed() {
  if (!state.runningStartedAt) return;
  renderRecordDetail();
}

setInterval(updateElapsed, 30000);

// ボタンが1つなので、押されたときの動作は「記録中かどうか」で決める
document.getElementById("work-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const button = document.getElementById("record-toggle-btn");
  if (state.day && state.day.running) {
    await stopRecording(button);
  } else {
    await startRecording(button);
  }
});

async function startRecording(button) {
  const activity_type = document.getElementById("start-type").value;
  const project = document.getElementById("start-project").value || null;
  const task = document.getElementById("start-task").value || null;
  if (!activity_type) { showMessage("種別を選ぶ", true); return; }
  const r = await callApi("/api/work/start", { method: "POST", body: { activity_type, project, task }, button });
  if (r.ok) {
    // 実行中の作業があると自動で終了される。起きたことをそのまま伝える
    const stopped = r.data && r.data.stopped_type;
    showMessage(stopped ? `前の記録（${typeLabel(stopped)}）を停止して開始した` : "記録を開始した", false);
    // 入力欄の値は renderRunning が記録中の内容に合わせるため、ここでは触らない
    await loadDay();
  }
}

// 停止の結果は「記録した」「取り消した」「実行中なし」「日をまたいで分割」の4通りある。
// 常に「記録を停止した」と出すと、記録されていない場合に表示と食い違う
function workStopMessage(data) {
  const list = (data && data.activities) || [];
  if (!list.length) return (data && data.message) || "実行中の作業なし";
  const total = list.reduce((sum, a) => sum + gapMinutes(a.start_at, a.end_at), 0);
  const base = `記録を停止した（${fmtMinutes(total)}）`;
  return list.length > 1 ? `${base}　日をまたぐため${list.length}件に分割` : base;
}

async function stopRecording(button) {
  const r = await callApi("/api/work/stop", { method: "POST", body: {}, button });
  if (r.ok) {
    showMessage(workStopMessage(r.data), false);
    await loadDay();
  }
}

// ---------------------------------------------------------------------------
// タイムライン（activities + gaps）
// ---------------------------------------------------------------------------

function notEditableReason(a) {
  if (a.layer === "observed") return "PCログから作られた行のため編集不可。直しても作り直しで戻る";
  if (a.layer === "planned") return "カレンダーの予定のため編集不可。予定はカレンダー側で直す";
  if (a.layer === "confirmed") return "突き合わせて作られた行のため編集不可。元になった手入力を直す";
  return "編集不可";
}

// 案件・種別・タスクが同じ活動が続いていれば、開始〜終了を合わせて1行にまとめる。
//
// まとめるのは編集できない行（build が作った確定層・PCログ・予定）だけ。
// 手入力の行までまとめると、まとまった行から個別の記録を編集・削除できなくなるため。
// 時間が離れている場合もまとめない（空いていた事実が消えるため）。
function groupActivities(activities) {
  const groups = [];
  activities.forEach((a) => {
    const last = groups[groups.length - 1];
    const sameKey = last
      && (last.project || "") === (a.project || "")
      && last.activity_type === a.activity_type
      && (last.task || "") === (a.task || "");
    // 直前の行の終わりに接している（または重なっている）ときだけ「続き」とみなす
    const contiguous = last && a.start_at <= last.end_at;
    const mergeable = sameKey && contiguous && !last.editable && !a.editable;
    if (mergeable) {
      if (a.end_at > last.end_at) last.end_at = a.end_at;
      last.items.push(a);
      if (!last.sources.includes(a.source)) last.sources.push(a.source);
    } else {
      groups.push({
        start_at: a.start_at,
        end_at: a.end_at,
        project: a.project,
        activity_type: a.activity_type,
        task: a.task,
        layer: a.layer,
        editable: a.editable,
        sources: [a.source],
        items: [a],
      });
    }
  });
  return groups;
}

function buildActivityRow(g) {
  const li = document.createElement("li");
  li.className = "timeline-row";

  const time = document.createElement("span");
  time.className = "time";
  time.textContent = `${hhmm(g.start_at)}-${hhmm(g.end_at)}`;
  li.appendChild(time);

  const info = document.createElement("span");
  info.className = "info";
  // 案件・種別・タスクの順。タスクは未設定のことが多いので、無ければ「-」で桁を保つ
  const parts = [
    g.project || "（未指定）",
    typeLabel(g.activity_type),
    g.task || "-",
    g.sources.join("+"),
  ];
  // まとめた行は、何件をまとめたのかが分かるようにする
  if (g.items.length > 1) parts.push(`${g.items.length}件`);
  info.textContent = parts.join(" / ");
  li.appendChild(info);

  const single = g.items.length === 1 ? g.items[0] : null;
  if (single && single.editable) {
    const editBtn = mkButton("編集");
    editBtn.addEventListener("click", () => openActivityEditForm(single));
    const delBtn = mkButton("削除");
    delBtn.addEventListener("click", () => deleteActivity(single, delBtn));
    li.appendChild(editBtn);
    li.appendChild(delBtn);
  } else if (single) {
    li.title = notEditableReason(single);
  } else {
    li.title = `案件・種別・タスクが同じ ${g.items.length} 件をまとめて表示。`
      + notEditableReason(g);
  }
  return li;
}

function buildGapRow(startIso, endIso) {
  const li = document.createElement("li");
  li.className = "timeline-row gap";
  li.textContent = `${hhmm(startIso)}-${hhmm(endIso)}　（空白 ${gapMinutes(startIso, endIso)}分）`;
  li.title = "クリックで記録を補完";
  li.addEventListener("click", () => openActivityAddForm(startIso, endIso));
  return li;
}

// ---------------------------------------------------------------------------
// 俯瞰図（縦軸=案件・種別、横軸=時間）
//
// タイムラインの各行は「いつ・何を」を正確に出すが、1日を見渡すには向かない。
// ここでは1時間ごとの占有分数を濃さで出し、いつ何をしていたかを一目で分かるようにする。
// ---------------------------------------------------------------------------

// ISO 文字列を、表示中の日の 0:00 からの分数へ。前日・翌日にまたがる端は 0 / 24:00 へ丸める
function minuteOfDay(iso, dayStr) {
  const day = iso.slice(0, 10);
  if (day < dayStr) return 0;
  if (day > dayStr) return 24 * 60;
  return Number(iso.slice(11, 13)) * 60 + Number(iso.slice(14, 16));
}

// 案件×種別ごとに、刻み（stepMin 分）ごとの占有分数を集計する。
// minutes のキーは、その日の 0:00 から数えた刻みの開始分。
function buildChartRows(activities, dayStr, stepMin) {
  const rows = new Map();   // "案件\u0000種別" -> {project, type, minutes: Map(開始分 -> 分), total}
  let minHour = 24;
  let maxHour = 0;
  activities.forEach((a) => {
    const start = minuteOfDay(a.start_at, dayStr);
    const end = minuteOfDay(a.end_at, dayStr);
    if (end <= start) return;
    const key = `${a.project || ""}\u0000${a.activity_type}`;
    let row = rows.get(key);
    if (!row) {
      row = { project: a.project, type: a.activity_type, minutes: new Map(), total: 0 };
      rows.set(key, row);
    }
    for (let s = Math.floor(start / stepMin) * stepMin; s < end; s += stepMin) {
      const overlap = Math.min(end, s + stepMin) - Math.max(start, s);
      if (overlap <= 0) continue;
      row.minutes.set(s, (row.minutes.get(s) || 0) + overlap);
      row.total += overlap;
    }
    // 列の範囲は刻みによらず「時」単位でそろえる
    minHour = Math.min(minHour, Math.floor(start / 60));
    maxHour = Math.max(maxHour, Math.ceil(end / 60));
  });
  // 並べ替えは sortChartRows に任せる（画面から切り替えられるため）
  return { list: [...rows.values()], minHour, maxHour };
}

// 俯瞰図の行の並べ替え。
//
// "settings" … 設定画面で決めた案件・種別の並び順に従う。
//              ドロップダウンやタイムラインの選択肢と根拠を1つにするため、こちらを既定にする。
// "total"    … その日の合計時間が多い順。何に時間を使ったかを上から見たいとき。
//              日ごとに行の順が入れ替わるので、日をまたいで位置で追うことはできない。
function sortChartRows(list, mode) {
  const projectOrder = currentProjectOptions().map((p) => p.key);
  const typeOrder = currentTypeOptions().map((t) => t.value);
  // 設定に無い案件・種別（隠した後も記録が残っている場合など）は、設定にあるものの後ろへ
  const projectIndex = (row) => {
    if (!row.project) return projectOrder.length + 1;   // 案件が未指定の行は最後
    const i = projectOrder.indexOf(row.project);
    return i === -1 ? projectOrder.length : i;
  };
  const typeIndex = (row) => {
    const i = typeOrder.indexOf(row.type);
    return i === -1 ? typeOrder.length : i;
  };
  const bySettings = (x, y) =>
    projectIndex(x) - projectIndex(y)
    || (x.project || "").localeCompare(y.project || "")
    || typeIndex(x) - typeIndex(y)
    || typeLabel(x.type).localeCompare(typeLabel(y.type));

  if (mode === "total") {
    // 合計が同じ行は設定順にして、同点で並びがぶれないようにする
    return list.sort((x, y) => y.total - x.total || bySettings(x, y));
  }
  return list.sort(bySettings);
}

// 分（0:00 起点）を "H:MM" へ。24:00 もそのまま出す
function slotLabel(minute) {
  return `${Math.floor(minute / 60)}:${pad2(minute % 60)}`;
}

function renderTimelineChart() {
  const box = document.getElementById("timeline-chart");
  const table = document.getElementById("chart-table");
  table.innerHTML = "";
  const step = state.chartStepMin;
  const { list, minHour, maxHour } = buildChartRows(state.day.activities || [], state.date, step);
  sortChartRows(list, state.chartSort);
  if (!list.length) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  // 刻みが細かいほど列が増えるので、セル幅を CSS 側で切り替える
  table.className = `chart-table step-${step}`;

  const perHour = 60 / step;
  const slots = [];
  for (let m = minHour * 60; m < maxHour * 60; m += step) slots.push(m);

  // 見出しは「時」単位。刻みが細かいときは colspan でその時間ぶんをまとめる
  const thead = document.createElement("thead");
  const headRow = document.createElement("tr");
  const corner = document.createElement("th");
  corner.className = "row-head";
  corner.textContent = "案件 / 種別";
  headRow.appendChild(corner);
  for (let h = minHour; h < maxHour; h += 1) {
    const th = document.createElement("th");
    th.textContent = String(h);
    if (perHour > 1) th.colSpan = perHour;
    th.className = "hour-head";
    headRow.appendChild(th);
  }
  const totalHead = document.createElement("th");
  totalHead.textContent = "合計";
  headRow.appendChild(totalHead);
  thead.appendChild(headRow);
  table.appendChild(thead);

  const tbody = document.createElement("tbody");
  list.forEach((row) => {
    const tr = document.createElement("tr");
    const head = document.createElement("th");
    head.className = "row-head";
    head.textContent = `${row.project || "（未指定）"} / ${typeLabel(row.type)}`;
    tr.appendChild(head);
    slots.forEach((m) => {
      const td = document.createElement("td");
      // 時の変わり目に区切り線を入れ、細かい刻みでも位置を追えるようにする
      if (m % 60 === 0) td.className = "hour-start";
      const minutes = row.minutes.get(m) || 0;
      if (minutes > 0) {
        const fill = document.createElement("span");
        fill.className = "chart-cell";
        // 1分でも見えるよう下限を持たせ、残りを占有率で伸ばす
        fill.style.opacity = String(0.2 + 0.8 * Math.min(1, minutes / step));
        td.appendChild(fill);
        td.title = `${slotLabel(m)}-${slotLabel(m + step)}　${head.textContent}　${minutes}分`;
      }
      tr.appendChild(td);
    });
    const total = document.createElement("td");
    total.className = "chart-total";
    total.textContent = fmtMinutes(row.total);
    tr.appendChild(total);
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
}

document.getElementById("chart-step").addEventListener("change", (e) => {
  state.chartStepMin = Number(e.target.value) || 60;
  renderTimelineChart();
});

document.getElementById("chart-sort").addEventListener("change", (e) => {
  state.chartSort = e.target.value === "total" ? "total" : "settings";
  renderTimelineChart();
});

function renderTimeline() {
  const list = document.getElementById("timeline-list");
  list.innerHTML = "";
  const rows = [];
  groupActivities(state.day.activities || []).forEach((g) => rows.push({ start: g.start_at, node: buildActivityRow(g) }));
  (state.day.gaps || []).forEach((g) => rows.push({ start: g[0], node: buildGapRow(g[0], g[1]) }));
  rows.sort((x, y) => x.start.localeCompare(y.start));
  rows.forEach((row) => list.appendChild(row.node));
  renderTimelineChart();
}

// --- 補完・編集フォーム（gap クリック / [編集] 共用） ---

function showActivityForm() {
  document.getElementById("activity-form").hidden = false;
}

function hideActivityForm() {
  document.getElementById("activity-form").hidden = true;
  state.activityForm = null;
}

function openActivityAddForm(startIso, endIso) {
  state.activityForm = { mode: "add" };
  document.getElementById("activity-form-title").textContent = "空白を記録";
  document.getElementById("af-start").value = hhmm(startIso);
  document.getElementById("af-end").value = hhmm(endIso);
  document.getElementById("af-type").selectedIndex = -1;
  document.getElementById("af-project").value = "";
  document.getElementById("af-task").value = "";
  showActivityForm();
}

function openActivityEditForm(a) {
  state.activityForm = { mode: "edit", id: a.id, startIso: a.start_at, endIso: a.end_at };
  document.getElementById("activity-form-title").textContent = "活動を編集";
  document.getElementById("af-start").value = hhmm(a.start_at);
  document.getElementById("af-end").value = hhmm(a.end_at);
  document.getElementById("af-type").value = a.activity_type;
  document.getElementById("af-project").value = a.project || "";
  document.getElementById("af-task").value = a.task || "";
  showActivityForm();
}

document.getElementById("af-cancel").addEventListener("click", hideActivityForm);

document.getElementById("af-submit").addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  const type = document.getElementById("af-type").value;
  const project = document.getElementById("af-project").value || null;
  const task = document.getElementById("af-task").value || null;
  const startHHMM = document.getElementById("af-start").value;
  const endHHMM = document.getElementById("af-end").value;
  if (!type || !startHHMM || !endHHMM) { showMessage("種別・開始・終了は必須", true); return; }

  let r;
  if (state.activityForm.mode === "add") {
    r = await callApi("/api/work/add", {
      method: "POST",
      body: { date: state.date, start: startHHMM, end: endHHMM, activity_type: type, project, task },
      button: btn,
    });
  } else {
    const start = withTime(state.activityForm.startIso, startHHMM);
    const end = withTime(state.activityForm.endIso, endHHMM);
    r = await callApi(`/api/activity/${state.activityForm.id}`, {
      method: "PUT",
      body: { start, end, activity_type: type, project, task },
      button: btn,
    });
  }
  if (r.ok) {
    showMessage("記録した", false);
    hideActivityForm();
    await loadDay();
  }
});

async function deleteActivity(a, btn) {
  if (!confirm("この活動を削除する。よいか。")) return;
  const r = await callApi(`/api/activity/${a.id}`, { method: "DELETE", button: btn });
  if (r.ok) {
    showMessage("削除した", false);
    await loadDay();
  }
}

document.getElementById("build-btn").addEventListener("click", async (e) => {
  const r = await callApi("/api/build", { method: "POST", body: { date: state.date }, button: e.currentTarget });
  if (r.ok) {
    showMessage("タイムラインを作り直した", false);
    await loadDay();
  }
});

// ---------------------------------------------------------------------------
// カレンダー予定の分類（GET /api/calendar/events・PUT /api/calendar/label）
//
// カレンダーから取った予定は既定で種別 meeting のまま保存される。ここで人が種別を選び直す。
// 日付には依存しない（直近7日分をまとめて扱う）ので、loadDay() の最後から毎回取り直す。
// API 未実装（404等）でもカードごと隠して他の画面を壊さない。
// ---------------------------------------------------------------------------

async function loadCalendarEvents() {
  const r = await callApi("/api/calendar/events?days=7");
  const card = document.getElementById("calendar-card");
  if (!r.ok) {
    card.hidden = true;
    return;
  }
  const data = r.data || {};
  state.calendarEvents = data.events || [];
  // Outlook 連携をしていない人には、予定も識別子無しの件数も無い。空のカードを出さない
  if (!state.calendarEvents.length && !data.no_uid) {
    card.hidden = true;
    return;
  }
  card.hidden = false;
  renderCalendarEvents(data);
}

function renderCalendarEvents(data) {
  const events = data.events || [];
  document.getElementById("calendar-summary").textContent =
    `${mdLabel(data.start)}〜${mdLabel(data.end)} の予定 ${events.length}件（未分類 ${data.unlabeled || 0}件）`;

  const list = document.getElementById("calendar-list");
  list.innerHTML = "";
  events.forEach((ev) => list.appendChild(buildCalendarRow(ev)));

  const noUidEl = document.getElementById("calendar-no-uid");
  if (data.no_uid > 0) {
    noUidEl.hidden = false;
    noUidEl.textContent = `識別子が無い予定が ${data.no_uid}件ある。再取得で対応付けできないため種別を付けられない`;
  } else {
    noUidEl.hidden = true;
    noUidEl.textContent = "";
  }
}

function buildCalendarRow(ev) {
  const li = document.createElement("li");
  li.className = ev.labeled ? "calendar-row" : "calendar-row unlabeled";

  const time = document.createElement("span");
  time.className = "time";
  // 定期予定・日をまたぐ分割で複数回になった場合は、開始〜終了と回数を添える
  time.textContent = ev.count > 1
    ? `${mdLabel(ev.first_start)} ${hhmm(ev.first_start)}〜${mdLabel(ev.last_end)} ${hhmm(ev.last_end)}・${ev.count}回`
    : `${mdLabel(ev.first_start)} ${hhmm(ev.first_start)}`;
  li.appendChild(time);

  const info = document.createElement("span");
  info.className = "info";
  info.textContent = ev.summary || "（件名なし）";
  li.appendChild(info);

  if (!ev.labeled) {
    const flag = document.createElement("span");
    flag.className = "calendar-flag";
    flag.textContent = "（未分類）";
    li.appendChild(flag);
  }

  const select = document.createElement("select");
  currentTypeOptions().forEach((t) => {
    const opt = document.createElement("option");
    opt.value = t.value;
    opt.textContent = t.label;
    select.appendChild(opt);
  });
  select.value = ev.activity_type;
  select.addEventListener("change", () => setCalendarLabel(ev.uid, select.value, select));
  li.appendChild(select);

  if (ev.labeled) {
    const clearBtn = mkButton("解除");
    clearBtn.addEventListener("click", () => setCalendarLabel(ev.uid, null, clearBtn));
    li.appendChild(clearBtn);
  }

  return li;
}

async function setCalendarLabel(uid, activityType, control) {
  const r = await callApi("/api/calendar/label", {
    method: "PUT",
    body: { uid, activity_type: activityType },
    button: control,
  });
  if (!r.ok) return;
  const message = activityType === null
    ? `予定の分類を解除（${r.data.updated}件へ反映）`
    : `予定を「${typeLabel(activityType)}」に分類（${r.data.updated}件へ反映）`;
  showMessage(message, false);
  // タイムラインの予定表示と俯瞰図にも効くため、画面全体を取り直す
  await loadDay();
}

// --- 生ログ ---
//
// 連続する同一内容（process・window_title が同じ）は1行に集約して表示する
// （利用者指示：14:49/14:50/14:52 が python.exe・Agent Watch Terminal のままなら1行）。
// 時間の空きでは区切らない。選択・記録もこの集約後の行（グループ）単位で行う。
//
// 行の選択はグループの start_ts で管理する。取り直すたびに行は入れ替わりうるため、
// 選択は毎回リセットする（複雑な追従はしない）。

// 生ログ（events, ts 昇順）を、直前行と process・window_title が同じものだけ束ねて集約する
function groupRawLogs(events) {
  const groups = [];
  events.forEach((ev) => {
    const last = groups[groups.length - 1];
    if (last && last.process === ev.process && last.window_title === ev.window_title) {
      last.end_ts = ev.ts;
      last.count += 1;
      last.max_idle = Math.max(last.max_idle, ev.idle_sec || 0);
    } else {
      groups.push({
        start_ts: ev.ts,
        end_ts: ev.ts,
        process: ev.process,
        window_title: ev.window_title,
        count: 1,
        max_idle: ev.idle_sec || 0,
      });
    }
  });
  return groups;
}

// 生ログを取り直して描画し直す。生ログ一覧が開いているときに loadDay() からも呼ばれる
async function loadRawLogs(button) {
  const range = dayIsoRange(state.date);
  const r = await callApi(`/api/raw?start=${encodeURIComponent(range.start)}&end=${encodeURIComponent(range.end)}`, { button });
  if (!r.ok) return;
  state.rawLogs = r.data || [];
  state.rawLogGroups = groupRawLogs(state.rawLogs);
  resetRawLogSelectionState();
  renderRawLogList();
  renderRawLogSummary();
  // 今日を収集中に見ているときだけ末尾（最新）へスクロール。過去日・停止中は先頭のまま
  const list = document.getElementById("raw-log-list");
  const running = state.date === todayStr() && state.day.collector && state.day.collector.running;
  list.scrollTop = running ? list.scrollHeight : 0;
}

function renderRawLogSummary() {
  const now = new Date();
  document.getElementById("raw-log-summary").textContent =
    `生ログ ${state.rawLogs.length}件 → ${state.rawLogGroups.length}行に集約 / 最終 ${pad2(now.getHours())}:${pad2(now.getMinutes())}`;
}

function renderRawLogList() {
  const list = document.getElementById("raw-log-list");
  list.innerHTML = "";
  rawLogRowEls = [];
  const groups = state.rawLogGroups;
  if (groups.length === 0) {
    // 0件のときに空欄のままだと「壊れている」のか「無い」のか分からない
    const li = document.createElement("li");
    li.className = "empty";
    li.textContent =
      state.day.collector && state.day.collector.running
        ? "この日の生ログは0件（収集を始めた直後かもしれない）"
        : "この日の生ログは0件（collect が動いていない可能性）";
    list.appendChild(li);
  } else {
    groups.forEach((g, idx) => {
      const row = buildRawLogRow(g, idx);
      rawLogRowEls.push(row);
      list.appendChild(row.li);
    });
  }
  renderRawLogSelectionStatus();
}

function buildRawLogRow(g, idx) {
  const li = document.createElement("li");
  li.className = "raw-log-row";

  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.className = "raw-log-check";
  checkbox.checked = state.rawLogSelected.has(g.start_ts);
  // チェックの反映は行クリック側でまとめて行うため、既定動作（自前トグル）は止める
  checkbox.addEventListener("click", (e) => e.preventDefault());

  const text = document.createElement("span");
  text.className = "raw-log-text";
  // 開始と終了が同じ分なら範囲表記にしない
  const timeRange = hhmm(g.start_ts) === hhmm(g.end_ts)
    ? hhmm(g.start_ts)
    : `${hhmm(g.start_ts)}-${hhmm(g.end_ts)}`;
  const idlePart = g.max_idle ? `　(idle ${g.max_idle}s)` : "";
  text.textContent = `${timeRange}　${g.process}　${g.window_title}${idlePart}　(${g.count}件)`;

  li.appendChild(checkbox);
  li.appendChild(text);
  if (checkbox.checked) li.classList.add("selected");

  // 行のどこをクリックしても選択が切り替わる。Shift+クリックで範囲選択
  li.addEventListener("click", (e) => toggleRawLogSelection(idx, e.shiftKey));

  return { li, checkbox };
}

// 選択の切り替え。shiftKey が true かつ直前に選んだ行があれば、その行から今の行までを選択する
function toggleRawLogSelection(idx, shiftKey) {
  const rows = state.rawLogGroups;
  if (shiftKey && state.rawLogAnchor !== null) {
    const lo = Math.min(state.rawLogAnchor, idx);
    const hi = Math.max(state.rawLogAnchor, idx);
    for (let i = lo; i <= hi; i++) {
      state.rawLogSelected.add(rows[i].start_ts);
      applySelectionToRow(i);
    }
  } else {
    const ts = rows[idx].start_ts;
    if (state.rawLogSelected.has(ts)) {
      state.rawLogSelected.delete(ts);
    } else {
      state.rawLogSelected.add(ts);
    }
    applySelectionToRow(idx);
  }
  state.rawLogAnchor = idx;
  renderRawLogSelectionStatus();
}

function applySelectionToRow(idx) {
  const row = rawLogRowEls[idx];
  if (!row) return;
  const selected = state.rawLogSelected.has(state.rawLogGroups[idx].start_ts);
  row.li.classList.toggle("selected", selected);
  row.checkbox.checked = selected;
}

// 選択中のグループを start_ts 昇順（state.rawLogGroups の並びのまま）で返す
function selectedRawLogRows() {
  return state.rawLogGroups.filter((g) => state.rawLogSelected.has(g.start_ts));
}

function renderRawLogSelectionStatus() {
  const statusEl = document.getElementById("raw-log-select-status");
  const recordBtn = document.getElementById("raw-log-record-btn");
  const selected = selectedRawLogRows();
  if (selected.length === 0) {
    statusEl.textContent = "選択: なし";
    recordBtn.disabled = true;
  } else {
    const first = selected[0];
    const last = selected[selected.length - 1];
    const count = selected.reduce((sum, g) => sum + g.count, 0);
    statusEl.textContent = `選択: ${hhmm(first.start_ts)}-${hhmm(last.end_ts)}（生ログ ${count}件）`;
    recordBtn.disabled = false;
  }
}

function resetRawLogSelectionState() {
  state.rawLogSelected = new Set();
  state.rawLogAnchor = null;
}

// 「選択を解除」ボタン用。行の DOM はそのまま、選択だけ解く
function clearRawLogSelection() {
  resetRawLogSelectionState();
  rawLogRowEls.forEach((row) => {
    row.li.classList.remove("selected");
    row.checkbox.checked = false;
  });
  renderRawLogSelectionStatus();
}

// 最新サンプルの時刻に採取間隔を足した ISO 文字列を作る（最後の1サンプルぶんを終了に含めるため）
function endIsoWithInterval(lastTs, intervalSec) {
  const d = new Date(lastTs);
  d.setSeconds(d.getSeconds() + intervalSec);
  return withTime(lastTs, `${pad2(d.getHours())}:${pad2(d.getMinutes())}`);
}

document.getElementById("raw-log-toggle").addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  const panel = document.getElementById("raw-log-panel");
  if (!panel.hidden) {
    panel.hidden = true;
    btn.textContent = "生ログを表示 ▾";
    return;
  }
  await loadRawLogs(btn);
  panel.hidden = false;
  btn.textContent = "生ログを隠す ▴";
});

document.getElementById("raw-log-clear-btn").addEventListener("click", clearRawLogSelection);

document.getElementById("raw-log-record-btn").addEventListener("click", () => {
  const selected = selectedRawLogRows();
  if (selected.length === 0) return;
  const first = selected[0];
  const last = selected[selected.length - 1];
  const intervalSec = (state.day.collector && state.day.collector.interval_sec) || 5;
  openActivityAddForm(first.start_ts, endIsoWithInterval(last.end_ts, intervalSec));
  // openActivityAddForm は見出しを「空白を記録」にするため、ここだけ上書きする（本体は変更しない）
  document.getElementById("activity-form-title").textContent = "生ログから記録";
});

// ---------------------------------------------------------------------------
// 変化
// ---------------------------------------------------------------------------

function resetChangeForm() {
  document.getElementById("ch-id").value = "";
  document.getElementById("change-form").reset();
  document.getElementById("ch-submit").textContent = "記録";
  document.getElementById("ch-cancel").hidden = true;
  state.changeEditTs = null;
}

function openChangeEdit(c) {
  document.getElementById("ch-id").value = c.id;
  document.getElementById("ch-description").value = c.description;
  document.getElementById("ch-project").value = c.project || "";
  document.getElementById("ch-time").value = hhmm(c.ts);
  document.getElementById("ch-submit").textContent = "更新";
  document.getElementById("ch-cancel").hidden = false;
  state.changeEditTs = c.ts;
}

function renderChanges() {
  const list = document.getElementById("change-list");
  list.innerHTML = "";
  (state.day.changes || []).forEach((c) => {
    const li = document.createElement("li");
    const span = document.createElement("span");
    // 時刻・案件・内容の順。案件が無い行でも位置がそろうよう「（未指定）」を出す
    span.textContent = `${hhmm(c.ts)}　${c.project || "（未指定）"}　${c.description}`;
    li.appendChild(span);
    const editBtn = mkButton("編集");
    editBtn.addEventListener("click", () => openChangeEdit(c));
    const delBtn = mkButton("削除");
    delBtn.addEventListener("click", () => deleteChange(c, delBtn));
    li.appendChild(editBtn);
    li.appendChild(delBtn);
    list.appendChild(li);
  });
}

document.getElementById("change-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = document.getElementById("ch-submit");
  const id = document.getElementById("ch-id").value;
  const description = document.getElementById("ch-description").value.trim();
  const project = document.getElementById("ch-project").value || null;
  const time = document.getElementById("ch-time").value || null;
  if (!description) { showMessage("説明を入力する", true); return; }

  let r;
  if (id) {
    const ts = time ? withTime(state.changeEditTs, time) : state.changeEditTs;
    r = await callApi(`/api/change/${id}`, { method: "PUT", body: { description, project, ts }, button: btn });
  } else {
    r = await callApi("/api/change", { method: "POST", body: { description, project, date: state.date, time }, button: btn });
  }
  if (r.ok) {
    showMessage(id ? "更新した" : "記録した", false);
    resetChangeForm();
    await loadDay();
  }
});

document.getElementById("ch-cancel").addEventListener("click", resetChangeForm);

async function deleteChange(c, btn) {
  if (!confirm("この変化を削除する。よいか。")) return;
  const r = await callApi(`/api/change/${c.id}`, { method: "DELETE", button: btn });
  if (r.ok) {
    showMessage("削除した", false);
    await loadDay();
  }
}

// ---------------------------------------------------------------------------
// 判断
// ---------------------------------------------------------------------------

function resetDecisionForm() {
  document.getElementById("de-id").value = "";
  document.getElementById("decision-form").reset();
  document.getElementById("de-submit").textContent = "記録";
  document.getElementById("de-cancel").hidden = true;
  state.decisionEditTs = null;
}

function openDecisionEdit(d) {
  document.getElementById("de-id").value = d.id;
  document.getElementById("de-decision").value = d.decision;
  document.getElementById("de-reason").value = d.reason || "";
  document.getElementById("de-project").value = d.project || "";
  document.getElementById("de-time").value = hhmm(d.ts);
  document.getElementById("de-submit").textContent = "更新";
  document.getElementById("de-cancel").hidden = false;
  state.decisionEditTs = d.ts;
}

function renderDecisions() {
  const list = document.getElementById("decision-list");
  list.innerHTML = "";
  (state.day.decisions || []).forEach((d) => {
    const li = document.createElement("li");
    const span = document.createElement("span");
    const reasonPart = d.reason ? `（${d.reason}）` : "";
    span.textContent = `${hhmm(d.ts)}　${d.project || "（未指定）"}　${d.decision}${reasonPart}`;
    li.appendChild(span);
    const editBtn = mkButton("編集");
    editBtn.addEventListener("click", () => openDecisionEdit(d));
    const delBtn = mkButton("削除");
    delBtn.addEventListener("click", () => deleteDecision(d, delBtn));
    li.appendChild(editBtn);
    li.appendChild(delBtn);
    list.appendChild(li);
  });
}

document.getElementById("decision-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = document.getElementById("de-submit");
  const id = document.getElementById("de-id").value;
  const decision = document.getElementById("de-decision").value.trim();
  const reason = document.getElementById("de-reason").value || null;
  const project = document.getElementById("de-project").value || null;
  const time = document.getElementById("de-time").value || null;
  if (!decision) { showMessage("判断を入力する", true); return; }

  let r;
  if (id) {
    const ts = time ? withTime(state.decisionEditTs, time) : state.decisionEditTs;
    r = await callApi(`/api/decision/${id}`, { method: "PUT", body: { decision, reason, project, ts }, button: btn });
  } else {
    r = await callApi("/api/decision", { method: "POST", body: { decision, reason, project, date: state.date, time }, button: btn });
  }
  if (r.ok) {
    showMessage(id ? "更新した" : "記録した", false);
    resetDecisionForm();
    await loadDay();
  }
});

document.getElementById("de-cancel").addEventListener("click", resetDecisionForm);

async function deleteDecision(d, btn) {
  if (!confirm("この判断を削除する。よいか。")) return;
  const r = await callApi(`/api/decision/${d.id}`, { method: "DELETE", button: btn });
  if (r.ok) {
    showMessage("削除した", false);
    await loadDay();
  }
}

// ---------------------------------------------------------------------------
// 現在の状態（判断エンジン向けスナップショット。python app/cf.py state と同じ処理）
// ---------------------------------------------------------------------------

// 表示中の日付の状態を読み込む（GET /api/state）。まだ無い日は state が null で返る
async function loadState() {
  const r = await callApi(`/api/state?date=${encodeURIComponent(state.date)}`);
  if (!r.ok) return; // 通信失敗は callApi 側のバナーのみ。ここでは二重に出さない
  state.currentState = (r.data && r.data.state) || null;
  state.currentStateGeneratedAt = (r.data && r.data.generated_at) || null;
  state.currentStatePath = (r.data && r.data.path) || null;
  renderStateCard();
}

// 状態を作り直す（POST /api/state）
async function runState(button) {
  const r = await callApi("/api/state", { method: "POST", body: { date: state.date }, button });
  if (!r.ok) return;
  state.currentState = r.data.state || null;
  state.currentStateGeneratedAt = r.data.generated_at || null;
  state.currentStatePath = r.data.path || null;
  renderStateCard();
  const at = state.currentStateGeneratedAt;
  showMessage("今の状態をまとめた: " + (at ? `${mdLabel(at)} ${hhmm(at)}` : "-"), false);
}

// キー・値の1行を state-body へ追加する（value は文字列 or Node）
function addStateRow(container, keyText, value) {
  const row = document.createElement("div");
  row.className = "state-row";
  const key = document.createElement("span");
  key.className = "state-key";
  key.textContent = keyText;
  const val = document.createElement("span");
  val.className = "state-val";
  if (typeof value === "string") {
    val.textContent = value;
  } else {
    val.appendChild(value);
  }
  row.appendChild(key);
  row.appendChild(val);
  container.appendChild(row);
}

// 複数行のテキストを div で積んだ Node を作る（state-val の中身用）
function buildStateLines(lines) {
  const wrap = document.createElement("div");
  lines.forEach((text) => {
    const line = document.createElement("div");
    line.textContent = text;
    wrap.appendChild(line);
  });
  return wrap;
}

// 文字列配列を箇条書き（ul）にする（state-val の中身用）
function buildStateList(items) {
  const ul = document.createElement("ul");
  ul.className = "state-list";
  items.forEach((text) => {
    const li = document.createElement("li");
    li.textContent = text;
    ul.appendChild(li);
  });
  return ul;
}

// 過去N日の傾向（PastSummary）を「時間」行と同じ書式の行配列にする
function buildPastLines(past) {
  if (!past.days || !past.total_min) return ["記録なし"];
  const byProject = Object.entries(past.by_project || {}).sort((a, b) => b[1] - a[1]);
  const byType = Object.entries(past.by_type || {}).sort((a, b) => b[1] - a[1]);
  return [
    `${mdLabel(past.start_date)}〜${mdLabel(past.end_date)}　合計 ${fmtMinutes(past.total_min)}　稼働 ${past.active_days || 0}日`,
    byProject.length ? `案件　${byProject.map(([k, v]) => `${k} ${fmtMinutes(v)}`).join("、")}` : "案件　-",
    byType.length ? `種別　${byType.map(([k, v]) => `${typeLabel(k)} ${fmtMinutes(v)}`).join("、")}` : "種別　-",
    `deep work ${fmtMinutes(past.deep_work_min || 0)}　/　切替 ${past.context_switches != null ? past.context_switches : "-"}回　/　変化 ${past.change_count || 0}件　/　判断 ${past.decision_count || 0}件`,
  ];
}

// 今後N日の予定・締切（UpcomingSummary）を行配列にする。予定は最大5件、締切は最大3件
function buildUpcomingLines(upcoming) {
  const items = upcoming.items || [];
  const deadlines = upcoming.deadlines || [];
  if (!items.length && !deadlines.length) return ["予定なし"];

  const lines = [];
  const byType = Object.entries(upcoming.by_type || {}).sort((a, b) => b[1] - a[1]);
  const typeSummary = byType.length ? byType.map(([k, v]) => `${typeLabel(k)} ${fmtMinutes(v)}`).join("、") : "-";
  lines.push(`予定 ${fmtMinutes(upcoming.planned_min || 0)}　/　${typeSummary}`);

  items.slice(0, 5).forEach((it) => {
    const summary = it.summary || "（件名なし）";
    lines.push(`${mdLabel(it.start_at)} ${hhmm(it.start_at)}-${hhmm(it.end_at)} ${typeLabel(it.activity_type)}　「${summary}」`);
  });
  if (items.length > 5) lines.push(`ほか ${items.length - 5}件`);

  deadlines.slice(0, 3).forEach((d) => {
    const blockedPart = d.blocked ? "・待ち" : "";
    const priority = d.priority != null ? d.priority : "-";
    lines.push(`締切 ${mdLabel(d.deadline)} ${d.title}（${d.project || "-"}・優先度${priority}${blockedPart}）`);
  });

  return lines;
}

function renderStateCard() {
  const generatedEl = document.getElementById("state-generated");
  const emptyEl = document.getElementById("state-empty");
  const bodyEl = document.getElementById("state-body");
  const pathEl = document.getElementById("state-path");
  const s = state.currentState;

  if (!s) {
    generatedEl.textContent = "";
    emptyEl.hidden = false;
    bodyEl.hidden = true;
    bodyEl.innerHTML = "";
    pathEl.hidden = true;
    pathEl.textContent = "";
    return;
  }

  emptyEl.hidden = true;
  bodyEl.hidden = false;
  bodyEl.innerHTML = "";

  const at = state.currentStateGeneratedAt;
  generatedEl.textContent = at
    ? `作成: ${mdLabel(at)} ${hhmm(at)} 時点（対象日 ${mdLabel(s.target_date || state.date)}）`
    : "";

  // 時間：合計・種別別（分の多い順）・案件別
  const today = s.today || {};
  const byType = Object.entries(today.by_type || {}).sort((a, b) => b[1] - a[1]);
  const byProject = Object.entries(today.by_project || {}).sort((a, b) => b[1] - a[1]);
  addStateRow(bodyEl, "時間", buildStateLines([
    `合計 ${fmtMinutes(today.total_min || 0)}`,
    byProject.length ? `案件　${byProject.map(([k, v]) => `${k} ${fmtMinutes(v)}`).join("、")}` : "案件　-",
    byType.length ? `種別　${byType.map(([k, v]) => `${typeLabel(k)} ${fmtMinutes(v)}`).join("、")}` : "種別　-",
  ]));

  // 集中：deep work・切替回数・最長集中・稼働・アイドル・最後の休憩
  const f = s.features || {};
  addStateRow(bodyEl, "集中", [
    `deep work ${fmtMinutes(f.deep_work_min || 0)}`,
    `切替 ${f.context_switches != null ? f.context_switches : "-"}回`,
    `最長集中 ${fmtMinutes(f.longest_focus_min || 0)}`,
    `稼働 ${fmtMinutes(f.active_min || 0)}`,
    `アイドル ${fmtMinutes(f.idle_min || 0)}`,
    `最後の休憩 ${f.last_break_min_ago != null ? f.last_break_min_ago + "分前" : "-"}`,
  ].join("　/　"));

  // 現在：現在の活動・現在のタスクと経過分
  const ca = s.current_activity;
  const ct = s.current_task;
  addStateRow(bodyEl, "現在", buildStateLines([
    ca ? `活動　${ca.project || "-"} / ${typeLabel(ca.activity_type)} / ${ca.task || "-"}　${hhmm(ca.start_at)}-${hhmm(ca.end_at)}` : "活動　-",
    ct ? `タスク　${ct.title}（経過 ${fmtMinutes(s.task_elapsed_min || 0)}）` : "タスク　-",
  ]));

  // タスク：未完了/blocked件数・候補タスク（最大5件）
  const taskWrap = document.createElement("div");
  const taskSummary = document.createElement("div");
  taskSummary.textContent = `未完了 ${s.open_tasks != null ? s.open_tasks : 0}件 / blocked ${s.blocked_tasks != null ? s.blocked_tasks : 0}件`;
  taskWrap.appendChild(taskSummary);
  const candidates = (s.candidate_tasks || []).slice(0, 5);
  if (candidates.length) {
    taskWrap.appendChild(buildStateList(candidates.map((t) =>
      `${t.title}（優先度${t.priority != null ? t.priority : "-"}・期限${t.deadline ? mdLabel(t.deadline) : "-"}）`
    )));
  } else {
    const none = document.createElement("div");
    none.textContent = "候補タスク　-";
    taskWrap.appendChild(none);
  }
  addStateRow(bodyEl, "タスク", taskWrap);

  // 過去N日の傾向（案件・種別の順は「時間」行と合わせる。分の多い順）
  const past = s.past || {};
  addStateRow(bodyEl, `過去${past.days || 0}日`, buildStateLines(buildPastLines(past)));

  // 今後N日の予定・締切
  const upcoming = s.upcoming || {};
  addStateRow(bodyEl, `今後${upcoming.days || 0}日`, buildStateLines(buildUpcomingLines(upcoming)));

  // 直近の変化
  const changes = s.recent_changes || [];
  addStateRow(bodyEl, "直近の変化", changes.length
    ? buildStateList(changes.map((c) => `${mdLabel(c.ts)} ${hhmm(c.ts)} ${c.description}${c.project ? `（${c.project}）` : ""}`))
    : "-");

  // 直近の判断
  const decisions = s.recent_decisions || [];
  addStateRow(bodyEl, "直近の判断", decisions.length
    ? buildStateList(decisions.map((d) => {
        const reasonPart = d.reason ? `（理由: ${d.reason}）` : "";
        const projectPart = d.project ? `（${d.project}）` : "";
        return `${mdLabel(d.ts)} ${hhmm(d.ts)} ${d.decision}${reasonPart}${projectPart}`;
      }))
    : "-");

  // 制約（無ければ行ごと省略）
  const constraints = s.constraints || [];
  if (constraints.length) {
    addStateRow(bodyEl, "制約", buildStateList(constraints));
  }

  pathEl.hidden = false;
  pathEl.textContent = `保存先: ${state.currentStatePath || "-"} と DB の state_snapshots`;
}

document.getElementById("state-run-btn").addEventListener("click", (e) => runState(e.currentTarget));

// ---------------------------------------------------------------------------
// 判断・計画（GET /api/decide/info・POST /api/decide・POST /api/plan）
//
// 押すたびに「現在の状態」が組み立て直されて保存されるため、成功時は loadState() も呼び直す。
// ---------------------------------------------------------------------------

// 判断・計画エンジンの情報を読み込む。日付に依存しないので init() から1回だけ呼ぶ。
// API が無い（404 等）場合は、判断・計画のまとまりごと隠して他の画面を壊さない
async function loadDecideInfo() {
  const r = await callApi("/api/decide/info");
  if (!r.ok) {
    document.getElementById("decide-box").hidden = true;
    return;
  }
  state.decideInfo = r.data;
  document.getElementById("decide-box").hidden = false;

  const setSelect = document.getElementById("decide-set");
  setSelect.innerHTML = "";
  (r.data.sets || []).forEach((s) => {
    const opt = document.createElement("option");
    opt.value = s.name;            // 保存値は英語のセット名のまま
    opt.textContent = s.label || s.name;
    setSelect.appendChild(opt);
  });
  setSelect.value = r.data.default_set || "";

  const info = r.data;
  const chain = (info.engines || []).join(" → ") || "-";
  document.getElementById("decide-engine").textContent =
    `エンジン: ${chain}　計画: ${info.planner || "-"}`
    + (info.sends_external ? "（外部送信あり）" : "（ローカルのみ）");
  const warn = document.getElementById("decide-llm-warn");
  warn.hidden = !info.sends_external;
  warn.textContent = externalNote(info);
}

// 外部へ問い合わせる先を、設定から組み立てて文章にする。
// 固定文言にすると、運用方針を変えたとき（jev など）に実態と食い違う
function externalNote(info) {
  const parts = [];
  if ((info.external_engines || []).length) parts.push(`判断は ${info.external_engines.join("・")}`);
  if (info.planner_external) parts.push(`計画は ${info.planner}`);
  return `${parts.join("、")} へ問い合わせる（外部送信と費用が発生する）`;
}

// 判断（withPlan=false）または計画（withPlan=true）を実行する
async function runDecide(button, withPlan) {
  const info = state.decideInfo;
  if (info && info.sends_external) {
    if (!confirm(`${externalNote(info)}
実行するか？`)) return;
  }
  const set = document.getElementById("decide-set").value;
  const path = withPlan ? "/api/plan" : "/api/decide";
  const r = await callApi(path, { method: "POST", body: { date: state.date, set }, button });
  if (!r.ok) return;
  renderDecideResult(r.data);
  if (withPlan) renderPlanResult(r.data);
  // 判断のたびに「現在の状態」が組み立て直されて保存されるため、カードを更新する
  await loadState();
  const label = withPlan ? "計画" : "判断";
  showMessage(`${label}: ${r.data.engine}（${r.data.latency_ms}ms）`, false);
}

// 判断結果（decide・plan 共通の answers 部分）を描く
// 質問セットの表示名。設定に無ければ保存値をそのまま返す
function questionSetLabel(name) {
  const sets = (state.decideInfo && state.decideInfo.sets) || [];
  const found = sets.find((s) => s.name === name);
  return (found && found.label) || name;
}

// choice 型の答えの表示名。
// 質問ごとの設定 → 種別のラベル → 保存値 の順に探す
// （gap_activity_type のように選択肢が種別そのものの質問は、種別のラベルで日本語になる）
function choiceLabel(questionKey, value) {
  const all = (state.decideInfo && state.decideInfo.choice_labels) || {};
  const perQuestion = all[questionKey];
  if (perQuestion && perQuestion[value]) return perQuestion[value];
  return typeLabel(value);
}

function renderDecideResult(data) {
  const box = document.getElementById("decide-result");
  box.hidden = false;
  box.innerHTML = "";

  const header = document.createElement("p");
  header.className = "hint";
  header.textContent = [
    `質問セット ${questionSetLabel(data.set)}`,
    `エンジン ${data.engine}`,
    `${data.latency_ms}ms`,
    `材料 ${mdLabel(data.generated_at)} ${hhmm(data.generated_at)} 時点`,
  ].join(" ／ ");
  box.appendChild(header);

  (data.answers || []).forEach((a) => {
    const row = document.createElement("div");
    row.className = a.held ? "decide-row held" : "decide-row";
    const keyPart = a.instruction ? `${a.key}（${a.instruction}）` : a.key;
    let valueText;
    if (a.value === true) valueText = "はい";
    else if (a.value === false) valueText = "いいえ";
    else if (a.value === null || a.value === undefined) valueText = "不明";
    else if (a.type === "choice") valueText = choiceLabel(a.key, a.value);
    else valueText = String(a.value);
    const parts = [keyPart, valueText];
    if (a.held) parts.push("保留");
    if (a.confidence != null) parts.push(`conf ${a.confidence}`);
    if (a.rationale) parts.push(a.rationale);
    row.textContent = parts.join("　/　");
    box.appendChild(row);
  });

  const note = document.createElement("p");
  note.className = "hint";
  note.textContent = `conf < ${data.threshold} の項目は保留扱い`;
  box.appendChild(note);
}

// 計画結果（plan の text。Markdown のまま <pre> へ入れる。HTML へは変換しない）を描く
function renderPlanResult(data) {
  const box = document.getElementById("plan-result");
  box.innerHTML = "";
  if (!data || !data.text) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  const pre = document.createElement("pre");
  pre.textContent = data.text;
  box.appendChild(pre);
}

document.getElementById("decide-btn").addEventListener("click", (e) => runDecide(e.currentTarget, false));
document.getElementById("plan-btn").addEventListener("click", (e) => runDecide(e.currentTarget, true));

// ---------------------------------------------------------------------------
// 設定パネル（種別・案件のラベル/表示/並び順、タスク候補の編集）
//
// 編集中の内容は state.settingsDraft（ワーキングコピー）に持ち、
// 保存・取り消しのときだけ現在値（state.day.options / state.projects）から作り直す。
// 他の操作（作業の開始停止・変化の記録など）で loadDay() が走っても、
// 編集中の内容を巻き込んで消さないため。
//
// 種別は ActivityType の全体（state.day.activity_types、既存のまま残る文字列配列）、
// 案件はリポジトリの全体（state.projects、/api/projects）を土台にし、
// options（表示対象の一覧）に無いものは「非表示」として扱う。
// こうすると、隠した種別・案件も設定パネルには残り、チェックを戻せば再表示できる。
// ---------------------------------------------------------------------------

function buildSettingsDraft() {
  const opts = (state.day && state.day.options) || {};

  const visibleTypes = opts.activity_types || [];
  const allTypeKeys = (state.day && state.day.activity_types) || [];
  const typeOrder = visibleTypes.map((t) => t.value);
  allTypeKeys.forEach((key) => {
    if (!typeOrder.includes(key)) typeOrder.push(key);
  });
  const types = typeOrder.map((key) => {
    const found = visibleTypes.find((t) => t.value === key);
    return { key, label: found ? found.label : key, visible: !!found };
  });

  const visibleProjects = opts.projects || [];
  const repoKeys = (state.projects || []).map((p) => p.key);
  const projectOrder = visibleProjects.map((p) => p.key);
  repoKeys.forEach((key) => {
    if (!projectOrder.includes(key)) projectOrder.push(key);
  });
  const projects = projectOrder.map((key) => {
    const found = visibleProjects.find((p) => p.key === key);
    const fromRepo = found ? found.from_repo !== false : repoKeys.includes(key);
    return { key, label: found ? found.label : key, visible: !!found, fromRepo };
  });

  const taskSuggestions = (opts.task_suggestions || []).slice();

  return { types, projects, taskSuggestions };
}

// 現在値（サーバから読んだ最新）へ作り直して描画し直す。保存成功後・取り消し時に使う
function resetSettingsDraft() {
  state.settingsDraft = buildSettingsDraft();
  renderSettingsPanel();
}

// 配列内の要素を1つ動かす（[▲][▼] 用）。範囲外なら何もしない
function moveSettingsItem(array, idx, delta) {
  const target = idx + delta;
  if (target < 0 || target >= array.length) return;
  const tmp = array[idx];
  array[idx] = array[target];
  array[target] = tmp;
}

function buildSettingsTypeRow(item, idx, total) {
  const li = document.createElement("li");
  li.className = "settings-row";

  const visible = document.createElement("input");
  visible.type = "checkbox";
  visible.title = "表示する";
  visible.checked = item.visible;
  visible.addEventListener("change", () => { item.visible = visible.checked; });
  li.appendChild(visible);

  const key = document.createElement("span");
  key.className = "settings-key";
  key.textContent = item.key;
  li.appendChild(key);

  const label = document.createElement("input");
  label.type = "text";
  label.className = "settings-label-input";
  label.value = item.label;
  label.addEventListener("input", () => { item.label = label.value; });
  li.appendChild(label);

  const upBtn = mkButton("▲");
  upBtn.disabled = idx === 0;
  upBtn.addEventListener("click", () => { moveSettingsItem(state.settingsDraft.types, idx, -1); renderSettingsTypes(); });
  const downBtn = mkButton("▼");
  downBtn.disabled = idx === total - 1;
  downBtn.addEventListener("click", () => { moveSettingsItem(state.settingsDraft.types, idx, 1); renderSettingsTypes(); });
  li.appendChild(upBtn);
  li.appendChild(downBtn);

  return li;
}

function buildSettingsProjectRow(item, idx, total) {
  const li = document.createElement("li");
  li.className = "settings-row";

  const visible = document.createElement("input");
  visible.type = "checkbox";
  visible.title = "表示する";
  visible.checked = item.visible;
  visible.addEventListener("change", () => { item.visible = visible.checked; });
  li.appendChild(visible);

  const key = document.createElement("span");
  key.className = "settings-key";
  // リポジトリ外（表示用に追加した）案件は、書き出し先が無いと分かるよう明記する
  key.textContent = item.fromRepo ? item.key : `${item.key}（リポジトリ外）`;
  li.appendChild(key);

  const label = document.createElement("input");
  label.type = "text";
  label.className = "settings-label-input";
  label.value = item.label;
  label.addEventListener("input", () => { item.label = label.value; });
  li.appendChild(label);

  const upBtn = mkButton("▲");
  upBtn.disabled = idx === 0;
  upBtn.addEventListener("click", () => { moveSettingsItem(state.settingsDraft.projects, idx, -1); renderSettingsProjects(); });
  const downBtn = mkButton("▼");
  downBtn.disabled = idx === total - 1;
  downBtn.addEventListener("click", () => { moveSettingsItem(state.settingsDraft.projects, idx, 1); renderSettingsProjects(); });
  li.appendChild(upBtn);
  li.appendChild(downBtn);

  return li;
}

function renderSettingsTypes() {
  const list = document.getElementById("settings-type-list");
  list.innerHTML = "";
  state.settingsDraft.types.forEach((item, idx) => {
    list.appendChild(buildSettingsTypeRow(item, idx, state.settingsDraft.types.length));
  });
}

function renderSettingsProjects() {
  const list = document.getElementById("settings-project-list");
  list.innerHTML = "";
  state.settingsDraft.projects.forEach((item, idx) => {
    list.appendChild(buildSettingsProjectRow(item, idx, state.settingsDraft.projects.length));
  });
}

function renderSettingsTasks() {
  document.getElementById("settings-task-textarea").value = state.settingsDraft.taskSuggestions.join("\n");
}

// 種別・案件の並び替え（[▲][▼]）だけを再描画する関数を分けているのは、
// タスク候補のテキストエリアを巻き込んで、入力途中の内容を消さないため
function renderSettingsPanel() {
  if (!state.settingsDraft) return;
  renderSettingsTypes();
  renderSettingsProjects();
  renderSettingsTasks();
}

document.getElementById("settings-project-add-btn").addEventListener("click", () => {
  const input = document.getElementById("settings-project-new");
  const name = input.value.trim();
  if (!name) { showMessage("追加する案件名を入力する", true); return; }
  if (state.settingsDraft.projects.some((p) => p.key === name)) {
    showMessage("同じ名前の案件が既にある", true);
    return;
  }
  // リポジトリには作らない、表示専用の案件として追加する（キー・ラベルとも入力値をそのまま使う）
  state.settingsDraft.projects.push({ key: name, label: name, visible: true, fromRepo: false });
  input.value = "";
  renderSettingsProjects();
});

document.getElementById("settings-cancel-btn").addEventListener("click", () => {
  resetSettingsDraft();
});

document.getElementById("settings-save-btn").addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  const draft = state.settingsDraft;

  const typeLabels = {};
  const typeOrder = [];
  const typeHidden = [];
  draft.types.forEach((item) => {
    typeLabels[item.key] = item.label;
    typeOrder.push(item.key);
    if (!item.visible) typeHidden.push(item.key);
  });

  const projectLabels = {};
  const projectOrder = [];
  const projectHidden = [];
  const projectExtra = [];
  draft.projects.forEach((item) => {
    projectLabels[item.key] = item.label;
    projectOrder.push(item.key);
    if (!item.visible) projectHidden.push(item.key);
    if (!item.fromRepo) projectExtra.push(item.key);
  });

  const taskSuggestions = document
    .getElementById("settings-task-textarea")
    .value.split("\n")
    .map((line) => line.trim())
    .filter((line) => line);

  const body = {
    activity_types: { labels: typeLabels, order: typeOrder, hidden: typeHidden },
    projects: { labels: projectLabels, order: projectOrder, hidden: projectHidden, extra: projectExtra },
    tasks: { suggestions: taskSuggestions },
  };

  const r = await callApi("/api/options", { method: "PUT", body, button: btn });
  if (r.ok) {
    showMessage("設定を保存した", false);
    await loadDay();
    resetSettingsDraft();
  }
});

// ---------------------------------------------------------------------------
// 日データの読み込み
// ---------------------------------------------------------------------------

async function loadDay() {
  const r = await callApi(`/api/day?date=${encodeURIComponent(state.date)}`);
  if (!r.ok) {
    // 取れなかった状態を、取れた最後の値で代弁しない
    renderStatusUnknown();
    return;
  }
  state.day = r.data;
  populateTypeSelects(currentTypeOptions());
  populateProjectSelects();
  populateTaskDatalist();
  renderRunning();
  renderStatusStrip();
  renderTimeline();
  renderChanges();
  renderDecisions();
  // 生ログ一覧が開いていれば、収集の続きが見えるよう取り直す
  if (!document.getElementById("raw-log-panel").hidden) {
    await loadRawLogs();
  }
  // 現在の状態カードも、表示中の日付に合わせて取り直す
  await loadState();
  // カレンダー予定の分類も、収集や取り込みの後に押し直せば最新になるようここで取り直す
  await loadCalendarEvents();
}

// ---------------------------------------------------------------------------
// 初期化
// ---------------------------------------------------------------------------

async function init() {
  state.token = document.querySelector('meta[name="cf-token"]').content;
  state.date = todayStr();
  document.getElementById("date-input").value = state.date;
  // 設定パネルの土台（案件の全体像）に state.projects を使うため、両方そろってから組み立てる
  await Promise.all([loadProjects(), loadDay()]);
  resetSettingsDraft();
  // 判断・計画のエンジン情報は日付に依存しないので、ここで1回だけ読み込む
  await loadDecideInfo();
}

init();
