import unittest
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend import main


class BackendTests(unittest.TestCase):
    def test_holiday_dates_parse_and_invalid_dates_are_rejected(self):
        holidays = main.parse_holidays("2026-01-01, 2026-12-25")
        self.assertEqual(
            holidays,
            {main.date(2026, 1, 1), main.date(2026, 12, 25)},
        )
        with self.assertRaisesRegex(ValueError, "HOLIDAYS must be"):
            main.parse_holidays("not-a-date")

    def test_database_urls_use_the_installed_postgres_driver(self):
        self.assertEqual(
            main.normalize_database_url("postgres://user:pass@db.example/app"),
            "postgresql+psycopg://user:pass@db.example/app",
        )
        self.assertEqual(
            main.normalize_database_url("postgresql://user:pass@db.example/app"),
            "postgresql+psycopg://user:pass@db.example/app",
        )
        self.assertEqual(main.normalize_database_url("sqlite:///app.db"), "sqlite:///app.db")

    def test_existing_database_gets_customer_ticket_ownership_column(self):
        legacy_engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        try:
            with legacy_engine.begin() as connection:
                connection.exec_driver_sql("CREATE TABLE accounts (id INTEGER PRIMARY KEY)")
                connection.exec_driver_sql("CREATE TABLE tickets (id INTEGER PRIMARY KEY)")

            main.ensure_customer_ticket_ownership_column(legacy_engine)

            columns = {column["name"] for column in inspect(legacy_engine).get_columns("tickets")}
            foreign_keys = inspect(legacy_engine).get_foreign_keys("tickets")
            self.assertIn("customer_account_id", columns)
            self.assertTrue(any(key["referred_table"] == "accounts" for key in foreign_keys))
        finally:
            legacy_engine.dispose()

    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        main.Base.metadata.create_all(bind=self.engine)
        main.ensure_audit_immutability(self.engine)
        self.db = Session(self.engine, expire_on_commit=False)
        self.agent = main.Account(
            email="agent@example.com",
            display_name="Agent One",
            password_hash=main.hash_password("correct horse battery staple"),
            role="AGENT",
        )
        self.db.add(self.agent)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def create_ticket(self, title="Unable to sign in"):
        return main.create_ticket(
            main.TicketCreate(
                title=title,
                customer_name="A Customer",
                customer_email="customer@example.com",
                description="The sign-in page is unavailable.",
            ),
            self.db,
            self.agent,
        )

    def create_customer(self, email="customer@example.com", name="A Customer"):
        customer = main.Account(
            email=email,
            display_name=name,
            password_hash=main.hash_password("customer password long enough"),
            role="CUSTOMER",
        )
        self.db.add(customer)
        self.db.commit()
        return customer

    def test_sla_hours_skip_weekend(self):
        friday_evening_utc = main.datetime(2024, 5, 3, 12, 30)
        due = main.add_working_hours(friday_evening_utc, 1.5)
        self.assertEqual(due, main.datetime(2024, 5, 6, 5, 0))

    def test_sla_hours_skip_configured_holidays(self):
        with patch.object(main, "HOLIDAYS", {main.date(2024, 5, 6)}):
            due = main.add_working_hours(main.datetime(2024, 5, 3, 12, 30), 1.5)
        self.assertEqual(due, main.datetime(2024, 5, 7, 5, 0))

    def test_ticket_claim_is_exclusive(self):
        ticket = self.create_ticket()
        main.claim_ticket(ticket["id"], main.ClaimRequest(), self.db, self.agent)
        other_agent = main.Account(
            email="other@example.com",
            display_name="Agent Two",
            password_hash=main.hash_password("correct horse battery staple"),
            role="AGENT",
        )

        with self.assertRaises(HTTPException) as error:
            main.claim_ticket(ticket["id"], main.ClaimRequest(), self.db, other_agent)

        self.assertEqual(error.exception.status_code, 409)
        saved = self.db.get(main.Ticket, ticket["id"])
        self.assertEqual(saved.assigned_agent, "Agent One")

    def test_only_ticket_owner_can_update_or_message_ticket(self):
        ticket = self.create_ticket()
        other_agent = main.Account(
            email="other@example.com",
            display_name="Agent Two",
            password_hash=main.hash_password("correct horse battery staple"),
            role="AGENT",
        )
        with self.assertRaises(HTTPException) as status_error:
            main.update_status(
                ticket["id"], main.StatusUpdate(status="RESOLVED"), self.db, other_agent
            )
        self.assertEqual(status_error.exception.status_code, 409)

        with self.assertRaises(HTTPException) as message_error:
            main.add_message(
                ticket["id"], main.MessageCreate(body="Unauthorized update"),
                self.db, other_agent,
            )
        self.assertEqual(message_error.exception.status_code, 409)

    def test_private_notes_are_excluded_from_public_messages_and_csv(self):
        ticket = self.create_ticket("=1+1")
        main.claim_ticket(ticket["id"], main.ClaimRequest(), self.db, self.agent)
        main.add_message(
            ticket["id"],
            main.MessageCreate(body="Internal-only detail", is_private=True),
            self.db,
            self.agent,
        )

        public_messages = main.list_messages(
            ticket["id"], include_private=False, db=self.db, account=self.agent
        )
        all_messages = main.list_messages(
            ticket["id"], include_private=True, db=self.db, account=self.agent
        )
        csv_body = main.ticket_csv(
            self.db,
            main.ticket_statement(None, None, None, None),
        )

        self.assertEqual(public_messages, [])
        self.assertEqual(len(all_messages), 1)
        self.assertNotIn("Internal-only detail", csv_body)
        self.assertIn("'=1+1", csv_body)

    def test_audit_events_cannot_be_updated_or_deleted_in_database(self):
        ticket = self.create_ticket()
        event = self.db.scalar(
            main.select(main.AuditEvent).where(main.AuditEvent.ticket_id == ticket["id"])
        )

        with self.assertRaises(IntegrityError):
            with self.engine.begin() as connection:
                connection.exec_driver_sql(
                    "UPDATE audit_events SET details = 'changed' WHERE id = ?",
                    (event.id,),
                )
        with self.assertRaises(IntegrityError):
            with self.engine.begin() as connection:
                connection.exec_driver_sql(
                    "DELETE FROM audit_events WHERE id = ?",
                    (event.id,),
                )

    def test_public_reply_can_pause_ticket_for_customer(self):
        ticket = self.create_ticket()
        main.claim_ticket(ticket["id"], main.ClaimRequest(), self.db, self.agent)
        main.add_message(
            ticket["id"],
            main.MessageCreate(body="Could you share a screenshot?", waiting_for_customer=True),
            self.db,
            self.agent,
        )

        saved = self.db.get(main.Ticket, ticket["id"])
        self.assertEqual(saved.status, "WAITING_CUSTOMER")
        self.assertIsNotNone(saved.waiting_since)

    def test_public_reply_resumes_waiting_ticket(self):
        ticket = self.create_ticket()
        main.claim_ticket(ticket["id"], main.ClaimRequest(), self.db, self.agent)
        main.update_status(
            ticket["id"], main.StatusUpdate(status="WAITING_CUSTOMER"), self.db, self.agent
        )
        main.add_message(
            ticket["id"], main.MessageCreate(body="Here is the requested update."),
            self.db, self.agent,
        )

        saved = self.db.get(main.Ticket, ticket["id"])
        self.assertEqual(saved.status, "IN_PROGRESS")
        self.assertIsNone(saved.waiting_since)

    def test_waiting_ticket_auto_closes_after_five_working_days(self):
        ticket = self.create_ticket()
        main.claim_ticket(ticket["id"], main.ClaimRequest(), self.db, self.agent)
        main.update_status(
            ticket["id"], main.StatusUpdate(status="WAITING_CUSTOMER"), self.db, self.agent
        )
        now = main.utcnow()
        saved = self.db.get(main.Ticket, ticket["id"])
        saved.waiting_since = now - main.timedelta(days=10)
        self.db.commit()

        closed_count = main.auto_close_stale_waiting_tickets(self.db, now)

        self.assertEqual(closed_count, 1)
        self.assertEqual(saved.status, "CLOSED")
        self.assertIsNone(saved.waiting_since)
        events = self.db.scalars(
            main.select(main.AuditEvent).where(
                main.AuditEvent.ticket_id == ticket["id"],
                main.AuditEvent.action == "AUTO_CLOSED",
            )
        ).all()
        self.assertEqual(len(events), 1)

    def test_priority_change_during_wait_restarts_pause_at_change_time(self):
        ticket = self.create_ticket()
        main.claim_ticket(ticket["id"], main.ClaimRequest(), self.db, self.agent)
        main.update_status(
            ticket["id"], main.StatusUpdate(status="WAITING_CUSTOMER"), self.db, self.agent
        )
        original_wait = self.db.get(main.Ticket, ticket["id"]).waiting_since

        main.update_priority(
            ticket["id"], main.PriorityUpdate(priority="URGENT"), self.db, self.agent
        )

        saved = self.db.get(main.Ticket, ticket["id"])
        self.assertEqual(saved.status, "WAITING_CUSTOMER")
        self.assertGreaterEqual(saved.waiting_since, original_wait)

    def test_expired_ticket_cannot_be_reopened(self):
        ticket = self.create_ticket()
        main.claim_ticket(ticket["id"], main.ClaimRequest(), self.db, self.agent)
        main.update_status(
            ticket["id"], main.StatusUpdate(status="RESOLVED"), self.db, self.agent
        )
        saved = self.db.get(main.Ticket, ticket["id"])
        saved.resolved_at = main.utcnow() - main.timedelta(days=8)

        with self.assertRaises(HTTPException) as error:
            main.update_status(
                ticket["id"], main.StatusUpdate(status="IN_PROGRESS"), self.db, self.agent
            )

        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(saved.status, "RESOLVED")

    def test_resolved_ticket_can_be_reopened_within_seven_days(self):
        ticket = self.create_ticket()
        main.claim_ticket(ticket["id"], main.ClaimRequest(), self.db, self.agent)
        main.update_status(
            ticket["id"], main.StatusUpdate(status="RESOLVED"), self.db, self.agent
        )

        reopened = main.update_status(
            ticket["id"], main.StatusUpdate(status="IN_PROGRESS"), self.db, self.agent
        )

        self.assertEqual(reopened["status"], "IN_PROGRESS")
        self.assertIsNone(self.db.get(main.Ticket, ticket["id"]).resolved_at)

    def test_password_hash_and_bearer_session(self):
        stored = main.hash_password("correct horse battery staple")
        self.assertTrue(main.verify_password("correct horse battery staple", stored))
        self.assertFalse(main.verify_password("wrong password", stored))

        token = "test-session-token"
        self.db.add(main.LoginSession(
            account_id=self.agent.id,
            token_hash=main.hashlib.sha256(token.encode()).hexdigest(),
            expires_at=main.utcnow() + main.timedelta(hours=1),
        ))
        self.db.commit()
        authenticated = main.get_current_account(
            HTTPAuthorizationCredentials(scheme="Bearer", credentials=token),
            self.db,
        )
        self.assertEqual(authenticated.id, self.agent.id)

    def test_public_registration_creates_customer_only_and_customer_can_sign_in(self):
        response = main.register_customer(
            main.CustomerRegister(
                email="New.Customer@example.com",
                display_name="Agent One",
                password="customer password long enough",
            ),
            self.db,
        )
        account = self.db.scalar(
            main.select(main.Account).where(main.Account.email == "new.customer@example.com")
        )
        self.assertEqual(response["message"], "Customer account created. You can now sign in.")
        self.assertEqual(account.role, "CUSTOMER")
        self.assertTrue(account.display_name.startswith("Agent One ("))
        with self.assertRaises(HTTPException) as duplicate_error:
            main.register_customer(
                main.CustomerRegister(
                    email="new.customer@example.com",
                    display_name="New Customer",
                    password="customer password long enough",
                ),
                self.db,
            )
        self.assertEqual(duplicate_error.exception.status_code, 409)
        signed_in = main.login(
            main.LoginRequest(
                email="new.customer@example.com",
                password="customer password long enough",
                role="CUSTOMER",
            ),
            self.db,
        )
        self.assertEqual(signed_in["user"]["role"], "CUSTOMER")
        with self.assertRaises(HTTPException) as staff_portal_error:
            main.login(
                main.LoginRequest(
                    email="new.customer@example.com",
                    password="customer password long enough",
                    role="AGENT",
                ),
                self.db,
            )
        self.assertEqual(staff_portal_error.exception.status_code, 401)
        admin = main.Account(
            email="admin@example.com",
            display_name="Administrator",
            password_hash=main.hash_password("admin password long enough"),
            role="ADMIN",
        )
        self.db.add(admin)
        self.db.commit()
        managed_accounts = main.list_accounts(self.db, admin)
        self.assertIn(
            ("new.customer@example.com", "CUSTOMER"),
            {(item["email"], item["role"]) for item in managed_accounts},
        )

    def test_customer_ticket_creation_uses_authenticated_identity(self):
        customer = self.create_customer()
        ticket = main.create_ticket(
            main.TicketCreate(
                title="Need help with my order",
                customer_name="Impersonated Customer",
                customer_email="other@example.com",
                description="Please help.",
            ),
            self.db,
            customer,
        )
        saved = self.db.get(main.Ticket, ticket["id"])
        self.assertEqual(saved.customer_email, customer.email)
        self.assertEqual(saved.customer_name, customer.display_name)
        self.assertEqual(saved.customer_account_id, customer.id)

    def test_customer_only_sees_own_tickets_and_public_messages(self):
        customer = self.create_customer()
        own_ticket = main.create_ticket(
            main.TicketCreate(
                title="My private support request",
                customer_name=customer.display_name,
                customer_email=customer.email,
                description="Please help me.",
            ),
            self.db,
            customer,
        )
        main.claim_ticket(own_ticket["id"], main.ClaimRequest(), self.db, self.agent)
        main.add_message(
            own_ticket["id"],
            main.MessageCreate(body="Internal-only detail", is_private=True),
            self.db,
            self.agent,
        )
        other_customer = self.create_customer("other@example.com", "Other Customer")
        other_ticket = main.create_ticket(
            main.TicketCreate(
                title="Another customer's ticket",
                customer_name=other_customer.display_name,
                customer_email=other_customer.email,
                description="Private request.",
            ),
            self.db,
            other_customer,
        )

        visible_tickets = main.list_tickets(
            status=None, priority=None, q=None, assigned_agent=None,
            unassigned=False, db=self.db, account=customer,
        )
        visible_messages = main.list_messages(
            own_ticket["id"], include_private=True, db=self.db, account=customer
        )

        self.assertEqual([ticket["id"] for ticket in visible_tickets], [own_ticket["id"]])
        self.assertEqual(visible_messages, [])
        with self.assertRaises(HTTPException) as error:
            main.get_ticket(other_ticket["id"], self.db, customer)
        self.assertEqual(error.exception.status_code, 404)
        with self.assertRaises(HTTPException) as message_error:
            main.list_messages(
                other_ticket["id"], include_private=False, db=self.db, account=customer
            )
        self.assertEqual(message_error.exception.status_code, 404)
        with self.assertRaises(HTTPException) as audit_error:
            main.list_audit(own_ticket["id"], db=self.db, account=customer)
        self.assertEqual(audit_error.exception.status_code, 403)
        with self.assertRaises(HTTPException) as export_error:
            main.export_tickets(
                status=None, priority=None, q=None, assigned_agent=None,
                unassigned=False, db=self.db, account=customer,
            )
        self.assertEqual(export_error.exception.status_code, 403)

    def test_customer_signup_does_not_claim_older_tickets_with_same_email(self):
        older_ticket = self.create_ticket()
        customer = self.create_customer()

        visible_tickets = main.list_tickets(
            status=None, priority=None, q=None, assigned_agent=None,
            unassigned=False, db=self.db, account=customer,
        )

        self.assertEqual(visible_tickets, [])
        self.assertIsNone(self.db.get(main.Ticket, older_ticket["id"]).customer_account_id)

    def test_customer_can_reply_but_cannot_claim_or_change_ticket(self):
        customer = self.create_customer()
        ticket = main.create_ticket(
            main.TicketCreate(
                title="Customer reply test",
                customer_name=customer.display_name,
                customer_email=customer.email,
                description="Please contact me.",
            ),
            self.db,
            customer,
        )
        main.claim_ticket(ticket["id"], main.ClaimRequest(), self.db, self.agent)
        main.update_status(
            ticket["id"], main.StatusUpdate(status="WAITING_CUSTOMER"), self.db, self.agent
        )

        main.add_message(
            ticket["id"], main.MessageCreate(body="Here is the requested information."),
            self.db, customer,
        )
        saved = self.db.get(main.Ticket, ticket["id"])
        self.assertEqual(saved.status, "IN_PROGRESS")
        self.assertIsNone(saved.waiting_since)
        self.assertIsNone(saved.first_response_at)

        with self.assertRaises(HTTPException) as private_error:
            main.add_message(
                ticket["id"],
                main.MessageCreate(body="Attempted internal note", is_private=True),
                self.db,
                customer,
            )
        self.assertEqual(private_error.exception.status_code, 403)
        with self.assertRaises(HTTPException) as status_error:
            main.update_status(
                ticket["id"], main.StatusUpdate(status="RESOLVED"), self.db, customer
            )
        self.assertEqual(status_error.exception.status_code, 403)
        with self.assertRaises(HTTPException) as claim_error:
            main.claim_ticket(ticket["id"], main.ClaimRequest(), self.db, customer)
        self.assertEqual(claim_error.exception.status_code, 403)

    def test_login_throttles_repeated_bad_passwords(self):
        for _ in range(main.LOGIN_FAILURE_LIMIT):
            with self.assertRaises(HTTPException) as error:
                main.login(
                    main.LoginRequest(email=self.agent.email, password="wrong password"),
                    self.db,
                )
            self.assertEqual(error.exception.status_code, 401)

        with self.assertRaises(HTTPException) as locked:
            main.login(
                main.LoginRequest(email=self.agent.email, password="correct horse battery staple"),
                self.db,
            )
        self.assertEqual(locked.exception.status_code, 429)

    def test_login_enforces_selected_admin_or_staff_portal(self):
        with self.assertRaises(HTTPException) as staff_on_admin_portal:
            main.login(
                main.LoginRequest(
                    email=self.agent.email,
                    password="correct horse battery staple",
                    role="ADMIN",
                ),
                self.db,
            )
        self.assertEqual(staff_on_admin_portal.exception.status_code, 401)

        agent_login = main.login(
            main.LoginRequest(
                email=self.agent.email,
                password="correct horse battery staple",
                role="AGENT",
            ),
            self.db,
        )
        self.assertEqual(agent_login["user"]["role"], "AGENT")

        admin = main.Account(
            email="admin@example.com",
            display_name="Administrator",
            password_hash=main.hash_password("admin password long enough"),
            role="ADMIN",
        )
        self.db.add(admin)
        self.db.commit()
        with self.assertRaises(HTTPException) as admin_on_staff_portal:
            main.login(
                main.LoginRequest(
                    email=admin.email,
                    password="admin password long enough",
                    role="AGENT",
                ),
                self.db,
            )
        self.assertEqual(admin_on_staff_portal.exception.status_code, 401)

        admin_login = main.login(
            main.LoginRequest(
                email=admin.email,
                password="admin password long enough",
                role="ADMIN",
            ),
            self.db,
        )
        self.assertEqual(admin_login["user"]["role"], "ADMIN")

    def test_disabled_account_session_is_rejected(self):
        token = "disabled-session-token"
        self.agent.is_active = False
        self.db.add(main.LoginSession(
            account_id=self.agent.id,
            token_hash=main.hashlib.sha256(token.encode()).hexdigest(),
            expires_at=main.utcnow() + main.timedelta(hours=1),
        ))
        self.db.commit()
        with self.assertRaises(HTTPException) as error:
            main.get_current_account(
                HTTPAuthorizationCredentials(scheme="Bearer", credentials=token),
                self.db,
            )
        self.assertEqual(error.exception.status_code, 401)

    def test_disabling_agent_revokes_sessions_and_transfers_active_tickets(self):
        admin = main.Account(
            email="admin@example.com",
            display_name="Administrator",
            password_hash=main.hash_password("admin password long enough"),
            role="ADMIN",
        )
        self.db.add(admin)
        ticket = self.create_ticket()
        main.claim_ticket(ticket["id"], main.ClaimRequest(), self.db, self.agent)
        token = "agent-session-to-revoke"
        self.db.add(main.LoginSession(
            account_id=self.agent.id,
            token_hash=main.hashlib.sha256(token.encode()).hexdigest(),
            expires_at=main.utcnow() + main.timedelta(hours=1),
        ))
        self.db.commit()

        main.update_account(self.agent.id, main.AccountUpdate(is_active=False), self.db, admin)

        saved = self.db.get(main.Ticket, ticket["id"])
        self.assertEqual(saved.assigned_agent, "Administrator")
        with self.assertRaises(HTTPException) as error:
            main.get_current_account(
                HTTPAuthorizationCredentials(scheme="Bearer", credentials=token),
                self.db,
            )
        self.assertEqual(error.exception.status_code, 401)

    def test_updating_active_account_does_not_transfer_its_tickets(self):
        admin = main.Account(
            email="admin@example.com",
            display_name="Administrator",
            password_hash=main.hash_password("admin password long enough"),
            role="ADMIN",
        )
        self.db.add(admin)
        ticket = self.create_ticket()
        main.claim_ticket(ticket["id"], main.ClaimRequest(), self.db, self.agent)
        self.db.commit()

        main.update_account(self.agent.id, main.AccountUpdate(is_active=True), self.db, admin)

        saved = self.db.get(main.Ticket, ticket["id"])
        self.assertEqual(saved.assigned_agent, "Agent One")

    def test_legacy_ticket_owner_is_migrated_to_active_admin(self):
        ticket = self.create_ticket()
        saved = self.db.get(main.Ticket, ticket["id"])
        saved.assigned_agent = "Old demo agent"
        admin = main.Account(
            email="admin@example.com",
            display_name="Administrator",
            password_hash=main.hash_password("admin password long enough"),
            role="ADMIN",
        )
        self.db.add(admin)
        self.db.commit()

        main.migrate_legacy_ticket_owners(self.db, admin)

        self.assertEqual(saved.assigned_agent, "Administrator")
        audit_events = self.db.scalars(
            main.select(main.AuditEvent).where(main.AuditEvent.ticket_id == ticket["id"])
        ).all()
        self.assertTrue(any(event.action == "OWNER_MIGRATED" for event in audit_events))

    def test_non_admin_cannot_manage_accounts(self):
        with self.assertRaises(HTTPException) as error:
            main.require_admin(self.agent)
        self.assertEqual(error.exception.status_code, 403)

    def test_protected_ticket_routes_require_bearer_auth(self):
        protected_paths = {"/api/tickets", "/api/tickets/export.csv"}
        for route in main.app.routes:
            path = getattr(route, "path", "")
            if path in protected_paths or path.startswith("/api/tickets/{"):
                dependencies = [dependency.call for dependency in route.dependant.dependencies]
                self.assertIn(main.get_current_account, dependencies, path)

if __name__ == "__main__":
    unittest.main()
