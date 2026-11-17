import os, hmac, hashlib, time, json, logging, io, zipfile
from decimal import Decimal, InvalidOperation
from xml.sax.saxutils import escape
from aiohttp import web
from db import conn, add_exchange, add_expense, add_payout, dashboard, partner_report, audit

APP_PASSWORD=os.getenv("APP_PASSWORD","")
ADMIN_PASSWORD=os.getenv("ADMIN_PASSWORD","")
SESSION_SECRET=os.getenv("SESSION_SECRET","")
APP_URL=os.getenv("APP_URL","")
COOKIE="soyer_session"
SESSION_TTL=60*60*24*7

def _signature(value):
    return hmac.new(SESSION_SECRET.encode(), value.encode(), hashlib.sha256).hexdigest()

def _session_role(request):
    raw=request.cookies.get(COOKIE,"")
    try:
        stamp,role,sig=raw.split(".",2)
        signed=stamp+"."+role
        if int(stamp)<int(time.time())-SESSION_TTL: return None
        if not hmac.compare_digest(sig,_signature(signed)): return None
        return role if role in ("admin","user") else None
    except Exception: return None

def _valid_session(request):
    return _session_role(request) is not None

def _require_admin(request):
    return _session_role(request)=="admin"

def _actor_id(request):
    return 1 if _require_admin(request) else 2

@web.middleware
async def auth_middleware(request,handler):
    if request.path in ("/","/login","/manifest.webmanifest","/sw.js","/icon.svg") or request.path.startswith("/assets/"):
        return await handler(request)
    if request.path=="/api/login" and request.method=="POST": return await handler(request)
    if request.path.startswith("/api/") and not _valid_session(request):
        return web.json_response({"error":"unauthorized"},status=401)
    return await handler(request)

def _decimal(value,name):
    try: v=Decimal(str(value).replace(",","."))
    except (InvalidOperation,ValueError): raise ValueError(f"Некоректне поле: {name}")
    if not v.is_finite(): raise ValueError(f"Некоректне поле: {name}")
    return v

async def login(request):
    if not APP_PASSWORD or not ADMIN_PASSWORD or not SESSION_SECRET:
        return web.json_response({"error":"APP_PASSWORD, ADMIN_PASSWORD та SESSION_SECRET потрібно додати в Render Environment"},status=503)
    try: data=await request.json()
    except Exception: return web.json_response({"error":"Некоректний запит"},status=400)
    password=str(data.get("password",""))
    if hmac.compare_digest(password,ADMIN_PASSWORD):
        role="admin"
    elif hmac.compare_digest(password,APP_PASSWORD):
        role="user"
    else:
        return web.json_response({"error":"Неправильний пароль"},status=401)
    stamp=str(int(time.time()))
    signed=stamp+"."+role
    response=web.json_response({"ok":True,"role":role})
    response.set_cookie(COOKIE,signed+"."+_signature(signed),httponly=True,secure=True,samesite="Strict",max_age=SESSION_TTL,path="/")
    return response

async def logout(request):
    response=web.json_response({"ok":True}); response.del_cookie(COOKIE,path="/"); return response

async def me(request):
    return web.json_response({"role":_session_role(request),"can_delete":_require_admin(request)})

async def delete_exchange(request):
    if not _require_admin(request):
        return web.json_response({"error":"Недостатньо прав. Видаляти обміни може лише Єгор."},status=403)
    try:
        exchange_id=int(request.match_info["exchange_id"])
        if exchange_id<=0: raise ValueError()
        with conn() as c,c.cursor() as q:
            q.execute("DELETE FROM exchanges WHERE id=%s RETURNING id,direction,gross,currency",(exchange_id,))
            row=q.fetchone()
            if not row:
                return web.json_response({"error":"Обмін не знайдено"},status=404)
            q.execute("INSERT INTO audit_log(actor,action,details) VALUES(%s,%s,%s)",(_actor_id(request),"exchange_deleted",f"id={row[0]} direction={row[1]} gross={row[2]} {row[3]}"))
            c.commit()
        return web.json_response({"ok":True,"deleted_id":exchange_id})
    except (ValueError,KeyError):
        return web.json_response({"error":"Некоректний ID обміну"},status=400)
    except Exception:
        logging.exception("Exchange deletion failed")
        return web.json_response({"error":"Не вдалося видалити обмін"},status=500)

