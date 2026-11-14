"""Customers API: Connection applications and Existing customers (premises). Mounted by main.py with
app.include_router(routes_customers.router). Errors are ValueError, which main.py returns as HTTP 400 with the reason in "detail"."""
from fastapi import APIRouter

import customers as C

router = APIRouter()


@router.get("/api/customers/pipeline")
def pipeline():
    """Connection applications: the pipeline by stage (open, stuck, blocked, median days in stage, ageing buckets), blockers by stage, the open
    applications by type and payment status, and the headline figures."""
    return C.pipeline()


@router.get("/api/customers/applications")
def applications(stage: str = "", type: str = "", stuck: bool = False, q: str = "", open: bool = True, limit: int = 2000):
    """Connection applications, stuck first. Filters: stage (apply to connected), type (connection type code), stuck, q (applicant, address,
    suburb or application id), open (true for open applications only)."""
    return C.applications(stage or None, type or None, stuck, q or None, open, limit)


@router.get("/api/customers/application/{app_id}")
def application(app_id: str):
    """One connection application's journey through the stages, its payment, provider, blocker and where it is stuck."""
    return C.application(app_id)


@router.get("/api/customers/existing/summary")
def existing_summary():
    """Existing customers at a glance: customers, life support, critical sites, DER, notification preferences, and supply off now."""
    return C.existing_summary()


@router.get("/api/customers/existing/search")
def existing_search(q: str = "", filter: str = "all", cohort: str = "", limit: int = 60):
    """Premises in one figure (all, life_support, critical, der, sms, off_supply) and optionally one of its cohorts, optionally matching a
    search; supply status now on each."""
    return C.existing_search(q, filter, limit, cohort or None)


@router.get("/api/customers/existing/cohorts")
def existing_cohorts(figure: str = "all"):
    """The cohorts of one figure, largest first, each with how many premises are in it."""
    return C.existing_cohorts(figure)


@router.get("/api/customers/existing/{nmi}")
def existing_customer(nmi: str):
    """One premises: the register, supply now, outages on its protective device in the last year, the notices sent to it, and its channels."""
    return C.existing_customer(nmi)
