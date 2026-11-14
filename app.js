const $ = (id) => document.getElementById(id);
let snapshot = null,
  lastRequest = null,
  busy = false;
const el = (tag, text, cls) => {
  const n = document.createElement(tag);
  if (text !== undefined) n.textContent = text;
  if (cls) n.className = cls;
  return n;
};
const tokens = (n) =>
  (n / 1e6).toLocaleString(undefined, {
    minimumFractionDigits: 3,
    maximumFractionDigits: 6,
  });
const freshID = () => `req-${crypto.randomUUID()}`;
function message(text) {
  $("status").textContent = text;
  $("request-status").textContent = text;
}
async function api(path, body) {
  const response = await fetch(
    path,
    body === undefined
      ? {}
      : {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        },
  );
  const data = await response.json();
  if (
    !response.ok &&
    !(response.status === 429 && typeof data.allowed === "boolean")
  )
    throw Error(data.error || `HTTP ${response.status}`);
  return data;
}
async function task(action) {
  if (busy) return;
  busy = true;
  document
    .querySelectorAll("button,select")
    .forEach((x) => (x.disabled = true));
  try {
    await action();
  } catch (e) {
    message(e.message);
  } finally {
    busy = false;
    document
      .querySelectorAll("button,select")
      .forEach((x) => (x.disabled = false));
    $("retry").disabled = !lastRequest;
    $("request-open").disabled = !snapshot;
    $("policy").disabled = !$("policy").options.length;
  }
}
async function policies(preferred) {
  const { policies } = await api("/api/policies");
  $("policy").replaceChildren(
    ...policies.map((p) => {
      const o = el("option", p.name);
      o.value = p.name;
      return o;
    }),
  );
  if (preferred && policies.some((p) => p.name === preferred))
    $("policy").value = preferred;
  $("empty").hidden = policies.length > 0;
  $("workspace").hidden = policies.length === 0;
  if (policies.length) await refresh();
}
async function refresh() {
  const key = $("policy").value;
  if (!key) return;
  if (snapshot && snapshot.policy.name !== key) {
    lastRequest = null;
    $("decision-title").textContent = "Awaiting a request";
    $("decision-description").textContent =
      "Send a request under the selected policy.";
    $("decision-facts").replaceChildren();
    document.querySelector(".receipt").classList.remove("denied");
    $("audit-result").textContent = "";
  }
  snapshot = await api(`/api/policies/${encodeURIComponent(key)}`);
  render();
}
function render() {
  const s = snapshot,
    p = s.policy;
  $("workspace").hidden = false;
  $("policy-name").textContent = p.name;
  $("policy-rule").textContent =
    `${p.capacity.toLocaleString()} token capacity · ${p.rate.toLocaleString()} tokens / second`;
  $("accepted").textContent = s.accepted;
  $("denied").textContent = s.denied;
  $("consumed").textContent = s.consumed_tokens;
  $("clients").textContent = s.buckets.length;
  $("cost").max = p.capacity;
  $("sampled").textContent =
    `Snapshot ${new Date(s.sampled_us / 1000).toLocaleTimeString()}`;
  $("receipt-note").textContent =
    `Showing the latest ${s.visible_receipts} of ${s.receipt_count} stored receipts. Replays do not add rows. Export includes all receipts.`;
  $("buckets").replaceChildren(
    ...s.buckets.map((b) => {
      const box = el("div", undefined, "bucket"),
        progress = el("progress"),
        gauge = el("div", undefined, "gauge");
      gauge.style.setProperty(
        "--fill",
        `${(b.projected_microtokens / (p.capacity * 1e6)) * 100}%`,
      );
      if (tokens(b.projected_microtokens).length > 9)
        gauge.classList.add("long-value");
      gauge.append(
        el("strong", tokens(b.projected_microtokens)),
        el("span", `of ${p.capacity} tokens`),
      );
      progress.max = p.capacity * 1e6;
      progress.value = b.projected_microtokens;
      progress.setAttribute("aria-label", `${b.client} projected tokens`);
      box.append(
        el("h3", b.client),
        gauge,
        progress,
        el("p", `Stored after last decision: ${tokens(b.balance)}`, "hint"),
      );
      return box;
    }),
  );
  if (!s.buckets.length)
    $("buckets").append(
      el(
        "p",
        "No clients yet. The first request initializes a full bucket.",
        "hint",
      ),
    );
  const recent = s.receipts.slice(-100);
  $("decision-strip").replaceChildren(
    ...recent.map((r) => {
      const bar = el("span", undefined, r.allowed ? "admitted" : "rejected");
      bar.title = `#${r.seq} · ${r.client} · ${r.allowed ? "Accepted" : "Denied"} · cost ${r.cost}`;
      bar.style.setProperty("--height", `${(r.cost / p.capacity) * 100}%`);
      return bar;
    }),
  );
  $("strip-label").textContent = recent.length
    ? `${recent.length} latest decisions · bar height represents token cost · green accepted / red denied`
    : "No decisions recorded";
  renderReceipts();
}
function renderReceipts() {
  if (!snapshot) return;
  const filter = $("filter").value.toLowerCase();
  $("receipts").replaceChildren(
    ...[...snapshot.receipts]
      .reverse()
      .filter((r) => r.client.toLowerCase().includes(filter))
      .map((r) => {
        const tr = el("tr"),
          client = el("td", r.client);
        client.append(el("small", r.request_id));
        const decision = el("td");
        decision.append(
          el(
            "span",
            r.allowed ? "Accepted" : "Denied",
            `badge ${r.allowed ? "" : "no"}`,
          ),
        );
        tr.append(
          el("td", String(r.seq)),
          client,
          decision,
          el("td", String(r.cost)),
          el("td", tokens(r.remaining_microtokens)),
          el("td", `${(r.retry_after_us / 1e6).toFixed(6)}s`),
        );
        return tr;
      }),
  );
}
function showReceipt(r) {
  const receipt = document.querySelector(".receipt");
  receipt.classList.toggle("denied", !r.allowed);
  if (!matchMedia("(prefers-reduced-motion: reduce)").matches)
    receipt.animate(
      [
        { opacity: 0.45, transform: "translateY(4px)" },
        { opacity: 1, transform: "translateY(0)" },
      ],
      { duration: 280, easing: "ease-out" },
    );
  $("decision-title").textContent =
    `${r.allowed ? "Accepted" : "Denied"}${r.replayed ? " · replay" : ""}`;
  $("decision-description").textContent = r.replayed
    ? "Original receipt returned without consuming tokens or updating the bucket. Its balances and retry delay describe the original decision."
    : r.allowed
      ? "Tokens were deducted and the decision was committed atomically."
      : "Insufficient tokens. No cost was deducted; refill accounting and the denial receipt were committed.";
  $("decision-facts").replaceChildren();
  for (const [label, value] of [
    ["Policy", r.policy],
    ["Client", r.client],
    ["Cost", r.cost],
    ["Available before", tokens(r.before_microtokens)],
    ["Remaining", tokens(r.remaining_microtokens)],
    ["Retry delay", `${(r.retry_after_us / 1e6).toFixed(6)} seconds`],
    ["Receipt sequence", r.seq],
    ["Clock clamped", r.clock_clamped ? "Yes" : "No"],
  ])
    $("decision-facts").append(el("dt", label), el("dd", String(value)));
}
async function consume(body, key = $("policy").value) {
  const r = await api(`/api/policies/${encodeURIComponent(key)}/consume`, body);
  lastRequest = { key, body: structuredClone(body) };
  showReceipt(r);
  return r;
}
$("consume").onsubmit = (event) => {
  event.preventDefault();
  const body = {
    request_id: $("request-id").value,
    client: $("client").value,
    cost: Number($("cost").value),
  };
  task(async () => {
    const r = await consume(body);
    await refresh();
    message(
      `${r.replayed ? "Replayed" : "Committed"} receipt ${r.seq}. ${r.allowed ? "Admission granted." : "Admission denied."}`,
    );
  });
};
$("new-id").onclick = () => {
  $("request-id").value = freshID();
};
$("request-id").value = freshID();
$("retry").onclick = () =>
  task(async () => {
    const r = await consume(lastRequest.body, lastRequest.key);
    await refresh();
    message(`Replayed receipt ${r.seq}; no new tokens consumed.`);
  });
