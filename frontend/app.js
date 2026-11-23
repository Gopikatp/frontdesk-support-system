const $ = (id) => document.getElementById(id);
const API_BASE = (window.FRONTDESK_API_BASE || "").replace(/\/$/, "");
const portalRole = window.location.pathname === "/admin/login" ? "ADMIN" : "AGENT";
let tickets = [];
let selectedId = null;
let authToken = sessionStorage.getItem("frontdesk_token");
let currentUser = null;
let refreshTimer = null;

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
  const headers = { "Content-Type": "application/json", ...options.headers };
  if (authToken) headers.Authorization = `Bearer ${authToken}`;
  const response = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers,
  });
  const data = await response.json().catch(() => ({ detail: "Unexpected server response" }));
  if (response.status === 401 && authToken && path !== "/api/auth/login") {
    expireSession();
  }
  if (!response.ok) throw new Error(data.detail || "Request failed");
  return data;
}

function showLogin(message = "") {
  $("loginScreen").hidden = false;
  $("appScreen").hidden = true;
  const isAdmin = portalRole === "ADMIN";
  $("loginTitle").textContent = isAdmin ? "Admin sign in" : "Staff sign in";
  $("loginDescription").textContent = isAdmin
    ? "Sign in with your administrator account to manage staff accounts and support operations."
    : "Sign in with the staff account provided to you by your administrator.";
  $("loginError").textContent = message;
  $("loginError").hidden = !message;
}

function expireSession(message = "Your session expired. Please sign in again.") {
  if (refreshTimer) {
    window.clearInterval(refreshTimer);
    refreshTimer = null;
  }
  sessionStorage.removeItem("frontdesk_token");
  authToken = null;
  currentUser = null;
  selectedId = null;
  showLogin(message);
}

