import os, logging
from datetime import datetime, timedelta
from typing import Dict, Any, Optional

import httpx
from fastapi import FastAPI, Body
from fastapi.responses import JSONResponse
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

# --- Log ---
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("paginatto")

# --- Config (Render → Settings → Environment) ---
ZAPI_INSTANCE = os.getenv("ZAPI_INSTANCE", "")
ZAPI_TOKEN = os.getenv("ZAPI_TOKEN", "")
ZAPI_CLIENT_TOKEN = os.getenv("ZAPI_CLIENT_TOKEN", "")
SENDER_NAME = os.getenv("WHATSAPP_SENDER_NAME", "Paginatto")
MSG_TEMPLATE = os.getenv(
    "MSG_TEMPLATE",
)
ZAPI_URL = f"https://api.z-api.io/instances/{ZAPI_INSTANCE}/token/{ZAPI_TOKEN}/send-text"

# --- App & Scheduler ---
app = FastAPI(title="Paginatto - PIX 5min", version="1.0.0")
scheduler = AsyncIOScheduler()
scheduler.start()

# order_id -> job_id (pra cancelar quando pagar)
PENDING_JOBS: Dict[str, str] = {}

# ---------- Helpers ----------
def normalize_phone(raw: Optional[str]) -> Optional[str]:
    if not raw: return None
    digits = "".join(ch for ch in raw if ch.isdigit())
    if digits.startswith("55"): return digits
    if len(digits) >= 10: return "55" + digits
    return None

def build_message(name, product, price, url):
    return MSG_TEMPLATE.format(
        name=name or "tudo bem",
        product=product or "seu eBook",
        price=price or "R$ --",
        checkout_url=url or "",
        brand=SENDER_NAME,
    )

async def send_whatsapp(phone: str, message: str) -> Dict[str, Any]:
    headers = {"Client-Token": ZAPI_CLIENT_TOKEN}
    payload = {"phone": phone, "message": message}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(ZAPI_URL, headers=headers, json=payload)
        return {"status": r.status_code, "body": r.text}

def parse(payload: dict) -> dict:
    order = payload.get("order") or payload.get("data") or payload
    customer = order.get("customer", {}) or {}
    items = order.get("items") or order.get("line_items") or []

    order_id = str(order.get("id") or order.get("order_id") or payload.get("id") or "")
    event = (payload.get("event") or order.get("status") or "").lower()
    payment_status = (order.get("payment_status") or order.get("financial_status") or "").lower()
    payment_method = (order.get("payment_method") or "").lower()

    name = customer.get("name") or customer.get("first_name")
    phone = normalize_phone(customer.get("phone") or customer.get("whatsapp"))
    product = items[0].get("title") if items else None
    price = str(items[0].get("price")) if (items and items[0].get("price") is not None) else None
    checkout_url = order.get("checkout_url") or order.get("abandoned_checkout_url") or ""

    return dict(order_id=order_id, event=event, payment_status=payment_status,
                payment_method=payment_method, name=name, phone=phone,
                product=product, price=price, checkout_url=checkout_url)

async def schedule_5min(info: dict):
    order_id = info["order_id"]
    if not order_id: return
    old = PENDING_JOBS.pop(order_id, None)
    if old:
        try: scheduler.remove_job(old)
        except: pass
    run_at = datetime.utcnow() + timedelta(minutes=5)
    job = scheduler.add_job(send_if_still_pending, trigger=DateTrigger(run_date=run_at),
                            kwargs={"info": info}, id=f"pix-{order_id}",
                            replace_existing=True, misfire_grace_time=180)
    PENDING_JOBS[order_id] = job.id
    log.info(f"[{order_id}] PIX pendente -> agendado para {run_at} UTC")

async def send_if_still_pending(info: dict):
    order_id = info["order_id"]
    if order_id not in PENDING_JOBS:
        log.info(f"[{order_id}] pago antes dos 5 min -> nada a enviar.")
        return
    phone = info.get("phone")
    if not phone:
        log.warning(f"[{order_id}] sem telefone -> não enviou.")
        PENDING_JOBS.pop(order_id, None)
        return
    msg = build_message(info.get("name"), info.get("product"), info.get("price"), info.get("checkout_url"))
    resp = await send_whatsapp(phone, msg)
    log.info(f"[{order_id}] WhatsApp status={resp['status']} body={resp['body']}")
    PENDING_JOBS.pop(order_id, None)

def cancel_if_paid(order_id: str):
    job_id = PENDING_JOBS.pop(order_id, None)
    if job_id:
        try: scheduler.remove_job(job_id)
        except: pass
        log.info(f"[{order_id}] pago recebido -> job cancelado.")

# ---------- Rotas ----------
@app.get("/health")
def health(): return {"ok": True, "service": "paginatto", "version": "1.0.0"}

@app.post("/webhook/cartpanda")
async def cartpanda_webhook(payload: Dict[str, Any] = Body(...)):
    log.info(f"Webhook: {payload}")
    info = parse(payload)
    event = info["event"]

    # 1) Pago? cancela
    if ("paid" in event) or (info["payment_status"] in {"paid", "pago"}):
        cancel_if_paid(info["order_id"])
        return JSONResponse({"ok": True, "action": "paid_cancelled", "order_id": info["order_id"]})

    # 2) PIX pendente/criado? agenda +5
    is_pix = "pix" in (info["payment_method"] or event)
    is_pending = any(k in (info["payment_status"] or event) for k in ["pending", "aguard", "pendente"])
    is_created = any(k in event for k in ["order.created", "pedido", "created", "criado"])

    if is_pix and (is_pending or is_created):
        await schedule_5min(info)
        return JSONResponse({"ok": True, "action": "scheduled_5min", "order_id": info["order_id"]})

    return JSONResponse({"ok": True, "action": "ignored", "event": event})