$("burst").onclick = () => {
  const count = Number($("burst-count").value),
    cost = Number($("cost").value),
    client = $("client").value;
  if (!Number.isInteger(count) || count < 1 || count > 30) {
    message("Burst size must be an integer from 1 through 30.");
    return;
  }
  task(async () => {
    let accepted = 0,
      denied = 0,
      completed = 0;
    try {
      for (let i = 0; i < count; i++) {
        const body = { request_id: freshID(), client, cost };
        const r = await consume(body);
        $("request-id").value = body.request_id;
        completed++;
        r.allowed ? accepted++ : denied++;
        message(
          `Burst ${completed} / ${count}: ${accepted} accepted, ${denied} denied.`,
        );
      }
    } catch (e) {
      await refresh();
      throw Error(
        `Burst stopped after ${completed} committed decisions: ${e.message}`,
      );
    }
    await refresh();
    message(
      `Burst finished: ${accepted} accepted, ${denied} denied. Each of ${completed} decisions has its own receipt.`,
    );
  });
};
$("policy").onchange = () =>
  task(async () => {
    lastRequest = null;
    $("decision-title").textContent = "Awaiting a request";
    $("decision-description").textContent =
      "Send a request under the selected policy.";
    $("decision-facts").replaceChildren();
    document.querySelector(".receipt").classList.remove("denied");
    $("audit-result").textContent = "";
    await refresh();
    message("Loaded policy snapshot.");
  });