function showApp(user) {
  currentUser = user;
  $("currentUser").textContent = `${user.display_name} (${pretty(user.role)})`;
  $("manageUsers").hidden = user.role !== "ADMIN";
  $("loginScreen").hidden = true;
  $("appScreen").hidden = false;
  if (!refreshTimer) refreshTimer = window.setInterval(() => {
    if (authToken) load().catch(showError);
  }, 60000);
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
  $("exportTickets").dataset.url = `${API_BASE}/api/tickets/export.csv${params.size ? `?${params}` : ""}`;
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
  const ownsTicket = ticket.assigned_agent === currentUser.display_name;
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
      ${ticket.assigned_agent && !ownsTicket ? `<span class="detail-meta">Owned by ${escapeHtml(ticket.assigned_agent)}; only the owner can update it.</span>` : ""}
      ${ownsTicket && ticket.status !== "WAITING_CUSTOMER" ? '<button id="wait">Waiting on customer</button>' : ""}
      ${ownsTicket && ticket.status !== "IN_PROGRESS" ? '<button id="progress">In progress</button>' : ""}
      ${ownsTicket && !["RESOLVED", "CLOSED"].includes(ticket.status) ? '<button id="resolve">Mark resolved</button>' : ""}
      ${ownsTicket && ["RESOLVED", "CLOSED"].includes(ticket.status) ? '<button id="reopen">Reopen</button>' : ""}
      ${ownsTicket && !["RESOLVED", "CLOSED"].includes(ticket.status) ? `<select id="priorityChange" aria-label="Change priority">
        <option value="">Change priority</option>
        <option value="URGENT">Urgent</option><option value="MEDIUM">Medium</option><option value="SMALL">Small</option>
      </select>` : ""}
    </div>
    <h3 class="section-title">Conversation & internal notes</h3>
    <div>${messages.map((message) => `
      <div class="message ${message.is_private ? "private" : ""}">
        <small>${message.is_private ? "PRIVATE NOTE" : "PUBLIC REPLY"} · ${escapeHtml(message.author)} · ${formatDate(message.created_at)}</small>
        ${escapeHtml(message.body)}
      </div>`).join("") || '<p class="detail-meta">No messages yet.</p>'}</div>
    ${!ownsTicket || ["RESOLVED", "CLOSED"].includes(ticket.status) ? "" : `
      <div class="compose"><textarea id="messageBody" rows="3" maxlength="10000" placeholder="Write a customer reply or internal note..."></textarea>
        <div class="compose-row">
          <label><input id="privateNote" type="checkbox"> Private team note</label>
          <label><input id="waitForCustomer" type="checkbox"> Waiting for customer reply</label>
          <button class="primary" id="sendMessage">Send message</button>
        </div>
      </div>`}
    <div class="audit"><h3>Activity history</h3>
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
  if ($("claim")) $("claim").onclick = () => action(`/api/tickets/${id}/claim`, {});
  if ($("wait")) $("wait").onclick = () => action(`/api/tickets/${id}/status`, { status: "WAITING_CUSTOMER" });
  if ($("progress")) $("progress").onclick = () => action(`/api/tickets/${id}/status`, { status: "IN_PROGRESS" });
  if ($("resolve")) $("resolve").onclick = () => action(`/api/tickets/${id}/status`, { status: "RESOLVED" });
  if ($("reopen")) $("reopen").onclick = () => action(`/api/tickets/${id}/status`, { status: "IN_PROGRESS" });
  if ($("priorityChange")) $("priorityChange").onchange = (event) => {
    if (event.target.value) action(`/api/tickets/${id}/priority`, { priority: event.target.value });
  };
  if ($("sendMessage")) $("sendMessage").onclick = () => {
    const body = $("messageBody").value.trim();
    if (!body) return;
    action(`/api/tickets/${id}/messages`, {
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

async function loadAccounts() {
  const users = await api("/api/users");
  $("userList").innerHTML = users.map((user) => `
    <div class="user-row">
      <div><strong>${escapeHtml(user.display_name)}</strong><small>${escapeHtml(user.email)} · ${pretty(user.role)}</small></div>
      <span class="pill ${user.is_active ? "small" : "urgent"}">${user.is_active ? "Active" : "Disabled"}</span>
      ${user.id === currentUser.id ? "" : `<button class="secondary" data-reset-user="${user.id}">Reset password</button><button class="secondary" data-user-id="${user.id}" data-active="${user.is_active}">${user.is_active ? "Disable" : "Enable"}</button>`}
    </div>`).join("");
  document.querySelectorAll("[data-user-id]").forEach((button) => {
    button.onclick = async () => {
      try {
        await api(`/api/users/${button.dataset.userId}`, {
          method: "PATCH",
          body: JSON.stringify({ is_active: button.dataset.active !== "true" }),
        });
        await loadAccounts();
      } catch (error) {
        showError(error);
      }
    };
  });
  document.querySelectorAll("[data-reset-user]").forEach((button) => {
    button.onclick = () => {
      $("resetPasswordForm").elements.namedItem("account_id").value = button.dataset.resetUser;
      $("resetPasswordDialog").showModal();
    };
  });
}

$("manageUsers").onclick = async () => {
  try {
    await loadAccounts();
    $("usersDialog").showModal();
  } catch (error) {
    showError(error);
  }
};
$("closeUsersDialog").onclick = () => $("usersDialog").close();
$("closeResetPasswordDialog").onclick = () => $("resetPasswordDialog").close();
$("resetPasswordForm").onsubmit = async (event) => {
  event.preventDefault();
  const form = Object.fromEntries(new FormData(event.target));
  try {
    await api(`/api/users/${form.account_id}/password`, {
      method: "POST",
      body: JSON.stringify({ password: form.password }),
    });
    event.target.reset();
    $("resetPasswordDialog").close();
    window.alert("Password reset. The user must sign in again.");
  } catch (error) {
    showError(error);
  }
};
$("userForm").onsubmit = async (event) => {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(event.target));
  try {
    await api("/api/users", { method: "POST", body: JSON.stringify(data) });
    event.target.reset();
    await loadAccounts();
  } catch (error) {
    showError(error);
  }
};

$("changePassword").onclick = () => $("passwordDialog").showModal();
$("closePasswordDialog").onclick = () => $("passwordDialog").close();
$("passwordForm").onsubmit = async (event) => {
  event.preventDefault();
  const form = Object.fromEntries(new FormData(event.target));
  try {
    await api("/api/auth/password", { method: "POST", body: JSON.stringify(form) });
    event.target.reset();
    $("passwordDialog").close();
    expireSession("Password changed. Sign in again with your new password.");
  } catch (error) {
    showError(error);
  }
};

async function downloadExport() {
  try {
    const response = await fetch($("exportTickets").dataset.url, {
      headers: { Authorization: `Bearer ${authToken}` },
    });
    if (response.status === 401) expireSession();
    if (!response.ok) throw new Error("Could not export tickets");
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement("a");
    link.href = url;
    link.download = "frontdesk-tickets.csv";
    link.click();
    URL.revokeObjectURL(url);
  } catch (error) {
    showError(error);
  }
}
$("exportTickets").onclick = downloadExport;

$("loginForm").onsubmit = async (event) => {
  event.preventDefault();
  $("loginError").hidden = true;
  const form = Object.fromEntries(new FormData(event.target));
  try {
    const result = await api("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ ...form, role: portalRole }),
    });
    authToken = result.access_token;
    sessionStorage.setItem("frontdesk_token", authToken);
    event.target.reset();
    showApp(result.user);
    await load();
  } catch (error) {
    $("loginError").textContent = error.message;
    $("loginError").hidden = false;
  }
};

$("logout").onclick = async () => {
  try {
    await api("/api/auth/logout", { method: "POST" });
  } catch (error) {
    if (authToken) showError(error);
  } finally {
    if (refreshTimer) {
      window.clearInterval(refreshTimer);
      refreshTimer = null;
    }
    sessionStorage.removeItem("frontdesk_token");
    authToken = null;
    currentUser = null;
    $("appScreen").hidden = true;
    showLogin();
  }
};

["searchFilter", "statusFilter", "priorityFilter", "ownerFilter"].forEach((id) => {
  $(id).addEventListener(id === "searchFilter" ? "input" : "change", renderList);
});

async function startApp() {
  if (!authToken) {
    showLogin();
    return;
  }
  try {
    const user = await api("/api/auth/me");
    if (portalRole && user.role !== portalRole) {
      await api("/api/auth/logout", { method: "POST" });
      expireSession("This account cannot use the selected sign-in. Choose the correct sign-in type.");
      return;
    }
    showApp(user);
    await load();
  } catch (error) {
    if (authToken) showLogin(error.message);
  }
}

startApp();
