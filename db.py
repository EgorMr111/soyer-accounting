import os
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timezone
import psycopg2
from psycopg2.extras import RealDictCursor
from urllib.parse import urlparse

DATABASE_URL=os.getenv("DATABASE_URL","")
def conn():
    if not DATABASE_URL: raise RuntimeError("DATABASE_URL is missing")
    url=DATABASE_URL.replace("postgres://","postgresql://",1)
    return psycopg2.connect(url, sslmode="require" if "render.com" in url else "prefer")

def init_db():
    with conn() as c, c.cursor() as q:
        q.execute("""
        CREATE TABLE IF NOT EXISTS exchanges(
          id BIGSERIAL PRIMARY KEY, external_id TEXT UNIQUE, direction TEXT NOT NULL,
          received NUMERIC(30,10) NOT NULL, paid NUMERIC(30,10) NOT NULL,
          cost NUMERIC(30,10) NOT NULL, gross NUMERIC(30,10) NOT NULL,
          currency TEXT NOT NULL, denis_share NUMERIC(30,10) NOT NULL,
          egor_share NUMERIC(30,10) NOT NULL, note TEXT, created_by BIGINT NOT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), status TEXT NOT NULL DEFAULT 'SUCCESS');
        CREATE TABLE IF NOT EXISTS expenses(
          id BIGSERIAL PRIMARY KEY, currency TEXT NOT NULL, amount NUMERIC(30,10) NOT NULL,
          description TEXT NOT NULL, created_by BIGINT NOT NULL, created_at TIMESTAMPTZ DEFAULT NOW());
        CREATE TABLE IF NOT EXISTS payouts(
          id BIGSERIAL PRIMARY KEY, partner TEXT NOT NULL, currency TEXT NOT NULL,
          amount NUMERIC(30,10) NOT NULL, created_by BIGINT NOT NULL, created_at TIMESTAMPTZ DEFAULT NOW());
        CREATE TABLE IF NOT EXISTS audit_log(
          id BIGSERIAL PRIMARY KEY, actor BIGINT NOT NULL, action TEXT NOT NULL,
          details TEXT, created_at TIMESTAMPTZ DEFAULT NOW());
        """)
        c.commit()

def add_exchange(direction, received, paid, cost, note, actor, external_id=None):
    received,paid,cost=Decimal(received),Decimal(paid),Decimal(cost)
    if direction not in ("USDT_UAH","UAH_USDT"): raise ValueError("invalid direction")
    if received<=0 or paid<=0 or cost<0: raise ValueError("invalid amounts")
    currency="UAH" if direction=="USDT_UAH" else "USDT"
    gross=(paid-cost).quantize(Decimal("0.00000001"),rounding=ROUND_HALF_UP)
    if gross<0: raise ValueError("cost cannot exceed paid amount")
    denis=(gross*Decimal("0.80")).quantize(Decimal("0.00000001"),rounding=ROUND_HALF_UP)
    egor=gross-denis
    with conn() as c, c.cursor(cursor_factory=RealDictCursor) as q:
        q.execute("""INSERT INTO exchanges(external_id,direction,received,paid,cost,gross,currency,denis_share,egor_share,note,created_by)
          VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
          (external_id,direction,received,paid,cost,gross,currency,denis,egor,note,actor))
        row=q.fetchone(); c.commit()
    audit(actor,"exchange_created",f"id={row['id']} external_id={external_id or ''}")
    return {"id":row["id"],"gross":gross,"denis":denis,"egor":egor,"currency":currency}

def audit(actor,action,details=""):
    with conn() as c,c.cursor() as q:
        q.execute("INSERT INTO audit_log(actor,action,details) VALUES(%s,%s,%s)",(actor,action,details)); c.commit()

def dashboard():
    with conn() as c,c.cursor(cursor_factory=RealDictCursor) as q:
        q.execute("SELECT COUNT(*) n, COALESCE(SUM(gross) FILTER(WHERE currency='UAH'),0) uah, COALESCE(SUM(gross) FILTER(WHERE currency='USDT'),0) usdt, COALESCE(SUM(denis_share) FILTER(WHERE currency='UAH'),0) du, COALESCE(SUM(egor_share) FILTER(WHERE currency='UAH'),0) eu FROM exchanges WHERE status='SUCCESS'")
        r=q.fetchone()
        q.execute("SELECT COALESCE(SUM(amount),0) v FROM expenses WHERE currency='UAH'"); exp=q.fetchone()["v"]
        q.execute("SELECT COALESCE(SUM(amount),0) v FROM payouts WHERE currency='UAH'"); po=q.fetchone()["v"]
    # Dashboard default shows UAH; USDT is separately visible in the detailed report in a future enhancement.
    return {"count":r["n"],"gross":r["uah"],"denis":r["du"],"egor":r["eu"],"expenses":exp,"paid_out":po,"currency":"UAH","gross_usdt":r["usdt"]}

def recent_exchanges():
    with conn() as c,c.cursor(cursor_factory=RealDictCursor) as q:
        q.execute("SELECT id,direction,gross,currency,created_at FROM exchanges ORDER BY id DESC LIMIT 10")
        return q.fetchall()

def add_expense(currency,amount,description,actor):
    with conn() as c,c.cursor() as q:
        q.execute("INSERT INTO expenses(currency,amount,description,created_by) VALUES(%s,%s,%s,%s)",(currency,amount,description,actor)); c.commit()
    audit(actor,"expense_created",f"{amount} {currency}: {description}")

def partner_report():
    with conn() as c,c.cursor(cursor_factory=RealDictCursor) as q:
        q.execute("""SELECT
          COALESCE(SUM(denis_share) FILTER(WHERE currency='UAH'),0) da,
          COALESCE(SUM(egor_share) FILTER(WHERE currency='UAH'),0) ea
          FROM exchanges WHERE status='SUCCESS'"""); r=q.fetchone()
        q.execute("SELECT COALESCE(SUM(amount) FILTER(WHERE partner='denis' AND currency='UAH'),0) dp, COALESCE(SUM(amount) FILTER(WHERE partner='egor' AND currency='UAH'),0) ep FROM payouts"); p=q.fetchone()
    return {"denis_accrued":r["da"],"egor_accrued":r["ea"],"denis_paid":p["dp"],"egor_paid":p["ep"],
            "denis_due":r["da"]-p["dp"],"egor_due":r["ea"]-p["ep"]}

def add_payout(partner,amount,currency,actor):
    with conn() as c,c.cursor() as q:
        q.execute("INSERT INTO payouts(partner,currency,amount,created_by) VALUES(%s,%s,%s,%s)",(partner, currency, amount, actor)); c.commit()
    audit(actor,"payout_created",f"{partner}: {amount} {currency}")