$("refresh").onclick = () =>
  task(async () => {
    await refresh();
    message("Refreshed projected balances without changing stored buckets.");
  });
$("filter").oninput = renderReceipts;
$("create").onsubmit = (event) => {
  event.preventDefault();
  task(async () => {
    const p = await api("/api/policies", {
      name: $("name").value,
      capacity: Number($("capacity").value),
      rate: Number($("rate").value),
    });
    lastRequest = null;
    await policies(p.name);
    message(`Created immutable policy ${p.name}.`);
  });
};
$("example").onclick = () =>
  task(async () => {
    const p = await api("/api/policies", {
      name: `example-${Date.now()}`,
      capacity: 5,
      rate: 1,
    });
    await policies(p.name);
    message("Created example: five-token capacity, one token per second.");
  });
$("audit").onclick = () =>
  task(async () => {
    const a = await api(
      `/api/policies/${encodeURIComponent($("policy").value)}/audit`,
    );
    $("audit-result").textContent = a.ok
      ? `Audit passed: replayed ${a.checked_receipts} receipts across ${a.checked_clients} clients; stored balances agree.`
      : `Audit failed: ${a.errors.join(" ")}`;
  });
function download(name, body, mime) {
  const url = URL.createObjectURL(new Blob([body], { type: mime })),
    a = el("a");
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
$("export").onclick = () =>
  task(async () => {
    const data = await api(
      `/api/policies/${encodeURIComponent($("policy").value)}/export`,
    );
    download(
      `${data.policy.name}-quota-evidence.json`,
      JSON.stringify(data, null, 2),
      "application/json",
    );
  });
$("csv").onclick = () =>
  task(async () => {
    const data = await api(
      `/api/policies/${encodeURIComponent($("policy").value)}/export`,
    );
    const columns = [
      "seq",
      "policy",
      "request_id",
      "client",
      "cost",
      "allowed",
      "observed_us",
      "effective_us",
      "before_microtokens",
      "remaining_microtokens",
      "retry_after_us",
      "clock_clamped",
    ];
    download(
      `${data.policy.name}-quota-receipts.csv`,
      [
        columns.join(","),
        ...data.receipts.map((r) => columns.map((c) => r[c]).join(",")),
      ].join("\r\n"),
      "text/csv",
    );
  });
task(() => policies());

$("request-open").onclick = () => $("request-dialog").showModal();
$("request-close").onclick = () => $("request-dialog").close();
