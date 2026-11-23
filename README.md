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
- Business-hour SLA calculations (Monday-Friday, 09:30-18:30), with a configurable festival-holiday calendar.
- Pause SLA time while waiting for a customer; optionally put a ticket into that state when sending a public reply.
- Automatically close waiting tickets after five working days without a customer response.
- Reopen resolved or closed tickets for up to seven calendar days.
- Append-only activity event creation for ticket operations.
- Priority changes record the decision and calculate new targets from the change time.
- Tests covering weekend SLA calculations, ticket ownership, private-note handling, and waiting status.

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
3. Configure the database. For PostgreSQL, start the included service and copy the example environment file:
   ```powershell
   docker compose up -d db
   Copy-Item .env.example .env
   ```
   Set `DATABASE_URL` in `.env` to the connection string for your local database.
4. Start the app from the repository root:
   ```powershell
   uvicorn backend.main:app --reload
   ```
   Or use the compatibility command `uvicorn main:app --reload`.
5. Open:
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

## Business rules and production limitations

- Working hours are Monday-Friday, 09:30-18:30 in `APP_TIMEZONE` (defaults to `Asia/Kolkata`). Add official festival days to `HOLIDAYS` in `backend/main.py`.
- Medium resolution is interpreted as three working days; one working week is five working days.
- When priority changes, new SLA targets begin at the change time; the priority change is recorded in the audit history.
- The agent selector is a demo identity picker, not authentication. Before production, add authenticated users, role-based/customer-specific authorization, migrations, delivery/notification integrations, and database-enforced audit immutability.
- The dashboard is an agent interface and requests private notes explicitly. Do not expose it or the `include_private=true` API to customers; a customer-facing API must always force public-only messages server-side and enforce customer ownership.
- Audit entries are append-only through the application routes. Database permissions or triggers are required to make them immutable against direct database access.
