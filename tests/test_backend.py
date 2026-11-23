import asyncio
import unittest

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend import main


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        main.Base.metadata.create_all(bind=self.engine)
        self.db = Session(self.engine, expire_on_commit=False)

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
                actor="Agent 1",
            ),
            self.db,
        )

    def test_sla_hours_skip_weekend(self):
        friday_evening_utc = main.datetime(2024, 5, 3, 12, 30)
        due = main.add_working_hours(friday_evening_utc, 1.5)
        self.assertEqual(due, main.datetime(2024, 5, 6, 5, 0))

    def test_ticket_claim_is_exclusive(self):
        ticket = self.create_ticket()
        main.claim_ticket(ticket["id"], main.ClaimRequest(agent="Agent 1"), self.db)

        with self.assertRaises(HTTPException) as error:
            main.claim_ticket(ticket["id"], main.ClaimRequest(agent="Agent 2"), self.db)

        self.assertEqual(error.exception.status_code, 409)
        saved = self.db.get(main.Ticket, ticket["id"])
        self.assertEqual(saved.assigned_agent, "Agent 1")

    def test_private_notes_are_excluded_from_public_messages_and_csv(self):
        ticket = self.create_ticket("=1+1")
        main.add_message(
            ticket["id"],
            main.MessageCreate(author="Agent 1", body="Internal-only detail", is_private=True),
            self.db,
        )

        public_messages = main.list_messages(ticket["id"], include_private=False, db=self.db)
        all_messages = main.list_messages(ticket["id"], include_private=True, db=self.db)
        exported = main.export_tickets(
            status=None, priority=None, q=None, assigned_agent=None,
            unassigned=False, db=self.db,
        )
        csv_body = asyncio.run(self.read_response(exported))

        self.assertEqual(public_messages, [])
        self.assertEqual(len(all_messages), 1)
        self.assertNotIn("Internal-only detail", csv_body)
        self.assertIn("'=1+1", csv_body)
        self.assertNotIn("Internal-only detail", str(ticket))

    def test_public_reply_can_pause_ticket_for_customer(self):
        ticket = self.create_ticket()
        main.claim_ticket(ticket["id"], main.ClaimRequest(agent="Agent 1"), self.db)

        main.add_message(
            ticket["id"],
            main.MessageCreate(
                author="Agent 1",
                body="Could you share a screenshot?",
                waiting_for_customer=True,
            ),
            self.db,
        )

        saved = self.db.get(main.Ticket, ticket["id"])
        self.assertEqual(saved.status, "WAITING_CUSTOMER")
        self.assertIsNotNone(saved.waiting_since)

    def test_expired_ticket_cannot_be_reopened(self):
        ticket = self.create_ticket()
        main.claim_ticket(ticket["id"], main.ClaimRequest(agent="Agent 1"), self.db)
        main.update_status(
            ticket["id"],
            main.StatusUpdate(status="RESOLVED", actor="Agent 1"),
            self.db,
        )
        saved = self.db.get(main.Ticket, ticket["id"])
        saved.resolved_at = main.utcnow() - main.timedelta(days=8)

        with self.assertRaises(HTTPException) as error:
            main.update_status(
                ticket["id"],
                main.StatusUpdate(status="IN_PROGRESS", actor="Agent 1"),
                self.db,
            )

        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(saved.status, "RESOLVED")

    @staticmethod
    async def read_response(response):
        return "".join([chunk.decode() if isinstance(chunk, bytes) else chunk
                        async for chunk in response.body_iterator])


if __name__ == "__main__":
    unittest.main()
