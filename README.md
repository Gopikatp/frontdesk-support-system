# Frontdesk

Frontdesk is a support-ticket workspace for Arvo Systems. It replaces spreadsheet and chat-based tracking with a central inbox for ticket ownership, customer replies, private notes, SLA deadlines, and an append-only activity history.

## Separate frontend and backend

- `frontend/` contains the standalone HTML, CSS, and JavaScript client.
- `backend/` contains the FastAPI application, database models, business rules, and API routes.
- The frontend talks to the backend through `/api`. FastAPI serves the frontend assets in the default deployment so the project runs with one origin and no CORS setup. To deploy them separately, set `window.FRONTDESK_API_BASE` in `frontend/index.html` to the API URL and set `FRONTEND_ORIGIN` in the backend environment to the frontend origin.
- `main.py` remains as a compatibility entry point for existing `uvicorn main:app` setups.

## Features

- Create, search, filter, assign, and track support tickets.
- Filter inbox tickets by status, priority, and owner; export filtered ticket data to CSV.
- Atomic ticket claiming so only one agent can claim an unassigned ticket.
- Public replies and private team notes, kept distinct in the messages API and omitted from CSV exports.
- Email/password sign-in with salted PBKDF2 password hashes, expiring opaque bearer sessions, logout, and session revocation.
- Separate `/admin/login` and `/staff/login` portals, with the selected account role checked by the backend.
- Public customer registration and a customer portal for creating requests, viewing only their own tickets, and sending public replies.
- Repeated failed sign-ins are throttled; accounts can change passwords and administrators can reset passwords or disable accounts.
- Admin-managed staff accounts, roles, account disabling, password resets, and a view of registered customer accounts.
- Server-derived audit actors, so request bodies cannot impersonate agents.
- Security response headers and no-store API responses.
- Business-hour SLA calculations (Monday-Friday, 09:30-18:30), with a configurable festival-holiday calendar.
- Pause SLA time while waiting for a customer; optionally put a ticket into that state when sending a public reply.
- Automatically close waiting tickets after five working days without a customer response.
- Reopen resolved or closed tickets for up to seven calendar days.
- Only the assigned agent may update or message a claimed ticket; agents must claim a ticket before working it.
- Append-only activity event creation for ticket operations.
- Priority changes record the decision and calculate new targets from the change time.
- On startup, active tickets assigned to names with no active account are transferred to the initial active administrator and recorded in the audit history.
- Customer-created tickets are linked to the signed-in customer account, not just its email. Existing tickets remain staff-only rather than being exposed to someone who registers using a matching email address.
- Tests cover database URLs, authentication and throttling, account access, ticket ownership and transfer, private-note handling, and waiting status.

## Technology

- Python 3.11+
- FastAPI and SQLAlchemy
- PostgreSQL (deployment) or SQLite (tests)
- HTML, CSS, and vanilla JavaScript
- Docker Compose for local PostgreSQL

## Run locally (Windows PowerShell)

1. Create and activate a virtual environment:
   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```
2. Install dependencies:
   ```powershell
   pip install -r requirements.txt
   ```
3. Configure the database and first administrator. For PostgreSQL, start the included service and copy the example environment file:
   ```powershell
   docker compose up -d db
   Copy-Item .env.example .env
   ```
   Set `DATABASE_URL`, `ADMIN_EMAIL`, and a unique `ADMIN_PASSWORD` of at least 12 characters in `.env`. The first startup creates the administrator; later startups will not reset its password from environment variables.
4. Start the app from the repository root:
   ```powershell
   uvicorn backend.main:app --reload
   ```
   Or use the compatibility command `uvicorn main:app --reload`.
5. Access the portals:
   - Customers: http://127.0.0.1:8000 — customers can create an account themselves.
   - Staff: http://127.0.0.1:8000/staff/login
   - Administrator: http://127.0.0.1:8000/admin/login
   Only the administrator should create internal staff (`AGENT`) accounts. Customers can self-register; public registration always creates a `CUSTOMER` account and never grants staff access. Do not share administrator credentials.
6. Open:
   - App: http://127.0.0.1:8000
   - API docs: http://127.0.0.1:8000/docs
   - Health check: http://127.0.0.1:8000/api/health

Run the test suite with:
```powershell
python -m unittest discover -s tests
```

## API overview

- `POST /api/tickets` — create a ticket
- `GET /api/tickets` — list and filter tickets (`status`, `priority`, `q`, `assigned_agent`, `unassigned`)
- `GET /api/tickets/export.csv` — export filtered ticket data (never includes messages or private notes)
- `GET /api/tickets/{id}` — ticket details
- `POST /api/tickets/{id}/claim` — atomically claim an unassigned ticket
- `POST /api/tickets/{id}/messages` — add a public reply or private note
- `GET /api/tickets/{id}/messages?include_private=false` — list public messages by default
- `POST /api/tickets/{id}/status` — change ticket status
- `POST /api/tickets/{id}/priority` — change priority
- `GET /api/tickets/{id}/audit` — activity history
- `GET /api/health` — health check
- `POST /api/auth/login` — sign in and create a session
- `POST /api/auth/register` — create a customer-only account
- `GET /api/auth/me` — current account
- `POST /api/auth/logout` — revoke current session
- `POST /api/auth/password` — change current password and revoke sessions
- `GET/POST /api/users` — administrator-only account management
- `PATCH /api/users/{id}` and `POST /api/users/{id}/password` — administrator-only account disable/enable and password reset

The login request includes a `role` of `ADMIN`, `AGENT`, or `CUSTOMER` (omitting it defaults to `AGENT`). The server rejects credentials whose account role does not match. Customers can only access tickets linked to their account ID, and the server always hides private notes and audit logs from them. Account-management routes remain administrator-only. When hosting the frontend separately, configure the static host to serve the customer page at `/` and `/customer/login`, and `index.html` at `/admin/login` and `/staff/login`.

## Business rules and production limitations

- Working hours are Monday-Friday, 09:30-18:30 in `APP_TIMEZONE` (defaults to `Asia/Kolkata`). Add official festival days to `HOLIDAYS` in `backend/main.py`.
- Medium resolution is interpreted as three working days; one working week is five working days.
- When priority changes, new SLA targets begin at the change time; the priority change is recorded in the audit history.
- User accounts improve access control, but this project is still a portfolio/demo, not production-ready. Before real customer data, add a formal database migration workflow, MFA or an identity provider, password recovery, monitoring/backups, and database-enforced audit immutability.
- On the first deployment, Render prompts for `ADMIN_EMAIL` and `ADMIN_PASSWORD` (minimum 12 characters). The initial administrator is created once; retain the credentials securely. Later changes to those environment variables do not reset the stored password.
- Customers can sign up at the deployed root URL and submit support requests. Email ownership is not verified yet; add email verification and abuse controls before using this for sensitive or high-volume customer support.
- Existing tickets are not automatically attached to newly registered customer accounts by matching email. This protects existing customer data; use staff access to handle older requests until a verified ownership flow is added.
- Share `/staff/login` with internal support staff. The administrator signs in at `/admin/login` to create their accounts. Never share administrator credentials.
- Audit entries are append-only through the application routes. Database permissions or triggers are required to make them immutable against direct database access.
