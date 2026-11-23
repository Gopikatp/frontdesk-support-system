# Frontdesk

Frontdesk is a support-ticket application . It replaces spreadsheet- and chat-based ticket tracking with a shared inbox, customer conversations, service-level tracking, and an audit history.

## Features

- **Support dashboard:** Search tickets; filter by status, priority, and owner; claim tickets; update status and priority; export ticket data as CSV.
- **Role-based access:** Customers self-register and see only their own requests. Staff handle tickets. Administrators manage staff and customer accounts.
- **Private notes:** Staff can add internal notes separately from customer replies. Customer-facing message responses and CSV exports exclude private notes.
- **Service-level tracking:** Deadlines use Monday–Friday working hours, 09:30–18:30, in the configured timezone, excluding configured holidays. Urgent tickets have a two-working-hour response target and same-working-day resolution target; Medium tickets have a 4.5-working-hour response target and three-working-day resolution target; Small tickets have a one-working-day response target and five-working-day resolution target.
- **Waiting and reopening:** SLA time pauses while a ticket waits for a customer. Tickets waiting five working days without a customer response are automatically closed. Resolved or closed tickets can be reopened within seven calendar days.
- **Ownership and history:** Ticket claiming is atomic, so only one agent can claim an unassigned ticket. Ticket actions record the actor, action, details, and timestamp. Database triggers prevent audit-event updates and deletions.
- **Account security:** Passwords are stored as salted PBKDF2 hashes; sessions expire and can be revoked. Failed sign-ins are throttled.

## Technology

- Python, FastAPI, SQLAlchemy
- PostgreSQL for deployment; SQLite is used by the automated tests
- HTML, CSS, and vanilla JavaScript
- Docker Compose for local PostgreSQL

## Run locally

The following commands are for Windows PowerShell, run from the project root.

1. Create and activate a virtual environment:

   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

2. Install dependencies and start the local PostgreSQL database:

   ```powershell
   pip install -r requirements.txt
   docker compose up -d db
   ```

3. Create `.env` from the example and configure the initial administrator:

   ```powershell
   Copy-Item .env.example .env
   ```

   Set `ADMIN_EMAIL` and a unique `ADMIN_PASSWORD` of at least 12 characters in `.env`. Keep this file private; it is excluded from Git. The first application startup creates the administrator. Changing these environment values later does not change the password of an existing account.

   Optionally set `HOLIDAYS` to comma-separated dates in `YYYY-MM-DD` format, for example `2026-12-25,2027-01-01`. Leave it blank if there are no dates to exclude. `APP_TIMEZONE` defaults to `Asia/Kolkata`.

4. Start the application:

   ```powershell
   uvicorn backend.main:app --reload
   ```

   Alternatively, `uvicorn main:app --reload` is supported from the project root.

5. Open the appropriate page:

   | User | Local URL |
   |---|---|
   | Customer | <http://127.0.0.1:8000/> |
   | Staff | <http://127.0.0.1:8000/staff/login> |
   | Administrator | <http://127.0.0.1:8000/admin/login> |
   | API documentation | <http://127.0.0.1:8000/docs> |
   | Health check | <http://127.0.0.1:8000/api/health> |

   Customers can create their own customer accounts. Only an administrator can create internal staff or administrator accounts. Do not share your administrator credentials; create a separate account for each person who needs access.

## Tests

Run the backend test suite from the project root:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The tests cover authentication and authorization, customer data isolation, ticket claiming and ownership, private-note visibility, SLA calculations, waiting-ticket closure, reopening, database migration behavior, and audit-event protections.

## Deployment

The repository includes a Render Blueprint in [`render.yaml`](./render.yaml). To deploy, push the repository to GitHub, create a Blueprint in Render from that repository, and provide `ADMIN_EMAIL` and a unique `ADMIN_PASSWORD` when prompted. The Blueprint describes a web service and PostgreSQL database. After deployment, verify `/api/health` and test the customer, staff, and administrator flows.


## Configuration

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | Database connection string. The example uses the local Docker PostgreSQL service. |
| `APP_TIMEZONE` | Timezone used for SLA calculations; defaults to `Asia/Kolkata`. |
| `HOLIDAYS` | Optional comma-separated holiday dates in `YYYY-MM-DD` format. |
| `ADMIN_NAME` | Display name for the initial administrator. |
| `ADMIN_EMAIL` | Email address for the initial administrator. |
| `ADMIN_PASSWORD` | Initial administrator password; at least 12 characters. |
| `FRONTEND_ORIGIN` | Optional allowed origin when hosting the frontend separately. |

## Important limitations

- Customer email addresses are not verified. Email verification, password recovery, MFA, monitoring, backups, and a formal database migration workflow are not implemented.
- Audit triggers protect records from ordinary database updates and deletions, but a database owner or superuser can disable or remove those triggers. Restrict database access appropriately.
- Newly registered customers do not automatically gain access to older tickets with a matching email address; this avoids exposing tickets without verified ownership.

## Hiring-test handoff

**Completed:** The customer portal, staff dashboard, administrator account management, ticket ownership, private/public conversations, SLA handling, and activity history are implemented and covered by tests.

**Not completed:** Production hardening such as customer email verification, password recovery, MFA, formal migrations, monitoring, and backups was outside the time-boxed implementation. The application should be treated as a demo and not used for sensitive customer data.


