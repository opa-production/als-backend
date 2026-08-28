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
    #: The floor. Not sold, never expires, and what everything else falls back
    #: to — a new account, a lapsed one, an unrecognised tier.
    FREE = "free"
    STANDARD = "standard"
    PRO = "pro"
    FRIENDS = "friends"
    #: Legacy. The fourteen-day trial is no longer granted to anyone; this
    #: exists so the accounts still inside one finish the fortnight they were
    #: promised, and so old subscription rows still resolve to something. It
    #: can go once the last of them has run out.
    TRIAL = "trial"


#: Subscription rows written before the free plan existed. A lapsed account
#: used to be stamped with this tier rather than computed, so the string is in
#: the database and has to keep resolving.
_LEGACY_TIERS = {"expired": Tier.FREE}


@dataclass(frozen=True)
class Limits:
    max_course_units: int
    total_pdf_pages_pool: int
    max_single_file_size_mb: int
    max_single_file_pages: int
    daily_ai_queries: int
    #: The most this tier will ever answer, across every day it is held.
    #:
    #: Only Free sets one. A daily limit alone bounds the rate and not the
    #: bill: a free account that never converts costs five questions a day
    #: for as long as it exists, and the point of the free plan is to show
    #: someone the product, not to be the product. UNLIMITED everywhere else,
    #: where a paid month is what bounds it.
    lifetime_ai_queries: int
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
    lifetime_ai_queries=UNLIMITED,
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
    # Enough to see whether the thing works, not enough to revise on.
    #
    # It replaced a fortnight's trial, and the reason is worth writing down:
    # a trial is a thing worth stealing, so it needed an identity ledger, a
    # keyed hash of every phone number, and a rule about what a returning
    # student gets. A free tier that never ends has nothing to steal, and all
    # of that machinery stopped being necessary along with it.
    #
    # This is also where a lapsed subscription lands. Every read stays open on
    # any tier — a student who stops paying keeps their notes, their timetable
    # and their ability to export or delete. Locking someone out of work they
    # wrote is not a business model, it is hostage-taking.
    Tier.FREE: Plan(
        id=Tier.FREE,
        name="Free",
        price_ksh=0,
        # It does not run out. `get_entitlement` never expires this tier, and
        # zero here says so rather than meaning "already over".
        duration_days=0,
        seats=1,
        limits=Limits(
            # One unit is the point: enough to file a course and ask about it,
            # not enough to carry a semester. The cap only ever refuses a
            # *new* unit, so a student who paid, lapsed and has four keeps
            # seeing all four.
            max_course_units=1,
            total_pdf_pages_pool=100,
            max_single_file_size_mb=10,
            # The whole pool in one document, so a single 100-page lecture PDF
            # is uploadable rather than being refused for being one file.
            max_single_file_pages=100,
            daily_ai_queries=5,
            # About three weeks of using it properly, or one very long night.
            # Either way, enough to have found out whether it helps.
            lifetime_ai_queries=100,
            # One, ever. None at all would hide the feature that most obviously
            # justifies paying for the thing.
            quiz_count=1,
            quiz_interval="lifetime",
            quiz_max_questions=5,
            timetable_mode="manual",
            source_citations="basic",
            allow_ocr_scans=False,
            monthly_ocr_page_limit=0,
        ),
    ),
    # No longer granted. Left here so the accounts already inside a trial keep
    # the limits they were promised until it runs out, at which point they
    # land on Free like everyone else.
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
            # The fortnight is the ceiling on a trial. It ends on its own.
            lifetime_ai_queries=UNLIMITED,
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
            lifetime_ai_queries=UNLIMITED,
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
        # A typo, a tampered row, or the "expired" tier this used to write
        # before Free existed. All of them resolve to the floor: never to a
        # paid plan, and never to the trial.
        return PLANS[_LEGACY_TIERS.get(str(tier), Tier.FREE)]


#: The tiers a student can actually buy, in the order they are shown.
#:
#: Free is not among them. There is nothing to buy and no trial to start — a
#: new account simply has it, which is what removed the whole "has this number
#: had a trial" question from the product.
SELLABLE = (Tier.STANDARD, Tier.PRO, Tier.FRIENDS)


def limits_for(tier: str | Tier) -> Limits:
    return plan_for(tier).limits


def unit_cap(tier: str | Tier) -> int:
    limit = limits_for(tier).max_course_units
    return UNIT_HARD_CAP if limit == UNLIMITED else min(limit, UNIT_HARD_CAP)
