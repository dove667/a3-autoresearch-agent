/* Read-only A3 research console. One page per view; no writes are sent. */

const PAGES = {
  overview: "总览",
  plan: "研究计划",
  jobs: "远程作业",
  reviews: "结果复盘",
  memory: "长期记忆",
  skills: "方法技能",
  activity: "运行记录",
};

const state = {
  snapshot: null,
  run: new URLSearchParams(location.search).get("run") || "latest",
  page: "overview",
  memoryFilter: "effective",
  memoryQuery: "",
  resetMemoryScroll: false,
  selectedRoute: null,
  signatures: {},
  dialog: null,
  detailSignature: null,
  loading: false,
};

const $ = (id) => document.getElementById(id);
const TONES = new Set(["active", "wait", "alert", "paused", "idle", "done"]);

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[character]);
}

function toneClass(tone) {
  return TONES.has(tone) ? `tone-${tone}` : "tone-idle";
}

function pill(label, tone) {
  return `<span class="pill ${toneClass(tone)}">${esc(label)}</span>`;
}

function short(text, limit = 80) {
  const value = String(text ?? "").replace(/\s+/g, " ").trim();
  return value.length > limit ? `${value.slice(0, limit - 1)}…` : value;
}

function relative(epoch) {
  if (!epoch) return "—";
  const seconds = Math.max(0, Date.now() / 1000 - epoch);
  if (seconds < 45) return "刚刚";
  if (seconds < 3600) return `${Math.max(1, Math.round(seconds / 60))} 分钟前`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} 小时前`;
  return `${Math.round(seconds / 86400)} 天前`;
}

function absolute(epoch) {
  if (!epoch) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(epoch * 1000));
}

function clock(epoch) {
  if (!epoch) return "—";
  const date = new Date(epoch * 1000);
  const now = new Date();
  if (date.toDateString() === now.toDateString()) {
    return new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit" }).format(date);
  }
  return new Intl.DateTimeFormat("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function preciseClock(epoch) {
  if (!epoch) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(epoch * 1000));
}

function duration(seconds) {
  if (seconds == null) return "—";
  if (seconds < 60) return "不到 1 分钟";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} 分钟`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  if (hours < 24) return rest ? `${hours} 小时 ${rest} 分` : `${hours} 小时`;
  const days = Math.floor(hours / 24);
  return `${days} 天 ${hours % 24} 小时`;
}

function runLabel(id) {
  const match = /^(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})/.exec(id || "");
  if (!match) return id || "—";
  return `${match[1]}年${Number(match[2])}月${Number(match[3])}日 ${match[4]}:${match[5]}`;
}

function empty(title, text) {
  return `<div class="empty"><strong>${esc(title)}</strong>${esc(text)}</div>`;
}

function inlineMarkdown(text) {
  return text
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, (match, label, url) => {
      if (!/^https?:\/\//.test(url)) return label;
      return `<a href="${url}" target="_blank" rel="noreferrer">${label}</a>`;
    });
}

function tableCells(line) {
  const value = line.trim().replace(/^\|/, "").replace(/\|$/, "");
  return value.split("|").map((cell) => cell.trim());
}

