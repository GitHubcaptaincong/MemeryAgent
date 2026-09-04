from __future__ import annotations
import sys
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

DB_URL = sys.argv[1] if len(sys.argv) > 1 else ""
if not DB_URL:
    print("Usage: python scripts/diagnose_reminder.py <DATABASE_URL>")
    sys.exit(1)

if DB_URL.startswith("postgres://"):
    DB_URL = DB_URL.replace("postgres://", "postgresql+psycopg://", 1)
elif DB_URL.startswith("postgresql://"):
    DB_URL = DB_URL.replace("postgresql://", "postgresql+psycopg://", 1)

from sqlalchemy import create_engine, text

engine = create_engine(DB_URL)

with engine.connect() as conn:
    print("=" * 70)
    print("REMINDER CLAIM DRY-RUN DIAGNOSTIC (READ-ONLY)")
    print("=" * 70)

    utc_now = conn.execute(text("SELECT now() AT TIME ZONE 'UTC'")).scalar()
    db_tz = conn.execute(text("SHOW timezone")).scalar()
    print(f"\n[0] DB current UTC: {utc_now}")
    print(f"    DB timezone setting: {db_tz}")

    print("\n" + "=" * 70)
    print("[1] reminder_preferences")
    print("=" * 70)
    prefs = conn.execute(text("""
        SELECT rp.user_id, rp.enabled, rp.preferred_time, rp.timezone,
               rp.overdue_enabled, rp.daily_limit, rp.created_at, rp.updated_at
        FROM reminder_preferences rp
        ORDER BY rp.updated_at DESC
    """)).mappings().all()
    if not prefs:
        print("  !! No reminder_preferences records found.")
    for p in prefs:
        print(f"  user_id={p['user_id']} enabled={p['enabled']} preferred_time={p['preferred_time']}")
        print(f"    timezone={p['timezone']} overdue_enabled={p['overdue_enabled']} daily_limit={p['daily_limit']}")

        try:
            tz = ZoneInfo(p["timezone"])
        except Exception as e:
            print(f"    !! ZoneInfo error: {e}")
            continue
        utc_dt = datetime.now(UTC)
        local_now = utc_dt.astimezone(tz)
        local_hm = local_now.strftime("%H:%M")
        local_date = local_now.date().isoformat()
        ge_pref = local_hm >= p["preferred_time"]
        print(f"    local_now={local_now.isoformat()}  local_HH:MM={local_hm}")
        print(f"    preferred_time={p['preferred_time']}")
        print(f"    local_now >= preferred_time: {ge_pref}  (CONDITION 3)")
        print(f"    local_date for uniqueness: {local_date}")

        print(f"\n  [2] grants for user {p['user_id']}")
        grants = conn.execute(text("""
            SELECT id, template_id, status, idempotency_key, created_at, consumed_at
            FROM reminder_subscription_grants
            WHERE user_id = :uid
            ORDER BY created_at DESC
        """), {"uid": str(p["user_id"])}).mappings().all()
        avail_count = 0
        for g in grants:
            print(f"    grant_id={g['id']} status={g['status']} template_id={g['template_id']}")
            print(f"      created_at={g['created_at']} consumed_at={g['consumed_at']}")
            if g["status"] == "available":
                avail_count += 1

        print(f"\n  [3] ReviewCard due count for user {p['user_id']}")
        base_where = "user_id = :uid AND status = 'active' AND due_at <= now()"
        total_due = conn.execute(text(f"SELECT count(*) FROM review_cards WHERE {base_where}"), {"uid": str(p["user_id"])}).scalar()
        print(f"    active + due_at <= now(): {total_due}")

        if not p["overdue_enabled"]:
            local_midnight_utc = local_now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
            filtered_due = conn.execute(text(
                f"SELECT count(*) FROM review_cards WHERE {base_where} AND due_at >= :midnight"
            ), {"uid": str(p["user_id"]), "midnight": local_midnight_utc}).scalar()
            print(f"    overdue_enabled=false -> local_midnight UTC={local_midnight_utc.isoformat()}")
            print(f"    restricted due_count: {filtered_due}")
            due_count = filtered_due
        else:
            print(f"    overdue_enabled=true -> no restriction")
            due_count = total_due

        print(f"    FINAL due_count: {due_count}")
        print(f"    CONDITION 4: {'PASS' if due_count > 0 else 'FAIL'}")

        print(f"\n  [4] deliveries today local_date={local_date}")
        delivs = conn.execute(text("""
            SELECT id, template_id, local_date, status, created_at, sent_at, response_code, error_code
            FROM reminder_deliveries
            WHERE user_id = :uid AND local_date = :ld
        """), {"uid": str(p["user_id"]), "ld": local_date}).mappings().all()
        if delivs:
            for d in delivs:
                print(f"    EXISTS: id={d['id']} status={d['status']} sent_at={d['sent_at']}")
        else:
            print(f"    No delivery for local_date={local_date}")
        print(f"    CONDITION 5: {'PASS' if not delivs else 'FAIL'}")

        print(f"\n  [5] All deliveries ever")
        all_delivs = conn.execute(text("""
            SELECT id, template_id, local_date, status, created_at, sent_at, response_code, error_code
            FROM reminder_deliveries
            WHERE user_id = :uid
            ORDER BY created_at DESC LIMIT 10
        """), {"uid": str(p["user_id"])}).mappings().all()
        for d in all_delivs:
            print(f"    id={d['id']} date={d['local_date']} status={d['status']} created={d['created_at']} sent={d['sent_at']} code={d['response_code']} err={d['error_code']}")
        if not all_delivs:
            print("    (none)")

        print(f"\n  [6] wechat_identity")
        ident = conn.execute(text("""
            SELECT openid, created_at FROM wechat_identities WHERE user_id = :uid
        """), {"uid": str(p["user_id"])}).mappings().first()
        if ident:
            print(f"    openid={ident['openid'][:8]}... created={ident['created_at']}")
        else:
            print(f"    !! NO wechat_identity")

        print(f"\n  SUMMARY:")
        print(f"  1. delivery_enabled (env): UNKNOWN - check worker env")
        print(f"  2. reminder enabled: {'PASS' if p['enabled'] else 'FAIL'}")
        print(f"  3. local_now >= preferred_time: {'PASS' if ge_pref else 'FAIL'}")
        print(f"  4. due_count > 0: {'PASS' if due_count > 0 else 'FAIL'} ({due_count})")
        print(f"  5. no delivery today: {'PASS' if not delivs else 'FAIL'}")
        print(f"  6. available grant: {'PASS' if avail_count > 0 else 'FAIL'} ({avail_count})")
        print(f"  7. wechat_identity: {'PASS' if ident else 'FAIL'}")

    print("\n" + "=" * 70)
    print("[8] WORKER GATE: dispatch_once() returns 0 if ANY of:")
    print("    APP_REMINDER_DELIVERY_ENABLED != true")
    print("    APP_WECHAT_SUBSCRIBE_TEMPLATE_ID empty")
    print("    APP_WECHAT_APP_ID empty")
    print("    APP_WECHAT_APP_SECRET empty")
    print("    => claim_due_reminders() NEVER called")
    print("=" * 70)
