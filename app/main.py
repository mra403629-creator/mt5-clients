import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from typing import List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel

from .mt5_service import service

load_dotenv()

log = logging.getLogger("mt5api")


class ConnectRequest(BaseModel):
    server: Optional[str] = None
    login: Optional[int] = None
    password: Optional[str] = None
    timeout: int = 120000
    pump_mode: str = "FULL"   # NONE | USERS | USERS|GROUPS | FULL


class UserCreate(BaseModel):
    group: str
    leverage: int = 100
    first_name: str = ""
    last_name: str = ""
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    country: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zipcode: Optional[str] = None
    address: Optional[str] = None
    comment: Optional[str] = None
    rights: Optional[int] = None
    password: str
    investor_password: str = ""


class MoneyBody(BaseModel):
    amount: float            # > 0
    comment: str = ""        # max 31 chars (extra is cut)


def require_api_key(x_api_key: Optional[str]):
    """API_KEY .env mein set ho to money endpoints par X-API-Key header zaroori."""
    expected = os.getenv("API_KEY")

    if expected and not secrets.compare_digest(
        (x_api_key or "").encode(), expected.encode()
    ):
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")


class AccountsRequest(BaseModel):
    logins: List[int]


class PasswordBody(BaseModel):
    password: str
    type: str = "main"   # main | investor


def handle(fn):
    try:
        return fn()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=exc.args[0] if exc.args else str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # .env mein credentials ho to startup par auto-connect (fail ho to API chalti rahe)
    server = os.getenv("MT5_SERVER")
    login = int(os.getenv("MT5_LOGIN", "0") or 0)
    password = os.getenv("MT5_PASSWORD")

    if server and login and password:
        try:
            service.connect(server, login, password)
            log.warning("MT5 auto-connected to %s as %s", server, login)
        except Exception as exc:
            log.warning("MT5 auto-connect failed: %s", exc)

    yield
    service.disconnect()


app = FastAPI(
    title="MT5 Manager REST API",
    version="1.0.0",
    lifespan=lifespan,
)


@app.post("/api/v1/connection/connect")
def connect(req: ConnectRequest):

    server = req.server or os.getenv("MT5_SERVER")
    login = req.login or int(os.getenv("MT5_LOGIN", "0"))
    password = req.password or os.getenv("MT5_PASSWORD")

    if not server:
        raise HTTPException(
            status_code=500,
            detail="MT5_SERVER is not configured"
        )

    if not login:
        raise HTTPException(
            status_code=500,
            detail="MT5_LOGIN is not configured"
        )

    if not password:
        raise HTTPException(
            status_code=500,
            detail="MT5_PASSWORD is not configured"
        )

    return handle(
        lambda: service.connect(
            server=server,
            login=login,
            password=password,
            timeout=req.timeout,
            pump_mode=req.pump_mode
        )
    )


@app.post("/api/v1/connection/disconnect")
def disconnect():
    return handle(service.disconnect)


@app.get("/api/v1/connection/status")
def connection_status():
    return {"connected": service.connected()}


@app.get("/api/v1/users")
def users(
    group: str = Query("*", description="Group mask, e.g. demo\\* or *"),
    limit: int = Query(1000, ge=1, le=10000),
    offset: int = Query(0, ge=0),
):
    return handle(lambda: service.list_users(group, limit, offset))


@app.get("/api/v1/users/{login}")
def user(login: int):
    return handle(lambda: service.get_user(login))


@app.post("/api/v1/users")
def create_user(req: UserCreate):
    return handle(
        lambda: service.create_user(req.model_dump())
    )


@app.post("/api/v1/users/{login}/password")
def password_change(login: int, req: PasswordBody):
    return handle(
        lambda: service.password_change(login, req.password, req.type)
    )


@app.get(
    "/api/v1/users/{login}/balance",
    summary="Current balance of a login",
)
def user_balance(login: int):
    return handle(lambda: service.balance(login))


@app.post(
    "/api/v1/users/{login}/deposit",
    summary="Deposit (DealerBalance, positive amount)",
)
def user_deposit(
    login: int,
    req: MoneyBody,
    x_api_key: Optional[str] = Header(None),
):
    require_api_key(x_api_key)
    return handle(lambda: service.deposit(login, req.amount, req.comment))


@app.post(
    "/api/v1/users/{login}/withdraw",
    summary="Withdrawal (DealerBalance, negative amount)",
)
def user_withdraw(
    login: int,
    req: MoneyBody,
    x_api_key: Optional[str] = Header(None),
):
    require_api_key(x_api_key)
    return handle(lambda: service.withdraw(login, req.amount, req.comment))


@app.post(
    "/api/v1/accounts",
    summary="Accounts data of multiple logins",
    description=(
        "Send a list of logins (max 500). Returns Balance, Credit, Equity, "
        "Margin, MarginFree, MarginLevel, Profit, Floating, Storage etc. for "
        "each login, plus Name and Group. Logins that do not exist are listed "
        "in not_found."
    ),
)
def accounts(req: AccountsRequest):
    return handle(lambda: service.accounts(req.logins))


@app.get("/api/v1/groups")
def groups():
    return handle(service.list_groups)


@app.get("/api/v1/groups/detail")
def group_detail(name: str = Query(..., description="e.g. demo\\forex")):
    return handle(lambda: service.get_group(name))


# =============================================================
# LOGIN (password check + in-memory session token)
# =============================================================

TOKEN_TTL = 3600          # token 1 ghante valid
MAX_FAILS = 5             # 5 galat attempts ...
FAIL_WINDOW = 300         # ... 5 minute mein -> temporary block

SESSIONS = {}             # token -> (login, expiry)
FAILS = {}                # login -> [timestamps]


class LoginBody(BaseModel):
    login: int
    password: str


def _public_user(user: dict) -> dict:
    keys = ["Login", "Group", "Name", "FirstName", "LastName", "EMail", "Leverage"]
    return {k: user.get(k) for k in keys}


@app.post("/api/v1/auth/login")
def auth_login(req: LoginBody):
    now = time.time()
    recent = [t for t in FAILS.get(req.login, []) if now - t < FAIL_WINDOW]

    if len(recent) >= MAX_FAILS:
        raise HTTPException(
            status_code=429,
            detail="Too many failed attempts. Try again in a few minutes.",
        )

    result = handle(
        lambda: service.password_check(req.login, req.password, "main")
    )

    if not result["valid"]:
        recent.append(now)
        FAILS[req.login] = recent
        raise HTTPException(status_code=401, detail="Invalid login or password")

    FAILS.pop(req.login, None)

    token = secrets.token_urlsafe(32)
    SESSIONS[token] = (req.login, now + TOKEN_TTL)

    user = handle(lambda: service.get_user(req.login))

    return {
        "token": token,
        "token_type": "Bearer",
        "expires_in": TOKEN_TTL,
        "user": _public_user(user),
    }


@app.get("/api/v1/auth/me")
def auth_me(authorization: Optional[str] = Header(None)):
    token = (authorization or "").removeprefix("Bearer ").strip()
    session = SESSIONS.get(token)

    if not session or session[1] < time.time():
        SESSIONS.pop(token, None)
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user = handle(lambda: service.get_user(session[0]))

    return _public_user(user)