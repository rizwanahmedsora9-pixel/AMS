from datetime import datetime, timedelta

from models import db, FollowUpContact, FollowUpReminder, PendingBill


def _login(client):
    return client.post(
        "/login",
        data={"username": "Admin", "password": "Admin@fbm12345"},
        follow_redirects=True,
    )


def _make_due_reminder(app, suffix):
    with app.app_context():
        bill = PendingBill(
            client_name=f"Reminder Test {suffix}",
            client_code=f"REM-{suffix}",
            bill_no=f"REM-BILL-{suffix}",
            amount=1250,
            is_paid=False,
            is_cash=False,
            is_void=False,
        )
        db.session.add(bill)
        db.session.flush()
        reminder = FollowUpReminder(
            pending_bill_id=bill.id,
            remind_at=datetime.now() - timedelta(minutes=5),
            note="Call about payment",
            is_done=False,
        )
        db.session.add(reminder)
        db.session.commit()
        return bill.id, reminder.id


def test_due_reminder_paid_action_marks_bill_paid_and_shows_completed(client, app):
    _login(client)
    bill_id, reminder_id = _make_due_reminder(app, "PAID")

    response = client.post(
        f"/notifications/resolve_due/{reminder_id}",
        json={"outcome": "paid", "response": "Customer paid in full", "channel": "Call"},
    )
    assert response.status_code == 200
    assert response.json["success"] is True

    with app.app_context():
        bill = db.session.get(PendingBill, bill_id)
        reminder = db.session.get(FollowUpReminder, reminder_id)
        contact = FollowUpContact.query.filter_by(reminder_id=reminder_id).one()
        assert bill.is_paid is True
        assert reminder.is_done is True
        assert contact.outcome == "paid"
        assert contact.response == "Customer paid in full"

    page = client.get("/notifications")
    assert page.status_code == 200
    assert b"Completed Reminders" in page.data
    assert b"Paid" in page.data
    assert b"Customer paid in full" in page.data


def test_due_reminder_callback_closes_old_and_creates_new_active_reminder(client, app):
    _login(client)
    bill_id, reminder_id = _make_due_reminder(app, "CALLBACK")
    callback_at = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")

    response = client.post(
        f"/notifications/resolve_due/{reminder_id}",
        json={
            "outcome": "callback",
            "response": "Customer asked us to call next week",
            "remind_at": callback_at,
            "note": "After 10 AM",
            "channel": "Call",
        },
    )
    assert response.status_code == 200
    assert response.json["success"] is True

    with app.app_context():
        bill = db.session.get(PendingBill, bill_id)
        old_reminder = db.session.get(FollowUpReminder, reminder_id)
        new_reminders = FollowUpReminder.query.filter_by(pending_bill_id=bill_id, is_done=False).all()
        contact = FollowUpContact.query.filter_by(reminder_id=reminder_id).one()
        assert bill.is_paid is False
        assert old_reminder.is_done is True
        assert contact.outcome == "callback"
        assert len(new_reminders) == 1
        assert new_reminders[0].remind_at.strftime("%Y-%m-%dT%H:%M") == callback_at
        assert new_reminders[0].note == "After 10 AM"

    page = client.get("/notifications")
    assert page.status_code == 200
    assert b"Callback scheduled" in page.data
    assert b"Customer asked us to call next week" in page.data


def test_due_reminder_rejects_missing_response_and_past_callback(client, app):
    _login(client)
    _bill_id, reminder_id = _make_due_reminder(app, "INVALID")

    missing_response = client.post(
        f"/notifications/resolve_due/{reminder_id}",
        json={"outcome": "paid", "response": "  "},
    )
    assert missing_response.status_code == 400

    past_callback = client.post(
        f"/notifications/resolve_due/{reminder_id}",
        json={
            "outcome": "callback",
            "response": "Call me later",
            "remind_at": (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M"),
        },
    )
    assert past_callback.status_code == 400

    with app.app_context():
        reminder = db.session.get(FollowUpReminder, reminder_id)
        assert reminder.is_done is False
        assert FollowUpContact.query.filter_by(reminder_id=reminder_id).count() == 0
