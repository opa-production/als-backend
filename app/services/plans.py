from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

#: Unlimited. Mirrors the same constant in the app's ``src/theme/plans.js``.
UNLIMITED = -1

#: How many units any student may file, on every tier including Free.
#:
#: Not a plan limit and not for sale. Units cost effectively nothing to hold —
#: what costs money is pages extracted and questions answered, and both are
#: metered on their own. Capping units by tier gated the cheap thing and did it
#: at the worst possible moment: a Kenyan student takes five or six units a
#: semester, so a two-unit ceiling refused them half-way through building their
#: own timetable, before the app had shown them anything worth paying for.
#:
#: Ten is a *quality* ceiling and applies to everybody for the same reason.
#: Retrieval is keyword search across one student's own corpus
#: (``app/services/ai/retrieval.py``); precision falls off as that corpus grows,
#: and an answer assembled from a fifty-unit haystack is worse than a refusal.
UNIT_HARD_CAP = 10


class Tier(StrEnum):
    #: The floor. Not sold, never expires, and what everything else falls back
    #: to — a new account, a lapsed one, an unrecognised tier.
    FREE = "free"
    STANDARD = "standard"
    PRO = "pro"
    FRIENDS = "friends"
    #: The same three plans bought four months at a time. Separate tiers rather
    #: than a flag, because a Kora charge has to say unambiguously what was
    #: bought — and because `duration_days` already carries the difference, so
    #: expiry, activation and the webhook need to know nothing new.
    STANDARD_SEASON = "standard_season"
    PRO_SEASON = "pro_season"
    FRIENDS_SEASON = "friends_season"
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
    #: Pages extracted a month, and the meter that now does the work the unit
    #: cap used to pretend to do. Pages are where a document actually costs
    #: something — extraction, storage, and every later retrieval over it — so
    #: this is the ceiling that bounds a filing habit, rather than a count of
    #: folders.
    #:
    #: Monthly on every paid tier. It was lifetime for all of them, which meant
    #: a student who paid for two years eventually could not upload anything
    #: and got no explanation, because a lifetime meter has no reset date to
    #: show. A paid period is what bounds a paid account; the pool refills with
    #: it.
    total_pdf_pages_pool: int
    #: The most this tier will ever extract, across every month it is held.
    #:
    #: Only Free sets one, for the same reason only Free sets
    #: ``lifetime_ai_queries``: a monthly ceiling bounds the rate and not the
    #: bill, and a free account that never converts must cost a finite amount
    #: in total. UNLIMITED everywhere else.
    lifetime_pdf_pages: int
    max_single_file_size_mb: int
    max_single_file_pages: int
    #: Questions a month, not a day.
    #:
    #: Revision is not spread evenly across a month — it happens the night
    #: before a CAT, in one long sitting. A daily ceiling refused a student at
    #: precisely the moment the app mattered to them, and it was never what
    #: bounded the bill: a month's allowance costs the same whether it is spent
    #: in one night or across thirty. The monthly total is the ceiling that
    #: does real work, so it is the only one left.
    monthly_ai_queries: int
    #: The most this tier will ever answer, across every month it is held.
    #:
    #: Only Free sets one. A monthly limit alone bounds the rate and not the
    #: bill: a free account that never converts costs thirty questions a month
    #: for as long as it exists, and the point of the free plan is to show
    #: someone the product, not to be the product. UNLIMITED everywhere else,
    #: where a paid period is what bounds it.
    lifetime_ai_queries: int
    quiz_count: int
    quiz_interval: str  # lifetime | monthly | unlimited
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

    #: Which card this plan appears on: "focus", "synapse", "friends". A plan
    #: and its Season are the same card with the toggle flipped, and this is
    #: what pairs them. Stated rather than parsed out of the id, because an id
    #: is a key and the day someone adds "pro_annual" a prefix rule quietly
    #: puts it on the wrong card.
    family: str = ""
    #: "monthly" | "season"
    billing_period: str = "monthly"

    @property
    def price_per_month_ksh(self) -> int:
        """
        What it works out at a month.

        Derived here rather than in the app so the figure under the price and
        the figure being charged cannot disagree — the same reason
        ``price_per_seat_ksh`` is derived.
        """
        months = max(1, round(self.duration_days / MONTH_DAYS))
        return self.price_ksh // months

    @property
    def price_per_seat_ksh(self) -> int:
        """
        What each person pays.

        Derived, never stored alongside the total: two numbers that must agree
        eventually stop agreeing, and the one on the pricing card is the one a
        student checks against their M-Pesa message.
        """
        return self.price_ksh // max(1, self.seats)


#: A month, for turning a plan's length into a per-month price. Thirty days:
#: the monthly plans are sold as 30, so a Season priced against anything else
#: would report a saving that is partly an artefact of the calendar.
MONTH_DAYS = 30

#: How long a Season runs. Four months, which is a semester in every calendar
#: that matters here without being called one.
SEASON_DAYS = 120


_FOCUS_LIMITS = Limits(
    total_pdf_pages_pool=400,
    lifetime_pdf_pages=UNLIMITED,
    max_single_file_size_mb=25,
    max_single_file_pages=100,
    monthly_ai_queries=400,
    lifetime_ai_queries=UNLIMITED,
    quiz_count=20,
    quiz_interval="monthly",
    quiz_max_questions=10,
    timetable_mode="alerts",
    source_citations="exact_page",
    allow_ocr_scans=False,
    monthly_ocr_page_limit=0,
)

