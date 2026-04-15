"""Admin UI router for DocklyOCR.

Server-rendered Jinja2 + HTMX operator console. Session-based auth via
Starlette's `SessionMiddleware` (mounted in `app/main.py`). All routes live
under `/admin/*`.

Design and route contract: see `OCR-API-Projekt-Anforderungen.md` §5 and
`docs/superpowers/specs/2026-04-15-docklyocr-implementation-design.md` §4.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, func, select

from app.auth import generate_api_key, hash_password, verify_password
from app.config import settings
from app.db import get_session
from app.models import AdminUser, ApiKey, Customer, Job, JobStatus

# ---------------------------------------------------------------------------
# Router + template engine
# ---------------------------------------------------------------------------

router = APIRouter(tags=["admin"], include_in_schema=False)

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


def _check_admin(request: Request) -> RedirectResponse | None:
    """Return a 303 redirect to /admin/login if not authenticated, else None."""
    if not request.session.get("admin_user"):
        return RedirectResponse("/admin/login", status_code=303)
    return None


def _authenticate(session: Session, username: str, password: str) -> bool:
    """Verify username + password.

    Preference order:
    1. Try the `AdminUser` DB row for `username` — this is the production path
       (the init script creates one).
    2. Fall back to `settings.admin_username` + `settings.admin_password_hash`
       from the environment (useful for first-boot before the row exists).
    """
    if username != settings.admin_username:
        return False

    row = session.exec(select(AdminUser).where(AdminUser.username == username)).first()
    if row is not None:
        return verify_password(password, row.password_hash)

    if not settings.admin_password_hash:
        return False
    return verify_password(password, settings.admin_password_hash)


# ---------------------------------------------------------------------------
# Login / Logout
# ---------------------------------------------------------------------------


@router.get("/admin/login", response_class=HTMLResponse)
async def login_form(request: Request) -> Response:
    """Render the login form. Already-authenticated users are bounced to /admin."""
    if request.session.get("admin_user"):
        return RedirectResponse("/admin", status_code=303)
    return templates.TemplateResponse(request, "admin/login.html", {"error": None})


@router.post("/admin/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    session: Session = Depends(get_session),  # noqa: B008 -- FastAPI dep pattern
) -> Response:
    """Verify credentials, set session, redirect to /admin."""
    if not _authenticate(session, username, password):
        return templates.TemplateResponse(
            request,
            "admin/login.html",
            {"error": "Invalid username or password."},
            status_code=200,
        )
    request.session["admin_user"] = username
    return RedirectResponse("/admin", status_code=303)


@router.post("/admin/logout")
async def logout(request: Request) -> Response:
    """Clear session, bounce to login."""
    request.session.clear()
    return RedirectResponse("/admin/login", status_code=303)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


def _job_row_view(job: Job, customer_name: str) -> dict[str, Any]:
    """Project a `Job` + customer name to the shape expected by the templates."""
    return {
        "id": job.id,
        "customer_name": customer_name,
        "output_format": job.output_format.value
        if hasattr(job.output_format, "value")
        else str(job.output_format),
        "status": job.status.value if hasattr(job.status, "value") else str(job.status),
        "page_count": job.page_count,
        "created_at": job.created_at,
    }


@router.get("/admin", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    session: Session = Depends(get_session),  # noqa: B008 -- FastAPI dep pattern
) -> Response:
    """Aggregate stats + last 10 jobs."""
    redirect = _check_admin(request)
    if redirect is not None:
        return redirect

    customer_count = session.exec(select(func.count()).select_from(Customer)).one()

    now = datetime.utcnow()
    today_start = datetime(now.year, now.month, now.day)
    week_start = now - timedelta(days=7)
    month_start = now - timedelta(days=30)

    jobs_today = session.exec(
        select(func.count()).select_from(Job).where(Job.created_at >= today_start)
    ).one()
    jobs_week = session.exec(
        select(func.count()).select_from(Job).where(Job.created_at >= week_start)
    ).one()
    jobs_month = session.exec(
        select(func.count()).select_from(Job).where(Job.created_at >= month_start)
    ).one()

    recent_rows = session.exec(
        select(Job, Customer)
        .join(Customer, Customer.id == Job.customer_id)
        .order_by(Job.created_at.desc())
        .limit(10)
    ).all()
    recent_jobs = [_job_row_view(job, customer.name) for job, customer in recent_rows]

    return templates.TemplateResponse(
        request,
        "admin/dashboard.html",
        {
            "customer_count": customer_count,
            "jobs_today": jobs_today,
            "jobs_week": jobs_week,
            "jobs_month": jobs_month,
            "recent_jobs": recent_jobs,
        },
    )


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------


def _list_customers_with_key_counts(session: Session) -> list[dict[str, Any]]:
    customers = session.exec(select(Customer).order_by(Customer.created_at.desc())).all()
    rows: list[dict[str, Any]] = []
    for c in customers:
        active_keys = session.exec(
            select(func.count())
            .select_from(ApiKey)
            .where(ApiKey.customer_id == c.id)
            .where(ApiKey.is_active == True)  # noqa: E712
        ).one()
        rows.append(
            {
                "id": c.id,
                "name": c.name,
                "email": c.email,
                "plan": c.plan,
                "active_keys": active_keys,
                "created_at": c.created_at,
            }
        )
    return rows


@router.get("/admin/customers", response_class=HTMLResponse)
async def customers_list(
    request: Request,
    session: Session = Depends(get_session),  # noqa: B008 -- FastAPI dep pattern
) -> Response:
    redirect = _check_admin(request)
    if redirect is not None:
        return redirect

    return templates.TemplateResponse(
        request,
        "admin/customers_list.html",
        {
            "customers": _list_customers_with_key_counts(session),
            "error": None,
            "form_name": "",
            "form_email": "",
            "form_plan": "free",
            "form_open": False,
        },
    )


@router.post("/admin/customers", response_class=HTMLResponse)
async def customers_create(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    plan: str = Form("free"),
    session: Session = Depends(get_session),  # noqa: B008 -- FastAPI dep pattern
) -> Response:
    redirect = _check_admin(request)
    if redirect is not None:
        return redirect

    name_clean = name.strip()
    email_clean = email.strip().lower()

    def _error(message: str) -> Response:
        return templates.TemplateResponse(
            request,
            "admin/customers_list.html",
            {
                "customers": _list_customers_with_key_counts(session),
                "error": message,
                "form_name": name,
                "form_email": email,
                "form_plan": plan,
                "form_open": True,
            },
            status_code=200,
        )

    if not name_clean:
        return _error("Name must not be empty.")
    if not email_clean or "@" not in email_clean:
        return _error("A valid email is required.")

    existing = session.exec(select(Customer).where(Customer.email == email_clean)).first()
    if existing is not None:
        return _error(f"A customer with email '{email_clean}' already exists.")

    customer = Customer(name=name_clean, email=email_clean, plan=plan or "free")
    session.add(customer)
    session.commit()
    session.refresh(customer)

    return RedirectResponse(f"/admin/customers/{customer.id}", status_code=303)


def _build_customer_detail_ctx(session: Session, customer: Customer) -> dict[str, Any]:
    keys = session.exec(
        select(ApiKey).where(ApiKey.customer_id == customer.id).order_by(ApiKey.created_at.desc())
    ).all()
    recent_rows = session.exec(
        select(Job).where(Job.customer_id == customer.id).order_by(Job.created_at.desc()).limit(10)
    ).all()
    recent_jobs = [_job_row_view(job, customer.name) for job in recent_rows]
    return {
        "customer": customer,
        "keys": keys,
        "recent_jobs": recent_jobs,
    }


@router.get("/admin/customers/{customer_id}", response_class=HTMLResponse)
async def customer_detail(
    customer_id: int,
    request: Request,
    session: Session = Depends(get_session),  # noqa: B008 -- FastAPI dep pattern
) -> Response:
    redirect = _check_admin(request)
    if redirect is not None:
        return redirect

    customer = session.get(Customer, customer_id)
    if customer is None:
        return RedirectResponse("/admin/customers", status_code=303)

    return templates.TemplateResponse(
        request,
        "admin/customer_detail.html",
        _build_customer_detail_ctx(session, customer),
    )


# ---------------------------------------------------------------------------
# API Keys
# ---------------------------------------------------------------------------


@router.post("/admin/customers/{customer_id}/keys", response_class=HTMLResponse)
async def customer_create_key(
    customer_id: int,
    request: Request,
    name: str = Form(...),
    session: Session = Depends(get_session),  # noqa: B008 -- FastAPI dep pattern
) -> Response:
    """Create a new API key. Returns an HTMX modal fragment with the plaintext."""
    redirect = _check_admin(request)
    if redirect is not None:
        return redirect

    customer = session.get(Customer, customer_id)
    if customer is None:
        return RedirectResponse("/admin/customers", status_code=303)

    label = name.strip() or "unnamed key"
    plaintext, key_hash, prefix = generate_api_key()

    api_key = ApiKey(
        customer_id=customer_id,
        key_hash=key_hash,
        key_prefix=prefix,
        name=label,
    )
    session.add(api_key)
    session.commit()
    session.refresh(api_key)

    return templates.TemplateResponse(
        request,
        "admin/_key_modal.html",
        {
            "customer": customer,
            "key_name": label,
            "plaintext": plaintext,
        },
    )


@router.post(
    "/admin/customers/{customer_id}/keys/{key_id}/revoke",
    response_class=HTMLResponse,
)
async def customer_revoke_key(
    customer_id: int,
    key_id: int,
    request: Request,
    session: Session = Depends(get_session),  # noqa: B008 -- FastAPI dep pattern
) -> Response:
    redirect = _check_admin(request)
    if redirect is not None:
        return redirect

    api_key = session.get(ApiKey, key_id)
    if api_key is None or api_key.customer_id != customer_id:
        return RedirectResponse(f"/admin/customers/{customer_id}", status_code=303)

    api_key.is_active = False
    session.add(api_key)
    session.commit()

    customer = session.get(Customer, customer_id)
    if customer is None:
        return RedirectResponse("/admin/customers", status_code=303)

    # If this came from HTMX, return just the keys table fragment so it can
    # be swapped into #keys-table. Otherwise full-page reload to detail.
    if request.headers.get("hx-request", "").lower() == "true":
        return templates.TemplateResponse(
            request,
            "admin/_keys_table.html",
            _build_customer_detail_ctx(session, customer),
        )
    return RedirectResponse(f"/admin/customers/{customer_id}", status_code=303)


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


@router.get("/admin/jobs", response_class=HTMLResponse)
async def jobs_list(
    request: Request,
    status: str | None = None,
    customer_id: int | None = None,
    limit: int = 50,
    offset: int = 0,
    session: Session = Depends(get_session),  # noqa: B008 -- FastAPI dep pattern
) -> Response:
    redirect = _check_admin(request)
    if redirect is not None:
        return redirect

    statement = select(Job, Customer).join(Customer, Customer.id == Job.customer_id)
    status_filter: JobStatus | None = None
    if status:
        try:
            status_filter = JobStatus(status)
        except ValueError:
            status_filter = None
    if status_filter is not None:
        statement = statement.where(Job.status == status_filter)
    if customer_id is not None:
        statement = statement.where(Job.customer_id == customer_id)
    statement = statement.order_by(Job.created_at.desc()).limit(limit).offset(offset)

    rows = session.exec(statement).all()
    jobs_view = [_job_row_view(job, customer.name) for job, customer in rows]

    customers = session.exec(select(Customer).order_by(Customer.name)).all()

    ctx = {
        "jobs": jobs_view,
        "customers": customers,
        "selected_status": status or "",
        "selected_customer_id": customer_id,
        "statuses": [s.value for s in JobStatus],
    }

    # HTMX partial: swap just the table body
    if request.headers.get("hx-request", "").lower() == "true":
        return templates.TemplateResponse(request, "admin/_jobs_table.html", ctx)
    return templates.TemplateResponse(request, "admin/jobs_list.html", ctx)


# ---------------------------------------------------------------------------
# Unused imports kept for potential future use / satisfy type checkers
# ---------------------------------------------------------------------------
_ = hash_password  # noqa: F401 -- re-exported for test convenience