async def stats(request):
    where,params=_date_filter(request)
    with conn() as c,c.cursor() as q:
        q.execute("SELECT COUNT(*),COALESCE(SUM(gross) FILTER(WHERE currency='UAH' AND gross>0),0),COALESCE(SUM(ABS(gross)) FILTER(WHERE currency='UAH' AND gross<0),0),COALESCE(SUM(gross) FILTER(WHERE currency='USDT' AND gross>0),0),COALESCE(SUM(ABS(gross)) FILTER(WHERE currency='USDT' AND gross<0),0),COALESCE(SUM(denis_share) FILTER(WHERE currency='UAH'),0),COALESCE(SUM(egor_share) FILTER(WHERE currency='UAH'),0) FROM exchanges"+where+" AND status='SUCCESS'" if where else "SELECT COUNT(*),COALESCE(SUM(gross) FILTER(WHERE currency='UAH' AND gross>0),0),COALESCE(SUM(ABS(gross)) FILTER(WHERE currency='UAH' AND gross<0),0),COALESCE(SUM(gross) FILTER(WHERE currency='USDT' AND gross>0),0),COALESCE(SUM(ABS(gross)) FILTER(WHERE currency='USDT' AND gross<0),0),COALESCE(SUM(denis_share) FILTER(WHERE currency='UAH'),0),COALESCE(SUM(egor_share) FILTER(WHERE currency='UAH'),0) FROM exchanges WHERE status='SUCCESS'",params)
        e=q.fetchone()
        q.execute("SELECT COALESCE(SUM(amount),0) FROM expenses"+where,params); expenses_sum=q.fetchone()[0]
        q.execute("SELECT COALESCE(SUM(amount),0) FROM payouts"+where,params); paid=q.fetchone()[0]
        q.execute("SELECT COALESCE(SUM(amount) FILTER(WHERE partner='denis'),0),COALESCE(SUM(amount) FILTER(WHERE partner='egor'),0) FROM payouts"+where,params); pp=q.fetchone()
    d={"count":e[0],"gross":e[1],"losses":e[2],"gross_usdt":e[3],"losses_usdt":e[4],"denis":e[5],"egor":e[6],"expenses":expenses_sum,"paid_out":paid,"currency":"UAH"}
    partners={"denis_accrued":e[5],"egor_accrued":e[6],"denis_paid":pp[0],"egor_paid":pp[1],"denis_due":e[5]-pp[0],"egor_due":e[6]-pp[1]}
    return web.json_response({"dashboard":d,"partners":partners},dumps=lambda obj:json.dumps(obj,default=str))

async def exchanges(request):
    if request.method=="GET":
        with conn() as c,c.cursor() as q:
            where,params=_date_filter(request)
            q.execute("SELECT id,direction,received,paid,cost,gross,currency,denis_share,egor_share,note,created_at FROM exchanges"+where+" ORDER BY id DESC LIMIT 100",params)
            rows=q.fetchall()
        keys=["id","direction","received","paid","cost","gross","currency","denis_share","egor_share","note","created_at"]
        return web.json_response([dict(zip(keys,[str(v) if isinstance(v,Decimal) else v.isoformat() if hasattr(v,"isoformat") else v for v in row])) for row in rows])
    try:
        d=await request.json()
        result=add_exchange(d["direction"],_decimal(d["received"],"received"),_decimal(d["paid"],"paid"),_decimal(d["cost"],"cost"),str(d.get("note",""))[:300],_actor_id(request))
        if notify: await notify(f"✅ Новий обмін #{result['id']} · {result['direction']} · результат {result['gross']} {result['currency']}")
        return web.json_response({"ok":True,**{k:str(v) for k,v in result.items()}})
    except (KeyError,ValueError) as e: return web.json_response({"error":str(e)},status=400)
    except Exception:
        logging.exception("PWA exchange creation failed")
        return web.json_response({"error":"Не вдалося записати обмін"},status=500)

