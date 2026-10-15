
import contextlib
import datetime
import os
import queue
import threading
import time

import MT5Manager

_lock = threading.Lock()

DEFAULT_EVENTS = "deals,users,positions,orders,accounts"
ALL_EVENTS = {"deals", "users", "positions", "position_updates",
              "orders", "logins", "accounts"}

MONEY_ACTIONS = {"BALANCE", "CREDIT", "CHARGE", "CORRECTION", "BONUS"}


def enabled_events():
    raw = os.getenv("LIVE_EVENTS", DEFAULT_EVENTS).strip().lower()

    if raw in ("", "0", "off", "none", "false"):
        return set()

    parts = {p.strip() for p in raw.split(",") if p.strip()}

    return set(ALL_EVENTS) if "all" in parts else parts


def emit(kind: str, text: str):
    stamp = datetime.datetime.now().strftime("%H:%M:%S")

    try:
        with _lock:
            print(f"[{stamp}] {kind:<9} {text}", flush=True)
    except Exception:
        # console encoding etc. - SDK ke network thread ko error nahi jana chahiye
        try:
            safe = f"[{stamp}] {kind:<9} {text}".encode("ascii", "replace").decode()
            with _lock:
                print(safe, flush=True)
        except Exception:
            pass


def _g(obj, name, default=None):
    try:
        return getattr(obj, name)
    except Exception:
        return default


def _lots(obj):
    # MT5: VolumeExt = lots * 100000000, Volume = lots * 10000
    ext = _g(obj, "VolumeExt")

    if ext:
        return ext / 1e8

    return (_g(obj, "Volume", 0) or 0) / 1e4


def _action_name(value):
    try:
        return MT5Manager.MTDeal.EnDealAction(int(value)).name.replace("DEAL_", "")
    except Exception:
        return str(value)


