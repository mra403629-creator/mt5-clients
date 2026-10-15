import math
import threading

import MT5Manager

from .live_events import LiveSink, enabled_events, subscribe_all


# =============================================================
# HELPERS
# =============================================================

def _last_error() -> str:
    """
    LastError() MT5Manager *module* ka function hai, ManagerAPI ka method nahi.
    Returns (code, EnMTAPIRetcode, message).
    """
    try:
        code, retcode, message = MT5Manager.LastError()
        return f"[{code}] {retcode} - {message}"
    except Exception as exc:  # pragma: no cover
        return f"unknown error ({exc})"


def _last_error_code() -> int:
    try:
        return int(MT5Manager.LastError()[1])
    except Exception:
        return -1


MT_RET_ERR_NOTFOUND = 13


def _resolve_pump_mode(value):
    """
    Docs ke hisaab se pump_mode 0 (bina sync) bhi valid hai - sabse tez connect.
    Accepts: "NONE" / "0" / 0, "USERS", "FULL", "PUMP_MODE_USERS",
             combinations jaise "USERS|POSITIONS".
    """
    modes = MT5Manager.ManagerAPI.EnPumpModes

    if value is None or str(value).strip() == "":
        value = "FULL"

    text = str(value).strip().upper()

    if text in ("0", "NONE", "NO_PUMP"):
        return 0

    if text.isdigit():
        return int(text)

    result = 0

    for part in text.replace("+", "|").split("|"):
        part = part.strip()
        name = part if part.startswith("PUMP_MODE_") else f"PUMP_MODE_{part}"
        mode = getattr(modes, name, None)

        if mode is None:
            valid = [m for m in dir(modes) if m.startswith("PUMP_MODE_")]
            raise ValueError(
                f"Invalid pump_mode '{part}'. Valid: NONE, "
                + ", ".join(v.replace("PUMP_MODE_", "") for v in valid)
            )

        result |= int(mode)

    return result


