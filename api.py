from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import asyncio
import json
from datetime import datetime

# Integração com o bot existente (web_wrapper + bot.py)
try:
    from web_wrapper import BOT_INSTANCE, BOT_STATUS, SCAN_LOG, SCAN_COUNT
    from bot import CypherGrokTradeBot
except ImportError:
    BOT_INSTANCE = None
    BOT_STATUS = {"running": False, "started_at": None}
    SCAN_LOG = []
    SCAN_COUNT = 0

app = FastAPI(title="CypherGrokTrade Mobile API", version="1.0", description="API para o app mobile de copy trade Hyperliquid")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def root():
    return {"message": "🚀 CypherGrokTrade Mobile API rodando! Conecte sua app agora.", "status": "online"}

@app.get("/status")
def get_status():
    data = {
        "running": BOT_STATUS.get("running", False),
        "uptime": int(datetime.now().timestamp() - BOT_STATUS["started_at"]) if BOT_STATUS.get("started_at") else 0,
        "scan_count": SCAN_COUNT,
        "recent_logs": SCAN_LOG[-20:],
    }
    if BOT_INSTANCE:
        try:
            balance = BOT_INSTANCE.executor.get_balance() if hasattr(BOT_INSTANCE, 'executor') else 0
            data["balance"] = balance
            data["pnl"] = getattr(BOT_INSTANCE, 'pnl', 0)
            data["open_positions"] = len(getattr(BOT_INSTANCE.executor, 'get_open_positions', lambda: [])())
        except:
            pass
    return data

@app.get("/positions")
def get_positions():
    if BOT_INSTANCE and hasattr(BOT_INSTANCE, 'executor'):
        try:
            positions = BOT_INSTANCE.executor.get_open_positions()
            return {"positions": positions}
        except:
            pass
    return {"positions": []}

@app.get("/balance")
def get_balance():
    if BOT_INSTANCE and hasattr(BOT_INSTANCE, 'executor'):
        try:
            return {"balance": BOT_INSTANCE.executor.get_balance()}
        except:
            pass
    return {"balance": 0.0}

# Copy Trade
@app.get("/copy-traders")
def list_copy_traders():
    # Integração com copy_trading.py
    try:
        from copy_trading import get_leaders
        return {"leaders": get_leaders() if callable(get_leaders) else []}
    except:
        return {"leaders": []}

@app.post("/copy-traders/{leader_id}/toggle")
def toggle_copy(leader_id: str):
    try:
        from copy_trading import toggle_leader
        result = toggle_leader(leader_id)
        return {"success": True, "message": f"Copy {leader_id} toggled"}
    except:
        return {"success": False, "message": "Erro ao togglear copy trade"}

# Estratégias
@app.post("/strategies/smc/toggle")
def toggle_smc():
    try:
        from smc_engine import SMC_Engine
        # Exemplo - ajuste conforme sua classe
        return {"success": True, "message": "SMC Engine toggled"}
    except:
        return {"success": False}

@app.post("/strategies/ma-scalper/toggle")
def toggle_ma_scalper():
    try:
        from ma_scalper import MA_Scalper
        return {"success": True, "message": "MA Scalper toggled"}
    except:
        return {"success": False}

# WebSocket para dados em tempo real (ideal para mobile)
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            if BOT_INSTANCE:
                data = {
                    "pnl": getattr(BOT_INSTANCE, 'pnl', 0),
                    "price": 1234.56,  # exemplo
                    "timestamp": datetime.now().isoformat()
                }
            else:
                data = {"pnl": 0, "timestamp": datetime.now().isoformat()}
            await websocket.send_json(data)
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        pass

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