class LiveSink:
    """Ek hi class sab events ke handlers rakhti hai ."""

    def __init__(self, events=None, manager=None, lock=None):
        self.events = events if events is not None else enabled_events()
        self.manager = manager
        self.lock = lock
        self._q = queue.Queue()
        self._stopped = False
        self._worker = None

        want = os.getenv("LIVE_ACCOUNT", "on").strip().lower()

        # Account snapshot (balance/equity/margin ...) ke liye alag thread.
        # Docs: handler ke ANDAR server calls (*Request) mana hain, isliye
        # handler sirf login queue mein daalta hai, request worker thread karta hai.
        if manager is not None and self.events and want not in ("off", "0", "false", "no"):
            self._worker = threading.Thread(target=self._run, daemon=True)
            self._worker.start()

    def stop(self):
        self._stopped = True
        self._q.put(None)

    def _want_account(self, login):
        if self._worker is not None and login is not None:
            self._q.put(login)

    def _run(self):
        while not self._stopped:
            first = self._q.get()

            if first is None or self._stopped:
                return

            logins = {first}
            time.sleep(0.4)          # ek saath aaye events ek hi snapshot mein

            while True:
                try:
                    item = self._q.get_nowait()
                except queue.Empty:
                    break
                if item is None:
                    return
                logins.add(item)

            for login in sorted(logins):
                self._snapshot(login)

    def _snapshot(self, login):
        try:
            guard = self.lock if self.lock is not None else contextlib.nullcontext()

            with guard:
                acc = self.manager.UserAccountRequest(login)

                if acc is None or acc is False:
                    emit("ACCOUNT", f"login={login}  no data returned: {MT5Manager.LastError()}")
                    return

                def num(name, fmt="{:.2f}"):
                    value = _g(acc, name)
                    return fmt.format(value) if isinstance(value, (int, float)) else "-"

                emit("ACCOUNT",
                     f"login={login}  balance={num('Balance')}  credit={num('Credit')}  "
                     f"equity={num('Equity')}  margin={num('Margin')}  "
                     f"free={num('MarginFree')}  level={num('MarginLevel')}%  "
                     f"profit={num('Profit')}")
        except Exception as exc:
            emit("LIVE", f"account snapshot error: {exc}")

    # ----- deals (balance deposit / withdraw yahin dikhta hai) -------
    def _deal(self, tag, deal):
        action = _action_name(_g(deal, "Action"))
        login = _g(deal, "Login")
        profit = _g(deal, "Profit", 0) or 0
        comment = _g(deal, "Comment", "")
        ticket = _g(deal, "Deal")

        if action in MONEY_ACTIONS:
            label = action
            if action == "BALANCE":
                label = "DEPOSIT" if profit > 0 else "WITHDRAWAL"
            emit("BALANCE" if action == "BALANCE" else action,
                 f"login={login}  {label}  {profit:+.2f}  "
                 f"comment='{comment}'  deal=#{ticket}  ({tag})")
        else:
            emit("DEAL",
                 f"login={login}  {action} {_lots(deal):g} {_g(deal, 'Symbol', '')} "
                 f"@ {_g(deal, 'Price')}  profit={profit:.2f}  "
                 f"deal=#{ticket}  ({tag})")

    def OnDealAdd(self, deal):
        if "deals" in self.events:
            self._deal("added", deal)
            self._want_account(_g(deal, "Login"))

    def OnDealUpdate(self, deal):
        if "deals" in self.events:
            self._deal("updated", deal)

    def OnDealDelete(self, deal):
        if "deals" in self.events:
            self._deal("deleted", deal)
            self._want_account(_g(deal, "Login"))

    # ----- users -----------------------------------------------------
    def _user(self, tag, user):
        emit("USER",
             f"{tag}  login={_g(user, 'Login')}  name='{_g(user, 'Name', '')}'  "
             f"group={_g(user, 'Group')}  balance={_g(user, 'Balance')}")

    def OnUserAdd(self, user):
        if "users" in self.events:
            self._user("ADDED", user)

    def OnUserUpdate(self, user):
        if "users" in self.events:
            self._user("UPDATED", user)
            self._want_account(_g(user, "Login"))

    def OnUserDelete(self, user):
        if "users" in self.events:
            self._user("DELETED", user)

    def OnUserLogin(self, login):
        if "logins" in self.events:
            emit("LOGIN", f"login={_g(login, 'Login', login)} connected")

    def OnUserLogout(self, login):
        if "logins" in self.events:
            emit("LOGOUT", f"login={_g(login, 'Login', login)} disconnected")

    # ----- positions -------------------------------------------------
    def _pos(self, tag, p):
        emit("POSITION",
             f"{tag}  login={_g(p, 'Login')}  #{_g(p, 'Position')}  "
             f"{_action_name(_g(p, 'Action'))} {_lots(p):g} {_g(p, 'Symbol', '')} "
             f"@ {_g(p, 'PriceOpen')}  profit={_g(p, 'Profit')}")

    def OnPositionAdd(self, position):
        if "positions" in self.events:
            self._pos("OPENED", position)
            self._want_account(_g(position, "Login"))

    def OnPositionUpdate(self, position):
        if "position_updates" in self.events:
            self._pos("UPDATED", position)

    def OnPositionDelete(self, position):
        if "positions" in self.events:
            self._pos("CLOSED", position)
            self._want_account(_g(position, "Login"))

    # ----- orders ----------------------------------------------------
    def _order(self, tag, o):
        emit("ORDER",
             f"{tag}  login={_g(o, 'Login')}  #{_g(o, 'Order')}  "
             f"type={_g(o, 'Type')} state={_g(o, 'State')}  "
             f"{_g(o, 'Symbol', '')}")

    def OnOrderAdd(self, order):
        if "orders" in self.events:
            self._order("ADDED", order)

    def OnOrderUpdate(self, order):
        if "orders" in self.events:
            self._order("UPDATED", order)

    def OnOrderDelete(self, order):
        if "orders" in self.events:
            self._order("DELETED", order)

    # ----- margin call / stop out -----------------------------------
    def _acc(self, tag, account):
        emit("ACCOUNT",
             f"{tag}  login={_g(account, 'Login')}  "
             f"balance={_g(account, 'Balance')}  equity={_g(account, 'Equity')}")

    def OnAccountMarginCallEnter(self, account):
        if "accounts" in self.events:
            self._acc("MARGIN CALL ENTER", account)

    def OnAccountMarginCallLeave(self, account):
        if "accounts" in self.events:
            self._acc("margin call leave", account)

    def OnAccountStopOutEnter(self, account):
        if "accounts" in self.events:
            self._acc("STOP OUT ENTER", account)

    def OnAccountStopOutLeave(self, account):
        if "accounts" in self.events:
            self._acc("stop out leave", account)


def _safe(fn):
    """Safe wrapper."""
    def wrapper(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except Exception as exc:
            emit("LIVE", f"{fn.__name__} error: {exc}")
    wrapper.__name__ = fn.__name__
    return wrapper


for _name in [n for n in dir(LiveSink) if n.startswith("On")]:
    setattr(LiveSink, _name, _safe(getattr(LiveSink, _name)))


def subscribe_all(manager, sink):
    """
    please subscribe after connect
    """
    plan = [
        ("deals", "DealSubscribe"),
        ("users", "UserSubscribe"),
        ("positions", "PositionSubscribe"),
        ("orders", "OrderSubscribe"),
        ("accounts", "UserAccountSubscribe"),
    ]

    wanted = sink.events
    wanted_pos = wanted & {"positions", "position_updates"}
    ok = []

    for group, method in plan:
        if group == "positions":
            if not wanted_pos:
                continue
        elif group == "users":
            if not wanted & {"users", "logins"}:
                continue
        elif group not in wanted:
            continue

        try:
            result = getattr(manager, method)(sink)
        except Exception as exc:
            emit("LIVE", f"{method} error: {exc}")
            continue

        if result is False:
            emit("LIVE", f"{method} failed: {MT5Manager.LastError()}")
        else:
            ok.append(group)

    if ok:
        emit("LIVE", "live events ON: " + ", ".join(ok))

    return ok