class MT5Service:

    def __init__(self):
        self.manager = None
        self._connected = False
        self._lock = threading.RLock()
        self._sink = None   # live-events sink; memory mein zinda rehna chahiye

    def connected(self):
        return self._connected

    # =========================================================
    # CONNECT
    # =========================================================

    def connect(
        self,
        server: str,
        login: int,
        password: str,
        timeout: int = 120000,
        pump_mode: str = "FULL",
    ):
        mode = _resolve_pump_mode(pump_mode)

        with self._lock:

            # Purana connection ho to pehle band karo
            self._safe_disconnect()

            manager = MT5Manager.ManagerAPI()

            # live events: docs ke hisaab se Connect se PEHLE subscribe
            events = enabled_events()
            self._sink = (
                LiveSink(events, manager=manager, lock=self._lock)
                if events else None
            )

            if self._sink:
                subscribe_all(manager, self._sink)

            try:
                ok = manager.Connect(
                    server,
                    int(login),
                    password,
                    mode,
                    int(timeout),
                )
            except Exception as exc:
                raise RuntimeError(
                    f"MT5 Manager connection error: {exc} | {_last_error()}"
                )

            if not ok:
                error = _last_error()

                try:
                    manager.Disconnect()
                except Exception:
                    pass

                raise RuntimeError(
                    f"MT5 Manager connection failed: {error}"
                )

            self.manager = manager
            self._connected = True

            return {
                "connected": True,
                "server": server,
                "login": login,
                "pump_mode": str(pump_mode).upper(),
            }

    # =========================================================
    # DISCONNECT
    # =========================================================

    def _safe_disconnect(self):
        if self.manager:
            try:
                self.manager.Disconnect()
            except Exception:
                pass

        self.manager = None
        self._connected = False

        if self._sink:
            self._sink.stop()

        self._sink = None

    def disconnect(self):
        with self._lock:
            self._safe_disconnect()

        return {"connected": False}

    # =========================================================
    # CONNECTION CHECK
    # =========================================================

    def _check_connection(self):
        if not self.manager or not self._connected:
            raise RuntimeError(
                "MT5 Manager is not connected. "
                "Call /api/v1/connection/connect first."
            )

    def _try(self, method: str, *args):
        """SDK method call; exception ko None bana deta hai (fallback ke liye)."""
        try:
            return getattr(self.manager, method)(*args)
        except Exception:
            return None

    # =========================================================
    # USERS
    # =========================================================

    def list_users(self, group: str = "*", limit: int = 1000, offset: int = 0):

        self._check_connection()

        with self._lock:
            # 1) pump cache (PUMP_MODE_USERS / FULL), 2) server se direct request
            users = self._try("UserGetByGroup", group)

            if not users:
                users = self._try("UserRequestByGroup", group)

            if users is None or users is False:
                raise RuntimeError(
                    f"Unable to get users: {_last_error()}"
                )

            users = list(users)

        total = len(users)
        page = users[offset: offset + limit]

        return {
            "total": total,
            "count": len(page),
            "offset": offset,
            "users": [self._object_to_dict(u) for u in page],
        }

    def get_user(self, login: int):

        self._check_connection()

        with self._lock:
            user = self._try("UserGet", login)

            if not user:
                user = self._try("UserRequest", login)

            if not user:
                raise KeyError(f"User {login} not found ({_last_error()})")

        return self._object_to_dict(user)

    def create_user(self, data: dict):

        self._check_connection()

        with self._lock:
            user = MT5Manager.MTUser(self.manager)

            user.Group = data["group"]
            user.Leverage = data.get("leverage", 100)
            user.FirstName = data.get("first_name") or ""
            user.LastName = data.get("last_name") or ""
            user.Name = (
                data.get("name")
                or f"{user.FirstName} {user.LastName}".strip()
            )

            self._apply_optional_fields(user, data)

            if not data.get("rights"):
                user.Rights = MT5Manager.MTUser.EnUsersRights.USER_RIGHT_DEFAULT

            main_pw = data["password"]
            investor_pw = data.get("investor_password") or ""

            if not self.manager.UserAdd(user, main_pw, investor_pw):
                raise RuntimeError(f"UserAdd failed: {_last_error()}")

            return self._object_to_dict(user)

    # =========================================================
    # ACCOUNTS (balance / equity / margin ...) - multiple logins
    # docs: UserAccountRequestByLogins, UserAccountRequest,
    #       UserRequestByLogins (name/group)
    # =========================================================

    MAX_ACCOUNT_LOGINS = 500

    def accounts(self, logins):

        self._check_connection()

        logins = list(dict.fromkeys(int(x) for x in logins))

        if not logins:
            raise ValueError("logins list is empty")

        if len(logins) > self.MAX_ACCOUNT_LOGINS:
            raise ValueError(
                f"Max {self.MAX_ACCOUNT_LOGINS} logins per request"
            )

        def valid(x):
            return x is not None and x is not False

        with self._lock:
            # 1) ek hi call mein sab accounts
            accs = self._try("UserAccountRequestByLogins", logins)

            # 2) fail ho to ek-ek login ka request
            if not valid(accs):
                accs = []
                for login in logins:
                    a = self._try("UserAccountRequest", login)
                    if valid(a):
                        accs.append(a)

            # name / group ke liye user records
            users = self._try("UserRequestByLogins", logins)

            if not valid(users):
                users = []
                for login in logins:
                    u = self._try("UserRequest", login)
                    if valid(u):
                        users.append(u)

            error = _last_error() if not list(accs) else None

            accs = list(accs)
            users = {u.Login: u for u in users}

        items = []
        found = set()

        for a in accs:
            u = users.get(a.Login)
            item = {
                "Login": a.Login,
                "Name": u.Name if u else None,
                "Group": u.Group if u else None,
            }
            item.update(self._object_to_dict(a))
            items.append(item)
            found.add(a.Login)

        result = {
            "requested": len(logins),
            "found": len(items),
            "not_found": [x for x in logins if x not in found],
            "accounts": items,
        }

        if error:
            result["last_error"] = error

        return result

    # =========================================================
    # BALANCE / DEPOSIT / WITHDRAWAL
    # docs: UserAccountRequest (IMTAccount), UserRequest().Balance,
    #       DealerBalance(login, value, type, comment)
    #       value > 0 -> deposit, value < 0 -> withdrawal
    # =========================================================

    MAX_MONEY = 1_000_000_000   # docs: MT_RET_TRADE_MAX_MONEY limit

    def balance(self, login: int):

        self._check_connection()

        def valid(x):
            return x is not None and x is not False

        with self._lock:
            acc = self._try("UserAccountRequest", login)

            if not valid(acc):
                acc = self._try("UserAccountGet", login)

            if valid(acc):
                return {
                    "login": login,
                    "balance": getattr(acc, "Balance", None),
                    "credit": getattr(acc, "Credit", None),
                    "equity": getattr(acc, "Equity", None),
                    "margin": getattr(acc, "Margin", None),
                    "margin_free": getattr(acc, "MarginFree", None),
                    "margin_level": getattr(acc, "MarginLevel", None),
                    "profit": getattr(acc, "Profit", None),
                }

            # docs example: manager.UserRequest(login).Balance
            user = self._try("UserRequest", login)

            if not valid(user):
                raise KeyError(f"User {login} not found ({_last_error()})")

            return {
                "login": login,
                "balance": user.Balance,
                "credit": user.Credit,
            }

    def _balance_operation(self, login: int, amount: float, sign: int,
                           comment: str):

        self._check_connection()

        amount = float(amount)

        if not math.isfinite(amount) or amount <= 0:
            raise ValueError("amount must be a number greater than 0")

        if amount > self.MAX_MONEY:
            raise ValueError(f"amount cannot exceed {self.MAX_MONEY}")

        # docs: comment max 32 chars including the terminator
        comment = (comment or "")[:31]

        deal_type = MT5Manager.MTDeal.EnDealAction.DEAL_BALANCE

        with self._lock:
            deal_id = self.manager.DealerBalance(
                int(login), sign * amount, deal_type, comment
            )

            if deal_id is False:
                retcodes = MT5Manager.EnMTAPIRetcode
                code = _last_error_code()
                error = _last_error()

                if code == int(getattr(retcodes, "MT_RET_REQUEST_NO_MONEY", -999)):
                    raise ValueError(
                        "Not enough money: withdrawal cannot exceed the "
                        f"balance / free margin ({error})"
                    )

                if code == int(getattr(retcodes, "MT_RET_TRADE_MAX_MONEY", -999)):
                    raise ValueError(f"Amount too large ({error})")

                raise RuntimeError(f"Balance operation failed: {error}")

            after = None

            try:
                after = self.balance(login)
            except Exception:
                pass

        return {
            "login": login,
            "type": "deposit" if sign > 0 else "withdrawal",
            "amount": amount,
            "deal_id": deal_id,
            "account_after": after,
        }

    def deposit(self, login: int, amount: float, comment: str = ""):
        return self._balance_operation(login, amount, 1, comment)

    def withdraw(self, login: int, amount: float, comment: str = ""):
        return self._balance_operation(login, amount, -1, comment)

    # =========================================================
    # GROUPS (read-only: docs ke hisaab se Manager API se nayi group
    # create nahi hoti; sirf margin/commission/symbol fields update hote hain)
    # =========================================================

    _GROUPS_HINT = (
        "Groups need PUMP_MODE_GROUPS. Reconnect with "
        '{"pump_mode": "USERS|GROUPS"}'
    )

    def list_groups(self):

        self._check_connection()

        with self._lock:
            total = self._try("GroupTotal")

            if total is None or total is False:
                raise RuntimeError(f"{self._GROUPS_HINT} ({_last_error()})")

            groups = []

            for pos in range(int(total)):
                g = self._try("GroupNext", pos)

                if g is None or g is False:
                    continue

                groups.append({
                    "group": g.Group,
                    "server": g.Server,
                    "company": g.Company,
                    "currency": g.Currency,
                    "margin_call": g.MarginCall,
                    "margin_stopout": g.MarginStopOut,
                })

        return {"total": len(groups), "groups": groups}

    def get_group(self, name: str):

        self._check_connection()

        with self._lock:
            g = self._try("GroupGet", name)

            if g is None or g is False:
                code = _last_error_code()
                error = _last_error()

                if code == MT_RET_ERR_NOTFOUND:
                    raise KeyError(
                        f"Group '{name}' not found on server. "
                        "Call GET /api/v1/groups to see exact group names."
                    )

                raise KeyError(
                    f"Cannot get group '{name}': {error}. {self._GROUPS_HINT}"
                )

        return self._object_to_dict(g)

    # =========================================================
    # PASSWORDS (docs: UserPasswordCheck / UserPasswordChange)
    # =========================================================

    @staticmethod
    def _password_type(kind: str):
        types = MT5Manager.MTUser.EnUsersPasswords
        kind = (kind or "main").strip().upper()

        if kind == "MAIN":
            return types.USER_PASS_MAIN
        if kind == "INVESTOR":
            return types.USER_PASS_INVESTOR

        raise ValueError("type must be 'main' or 'investor'")

    def password_check(self, login: int, password: str, kind: str = "main"):

        self._check_connection()
        ptype = self._password_type(kind)

        with self._lock:
            valid = bool(
                self.manager.UserPasswordCheck(ptype, login, password)
            )
            error = None if valid else _last_error()

        return {"login": login, "type": kind, "valid": valid, "error": error}

    def password_change(self, login: int, password: str, kind: str = "main"):

        self._check_connection()
        ptype = self._password_type(kind)

        with self._lock:
            if not self.manager.UserPasswordChange(ptype, login, password):
                raise RuntimeError(
                    f"UserPasswordChange failed: {_last_error()}"
                )

        return {"login": login, "type": kind, "changed": True}

    @staticmethod
    def _apply_optional_fields(user, data: dict):
        mapping = {
            "name": "Name",
            "email": "EMail",
            "phone": "Phone",
            "country": "Country",
            "city": "City",
            "state": "State",
            "zipcode": "ZIPCode",
            "address": "Address",
            "comment": "Comment",
            "rights": "Rights",
        }

        for key, attr in mapping.items():
            if data.get(key) is None:
                continue

            # Swagger ka default "rights": 0 account ke saare rights hata deta hai
            if key == "rights" and not data[key]:
                continue

            setattr(user, attr, data[key])

    # =========================================================
    # POSITIONS
    # =========================================================

    # =========================================================
    # OBJECT -> DICTIONARY
    # =========================================================

    def _object_to_dict(self, obj):

        if obj is None:
            return None

        if isinstance(obj, (str, int, float, bool)):
            return obj

        if isinstance(obj, (list, tuple)):
            return [self._object_to_dict(item) for item in obj]

        if isinstance(obj, dict):
            return {
                str(k): self._object_to_dict(v)
                for k, v in obj.items()
            }

        result = {}

        try:
            for key in dir(obj):

                if key.startswith("_"):
                    continue

                try:
                    value = getattr(obj, key)
                except Exception:
                    continue

                if callable(value):
                    continue

                if isinstance(value, (str, int, float, bool, type(None))):
                    result[key] = value

        except Exception:
            pass

        return result


# =============================================================
# GLOBAL SERVICE INSTANCE
# =============================================================

service = MT5Service()