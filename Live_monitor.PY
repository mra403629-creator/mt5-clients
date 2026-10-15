"""
Watch live MT5 events in a separate terminal (without FastAPI).

Run (from the project root, venv active):
    python live_monitor.py                  # pump USERS (fast connect)
    python live_monitor.py --pump FULL      # pump everything (slower connect)
    python live_monitor.py --test 123456    # deposit 1.0 then withdraw 1.0 on a TEST login
Stop with Ctrl + C (works even while connecting).

Reads MT5_SERVER / MT5_LOGIN / MT5_PASSWORD from .env.
Optional .env settings: LIVE_EVENTS, LIVE_PUMP, LIVE_TIMEOUT, LIVE_ACCOUNT
"""
import argparse
import os
import threading
import time

import MT5Manager
from dotenv import load_dotenv

from app.live_events import LiveSink, emit, subscribe_all
from app.mt5_service import _resolve_pump_mode

load_dotenv()

parser = argparse.ArgumentParser(description="Watch live MT5 events")
parser.add_argument("--pump", default=os.getenv("LIVE_PUMP", "USERS"),
                    help="NONE | USERS | FULL | USERS|POSITIONS (default USERS)")
parser.add_argument("--timeout", type=int,
                    default=int(os.getenv("LIVE_TIMEOUT", "60")),
                    help="connect timeout in seconds (default 60)")
parser.add_argument("--test", type=int, metavar="LOGIN",
                    help="deposit 1.0 then withdraw 1.0 on this TEST login")
args = parser.parse_args()

server = os.getenv("MT5_SERVER")
login = int(os.getenv("MT5_LOGIN", "0"))
password = os.getenv("MT5_PASSWORD")

try:
    pump = _resolve_pump_mode(args.pump)
except ValueError as exc:
    print(exc)
    raise SystemExit(2)

manager = MT5Manager.ManagerAPI()
sink = LiveSink(manager=manager)      # must stay alive while subscribed (docs)

# docs: subscribe before Connect
subscribe_all(manager, sink)

# Connect() is a blocking native call, so run it in a thread. The main
# thread keeps printing progress and can still react to Ctrl+C.
result = {}


def do_connect():
    result["ok"] = manager.Connect(server, login, password, pump, args.timeout * 1000)


emit("LIVE", f"connecting to {server} as {login} "
             f"(pump={args.pump}, timeout={args.timeout}s) ...")

worker = threading.Thread(target=do_connect, daemon=True)
worker.start()

try:
    waited = 0
    while worker.is_alive():
        worker.join(1)
        waited += 1
        if waited % 5 == 0 and worker.is_alive():
            emit("LIVE", f"still connecting ... {waited}s")
except KeyboardInterrupt:
    emit("LIVE", "stopped while connecting")
    os._exit(1)

if not result.get("ok"):
    print("Connect failed:", MT5Manager.LastError())
    os._exit(1)

emit("LIVE", "connected - waiting for events (press Ctrl+C to stop)")
emit("LIVE", "tip: if nothing appears after a change on the server, "
             "try: python live_monitor.py --pump FULL")

if args.test:
    deal_type = MT5Manager.MTDeal.EnDealAction.DEAL_BALANCE

    time.sleep(1)
    emit("TEST", f"login {args.test}: deposit 1.0, then withdraw 1.0 (net 0)")

    for amount in (1.0, -1.0):
        deal_id = manager.DealerBalance(args.test, amount, deal_type, "live test")

        if deal_id is False:
            emit("TEST", f"{amount:+.1f} FAILED: {MT5Manager.LastError()}")
            break

        emit("TEST", f"{amount:+.1f} accepted by the server (deal #{deal_id})")
        time.sleep(1.5)

    emit("TEST", "done. Did BALANCE lines appear above? "
                 "If not, events are not being delivered to this manager.")

try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    pass

manager.Disconnect()
emit("LIVE", "disconnected")
sink.stop()