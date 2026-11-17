import os, hmac, hashlib, time, json, logging
from decimal import Decimal, InvalidOperation
from aiohttp import web
from db import conn, add_exchange, add_expense, add_payout, dashboard, partner_report, audit

APP_PASSWORD=os.getenv("APP_PASSWORD","")
SESSION_SECRET=os.getenv("SESSION_SECRET","")
APP_URL=os.getenv("APP_URL","")
COOKIE="soyer_session"
SESSION_TTL=60*60*24*7

def _signature(value):
    return hmac.new(SESSION_SECRET.encode(), value.encode(), hashlib.sha256).hexdigest()

def _valid_session(request):
    raw=request.cookies.get(COOKIE,"")
    try:
        stamp,sig=raw.split(".",1)
        if int(stamp)<int(time.time())-SESSION_TTL: return False
        return hmac.compare_digest(sig,_signature(stamp))
    except Exception: return False

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
    if not APP_PASSWORD or not SESSION_SECRET:
        return web.json_response({"error":"APP_PASSWORD та SESSION_SECRET потрібно додати в Render Environment"},status=503)
    try: data=await request.json()
    except Exception: return web.json_response({"error":"Некоректний запит"},status=400)
    if not hmac.compare_digest(str(data.get("password","")),APP_PASSWORD):
        return web.json_response({"error":"Неправильний пароль"},status=401)
    stamp=str(int(time.time()))
    response=web.json_response({"ok":True})
    response.set_cookie(COOKIE,stamp+"."+_signature(stamp),httponly=True,secure=True,samesite="Strict",max_age=SESSION_TTL,path="/")
    return response

async def logout(request):
    response=web.json_response({"ok":True}); response.del_cookie(COOKIE,path="/"); return response

async def stats(request):
    return web.json_response({"dashboard":dashboard(),"partners":partner_report()},dumps=lambda obj:json.dumps(obj,default=str))

async def exchanges(request):
    if request.method=="GET":
        with conn() as c,c.cursor() as q:
            q.execute("SELECT id,direction,received,paid,cost,gross,currency,denis_share,egor_share,note,created_at FROM exchanges ORDER BY id DESC LIMIT 100")
            rows=q.fetchall()
        keys=["id","direction","received","paid","cost","gross","currency","denis_share","egor_share","note","created_at"]
        return web.json_response([dict(zip(keys,[str(v) if isinstance(v,Decimal) else v.isoformat() if hasattr(v,"isoformat") else v for v in row])) for row in rows])
    try:
        d=await request.json()
        result=add_exchange(d["direction"],_decimal(d["received"],"received"),_decimal(d["paid"],"paid"),_decimal(d["cost"],"cost"),str(d.get("note",""))[:300],int(d.get("actor",0)))
        if notify: await notify(f"✅ Новий обмін #{result['id']} · {result['direction']} · результат {result['gross']} {result['currency']}")
        return web.json_response({"ok":True,**{k:str(v) for k,v in result.items()}})
    except (KeyError,ValueError) as e: return web.json_response({"error":str(e)},status=400)
    except Exception:
        logging.exception("PWA exchange creation failed")
        return web.json_response({"error":"Не вдалося записати обмін"},status=500)

async def expenses(request):
    if request.method=="GET":
        with conn() as c,c.cursor() as q:
            q.execute("SELECT id,currency,amount,description,created_at FROM expenses ORDER BY id DESC LIMIT 100")
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
            q.execute("SELECT id,partner,currency,amount,created_at FROM payouts ORDER BY id DESC LIMIT 100")
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

async def health(request): return web.json_response({"ok":True,"service":"SOYER ACCOUNTING"})

def create_app(notify=None):
    app=web.Application(middlewares=[auth_middleware],client_max_size=1024*1024)
    app.router.add_post("/api/login",login); app.router.add_post("/api/logout",logout)
    app.router.add_get("/api/stats",stats); app.router.add_get("/api/exchanges",exchanges); app.router.add_post("/api/exchanges",exchanges)
    app.router.add_get("/api/expenses",expenses); app.router.add_post("/api/expenses",expenses)
    app.router.add_get("/api/payouts",payouts); app.router.add_post("/api/payouts",payouts)
    app.router.add_get("/health",health)
    app.router.add_get("/",lambda r:web.FileResponse("static/index.html"))
    app.router.add_get("/manifest.webmanifest",lambda r:web.FileResponse("static/manifest.webmanifest"))
    app.router.add_get("/sw.js",lambda r:web.FileResponse("static/sw.js",headers={"Cache-Control":"no-cache"}))
    app.router.add_get("/icon.svg",lambda r:web.FileResponse("static/icon.svg"))
    app.router.add_static("/assets/","static/assets",show_index=False)
    return app