function renderMarkdown(source) {
  const lines = esc(source || "").split("\n");
  const output = [];
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (/^\s*```/.test(line)) {
      const language = line.replace(/^\s*```/, "").trim();
      const code = [];
      index += 1;
      while (index < lines.length && !/^\s*```/.test(lines[index])) {
        code.push(lines[index]);
        index += 1;
      }
      index += 1;
      output.push(
        `<pre><code class="${esc(language)}">${code.join("\n")}</code></pre>`,
      );
      continue;
    }
    if (
      /^\s*\|/.test(line) &&
      index + 1 < lines.length &&
      /^\s*\|?[\s:|-]+\|/.test(lines[index + 1])
    ) {
      const headers = tableCells(line);
      index += 2;
      const rows = [];
      while (index < lines.length && /^\s*\|/.test(lines[index])) {
        rows.push(tableCells(lines[index]));
        index += 1;
      }
      output.push(
        `<table><thead><tr>${headers
          .map((cell) => `<th>${inlineMarkdown(cell)}</th>`)
          .join("")}</tr></thead><tbody>${rows
          .map(
            (row) =>
              `<tr>${row.map((cell) => `<td>${inlineMarkdown(cell)}</td>`).join("")}</tr>`,
          )
          .join("")}</tbody></table>`,
      );
      continue;
    }
    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    if (heading) {
      const level = Math.min(5, heading[1].length);
      output.push(`<h${level}>${inlineMarkdown(heading[2])}</h${level}>`);
      index += 1;
      continue;
    }
    if (/^\s*([-*]|\d+\.)\s+/.test(line)) {
      const ordered = /^\s*\d+\.\s+/.test(line);
      const items = [];
      while (index < lines.length && /^\s*([-*]|\d+\.)\s+/.test(lines[index])) {
        items.push(inlineMarkdown(lines[index].replace(/^\s*([-*]|\d+\.)\s+/, "")));
        index += 1;
      }
      const tag = ordered ? "ol" : "ul";
      output.push(`<${tag}>${items.map((item) => `<li>${item}</li>`).join("")}</${tag}>`);
      continue;
    }
    if (/^\s*>\s?/.test(line)) {
      const quote = [];
      while (index < lines.length && /^\s*>\s?/.test(lines[index])) {
        quote.push(lines[index].replace(/^\s*>\s?/, ""));
        index += 1;
      }
      output.push(`<blockquote>${inlineMarkdown(quote.join(" "))}</blockquote>`);
      continue;
    }
    if (/^\s*---+\s*$/.test(line)) {
      output.push("<hr />");
      index += 1;
      continue;
    }
    if (!line.trim()) {
      index += 1;
      continue;
    }
    const paragraph = [line];
    index += 1;
    while (
      index < lines.length &&
      lines[index].trim() &&
      !/^(#{1,6})\s+/.test(lines[index]) &&
      !/^\s*([-*]|\d+\.)\s+/.test(lines[index]) &&
      !/^\s*>\s?/.test(lines[index]) &&
      !/^\s*```/.test(lines[index]) &&
      !/^\s*\|/.test(lines[index]) &&
      !/^\s*---+\s*$/.test(lines[index])
    ) {
      paragraph.push(lines[index]);
      index += 1;
    }
    output.push(`<p>${paragraph.map(inlineMarkdown).join("<br />")}</p>`);
  }
  return output.join("");
}

function statusTone(status) {
  return {
    active: "active",
    done: "done",
    blocked: "alert",
    pending: "idle",
    skipped: "idle",
  }[status] || "idle";
}

function decisionTone(decision) {
  return { continue: "active", pause: "paused", complete: "done" }[decision] || "idle";
}

/* Navigation */

function pageFromHash() {
  const name = location.hash.slice(1);
  return Object.prototype.hasOwnProperty.call(PAGES, name) ? name : "overview";
}

