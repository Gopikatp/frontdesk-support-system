const $ = (id) => document.getElementById(id);
const API_BASE = (window.FRONTDESK_API_BASE || "").replace(/\/$/, "");
let tickets = [];
let selectedId = null;

const agent = () => $("agent").value;
const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[char]));
const pretty = (value) => (value || "").replaceAll("_", " ").toLowerCase()
  .replace(/\b\w/g, (char) => char.toUpperCase());
const formatDate = (value) => {
  if (!value) return "—";
  const date = new Date(/[zZ]|[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString();
};

async function api(path, options = {}) {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await response.json().catch(() => ({ detail: "Unexpected server response" }));
  if (!response.ok) throw new Error(data.detail || "Request failed");
  return data;
}

function filterTickets() {
  const query = $("searchFilter").value.trim().toLowerCase();
  const status = $("statusFilter").value;
  const priority = $("priorityFilter").value;
  const owner = $("ownerFilter").value;
  return tickets.filter((ticket) => {
    const text = `${ticket.title} ${ticket.customer_name} ${ticket.customer_email} ${ticket.id}`.toLowerCase();
    return (!query || text.includes(query))
      && (!status || ticket.status === status)
      && (!priority || ticket.priority === priority)
      && (!owner || (owner === "unassigned" ? !ticket.assigned_agent : ticket.assigned_agent === owner));
  });
}

function updateOwnerOptions() {
  const select = $("ownerFilter");
  const selected = select.value;
  const owners = [...new Set(tickets.map((ticket) => ticket.assigned_agent).filter(Boolean))].sort();
  select.innerHTML = '<option value="">All owners</option><option value="unassigned">Unassigned</option>'
    + owners.map((owner) => `<option value="${escapeHtml(owner)}">${escapeHtml(owner)}</option>`).join("");
  if ([...select.options].some((option) => option.value === selected)) select.value = selected;
}

function updateExportLink() {
  const params = new URLSearchParams();
  if ($("statusFilter").value) params.set("status", $("statusFilter").value);
  if ($("priorityFilter").value) params.set("priority", $("priorityFilter").value);
  if ($("searchFilter").value.trim()) params.set("q", $("searchFilter").value.trim());
  const owner = $("ownerFilter").value;
  if (owner === "unassigned") params.set("unassigned", "true");
  else if (owner) params.set("assigned_agent", owner);
  $("exportTickets").href = `${API_BASE}/api/tickets/export.csv${params.size ? `?${params}` : ""}`;
}

function renderList() {
  const shown = filterTickets();
  $("ticketList").innerHTML = shown.length ? shown.map((ticket) => `
    <article class="ticket-card ${selectedId === ticket.id ? "selected" : ""}" data-id="${ticket.id}">
      <div class="ticket-top">
        <h3>#FD-${String(ticket.id).padStart(4, "0")} · ${escapeHtml(ticket.title)}</h3>
        <span class="status">${pretty(ticket.status)}</span>
      </div>
      <p>${escapeHtml(ticket.customer_name)} · ${escapeHtml(ticket.customer_email)}</p>
      <div class="pills">
        <span class="pill ${ticket.priority.toLowerCase()}">${pretty(ticket.priority)}</span>
        <span class="pill">${ticket.assigned_agent ? `Owner: ${escapeHtml(ticket.assigned_agent)}` : "Unassigned"}</span>
        ${ticket.is_waiting ? '<span class="pill">Waiting customer</span>' : ""}
        ${ticket.response_late || ticket.resolution_late ? '<span class="pill urgent">Overdue</span>' : ""}
      </div>
    </article>`).join("") : '<div class="empty-list">No tickets match these filters.</div>';
  document.querySelectorAll(".ticket-card").forEach((element) => {
    element.onclick = () => openTicket(Number(element.dataset.id)).catch(showError);
  });
  updateExportLink();
}

function showError(error) {
  window.alert(error.message || "Something went wrong. Please try again.");
}

async function load() {
  tickets = await api("/api/tickets");
  $("total").textContent = tickets.length;
  $("active").textContent = tickets.filter((ticket) => ["OPEN", "IN_PROGRESS"].includes(ticket.status)).length;
  $("waiting").textContent = tickets.filter((ticket) => ticket.status === "WAITING_CUSTOMER").length;
  $("overdue").textContent = tickets.filter((ticket) => ticket.response_late || ticket.resolution_late).length;
  updateOwnerOptions();
  renderList();
  if (selectedId && tickets.some((ticket) => ticket.id === selectedId)) await openTicket(selectedId);
}

async function openTicket(id) {
  selectedId = id;
  renderList();
  const ticket = await api(`/api/tickets/${id}`);
  const [messages, audit] = await Promise.all([
    api(`/api/tickets/${id}/messages?include_private=true`),
    api(`/api/tickets/${id}/audit`),
  ]);
  $("detail").className = "detail";
  $("detail").innerHTML = `
    <div class="detail-head">
      <div><p class="eyebrow">TICKET #FD-${String(ticket.id).padStart(4, "0")}</p>
        <h2>${escapeHtml(ticket.title)}</h2>
        <div class="detail-meta">${escapeHtml(ticket.customer_name)} · ${escapeHtml(ticket.customer_email)}</div>
      </div><span class="pill ${ticket.priority.toLowerCase()}">${pretty(ticket.priority)}</span>
    </div>
    <p class="detail-meta description">${escapeHtml(ticket.description)}</p>
    <div class="pills"><span class="pill">${pretty(ticket.status)}</span>
      <span class="pill">${ticket.assigned_agent ? `Assigned: ${escapeHtml(ticket.assigned_agent)}` : "Unassigned"}</span>
    </div>
    <div class="due">First response due: <strong>${formatDate(ticket.first_response_due_at)}</strong>
      ${ticket.response_late ? '<span class="late">· LATE</span>' : ""}<br>
      Resolution due: <strong>${formatDate(ticket.resolution_due_at)}</strong>
      ${ticket.resolution_late ? '<span class="late">· LATE</span>' : ""}
    </div>
    <div class="actions">
      ${!ticket.assigned_agent ? '<button id="claim">Take this ticket</button>' : ""}
      ${ticket.status !== "WAITING_CUSTOMER" ? '<button id="wait">Waiting on customer</button>' : ""}
      ${ticket.status !== "IN_PROGRESS" ? '<button id="progress">In progress</button>' : ""}
      ${!["RESOLVED", "CLOSED"].includes(ticket.status) ? '<button id="resolve">Mark resolved</button>' : ""}
      ${["RESOLVED", "CLOSED"].includes(ticket.status) ? '<button id="reopen">Reopen</button>' : ""}
      <select id="priorityChange" aria-label="Change priority">
        <option value="">Change priority</option>
        <option value="URGENT">Urgent</option><option value="MEDIUM">Medium</option><option value="SMALL">Small</option>
      </select>
    </div>
    <h3 class="section-title">Conversation & internal notes</h3>
    <div>${messages.map((message) => `
      <div class="message ${message.is_private ? "private" : ""}">
        <small>${message.is_private ? "PRIVATE NOTE" : "PUBLIC REPLY"} · ${escapeHtml(message.author)} · ${formatDate(message.created_at)}</small>
        ${escapeHtml(message.body)}
      </div>`).join("") || '<p class="detail-meta">No messages yet.</p>'}</div>
    ${["RESOLVED", "CLOSED"].includes(ticket.status) ? "" : `
      <div class="compose"><textarea id="messageBody" rows="3" maxlength="10000" placeholder="Write a customer reply or internal note..."></textarea>
        <div class="compose-row">
          <label><input id="privateNote" type="checkbox"> Private team note</label>
          <label><input id="waitForCustomer" type="checkbox"> Waiting for customer reply</label>
          <button class="primary" id="sendMessage">Send message</button>
        </div>
      </div>`}
    <div class="audit"><h3>Immutable activity history</h3>
      ${audit.map((entry) => `<div class="audit-item"><strong>${escapeHtml(pretty(entry.action))}</strong>
        · ${escapeHtml(entry.actor)} · ${formatDate(entry.created_at)}<br>${escapeHtml(entry.details)}</div>`).join("")}
    </div>`;

  const action = async (path, body) => {
    try {
      await api(path, { method: "POST", body: JSON.stringify(body) });
      await load();
    } catch (error) {
      showError(error);
    }
  };
  if ($("claim")) $("claim").onclick = () => action(`/api/tickets/${id}/claim`, { agent: agent() });
  if ($("wait")) $("wait").onclick = () => action(`/api/tickets/${id}/status`, { status: "WAITING_CUSTOMER", actor: agent() });
  if ($("progress")) $("progress").onclick = () => action(`/api/tickets/${id}/status`, { status: "IN_PROGRESS", actor: agent() });
  if ($("resolve")) $("resolve").onclick = () => action(`/api/tickets/${id}/status`, { status: "RESOLVED", actor: agent() });
  if ($("reopen")) $("reopen").onclick = () => action(`/api/tickets/${id}/status`, { status: "IN_PROGRESS", actor: agent() });
  $("priorityChange").onchange = (event) => {
    if (event.target.value) action(`/api/tickets/${id}/priority`, { priority: event.target.value, actor: agent() });
  };
  if ($("sendMessage")) $("sendMessage").onclick = () => {
    const body = $("messageBody").value.trim();
    if (!body) return;
    action(`/api/tickets/${id}/messages`, {
      author: agent(),
      body,
      is_private: $("privateNote").checked,
      waiting_for_customer: $("waitForCustomer").checked,
    });
  };
}

$("newBtn").onclick = () => $("createDialog").showModal();
$("closeDialog").onclick = () => $("createDialog").close();
$("createForm").onsubmit = async (event) => {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(event.target));
  data.actor = agent();
  try {
    const ticket = await api("/api/tickets", { method: "POST", body: JSON.stringify(data) });
    $("createDialog").close();
    event.target.reset();
    await load();
    await openTicket(ticket.id);
  } catch (error) {
    showError(error);
  }
};

["searchFilter", "statusFilter", "priorityFilter", "ownerFilter"].forEach((id) => {
  $(id).addEventListener(id === "searchFilter" ? "input" : "change", renderList);
});
load().catch((error) => window.alert(`Could not load Frontdesk: ${error.message}`));
window.setInterval(() => load().catch(showError), 60000);
