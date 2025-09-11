
# main.py  —  PIX pendente (checkout)

import os
import logging
from typing import Dict, Any, Optional
from fastapi import FastAPI, Body
from fastapi.responses import JSONResponse
import httpx

# -------- Log --------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("paginatto-pix")

# -------- Env --------
ZAPI_INSTANCE: str = os.getenv("ZAPI_INSTANCE", "")
ZAPI_TOKEN: str = os.getenv("ZAPI_TOKEN", "")
ZAPI_CLIENT_TOKEN: str = os.getenv("ZAPI_CLIENT_TOKEN", "")
WHATSAPP_SENDER_NAME: str = os.getenv("WHATSAPP_SENDER_NAME", "Paginatto")

MSG_TEMPLATE: str = os.getenv(
    "MSG_TEMPLATE",
    "Oi {name}! Seu pedido via PIX de {product} no valor de {price} está aguardando pagamento. "
    "Pague aqui: {checkout_url}"
)

ZAPI_URL: str = f"https://api.z-api.io/instances/{ZAPI_INSTANCE}/token/{ZAPI_TOKEN}/send-text"

app = FastAPI(title="Paginatto - PIX pendente", version="1.0.0")

# -------- Helpers --------
def normalize_phone(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        return None
    if digits.startswith("55"):
        return digits
    if len(digits) >= 10:
        return "55" + digits
    return None

async def send_whatsapp(phone: str, message: str) -> Dict[str, Any]:
    headers = {"Client-Token": ZAPI_CLIENT_TOKEN}
    payload = {"phone": phone, "message": message}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(ZAPI_URL, headers=headers, json=payload)
        return {"status": r.status_code, "body": r.text}

def safe_format(template: str, **kwargs) -> str:
    """Não quebra se faltar placeholder."""
    class _Safe(dict):
        def __missing__(self, k): return "{" + k + "}"
    return template.format_map(_Safe(**kwargs))

# -------- Parser (pedido/checkout) --------
def parse_order(payload: dict) -> dict:
    # alguns webhooks vêm como {"order": {...}}, outros como {"data": {...}} ou direto
    order = payload.get("order") or payload.get("data") or payload
    if not isinstance(order, dict):
        order = {}

    customer = order.get("customer") or {}
    items = order.get("items") or [{}]

    # link de pagamento/checkout (garante os 3 nomes)
    checkout_url = (
        order.get("checkout_url")
        or order.get("checkout_link")
        or payload.get("checkout_url")
        or payload.get("checkout_link")
        or ""
    )

    # produto/preço
    product_title = (items[0] or {}).get("title") or "Seu produto"
    price = (items[0] or {}).get("price")
    if isinstance(price, (int, float)):
        price = f"R$ {price:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

    # nome
    name = (
        customer.get("name")
        or customer.get("full_name")
        or customer.get("first_name")
        or "cliente"
    )

    return {
        "order_id": order.get("id"),
        "status": order.get("status"),
        "payment_status": order.get("payment_status"),
        "payment_method": order.get("payment_method"),
        "checkout_url": checkout_url,"checkout_url": order.get("checkout_url") or order.get("checkout_link") or order.get("cart_url"),
        "name": name,
        "phone": customer.get("phone"),
        "product": product_title,
        "price": price or "R$ 0,00",
    }

# -------- Webhook --------
@app.post("/webhook/pixpendente")
async def pix_pendente_webhook(payload: Dict[str, Any] = Body(...)):
    log.info(f"[PIX] Webhook recebido: {payload}")

    info = parse_order(payload)
    event = (payload.get("event") or info.get("status") or "").lower()

    payment_method = (info.get("payment_method") or "").lower()
    payment_status = (info.get("payment_status") or "").lower()

    is_pix = payment_method.startswith("pix")
    is_pending = payment_status in {"pending", "pendente", "aguardando"}

    # Também trata 'order.created' com status pendente
    trigger = is_pix and (is_pending or "order.created" in event)

    if not trigger:
        log.info(f"[{info.get('order_id')}] ignorado (event={event}, method={payment_method}, status={payment_status})")
        return JSONResponse({"ok": True, "action": "ignored", "order_id": info.get("order_id")})

    phone = normalize_phone(info.get("phone"))
    if not phone:
        log.warning(f"[{info.get('order_id')}] telefone inválido -> não enviou")
        return JSONResponse({"ok": False, "error": "telefone inválido", "order_id": info.get("order_id")})

    msg = safe_format(
        MSG_TEMPLATE,
        name=info.get("name", "cliente"),
        product=info.get("product", "seu produto"),
        price=info.get("price", "R$ 0,00"),
        checkout_url=info.get("checkout_url", "#"),
        brand=WHATSAPP_SENDER_NAME,
    )

    result = await send_whatsapp(phone, msg)
    log.info(f"[{info.get('order_id')}] WhatsApp -> {result}")
    return JSONResponse({"ok": True, "action": "whatsapp_sent", "order_id": info.get("order_id")})

# -------- Health --------
@app.get("/health")
async def health():
    return {"ok": True, "serviço": "paginatto-pix", "versão": "1.0.0"}
