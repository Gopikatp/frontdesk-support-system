import unittest

from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend import main


class BackendTests(unittest.TestCase):
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

    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        main.Base.metadata.create_all(bind=self.engine)
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

    def test_sla_hours_skip_weekend(self):
        friday_evening_utc = main.datetime(2024, 5, 3, 12, 30)
        due = main.add_working_hours(friday_evening_utc, 1.5)
        self.assertEqual(due, main.datetime(2024, 5, 6, 5, 0))

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
            ticket["id"], include_private=False, db=self.db, _account=self.agent
        )
        all_messages = main.list_messages(
            ticket["id"], include_private=True, db=self.db, _account=self.agent
        )
        csv_body = main.ticket_csv(
            self.db,
            main.ticket_statement(None, None, None, None),
        )

        self.assertEqual(public_messages, [])
        self.assertEqual(len(all_messages), 1)
        self.assertNotIn("Internal-only detail", csv_body)
        self.assertIn("'=1+1", csv_body)

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
