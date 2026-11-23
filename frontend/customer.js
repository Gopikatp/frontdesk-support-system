const $ = (id) => document.getElementById(id);
const API_BASE = (window.FRONTDESK_API_BASE || "").replace(/\/$/, "");
let customerToken = sessionStorage.getItem("frontdesk_customer_token");
let customer = null;
let customerTickets = [];
let selectedTicketId = null;

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
  if (customerToken) headers.Authorization = `Bearer ${customerToken}`;
  const response = await fetch(`${API_BASE}${path}`, { ...options, headers });
  const data = await response.json().catch(() => ({ detail: "Unexpected server response" }));
  if (response.status === 401 && customerToken && path !== "/api/auth/login") {
    showAuth();
  }
  if (!response.ok) throw new Error(data.detail || "Request failed");
  return data;
}

function showAuth() {
  sessionStorage.removeItem("frontdesk_customer_token");
  customerToken = null;
  customer = null;
  selectedTicketId = null;
  $("customerApp").hidden = true;
  $("authScreen").hidden = false;
}

function showMessage(id, message) {
  $(id).textContent = message;
  $(id).hidden = !message;
}

function renderTickets() {
  $("customerTicketList").innerHTML = customerTickets.length
    ? customerTickets.map((ticket) => `
      <article class="ticket-card ${selectedTicketId === ticket.id ? "selected" : ""}" data-ticket-id="${ticket.id}">
        <div class="ticket-top"><h3>#FD-${String(ticket.id).padStart(4, "0")} · ${escapeHtml(ticket.title)}</h3>
          <span class="status">${pretty(ticket.status)}</span></div>
        <p>Updated ${formatDate(ticket.updated_at)}</p>
        ${ticket.response_late || ticket.resolution_late ? '<span class="pill urgent">Overdue</span>' : ""}
      </article>`).join("")
    : '<div class="empty-list">You have not submitted any support requests yet.</div>';
  document.querySelectorAll("[data-ticket-id]").forEach((element) => {
    element.onclick = () => openTicket(Number(element.dataset.ticketId)).catch(showTicketError);
  });
}

async function loadTickets() {
  customerTickets = await api("/api/tickets");
  renderTickets();
  if (selectedTicketId && customerTickets.some((ticket) => ticket.id === selectedTicketId)) {
    await openTicket(selectedTicketId);
  } else if (selectedTicketId) {
    selectedTicketId = null;
    $("customerDetail").className = "detail empty";
    $("customerDetail").innerHTML = "<h3>Select a request</h3><p>Your support conversation will appear here.</p>";
  }
}

async function openTicket(id) {
  selectedTicketId = id;
  renderTickets();
  const [ticket, messages] = await Promise.all([
    api(`/api/tickets/${id}`),
    api(`/api/tickets/${id}/messages`),
  ]);
  $("customerDetail").className = "detail";
  $("customerDetail").innerHTML = `
    <div class="detail-head"><div><p class="eyebrow">TICKET #FD-${String(ticket.id).padStart(4, "0")}</p>
      <h2>${escapeHtml(ticket.title)}</h2></div><span class="pill">${pretty(ticket.status)}</span></div>
    <p class="detail-meta description">${escapeHtml(ticket.description)}</p>
    <div class="due">Submitted ${formatDate(ticket.created_at)}
      ${ticket.assigned_agent ? `<br>Support agent: ${escapeHtml(ticket.assigned_agent)}` : "<br>Waiting for an agent to pick up your request."}
    </div>
    <h3 class="section-title">Conversation</h3>
    <div>${messages.map((message) => `<div class="message">
      <small>${escapeHtml(message.author)} · ${formatDate(message.created_at)}</small>${escapeHtml(message.body)}
    </div>`).join("") || '<p class="detail-meta">Your request has been submitted. A support agent will reply here.</p>'}</div>
    ${["RESOLVED", "CLOSED"].includes(ticket.status) ? '<p class="detail-meta">This request is closed. Create a new request if you need more help.</p>' : `
      <form id="customerReplyForm" class="compose">
        <label for="customerReply">Reply to the support team</label>
        <textarea id="customerReply" name="body" rows="3" maxlength="10000" required></textarea>
        <button class="primary" type="submit">Send reply</button>
      </form>`}`;
  const form = $("customerReplyForm");
  if (form) form.onsubmit = async (event) => {
    event.preventDefault();
    const body = new FormData(form).get("body").toString().trim();
    if (!body) return;
    try {
      await api(`/api/tickets/${id}/messages`, {
        method: "POST",
        body: JSON.stringify({ body }),
      });
      await loadTickets();
    } catch (error) {
      showTicketError(error);
    }
  };
}