async def expenses(request):
    if request.method=="GET":
        with conn() as c,c.cursor() as q:
            where,params=_date_filter(request)
            q.execute("SELECT id,currency,amount,description,created_at FROM expenses"+where+" ORDER BY id DESC LIMIT 500",params)
            rows=q.fetchall()
        return web.json_response([{"id":r[0],"currency":r[1],"amount":str(r[2]),"description":r[3],"created_at":r[4].isoformat()} for r in rows])
    try:
        d=await request.json(); amount=_decimal(d["amount"],"amount")
        if amount<=0: raise ValueError("Сума має бути більшою за нуль")
        currency=str(d["currency"]).upper()
        if currency not in ("UAH","USDT"): raise ValueError("Валюта має бути UAH або USDT")
        add_expense(currency,amount,str(d["description"])[:300],int(d.get("actor",0)))
        if notify: await notify(f"💸 Нова витрата: {amount} {currency} · {str(d['description'])[:120]}")
        return web.json_response({"ok":True})
    except (KeyError,ValueError) as e: return web.json_response({"error":str(e)},status=400)
    except Exception:
        logging.exception("PWA expense creation failed"); return web.json_response({"error":"Не вдалося записати витрату"},status=500)

async def payouts(request):
    if request.method=="GET":
        with conn() as c,c.cursor() as q:
            where,params=_date_filter(request)
            q.execute("SELECT id,partner,currency,amount,created_at FROM payouts"+where+" ORDER BY id DESC LIMIT 500",params)
            rows=q.fetchall()
        return web.json_response([{"id":r[0],"partner":r[1],"currency":r[2],"amount":str(r[3]),"created_at":r[4].isoformat()} for r in rows])
    try:
        d=await request.json(); partner=str(d["partner"]).lower(); currency=str(d["currency"]).upper(); amount=_decimal(d["amount"],"amount")
        if partner not in ("denis","egor"): raise ValueError("Невідомий партнер")
        if currency not in ("UAH","USDT") or amount<=0: raise ValueError("Перевірте валюту та суму")
        add_payout(partner,amount,currency,int(d.get("actor",0)))
        if notify: await notify(f"💰 Виплата: {partner} · {amount} {currency}")
        return web.json_response({"ok":True})
    except (KeyError,ValueError) as e: return web.json_response({"error":str(e)},status=400)
    except Exception:
        logging.exception("PWA payout creation failed"); return web.json_response({"error":"Не вдалося записати виплату"},status=500)


def _date_filter(request, column="created_at"):
    start=request.query.get("from","").strip()
    end=request.query.get("to","").strip()
    clauses=[]; params=[]
    if start:
        clauses.append(f"{column} >= %s::date"); params.append(start)
    if end:
        clauses.append(f"{column} < (%s::date + INTERVAL '1 day')"); params.append(end)
    return (" WHERE "+" AND ".join(clauses) if clauses else ""), params

async def chart_data(request):
    where,params=_date_filter(request)
    with conn() as c,c.cursor() as q:
        q.execute("SELECT DATE(created_at) day, COALESCE(SUM(gross) FILTER(WHERE currency='UAH'),0) uah, COALESCE(SUM(gross) FILTER(WHERE currency='USDT'),0) usdt FROM exchanges"+where+" GROUP BY DATE(created_at) ORDER BY day",params)
        rows=q.fetchall()
    return web.json_response([{"day":r[0].isoformat(),"uah":str(r[1]),"usdt":str(r[2])} for r in rows])

async def audit_history(request):
    if not _require_admin(request):
        return web.json_response({"error":"Журнал доступний лише Єгору."},status=403)
    where,params=_date_filter(request)
    with conn() as c,c.cursor() as q:
        q.execute("SELECT id,actor,action,details,created_at FROM audit_log"+where+" ORDER BY id DESC LIMIT 500",params)
        rows=q.fetchall()
    return web.json_response([{"id":r[0],"actor":r[1],"action":r[2],"details":r[3],"created_at":r[4].isoformat() if r[4] else None} for r in rows])

def _xlsx_cell(value):
    if value is None: return '<c t="inlineStr"><is><t></t></is></c>'
    if isinstance(value,(int,float,Decimal)) and not isinstance(value,bool):
        return '<c><v>'+escape(str(value))+'</v></c>'
    return '<c t="inlineStr"><is><t xml:space="preserve">'+escape(str(value))+'</t></is></c>'

