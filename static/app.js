const $ = (s) => document.querySelector(s);
const output = $("#output");
const statusEl = $("#status");
let mode = "single";
let lane = "auto";

const LANES = ["auto", "http", "browser"];
const LANE_LABEL = { auto: "Auto", http: "Fast (HTTP only)", browser: "Browser only" };
const LANE_HINT = {
  auto: "Auto: fast HTTP pass, then a headless browser if a wall is in the way.",
  http: "Fast (HTTP only): no browser, stops at anything that needs a real click.",
  browser: "Browser only: slow, but clears countdowns and clicks every button.",
};

document.querySelectorAll(".tab[data-mode]").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab[data-mode]").forEach((t) => t.classList.remove("active"));
    tab.classList.add("active");
    mode = tab.dataset.mode;
    $("#pane-single").classList.toggle("hidden", mode !== "single");
    $("#pane-bulk").classList.toggle("hidden", mode !== "bulk");
    statusEl.textContent = "";
    output.innerHTML = "";
  });
});

$("#tab-mode").addEventListener("click", () => {
  lane = LANES[(LANES.indexOf(lane) + 1) % LANES.length];
  $("#tab-mode").textContent = "Mode: " + LANE_LABEL[lane];
  $("#mode-hint").textContent = LANE_HINT[lane];
});

$("#url").addEventListener("keydown", (e) => {
  if (e.key === "Enter") run();
});
$("#go").addEventListener("click", run);

async function run() {
  const btn = $("#go");
  btn.disabled = true;
  output.innerHTML = "";
  statusEl.textContent = lane === "browser" ? "Opening headless browser, clearing the wall..." : "Resolving...";

  // multi-step campaigns take a while; say so before it looks frozen
  const slow = setTimeout(
    () => (statusEl.textContent = "Walking the multi-step flow (countdown + verify + continue), usually 20-40s..."),
    5000
  );

  try {
    if (mode === "single") {
      const url = $("#url").value.trim();
      if (!url) { statusEl.textContent = "Enter a link first."; return; }
      const res = await post("/api/expand", { url, mode: lane });
      statusEl.textContent = res.bypassed ? "" : "Could not fully bypass this link.";
      output.innerHTML = card(res);
    } else {
      const text = $("#bulk").value.trim();
      if (!text) { statusEl.textContent = "Paste at least one link."; return; }
      const res = await post("/api/bulk", { text, mode: lane });
      statusEl.textContent = `${res.bypassed}/${res.count} link(s) fully bypassed.`;
      output.innerHTML = table(res.results);
    }
  } catch (err) {
    statusEl.textContent = "Request failed: " + err.message;
  } finally {
    clearTimeout(slow);
    btn.disabled = false;
  }
}

async function post(path, body) {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );

const PROTECTION_TEXT = {
  captcha: "Captcha gated (reCAPTCHA / hCaptcha / Cloudflare Turnstile). Solving it is the visitor's job, so no tool can skip it.",
  safelink: "SafeLink / 'click to continue' verification page.",
  cloudflare: "Cloudflare interstitial.",
  "shortener-wall": "Still on the shortener wall after both passes.",
  unresolved: "The wall did not release a destination in either pass.",
};

function card(r) {
  if (r.bypassed && r.destination) {
    const p = r.preview || {};
    const badges = [
      `<span class="badge ok">bypassed</span>`,
      `<span class="badge">${esc(r.method)}</span>`,
      `<span class="badge">${esc(r.lane)}</span>`,
      `<span class="badge">${r.hops} hop${r.hops === 1 ? "" : "s"}</span>`,
      `<span class="badge">${r.elapsed_ms} ms</span>`,
      ...(r.warnings || []).map((w) => `<span class="badge warn">${esc(w)}</span>`),
    ].join("");
    const chain = (r.chain || [])
      .map(
        (h) => `<li>${esc(h.url)}
          <span class="meta">[${h.status ?? "-"}] ${esc(h.type)}${h.note ? " - " + esc(h.note) : ""}</span>
          ${h.location ? `<br><span class="meta">-> ${esc(h.location)}</span>` : ""}
        </li>`
      )
      .join("");

    return `<div class="result ok">
      <p class="label">Destination</p>
      <a class="final" href="${esc(r.destination)}" target="_blank" rel="noopener noreferrer">${esc(r.destination)}</a>
      <div class="actions">
        <button class="copy">Copy</button>
        <a class="open" href="${esc(r.destination)}" target="_blank" rel="noopener noreferrer">Open</a>
      </div>
      <div class="rhead">
        ${p.image ? `<img class="thumb" src="${esc(p.image)}" onerror="this.style.display='none'">` : ""}
        <div style="min-width:0">
          <p class="rtitle">${esc(p.title || "")}</p>
          <p class="rdesc">${esc(p.description || "")}</p>
        </div>
      </div>
      <div class="badges">${badges}</div>
      ${r.note ? `<p class="note">${esc(r.note)}</p>` : ""}
      ${chain ? `<details><summary>How it got there (${r.chain.length} steps)</summary><ol class="chain">${chain}</ol></details>` : ""}
    </div>`;
  }

  return `<div class="result fail">
    <p class="label">Not bypassed</p>
    <p class="rtitle">${esc(r.error || PROTECTION_TEXT[r.protection] || "The destination was not exposed.")}</p>
    ${r.note ? `<p class="rdesc">${esc(r.note)}</p>` : ""}
    <div class="badges">
      <span class="badge err">${esc(r.protection || "unresolved")}</span>
      <span class="badge">${esc(r.lane)}</span>
      <span class="badge">${r.hops} hop${r.hops === 1 ? "" : "s"}</span>
      <span class="badge">${r.elapsed_ms} ms</span>
    </div>
    ${r.final_url ? `<p class="rdesc">Stopped here: <a class="wall" href="${esc(r.final_url)}" target="_blank" rel="noopener noreferrer">${esc(r.final_url)}</a></p>` : ""}
    <p class="note">Try <b>Mode: Browser only</b>, or open the wall yourself and finish the last click.</p>
  </div>`;
}

function table(results) {
  const rows = results
    .map((r) => {
      const dest = r.bypassed && r.destination
        ? `<a href="${esc(r.destination)}" target="_blank" rel="noopener noreferrer">${esc(r.destination)}</a>`
        : `<span class="badge err">${esc(r.protection || r.error || "not bypassed")}</span>`;
      return `<tr class="${r.bypassed ? "" : "dim"}">
        <td style="max-width:200px">${esc(r.input_url)}</td>
        <td>${dest}</td>
        <td>${esc(r.method)}</td>
        <td>${r.elapsed_ms} ms</td>
      </tr>`;
    })
    .join("");
  return `<table class="bulk">
    <thead><tr><th>Input</th><th>Destination</th><th>Method</th><th>Time</th></tr></thead>
    <tbody>${rows}</tbody>
  </table>`;
}

output.addEventListener("click", (e) => {
  if (!e.target.classList.contains("copy")) return;
  const link = e.target.closest(".result").querySelector(".final");
  navigator.clipboard.writeText(link.textContent.trim()).then(() => {
    e.target.textContent = "Copied";
    setTimeout(() => (e.target.textContent = "Copy"), 1500);
  });
});
