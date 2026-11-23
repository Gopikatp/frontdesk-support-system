# Frontdesk

Frontdesk is a support-ticket workspace for Arvo Systems. It replaces spreadsheet and chat-based tracking with a central inbox for ticket ownership, customer replies, private notes, SLA deadlines, and an append-only activity history.

## Separate frontend and backend

- `frontend/` contains the standalone HTML, CSS, and JavaScript client.
- `backend/` contains the FastAPI application, database models, business rules, and API routes.
- The frontend talks to the backend through `/api`. FastAPI serves the frontend assets in the default deployment so the project runs with one origin and no CORS setup. To deploy them separately, set `window.FRONTDESK_API_BASE` in both `frontend/index.html` and `frontend/customer.html` to the API URL, and set `FRONTEND_ORIGIN` in the backend environment to the frontend origin.
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
- Database triggers prevent audit-event updates and deletions in PostgreSQL and SQLite.
- Security response headers and no-store API responses.
- Business-hour SLA calculations (Monday-Friday, 09:30-18:30), with a configurable festival-holiday calendar.
- Pause SLA time while waiting for a customer; optionally put a ticket into that state when sending a public reply.
- Automatically close waiting tickets after five working days without a customer response.
- Reopen resolved or closed tickets for up to seven calendar days.
- Only the assigned agent may update or message a claimed ticket; agents must claim a ticket before working it.
- Append-only activity event creation for ticket operations.
- Priority changes record the decision and calculate new targets from the change time.
- SLA targets: Urgent replies are due in 2 working hours and resolution by that workday's close; Medium replies are due in 4.5 working hours and resolution in 3 working days; Small replies are due in 1 working day and resolution in 5 working days.
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
   Set `DATABASE_URL`, `ADMIN_EMAIL`, and a unique `ADMIN_PASSWORD` of at least 12 characters in `.env`. Set `HOLIDAYS` to a comma-separated list of dates (`YYYY-MM-DD`) from Arvo's holiday calendar. The first startup creates the administrator; later startups will not reset its password from environment variables.
4. Start the app from the repository root:
   ```powershell
   uvicorn backend.main:app --reload
   ```
   Or use the compatibility command `uvicorn main:app --reload`.
5. Access the portals:
   - Customers: http://127.0.0.1:8000 — customers can create an account themselves.
   - Staff: http://127.0.0.1:8000/staff/login — full support dashboard with ticket search, status/priority/owner filters, ticket actions, and CSV export.
   - Administrator: http://127.0.0.1:8000/admin/login
   The customer portal links to staff sign-in; staff pages link back to the customer portal. Customer accounts only see their own requests and do not have staff dashboard controls.
   Only the administrator should create internal staff (`AGENT`) accounts. Customers can self-register; public registration always creates a `CUSTOMER` account and never grants staff access. Do not share administrator credentials.
6. Open:
   - App: http://127.0.0.1:8000
   - API docs: http://127.0.0.1:8000/docs
   - Health check: http://127.0.0.1:8000/api/health

Run the test suite with:
```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## Deploy

The repository includes a Render Blueprint in `render.yaml`. To deploy, push the project to a GitHub repository, create a new Blueprint on Render from that repository, and provide `ADMIN_EMAIL` and a unique `ADMIN_PASSWORD` when prompted. Render provisions the web service and PostgreSQL database from the Blueprint. Wait for `/api/health` to pass, then open the service URL and test the customer, staff, and admin routes. Keep the admin password private and do not commit `.env`.

The application URL is generated by the hosting provider; it is not embedded in this repository. Add the actual live URL to the handoff only after deployment succeeds, and keep the service available for the requested test period.

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

- Working hours are Monday-Friday, 09:30-18:30 in `APP_TIMEZONE` (defaults to `Asia/Kolkata`). Configure company festival dates with `HOLIDAYS` in `.env`.
- Medium resolution is interpreted as three working days; one working week is five working days.
- When priority changes, new SLA targets begin at the change time; the priority change is recorded in the audit history.
- Audit-event rows cannot be updated or deleted through the configured SQLite/PostgreSQL database triggers. A database owner or superuser can still disable/drop triggers; restrict database credentials and access accordingly.
- This project is a hiring-test/portfolio demo, not production-ready for real customer data. Formal schema migrations, MFA or an identity provider, password recovery, monitoring/backups, and customer email verification are not implemented.
- On the first deployment, Render prompts for `ADMIN_EMAIL` and `ADMIN_PASSWORD` (minimum 12 characters). The initial administrator is created once; retain the credentials securely. Later changes to those environment variables do not reset the stored password.
- Customers can sign up at the deployed root URL and submit support requests. Email ownership is not verified yet; add email verification and abuse controls before using this for sensitive or high-volume customer support.
- Existing tickets are not automatically attached to newly registered customer accounts by matching email. This protects existing customer data; use staff access to handle older requests until a verified ownership flow is added.
- Share `/staff/login` with internal support staff. The administrator signs in at `/admin/login` to create their accounts. Never share administrator credentials.

## Hiring-test handoff note

Implemented: the agent dashboard, role-based access, ticket claiming, private/public messages, SLA tracking, waiting/auto-close and reopen rules, and an append-only audit history protected by database triggers. Not finished: production hardening (email verification, MFA, password recovery, formal migrations, monitoring, and backups). These were kept out of this time-boxed exercise; do not use the demo with sensitive customer data. Send the live URL, repository URL, this README, and this note to the evaluator through the requested submission channel. Deploy first and include a live URL only after confirming it works.