def _make_xlsx(sheets):
    out=io.BytesIO()
    with zipfile.ZipFile(out,"w",zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'+''.join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(1,len(sheets)+1))+'</Types>')
        z.writestr("_rels/.rels",'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr("xl/workbook.xml",'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'+''.join(f'<sheet name="{escape(name)}" sheetId="{i}" r:id="rId{i}"/>' for i,(name,_) in enumerate(sheets,1))+'</sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels",'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'+''.join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1,len(sheets)+1))+'</Relationships>')
        for i,(_,rows) in enumerate(sheets,1):
            xml='<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
            for row in rows: xml+='<row>'+''.join(_xlsx_cell(v) for v in row)+'</row>'
            xml+='</sheetData></worksheet>'
            z.writestr(f"xl/worksheets/sheet{i}.xml",xml)
    return out.getvalue()

async def export_xlsx(request):
    if not _require_admin(request):
        return web.json_response({"error":"Експорт доступний лише Єгору."},status=403)
    where,params=_date_filter(request)
    with conn() as c,c.cursor() as q:
        q.execute("SELECT id,direction,received,paid,cost,gross,currency,note,created_at FROM exchanges"+where+" ORDER BY id DESC",params)
        ex=[["ID","Напрямок","Отримано","Видано","Собівартість","Результат","Валюта","Примітка","Дата"]]+[list(r) for r in q.fetchall()]
        q.execute("SELECT id,currency,amount,description,created_at FROM expenses"+where+" ORDER BY id DESC",params)
        expenses=[["ID","Валюта","Сума","Опис","Дата"]]+[list(r) for r in q.fetchall()]
        q.execute("SELECT id,partner,currency,amount,created_at FROM payouts"+where+" ORDER BY id DESC",params)
        payouts=[["ID","Партнер","Валюта","Сума","Дата"]]+[list(r) for r in q.fetchall()]
    payload=_make_xlsx([("Обміни",ex),("Витрати",expenses),("Виплати",payouts)])
    return web.Response(body=payload,content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",headers={"Content-Disposition":"attachment; filename=soyer-accounting.xlsx"})

async def backup_json(request):
    if not _require_admin(request):
        return web.json_response({"error":"Резервна копія доступна лише Єгору."},status=403)
    tables=("exchanges","expenses","payouts","audit_log")
    backup={"app":"SOYER ACCOUNTING","created_at":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),"tables":{}}
    with conn() as c,c.cursor() as q:
        for table_name in tables:
            q.execute("SELECT * FROM "+table_name+" ORDER BY id")
            columns=[d[0] for d in q.description]
            backup["tables"][table_name]=[dict(zip(columns,row)) for row in q.fetchall()]
    audit(_actor_id(request),"backup_created","JSON backup downloaded")
    return web.Response(text=json.dumps(backup,ensure_ascii=False,default=str,indent=2),content_type="application/json",headers={"Content-Disposition":"attachment; filename=soyer-accounting-backup.json"})


async def health(request): return web.json_response({"ok":True,"service":"SOYER ACCOUNTING"})

def create_app(notify=None):
    app=web.Application(middlewares=[auth_middleware],client_max_size=1024*1024)
    app.router.add_post("/api/login",login); app.router.add_post("/api/logout",logout)
    app.router.add_get("/api/me",me)
    app.router.add_get("/api/chart",chart_data)
    app.router.add_get("/api/audit",audit_history)
    app.router.add_get("/api/export.xlsx",export_xlsx)
    app.router.add_get("/api/backup",backup_json)
    app.router.add_get("/api/stats",stats); app.router.add_get("/api/exchanges",exchanges); app.router.add_post("/api/exchanges",exchanges)
    app.router.add_delete("/api/exchanges/{exchange_id:\\d+}",delete_exchange)
    app.router.add_get("/api/expenses",expenses); app.router.add_post("/api/expenses",expenses)
    app.router.add_get("/api/payouts",payouts); app.router.add_post("/api/payouts",payouts)
    app.router.add_get("/health",health)
    app.router.add_get("/",lambda r:web.FileResponse("static/index.html"))
    app.router.add_get("/manifest.webmanifest",lambda r:web.FileResponse("static/manifest.webmanifest"))
    app.router.add_get("/sw.js",lambda r:web.FileResponse("static/sw.js",headers={"Cache-Control":"no-cache"}))
    app.router.add_get("/icon.svg",lambda r:web.FileResponse("static/icon.svg"))
    app.router.add_static("/assets/","static/assets",show_index=False)
    return app