function showTicketError(error) {
  window.alert(error.message || "Something went wrong. Please try again.");
}

async function enterCustomerApp(account) {
  customer = account;
  $("customerName").textContent = account.display_name;
  $("authScreen").hidden = true;
  $("customerApp").hidden = false;
  await loadTickets();
}

$("showRegister").onclick = () => {
  $("signInPanel").hidden = true;
  $("registerPanel").hidden = false;
  showMessage("registerError", "");
};
$("showSignIn").onclick = () => {
  $("registerPanel").hidden = true;
  $("signInPanel").hidden = false;
  showMessage("signInError", "");
};

$("registerForm").onsubmit = async (event) => {
  event.preventDefault();
  showMessage("registerError", "");
  const form = Object.fromEntries(new FormData(event.target));
  try {
    await api("/api/auth/register", { method: "POST", body: JSON.stringify(form) });
    $("signInForm").elements.namedItem("email").value = form.email;
    $("registerPanel").hidden = true;
    $("signInPanel").hidden = false;
    event.target.reset();
    showMessage("signInError", "");
    showMessage("signInSuccess", "Account created. Sign in with your new email and password.");
  } catch (error) {
    showMessage("registerError", error.message);
  }
};

$("signInForm").onsubmit = async (event) => {
  event.preventDefault();
  showMessage("signInError", "");
  showMessage("signInSuccess", "");
  const form = Object.fromEntries(new FormData(event.target));
  try {
    const result = await api("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ ...form, role: "CUSTOMER" }),
    });
    customerToken = result.access_token;
    sessionStorage.setItem("frontdesk_customer_token", customerToken);
    event.target.reset();
    await enterCustomerApp(result.user);
  } catch (error) {
    showMessage("signInError", `${error.message}. If you are new, create a customer account first.`);
  }
};

$("newCustomerTicket").onclick = () => $("customerTicketDialog").showModal();
$("closeCustomerTicketDialog").onclick = () => $("customerTicketDialog").close();
$("customerTicketForm").onsubmit = async (event) => {
  event.preventDefault();
  const form = Object.fromEntries(new FormData(event.target));
  try {
    const ticket = await api("/api/tickets", {
      method: "POST",
      body: JSON.stringify({
        ...form,
        customer_name: customer.display_name,
        customer_email: customer.email,
      }),
    });
    event.target.reset();
    $("customerTicketDialog").close();
    await loadTickets();
    await openTicket(ticket.id);
  } catch (error) {
    showTicketError(error);
  }
};

$("customerLogout").onclick = async () => {
  try {
    await api("/api/auth/logout", { method: "POST" });
  } catch (error) {
    showTicketError(error);
  } finally {
    showAuth();
  }
};

async function startCustomerApp() {
  if (!customerToken) return;
  try {
    const account = await api("/api/auth/me");
    if (account.role !== "CUSTOMER") {
      showAuth();
      return;
    }
    await enterCustomerApp(account);
  } catch (error) {
    showAuth();
    showMessage("signInError", error.message || "Your session expired. Please sign in again.");
  }
}

startCustomerApp();
