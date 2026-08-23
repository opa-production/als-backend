from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

#: Unlimited. Mirrors the same constant in the app's ``src/theme/plans.js``.
UNLIMITED = -1

#: "Unlimited" units is still capped: retrieval quality falls off long before a
#: student has fifty units filed, and an unlimited that degrades the product is
#: worse than a stated number.
UNIT_HARD_CAP = 10


class Tier(StrEnum):
    TRIAL = "trial"
    STANDARD = "standard"
    PRO = "pro"
    FRIENDS = "friends"
    #: Not sold. What a lapsed account resolves to — see EXPIRED_LIMITS.
    EXPIRED = "expired"


@dataclass(frozen=True)
class Limits:
    max_course_units: int
    total_pdf_pages_pool: int
    max_single_file_size_mb: int
    max_single_file_pages: int
    daily_ai_queries: int
    quiz_count: int
    quiz_interval: str  # lifetime | weekly | unlimited
    quiz_max_questions: int
    timetable_mode: str  # manual | alerts | ai_sync
    source_citations: str  # basic | exact_page | deep_summary
    allow_ocr_scans: bool
    monthly_ocr_page_limit: int


@dataclass(frozen=True)
class Plan:
    id: Tier
    name: str
    price_ksh: int
    duration_days: int
    seats: int
    limits: Limits

    @property
    def price_per_seat_ksh(self) -> int:
        """
        What each person pays.

        Derived, never stored alongside the total: two numbers that must agree
        eventually stop agreeing, and the one on the pricing card is the one a
        student checks against their M-Pesa message.
        """
        return self.price_ksh // max(1, self.seats)


_PRO_LIMITS = Limits(
    max_course_units=10,
    total_pdf_pages_pool=1500,
    max_single_file_size_mb=50,
    max_single_file_pages=300,
    daily_ai_queries=120,
    quiz_count=UNLIMITED,
    quiz_interval="unlimited",
    quiz_max_questions=20,
    timetable_mode="ai_sync",
    source_citations="deep_summary",
    allow_ocr_scans=True,
    monthly_ocr_page_limit=30,
)

#: The plans, mirroring ``src/theme/plans.js`` exactly.
#:
#: Two copies of a limit is one copy too many, and this is the one that counts:
#: the client's is a convenience so the app can refuse before making a request,
#: but it runs on the student's device and is unenforceable by definition. When
#: these disagree, this file wins. Changing a number here means changing it
#: there in the same commit.
PLANS: dict[Tier, Plan] = {
    # Not a product. Every metered allowance is zero, and every read stays
    # open: a student who stops paying keeps their notes, their timetable and
    # their ability to export or delete. Locking someone out of work they
    # wrote is not a business model, it is hostage-taking.
    Tier.EXPIRED: Plan(
        id=Tier.EXPIRED,
        name="Expired",
        price_ksh=0,
        duration_days=0,
        seats=1,
        limits=Limits(
            # The units they already have stay visible; the cap only ever
            # refuses a *new* one, so zero here means "add no more".
            max_course_units=0,
            total_pdf_pages_pool=0,
            max_single_file_size_mb=0,
            max_single_file_pages=0,
            daily_ai_queries=0,
            quiz_count=0,
            quiz_interval="lifetime",
            quiz_max_questions=0,
            timetable_mode="manual",
            source_citations="basic",
            allow_ocr_scans=False,
            monthly_ocr_page_limit=0,
        ),
    ),
    Tier.TRIAL: Plan(
        id=Tier.TRIAL,
        name="14-Day Free Trial",
        price_ksh=0,
        duration_days=14,
        seats=1,
        limits=Limits(
            max_course_units=2,
            total_pdf_pages_pool=100,
            max_single_file_size_mb=10,
            max_single_file_pages=30,
            daily_ai_queries=15,
            quiz_count=2,
            quiz_interval="lifetime",
            quiz_max_questions=5,
            timetable_mode="manual",
            source_citations="basic",
            allow_ocr_scans=False,
            monthly_ocr_page_limit=0,
        ),
    ),
    Tier.STANDARD: Plan(
        id=Tier.STANDARD,
        name="Focus",
        price_ksh=150,
        duration_days=30,
        seats=1,
        limits=Limits(
            max_course_units=4,
            total_pdf_pages_pool=400,
            max_single_file_size_mb=25,
            max_single_file_pages=100,
            daily_ai_queries=40,
            quiz_count=5,
            quiz_interval="weekly",
            quiz_max_questions=10,
            timetable_mode="alerts",
            source_citations="exact_page",
            allow_ocr_scans=False,
            monthly_ocr_page_limit=0,
        ),
    ),
    Tier.PRO: Plan(
        id=Tier.PRO,
        name="Synapse",
        price_ksh=350,
        duration_days=30,
        seats=1,
        limits=_PRO_LIMITS,
    ),
    # Synapse's limits at a group price. The same Limits object, not a copy —
    # a number changed on Pro cannot drift away from Friends.
    Tier.FRIENDS: Plan(
        id=Tier.FRIENDS,
        name="Friends",
        # KES 250 each for five. Below Synapse's 350 per head, which is the
        # whole proposition, and above Focus so it never undercuts the plan a
        # single student would otherwise buy.
        price_ksh=1250,
        duration_days=30,
        seats=5,
        limits=_PRO_LIMITS,
    ),
}


def plan_for(tier: str | Tier) -> Plan:
    try:
        return PLANS[Tier(tier)]
    except (ValueError, KeyError):
        # An unrecognised tier resolves to *expired*, not to trial. A typo or a
        # tampered row must never be worth more than nothing.
        return PLANS[Tier.EXPIRED]


#: The tiers a student can actually buy, in the order they are shown.
SELLABLE = (Tier.STANDARD, Tier.PRO, Tier.FRIENDS)


def limits_for(tier: str | Tier) -> Limits:
    return plan_for(tier).limits


def unit_cap(tier: str | Tier) -> int:
    limit = limits_for(tier).max_course_units
    return UNIT_HARD_CAP if limit == UNLIMITED else min(limit, UNIT_HARD_CAP)