function showPage(name, { updateHash = false } = {}) {
  if (!Object.prototype.hasOwnProperty.call(PAGES, name)) name = "overview";
  state.page = name;
  document.querySelectorAll(".page").forEach((page) => {
    const active = page.dataset.page === name;
    page.hidden = !active;
    page.classList.toggle("is-active", active);
  });
  document.querySelectorAll(".rail-nav a").forEach((link) => {
    const active = link.dataset.page === name;
    link.classList.toggle("is-active", active);
    if (active) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  document.title = `A3 研究台 · ${PAGES[name]}`;
  if (updateHash && location.hash !== `#${name}`) {
    history.replaceState(null, "", `#${name}`);
  }
}

/* Rendering */

function renderRuns(data) {
  const select = $("run-select");
  if (!data.runs.length) {
    select.innerHTML = '<option value="">还没有运行</option>';
    select.disabled = true;
    return;
  }
  select.disabled = false;
  const signature = data.runs.map((run) => `${run.id}:${run.state_label}`).join("|");
  if (select.dataset.signature !== signature) {
    select.innerHTML =
      '<option value="latest">最新运行（自动）</option>' +
      data.runs
        .map(
          (run) =>
            `<option value="${esc(run.id)}" ${run.selected ? "selected" : ""}>${esc(
              runLabel(run.id),
            )}  ${esc(run.state_label)}</option>`,
        )
        .join("");
    select.dataset.signature = signature;
  }
  select.value = state.run === "latest" ? "latest" : data.selected_run || "latest";
}

function renderStatus(data) {
  const agent = data.agent;
  const tone = toneClass(agent.tone);
  $("state-dot").className = `status-dot ${tone}`;
  $("rail-dot").className = `status-dot ${tone}`;
  $("state-label").textContent = agent.state_label;
  $("rail-state").textContent = agent.state_label;

  const sub = {
    starting: "准备中",
    running: "正在推理",
    waiting_jobs: "等待结果",
    waiting_event: "等待通知",
    context_waiting: "整理上下文",
    context_blocked: "上下文受阻",
    blocked: "需要人工处理",
    suspended: "Host 暂停",
    stopped: "已停止",
    interrupted: "进程已退出",
    not_started: "",
    unknown: "无法确认",
  }[agent.state] || "";
  $("state-sub").textContent = sub;

  $("overview-title").textContent = agent.headline;
  let detail = agent.detail;
  if (agent.state === "interrupted" || agent.state === "stopped") {
    detail = `${detail} 最后一次活动在 ${relative(agent.last_activity)}。`;
  }
  $("agent-detail").textContent = detail;

  $("last-activity").textContent = `最近活动 ${relative(agent.last_activity)}`;
  const usage = agent.usage || {};
  $("context-use").textContent =
    usage.percent == null ? "上下文 —" : `上下文 ${usage.percent}%`;
  $("run-duration").textContent =
    agent.elapsed_seconds == null ? "运行时长 —" : `已运行 ${duration(agent.elapsed_seconds)}`;

  const activeJobs = agent.active_jobs || [];
  const liveStates = ["running", "waiting_jobs", "waiting_event", "context_waiting"];
  $("job-meta").hidden = !activeJobs.length || !liveStates.includes(agent.state);
  $("job-meta").textContent = activeJobs.length ? `${activeJobs.length} 个作业进行中` : "";

  let hint = "";
  if (["running", "waiting_jobs", "waiting_event", "context_waiting"].includes(agent.state)) {
    hint = data.plan.next_hint || "";
  }
  const viewingLatest = !data.selected_run || data.selected_run === data.runs[0]?.id;
  if (
    viewingLatest &&
    data.schedule &&
    data.schedule.state === "waiting" &&
    ["not_started", "stopped", "interrupted", "suspended"].includes(agent.state)
  ) {
    hint = `夜间研究窗口将在 ${absolute(data.schedule.starts_at)} 启动。`;
  }
  $("next-hint").textContent = hint;
  $("next-hint").hidden = !hint;

  const selected = data.runs.find((run) => run.selected);
  if (!selected) {
    $("run-meta").textContent = "还没有研究运行记录";
  } else {
    const model = data.runtime.model ? `，${data.runtime.model}` : "";
    $("run-meta").textContent = `${runLabel(selected.id)} 启动${model}`;
  }

  $("rail-plan").textContent = data.plan.progress.total
    ? `${data.plan.progress.done}/${data.plan.progress.total}`
    : "—";
  $("rail-memory").textContent = data.memory.counts.effective;
  $("rail-skills").textContent = data.skills.counts.active;
  $("rail-jobs").textContent = data.jobs.counts.total
    ? `${data.jobs.counts.active}/${data.jobs.counts.total}`
    : "—";
  $("rail-updated").textContent = `更新于 ${preciseClock(data.generated_at)}`;

  renderPulse(data);
  renderStats(data);
}

function renderPulse(data) {
  const pulse = data.pulse || { buckets: [], total: 0, hours: 12, start: 0 };
  const maximum = Math.max(1, ...pulse.buckets);
  const bucketSeconds = ((pulse.hours || 12) * 3600) / Math.max(1, pulse.buckets.length);
  $("pulse-chart").innerHTML = pulse.buckets
    .map((count, index) => {
      const height = count ? 8 + (count / maximum) * 92 : 2;
      const starts = absolute((pulse.start || 0) + index * bucketSeconds);
      return `<span class="pulse-bar" data-count="${count}" style="height:${height}%" title="${esc(
        starts,
      )}，${count} 次决策"></span>`;
    })
    .join("");
  $("pulse-total").textContent = `${pulse.total} 次模型决策`;
  $("pulse-last").textContent = `最近 ${relative(data.agent.last_activity)}`;
  $("pulse-label").textContent = `过去 ${Math.round(pulse.hours || 12)} 小时`;
}

function renderStats(data) {
  const plan = data.plan.progress;
  $("stat-plan").textContent = plan.total ? `${plan.done} / ${plan.total}` : "—";
  $("stat-plan-note").textContent = plan.total ? `${plan.percent}%` : "还没有步骤";

  const activeRoutes = data.plan.routes.filter((route) => route.status === "active").length;
  $("stat-routes").textContent = activeRoutes || "—";
  $("stat-routes-note").textContent = `${data.plan.routes.length} 条`;

  $("stat-memory").textContent = data.memory.counts.effective;
  $("stat-memory-note").textContent = `${data.memory.counts.total} 条`;

  $("stat-skills").textContent = data.skills.counts.active;
  $("stat-skills-note").textContent = data.skills.counts.total
    ? `${data.skills.counts.total} 个`
    : "还没有技能";
}

function routeTone(status) {
  return {
    active: "active",
    completed: "done",
    paused: "paused",
    proposed: "idle",
  }[status] || "idle";
}

function routeSteps(plan, route) {
  return plan.steps.filter((step) => step.route === route.id);
}

function routeStage(route, steps) {
  if (route.status === "completed") {
    return { label: "路线已完成", detail: route.conclusion || "结论已记录。" };
  }
  if (route.status === "paused") {
    return { label: "路线已暂停", detail: route.conclusion || "等待重新评估后再开始。" };
  }
  if (!steps.length) {
    return { label: "还没有执行步骤", detail: "这条路线还没有写下具体动作。" };
  }
  const blocked = steps.find((step) => step.status === "blocked");
  if (blocked) {
    const index = steps.indexOf(blocked) + 1;
    return {
      label: `第 ${index}/${steps.length} 步受阻`,
      detail: `${blocked.task}${blocked.progress ? `：${blocked.progress}` : ""}`,
    };
  }
  const active = steps.find((step) => step.status === "active");
  if (active) {
    const index = steps.indexOf(active) + 1;
    return {
      label: `第 ${index}/${steps.length} 步进行中`,
      detail: `${active.task}${active.progress ? `：${active.progress}` : ""}`,
    };
  }
  const pending = steps.find((step) => step.status === "pending");
  if (pending) {
    const index = steps.indexOf(pending) + 1;
    return {
      label: `下一步是第 ${index}/${steps.length} 步`,
      detail: pending.task,
    };
  }
  return {
    label: "步骤都已完成，路线尚未关闭",
    detail: "需要一次路线复盘，决定继续、暂停还是完成。",
  };
}

function renderRouteDetail(plan, route) {
  const steps = routeSteps(plan, route);
  const done = steps.filter((step) => step.status === "done");
  const open = steps.filter((step) => !["done", "skipped"].includes(step.status));
  const stage = routeStage(route, steps);
  const reviews = plan.reviews.filter((review) => review.route === route.id).slice(0, 3);

  const stepRows = steps.length
    ? steps
        .map((step) => {
          const detail = step.status === "done"
            ? step.progress || step.criterion
            : step.criterion || step.progress;
          return `<div class="route-step" data-status="${esc(step.status)}">
            ${pill(step.status_label, statusTone(step.status))}
            <div><strong>${esc(step.task)}</strong>${
              detail ? `<p>${esc(short(detail, 140))}</p>` : ""
            }</div>
          </div>`;
        })
        .join("")
    : `<p class="route-empty">还没有执行步骤。</p>`;

  const reviewRows = reviews.length
    ? reviews
        .map(
          (review) => `<button class="route-review" type="button" data-detail="review" data-id="${esc(
            review.revision,
          )}">
            ${pill(review.decision_label, decisionTone(review.decision))}
            <span>${esc(short(review.conclusion, 110))}</span>
          </button>`,
        )
        .join("")
    : `<p class="route-empty">还没有复盘记录。</p>`;

  return `<header class="route-detail-head">
      <div><span class="detail-kicker">这条路线在问什么</span><h3>${esc(
        route.question || route.id,
      )}</h3></div>
      <div class="route-detail-meta">${pill(
        route.status_label,
        routeTone(route.status),
      )}<span>${done.length}/${steps.length} 步完成</span></div>
    </header>
    <div class="route-stage">
      <span class="route-stage-label">现在到哪一步</span>
      <strong>${esc(stage.label)}</strong>
      <p>${esc(short(stage.detail, 220))}</p>
    </div>
    <dl class="route-fields">
      <div><dt>假设</dt><dd>${esc(route.hypothesis || "未记录")}</dd></div>
      <div><dt>实验方法</dt><dd>${esc(route.experiment || "未记录")}</dd></div>
      <div><dt>什么结果算成功</dt><dd>${esc(route.decision_rule || "未记录")}</dd></div>
    </dl>
    <section class="route-section">
      <div class="route-section-head"><h4>做了哪些、到哪一步</h4><span>${done.length} 完成，${open.length} 待推进</span></div>
      <div class="route-steps">${stepRows}</div>
    </section>
    <section class="route-section">
      <div class="route-section-head"><h4>最后结论</h4></div>
      <p class="route-conclusion-text">${esc(route.conclusion || "还没有结论。")}</p>
    </section>
    <section class="route-section">
      <div class="route-section-head"><h4>这条路线最近的复盘</h4><span>${reviews.length} 条</span></div>
      <div class="route-reviews">${reviewRows}</div>
    </section>`;
}

function renderPlan(data) {
  const plan = data.plan;
  $("objective").textContent = plan.objective || "计划里还没有写长期方向。";
  $("metrics").innerHTML = plan.metrics.map((metric) => `<li>${esc(metric)}</li>`).join("");
  const open = plan.steps.filter((step) => !["done", "skipped"].includes(step.status)).length;
  $("plan-summary").textContent = plan.progress.total
    ? `${plan.progress.done}/${plan.progress.total} 步完成，${open} 步待推进`
    : "还没有步骤";

  if (!plan.routes.some((route) => route.id === state.selectedRoute)) {
    const preferred = plan.routes.find((route) => route.status === "active") || plan.routes[0];
    state.selectedRoute = preferred ? preferred.id : null;
  }

  $("routes").innerHTML = plan.routes.length
    ? plan.routes
        .map((route) => {
          const steps = routeSteps(plan, route);
          const stage = routeStage(route, steps);
          const selected = route.id === state.selectedRoute;
          return `<button class="route-card${selected ? " is-selected" : ""}" type="button" data-route-select="${esc(
            route.id,
          )}" data-status="${esc(route.status)}" aria-pressed="${selected}">
            <span class="route-top"><span class="route-question">${esc(
              route.question || route.id,
            )}</span>${pill(route.status_label, routeTone(route.status))}</span>
            <span class="route-sub">${esc(short(stage.label, 60))}</span>
            <span class="route-bar"><span class="bar"><i style="width:${route.progress.percent}%"></i></span><em>${
              route.progress.total ? `${route.progress.done}/${route.progress.total}` : "暂无步骤"
            }</em></span>
          </button>`;
        })
        .join("")
    : empty("还没有研究路线", "写下第一条路线后显示在这里。");

  const selected = plan.routes.find((route) => route.id === state.selectedRoute);
  $("route-detail").innerHTML = selected
    ? renderRouteDetail(plan, selected)
    : empty("还没有研究路线", "写下第一条路线后，这里会显示问题、假设、实验方法和结论。");
}

function renderJobs(data) {
  const jobs = data.jobs;
  const terminal = new Set(["completed", "failed", "cancelled"]);
  const active = jobs.items.filter((job) => !terminal.has(job.status));
  const finished = jobs.items.filter((job) => terminal.has(job.status));
  const parts = [
    `${active.length} 个进行中`,
    `${jobs.counts.completed} 个已完成`,
  ];
  if (jobs.counts.failed) parts.push(`${jobs.counts.failed} 个失败`);
  $("job-summary").textContent = jobs.counts.total ? parts.join("，") : "还没有远程作业";

  const routeName = (identifier) => {
    const route = data.plan.routes.find((item) => item.id === identifier);
    return route ? short(route.question, 52) : "";
  };
  const row = (job) => `<div class="job-row" data-tone="${esc(job.tone)}">
      ${pill(job.status_label, job.tone)}
      <div><strong>${esc(job.task || "未绑定到计划步骤")}</strong><p>${esc(
        [routeName(job.route), job.observed_at ? relative(job.observed_at) : ""].filter(Boolean).join("，"),
      )}</p></div>
    </div>`;
  const group = (label, items) => `<section class="job-group">
      <div class="job-group-head"><span>${esc(label)}</span><span>${items.length}</span></div>
      ${items.length ? items.map(row).join("") : `<p class="job-empty">没有${esc(label)}的作业。</p>`}
    </section>`;

  $("job-list").innerHTML = jobs.items.length
    ? group("进行中", active) + (finished.length ? group("已结束", finished) : "")
    : empty("还没有远程作业", "Agent 提交采集、训练或评估后，这里会显示作业状态。");
}

function renderReviews(data) {
  const reviews = data.plan.reviews;
  $("review-count").textContent = reviews.length ? `${reviews.length} 条` : "还没有复盘";
  $("reviews").innerHTML = reviews.length
    ? reviews
        .map(
          (review) => `<button class="review-row" type="button" data-detail="review" data-id="${esc(
            review.revision,
          )}">
            ${pill(review.decision_label, decisionTone(review.decision))}
            <span><span class="review-title">${esc(
              review.step_task || review.route_question || "路线复盘",
            )}</span><span class="review-sub">${esc(
              short(review.conclusion, 100),
            )}</span></span>
            <span class="review-arrow" aria-hidden="true">›</span>
          </button>`,
        )
        .join("")
    : empty("还没有结果复盘", "作业结束后显示结论。");
}

function renderMemory(data) {
  const counts = data.memory.counts;
  const labels = {
    effective: "有效",
    archived: "已归档",
    superseded: "已被替代",
    all: "全部",
  };
  document.querySelectorAll("#memory-tabs button").forEach((button) => {
    const filter = button.dataset.filter;
    const count = filter === "all" ? counts.total : counts[filter];
    button.textContent = `${labels[filter]} ${count}`;
    const active = filter === state.memoryFilter;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-pressed", String(active));
  });

  const query = state.memoryQuery.trim().toLowerCase();
  const items = data.memory.items.filter((item) => {
    if (state.memoryFilter !== "all" && item.status !== state.memoryFilter) return false;
    if (!query) return true;
    return [item.text, item.scope, item.kind_label, ...(item.tags || [])]
      .join(" ")
      .toLowerCase()
      .includes(query);
  });

  const list = $("memory-list");
  const previousTop = list.scrollTop;
  list.innerHTML = items.length
    ? items
        .map((item) => {
          const meta = [
            item.scope ? esc(item.scope) : "",
            relative(item.ts),
            item.lifetime_label,
            item.status !== "effective" ? item.status_label : "",
          ]
            .filter(Boolean)
            .join("，");
          return `<button class="memory-row" type="button" data-detail="memory" data-id="${esc(
            item.id,
          )}">
            <span class="memory-kind">${esc(item.kind_label)}</span>
            <span><span class="memory-preview">${esc(
              item.text,
            )}</span><span class="memory-meta">${meta}</span></span>
            <span class="memory-arrow" aria-hidden="true">›</span>
          </button>`;
        })
        .join("")
    : empty("没有匹配的记忆", "换个筛选条件或搜索词试试。");
  list.scrollTop = state.resetMemoryScroll ? 0 : previousTop;
  state.resetMemoryScroll = false;
}

function renderSkills(data) {
  const skills = data.skills.items;
  $("skill-count").textContent = data.skills.counts.active
    ? `${data.skills.counts.active} 个可用`
    : "还没有技能";
  $("skills").innerHTML = skills.length
    ? skills
        .map((skill) => {
          return `<button class="skill-card" type="button" data-detail="skill" data-id="${esc(
            skill.id,
          )}">
            <span class="skill-title">${esc(skill.description || skill.id)}</span>
            <span class="skill-id">${esc(skill.id)}，v${esc(
              skill.version || "—",
            )}${skill.status !== "active" ? `，${esc(skill.status_label)}` : ""}</span>
          </button>`;
        })
        .join("")
    : empty("还没有方法技能", "还没有方法技能。");
}

function renderActivity(data) {
  const activity = data.activity.slice(0, 6);
  $("activity-count").textContent = activity.length ? `最近 ${activity.length} 条` : "还没有记录";
  $("activity-list").innerHTML = activity.length
    ? activity
        .map(
          (item) => `<li class="activity-item" data-tone="${esc(item.tone)}">
            <time class="activity-time" datetime="${new Date(item.ts * 1000).toISOString()}" title="${esc(
              absolute(item.ts),
            )}">${esc(clock(item.ts))}</time>
            <div><strong class="activity-title">${esc(item.title)}</strong>${
              item.detail ? `<p class="activity-detail">${esc(item.detail)}</p>` : ""
            }</div>
          </li>`,
        )
        .join("")
    : `<li class="activity-item"><time class="activity-time">—</time><div><strong class="activity-title">还没有运行记录</strong><p class="activity-detail">研究启动后显示作业与决策。</p></div></li>`;

  const usage = data.agent.usage || {};
  const facts = [
    ["模型", data.runtime.model || "—"],
    ["上下文", usage.percent == null ? "—" : `${usage.percent}%`],
    ["决策", String(data.agent.decisions ?? 0)],
    ["作业", data.jobs.counts.total ? `${data.jobs.counts.active}/${data.jobs.counts.total}` : "—"],
    ["最近活动", relative(data.agent.last_activity)],
  ];
  $("facts").innerHTML = `<dl>${facts
    .map(
      ([label, value]) =>
        `<div class="fact"><dt>${esc(label)}</dt><dd>${esc(value)}</dd></div>`,
    )
    .join("")}</dl>`;
}

function render(data) {
  const runChanged = state.snapshot && state.snapshot.selected_run !== data.selected_run;
  if (runChanged) {
    state.signatures = {};
    state.resetMemoryScroll = true;
    state.selectedRoute = null;
    closeDetail();
  }
  state.snapshot = data;
  renderRuns(data);
  renderStatus(data);

  const planSignature = JSON.stringify([
    data.plan.objective,
    data.plan.metrics,
    data.plan.routes,
    data.plan.steps,
    data.plan.progress,
  ]);
  const signatures = {
    plan: planSignature,
    jobs: JSON.stringify([data.jobs, data.plan.routes]),
    reviews: JSON.stringify(data.plan.reviews),
    memory: JSON.stringify(data.memory),
    skills: JSON.stringify(data.skills),
    activity: JSON.stringify(data.activity),
  };
  const draw = {
    plan: () => renderPlan(data),
    jobs: () => renderJobs(data),
    reviews: () => renderReviews(data),
    memory: () => renderMemory(data),
    skills: () => renderSkills(data),
    activity: () => renderActivity(data),
  };
  for (const [key, signature] of Object.entries(signatures)) {
    if (state.signatures[key] !== signature) {
      state.signatures[key] = signature;
      draw[key]();
    }
  }
  if (state.dialog && $("detail").open && !fillDetail()) closeDetail();
  renderError("");
}

/* Detail dialog */

function detailMarkup(type, id) {
  const data = state.snapshot;
  if (!data) return null;
  if (type === "route") {
    const route = data.plan.routes.find((item) => item.id === id);
    if (!route) return null;
    const fields = [
      ["假设", route.hypothesis],
      ["实验", route.experiment],
      ["决策规则", route.decision_rule],
      ["结论", route.conclusion],
    ].filter(([, value]) => value);
    return {
      kicker: "研究路线",
      title: route.question || route.id,
      meta: `${pill(route.status_label, route.status === "active" ? "active" : route.status === "completed" ? "done" : route.status === "paused" ? "paused" : "idle")}<span>${route.progress.done}/${route.progress.total} 步</span>`,
      body: `<dl class="detail-fields">${fields
        .map(
          ([label, value]) =>
            `<div><dt>${esc(label)}</dt><dd>${esc(value)}</dd></div>`,
        )
        .join("")}</dl>`,
    };
  }
  if (type === "review") {
    const review = data.plan.reviews.find((item) => String(item.revision) === id);
    if (!review) return null;
    return {
      kicker: "结果复盘",
      title: `${review.decision_label}，${review.step_task || review.route_question || "路线复盘"}`,
      meta: `${pill(review.decision_label, decisionTone(review.decision))}<span>${esc(
        review.route_question || "",
      )}</span>`,
      body: `<p>${esc(review.conclusion)}</p>${
        review.metrics.length
          ? `<div class="metric-chips">${review.metrics
              .map((metric) => `<span>${esc(metric)}</span>`)
              .join("")}</div>`
          : ""
      }`,
    };
  }
  if (type === "memory") {
    const item = data.memory.items.find((entry) => entry.id === id);
    if (!item) return null;
    const tags = item.tags.length
      ? `<div class="detail-tags">${item.tags
          .map((tag) => `<span>${esc(tag)}</span>`)
          .join("")}</div>`
      : "";
    const refs = item.refs.length
      ? `<p class="detail-refs">依据：${item.refs
          .slice(0, 8)
          .map((ref) => `<code>${esc(ref)}</code>`)
          .join("　")}${item.refs.length > 8 ? ` 另有 ${item.refs.length - 8} 条` : ""}</p>`
      : "";
    return {
      kicker: item.kind_label,
      title: short(item.text, 60),
      meta: [
        item.scope ? esc(item.scope) : "",
        relative(item.ts),
        item.lifetime_label,
        item.status !== "effective" ? item.status_label : "",
      ]
        .filter(Boolean)
        .join("，"),
      body: `<p>${esc(item.text)}</p>${tags}${refs}`,
    };
  }
  if (type === "skill") {
    const skill = data.skills.items.find((entry) => entry.id === id);
    if (!skill) return null;
    return {
      kicker: "方法技能",
      title: skill.description || skill.id,
      meta: `<span>${esc(skill.id)}</span><span>v${esc(skill.version || "—")}</span>${
        skill.resources.length ? `<span>附件 ${skill.resources.length}</span>` : ""
      }`,
      body: `<div class="md">${renderMarkdown(skill.body)}</div>${
        skill.resources.length
          ? `<p class="detail-refs">附件：${skill.resources
              .slice(0, 8)
              .map((name) => `<code>${esc(name)}</code>`)
              .join("　")}</p>`
          : ""
      }`,
    };
  }
  return null;
}

function fillDetail({ resetScroll = false } = {}) {
  if (!state.dialog) return false;
  const detail = detailMarkup(state.dialog.type, state.dialog.id);
  if (!detail) return false;
  const signature = JSON.stringify(detail);
  if (!resetScroll && state.detailSignature === signature) return true;
  state.detailSignature = signature;
  const body = $("detail-body");
  const previousTop = resetScroll ? 0 : body.scrollTop;
  $("detail-kicker").textContent = detail.kicker;
  $("detail-title").textContent = detail.title;
  $("detail-meta").innerHTML = detail.meta || "";
  body.innerHTML = detail.body || "";
  body.scrollTop = previousTop;
  return true;
}

function openDetail(type, id) {
  state.dialog = { type, id: String(id) };
  state.detailSignature = null;
  if (!fillDetail({ resetScroll: true })) {
    state.dialog = null;
    return;
  }
  const dialog = $("detail");
  if (!dialog.open) dialog.showModal();
}

function closeDetail() {
  state.dialog = null;
  state.detailSignature = null;
  const dialog = $("detail");
  if (dialog.open) dialog.close();
}

/* Data loading */

function renderError(message) {
  const banner = $("error");
  if (!message) {
    banner.hidden = true;
    banner.textContent = "";
    return;
  }
  banner.hidden = false;
  banner.textContent = message;
}

async function load({ quiet = false } = {}) {
  if (state.loading) return;
  state.loading = true;
  if (!quiet) $("refresh").classList.add("is-spinning");
  try {
    const response = await fetch(
      `/api/snapshot?run=${encodeURIComponent(state.run || "latest")}`,
      { cache: "no-store" },
    );
    if (response.status === 404 && state.run !== "latest") {
      state.run = "latest";
      history.replaceState(null, "", location.pathname + location.hash);
      state.loading = false;
      return load({ quiet });
    }
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    render(await response.json());
  } catch (error) {
    renderError(`无法读取面板数据：${error.message}。检查研究台服务是否仍在运行。`);
  } finally {
    state.loading = false;
    $("refresh").classList.remove("is-spinning");
  }
}

/* Events */

function bindEvents() {
  $("run-select").addEventListener("change", (event) => {
    state.run = event.target.value || "latest";
    const url = new URL(location.href);
    if (state.run === "latest") url.searchParams.delete("run");
    else url.searchParams.set("run", state.run);
    history.replaceState(null, "", url);
    state.signatures = {};
    load();
  });

  $("refresh").addEventListener("click", () => load());

  $("memory-tabs").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-filter]");
    if (!button) return;
    state.memoryFilter = button.dataset.filter;
    state.resetMemoryScroll = true;
    if (state.snapshot) renderMemory(state.snapshot);
  });

  $("memory-search").addEventListener("input", (event) => {
    state.memoryQuery = event.target.value;
    state.resetMemoryScroll = true;
    if (state.snapshot) renderMemory(state.snapshot);
  });

  document.addEventListener("click", (event) => {
    const route = event.target.closest("[data-route-select]");
    if (route) {
      state.selectedRoute = route.dataset.routeSelect;
      if (state.snapshot) renderPlan(state.snapshot);
      return;
    }
    const trigger = event.target.closest("[data-detail]");
    if (!trigger) return;
    openDetail(trigger.dataset.detail, trigger.dataset.id);
  });

  $("detail-close").addEventListener("click", closeDetail);
  $("detail").addEventListener("click", (event) => {
    if (event.target === $("detail")) closeDetail();
  });
  $("detail").addEventListener("close", () => {
    state.dialog = null;
  });

  window.addEventListener("hashchange", syncPageFromHash);

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) load({ quiet: true });
  });
}

function startPolling() {
  setInterval(() => {
    if (!document.hidden) load({ quiet: true });
  }, 5000);
}

function syncPageFromHash() {
  const page = pageFromHash();
  showPage(page, { updateHash: false });
  if (location.hash !== `#${page}`) {
    const url = new URL(location.href);
    url.hash = page;
    history.replaceState(null, "", url);
  }
}

bindEvents();
syncPageFromHash();
load();
startPolling();