_PRO_LIMITS = Limits(
    total_pdf_pages_pool=1500,
    lifetime_pdf_pages=UNLIMITED,
    max_single_file_size_mb=50,
    max_single_file_pages=300,
    monthly_ai_queries=1200,
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
        family="free",
        price_ksh=0,
        # It does not run out. `get_entitlement` never expires this tier, and
        # zero here says so rather than meaning "already over".
        duration_days=0,
        seats=1,
        limits=Limits(
            # Units are not rationed here any more — a free student files
            # their whole semester, sees their real week, and meets the limit
            # at the two things that actually cost us something: pages and
            # questions. See UNIT_HARD_CAP.
            #
            # A hundred pages, and they do not come back. Both pool figures
            # are the same number on Free, which is what makes it a lifetime
            # allowance wearing a monthly meter's clothes: the month can never
            # refill past the total.
            total_pdf_pages_pool=100,
            lifetime_pdf_pages=100,
            max_single_file_size_mb=10,
            # The whole pool in one document, so a single 100-page lecture PDF
            # is uploadable rather than being refused for being one file.
            max_single_file_pages=100,
            monthly_ai_queries=30,
            # About three weeks of using it properly, or one very long night.
            # Either way, enough to have found out whether it helps.
            #
            # This is the number that actually bounds what a free account can
            # cost. The monthly one only shapes how fast it is reached.
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
        family="free",
        price_ksh=0,
        duration_days=14,
        seats=1,
        limits=Limits(
            total_pdf_pages_pool=100,
            lifetime_pdf_pages=100,
            max_single_file_size_mb=10,
            max_single_file_pages=30,
            # The fortnight's worth, on a clock that now counts months. The
            # trial was fifteen questions a day for fourteen days, and 450 —
            # thirty days of it — would hand the last people inside one more
            # than they were ever promised.
            monthly_ai_queries=210,
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
        family="focus",
        price_ksh=150,
        duration_days=30,
        seats=1,
        limits=_FOCUS_LIMITS,
    ),
    Tier.PRO: Plan(
        id=Tier.PRO,
        name="Synapse",
        family="synapse",
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
        family="friends",
        # KES 208 each for six. Below Synapse's 350 per head, which is the
        # whole proposition, and above Focus so it never undercuts the plan a
        # single student would otherwise buy.
        #
        # Six rather than five: a study table is six, the price did not have to
        # move to get there, and "one seat left" is a much easier thing to say
        # to a sixth friend than "sorry, we are full".
        price_ksh=1250,
        duration_days=30,
        seats=6,
        limits=_PRO_LIMITS,
    ),
    # --- Seasons ---------------------------------------------------------
    #
    # Four months, because a student budgets the way their fees are billed and
    # not the way a SaaS bills. It is also cheaper to collect: every KES 150
    # charge pays a transaction fee with a fixed part in it, so one payment of
    # 500 keeps more of itself than four of 150.
    #
    # A Season buys *time*, not a bigger allowance. The limits are the monthly
    # plan's own object, so the refill is the same 400 or 1,200 a month, four
    # times over — and a number changed on Focus cannot drift away from Focus
    # Season.
    Tier.STANDARD_SEASON: Plan(
        id=Tier.STANDARD_SEASON,
        name="Focus Season",
        family="focus",
        billing_period="season",
        price_ksh=500,
        duration_days=SEASON_DAYS,
        seats=1,
        limits=_FOCUS_LIMITS,
    ),
    Tier.PRO_SEASON: Plan(
        id=Tier.PRO_SEASON,
        name="Synapse Season",
        family="synapse",
        billing_period="season",
        price_ksh=1100,
        duration_days=SEASON_DAYS,
        seats=1,
        limits=_PRO_LIMITS,
    ),
    Tier.FRIENDS_SEASON: Plan(
        id=Tier.FRIENDS_SEASON,
        name="Friends Season",
        family="friends",
        billing_period="season",
        price_ksh=4200,
        duration_days=SEASON_DAYS,
        seats=6,
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
SELLABLE = (
    Tier.STANDARD,
    Tier.PRO,
    Tier.FRIENDS,
    Tier.STANDARD_SEASON,
    Tier.PRO_SEASON,
    Tier.FRIENDS_SEASON,
)


def monthly_counterpart(plan: Plan) -> Plan | None:
    """
    The 30-day plan a Season is the long version of.

    Found by family rather than by a hand-written pairing table: a table is a
    second place to remember, and the one thing a new plan is guaranteed to
    have is a family.
    """
    if plan.billing_period == "monthly":
        return None

    return next(
        (
            other
            for other in PLANS.values()
            if other.family == plan.family and other.billing_period == "monthly"
        ),
        None,
    )


def saving_percent(plan: Plan) -> int:
    """
    How much cheaper a month is on this plan than on the monthly one.

    Zero for a monthly plan, which is what the badge on the toggle reads as
    "no badge". Computed from the two prices rather than written down, because
    a saving that is typed in is a saving that survives a price change and
    starts lying.

    Floored, never rounded. Focus Season saves 16.7%, and a badge promising 17%
    is a number nobody can reproduce from the two prices printed beside it —
    small, but it is the kind of small that a student notices and we cannot
    explain. Understating is free; overstating is a claim.
    """
    monthly = monthly_counterpart(plan)
    if monthly is None or monthly.price_ksh <= 0:
        return 0

    return int(100 * (1 - plan.price_per_month_ksh / monthly.price_ksh))


def limits_for(tier: str | Tier) -> Limits:
    return plan_for(tier).limits


def unit_cap() -> int:
    """
    How many units anyone may file. The same number on every tier.

    Kept as a function rather than letting callers reach for ``UNIT_HARD_CAP``
    directly, because it used to take a tier and the call sites read better
    asking a question than quoting a constant — and because if a future plan
    ever does buy more units, this is the one place that has to learn about it.
    """
    return UNIT_HARD_CAP
