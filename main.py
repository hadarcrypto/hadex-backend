from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import httpx
import os
import random
import string
from datetime import datetime
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

# --- Config ---
SUPABASE_URL: Optional[str] = os.environ.get("SUPABASE_URL")
SUPABASE_KEY: Optional[str] = os.environ.get("SUPABASE_KEY")
ADMIN_TOKEN: str = os.environ.get("ADMIN_TOKEN", "hadex-admin-secret")
UAH_CARD: str = os.environ.get("UAH_CARD", "5375 4141 2089 7634")
TRON_WALLET: str = os.environ.get("TRON_WALLET", "TW4HBxphrFEAfKELGszsuMWzsqh8qsbYoQ")

VALID_DIRECTIONS = ["usdt_to_uah", "uah_to_usdt"]

# --- Supabase singleton ---
_supabase_client: Optional[Client] = None

def get_supabase() -> Client:
    global _supabase_client
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise HTTPException(503, "Database not configured")
    if _supabase_client is None:
        _supabase_client = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _supabase_client

# --- App ---
app = FastAPI(title="HADEX Exchange API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Helpers ---
def get_spread(amount_usd: float) -> float:
    if amount_usd >= 50000: return 0.01
    elif amount_usd >= 10000: return 0.015
    elif amount_usd >= 5000: return 0.02
    elif amount_usd >= 1000: return 0.03
    elif amount_usd >= 500: return 0.04
    else: return 0.05

def generate_order_id() -> str:
    p1 = ''.join(random.choices(string.digits, k=4))
    p2 = ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))
    return f"HDX-{p1}-{p2}"

async def fetch_rate() -> dict:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.post(
                "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search",
                json={"asset": "USDT", "fiat": "UAH", "merchantCheck": False,
                      "page": 1, "rows": 10, "side": "SELL", "tradeType": "SELL"},
                headers={"Content-Type": "application/json"},
            )
            data = r.json().get("data", [])
            if not data:
                raise ValueError("Empty Binance response")
            prices = [float(ad["adv"]["price"]) for ad in data[:5]]
            mid = sum(prices) / len(prices)
            return {"buy": round(mid * 0.97, 2), "sell": round(mid * 1.03, 2),
                    "mid": round(mid, 2), "source": "binance"}
    except Exception:
        pass

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": "tether", "vs_currencies": "uah"},
            )
            mid = float(r.json()["tether"]["uah"])
            return {"buy": round(mid * 0.97, 2), "sell": round(mid * 1.03, 2),
                    "mid": round(mid, 2), "source": "coingecko"}
    except Exception:
        pass

    return {"buy": 42.20, "sell": 47.38, "mid": 44.79, "source": "fallback"}

# --- Models ---
class OrderCreate(BaseModel):
    direction: str
    amount_from: float
    currency_from: str
    currency_to: str
    client_card: Optional[str] = None
    client_wallet: Optional[str] = None

class OrderVerify(BaseModel):
    order_id: str
    tx_hash: Optional[str] = None
    bank_receipt: Optional[str] = None

class OrderPayout(BaseModel):
    order_id: str
    admin_note: Optional[str] = None

# --- Routes ---
@app.get("/")
async def root():
    return {"status": "ok", "service": "HADEX API"}

@app.get("/rate")
async def get_rate():
    rate = await fetch_rate()
    return {
        **rate,
        "tron_wallet": TRON_WALLET,
        "uah_card": UAH_CARD,
        "timestamp": datetime.utcnow().isoformat(),
    }

@app.post("/order/create")
async def create_order(order: OrderCreate):
    if order.direction not in VALID_DIRECTIONS:
        raise HTTPException(400, f"Invalid direction. Must be one of: {VALID_DIRECTIONS}")
    if order.amount_from <= 0:
        raise HTTPException(400, "Amount must be positive")
    if order.amount_from < 100:
        raise HTTPException(400, "Minimum amount is $100")
    if order.direction == "usdt_to_uah" and not order.client_card:
        raise HTTPException(400, "client_card is required for usdt_to_uah")
    if order.direction == "uah_to_usdt" and not order.client_wallet:
        raise HTTPException(400, "client_wallet is required for uah_to_usdt")

    rate = await fetch_rate()
    order_id = generate_order_id()

    if order.direction == "usdt_to_uah":
        r = rate["buy"]
        amount_usd = order.amount_from
        amount_to = round(order.amount_from * r, 2)
    else:
        r = rate["sell"]
        amount_usd = round(order.amount_from / r, 2)
        amount_to = round(order.amount_from / r, 6)

    row = {
        "order_id": order_id,
        "direction": order.direction,
        "amount_from": order.amount_from,
        "currency_from": order.currency_from,
        "currency_to": order.currency_to,
        "amount_to": amount_to,
        "rate": r,
        "spread": get_spread(amount_usd),
        "client_card": order.client_card,
        "client_wallet": order.client_wallet,
        "status": "pending",
        "created_at": datetime.utcnow().isoformat(),
    }

    try:
        get_supabase().table("orders").insert(row).execute()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"DB error: {e}")

    return {
        "order_id": order_id,
        "amount_from": order.amount_from,
        "currency_from": order.currency_from,
        "amount_to": amount_to,
        "currency_to": order.currency_to,
        "rate": r,
        "tron_wallet": TRON_WALLET if order.direction == "usdt_to_uah" else None,
        "uah_card": UAH_CARD if order.direction == "uah_to_usdt" else None,
        "status": "pending",
    }

@app.post("/order/verify")
async def verify_order(data: OrderVerify):
    if not data.tx_hash and not data.bank_receipt:
        raise HTTPException(400, "Either tx_hash or bank_receipt is required")
    try:
        sb = get_supabase()
        res = sb.table("orders").select("*").eq("order_id", data.order_id).execute()
        if not res.data:
            raise HTTPException(404, "Order not found")
        if res.data[0]["status"] != "pending":
            raise HTTPException(400, f"Order already in status: {res.data[0]['status']}")
        sb.table("orders").update({
            "tx_hash": data.tx_hash,
            "bank_receipt": data.bank_receipt,
            "status": "verified",
        }).eq("order_id", data.order_id).execute()
        return {"order_id": data.order_id, "status": "verified", "verified": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Error: {e}")

@app.post("/order/payout")
async def payout_order(data: OrderPayout, x_admin_token: str = Header(None)):
    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(403, "Unauthorized")
    try:
        sb = get_supabase()
        res = sb.table("orders").select("*").eq("order_id", data.order_id).execute()
        if not res.data:
            raise HTTPException(404, "Order not found")
        if res.data[0]["status"] != "verified":
            raise HTTPException(400, f"Order must be in 'verified' status, got: {res.data[0]['status']}")
        sb.table("orders").update({
            "status": "completed",
            "admin_note": data.admin_note,
            "completed_at": datetime.utcnow().isoformat(),
        }).eq("order_id", data.order_id).execute()
        return {"order_id": data.order_id, "status": "completed"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Error: {e}")

@app.get("/admin/orders")
async def admin_orders(x_admin_token: str = Header(None), status: Optional[str] = None):
    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(403, "Unauthorized")
    valid_statuses = ["pending", "verified", "completed", "cancelled"]
    if status and status not in valid_statuses:
        raise HTTPException(400, f"Invalid status. Must be one of: {valid_statuses}")
    try:
        sb = get_supabase()
        q = sb.table("orders").select("*").order("created_at", desc=True)
        if status:
            q = q.eq("status", status)
        res = q.execute()
        return {"orders": res.data, "count": len(res.data)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Error: {e}")
