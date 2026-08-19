from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from memory_agent.models import (
    DraftUnit,
    KnowledgeDraft,
    ReviewCard,
    ReviewEvent,
    Source,
)


_RATING_WEAKNESS = {1: 1.0, 2: 0.65, 3: 0.15, 4: 0.0}
_RECENT_REVIEW_WEIGHTS = (5, 4, 3, 2, 1)
_LEARNING_STATES = {1, 3}


@dataclass(frozen=True, slots=True)
class _RatedReview:
    card_id: UUID
    rating: int
    reviewed_at: datetime
    lapsed: bool


@dataclass(frozen=True, slots=True)
class _CardBundle:
    card: ReviewCard
    unit: DraftUnit
    draft: KnowledgeDraft
    source: Source


def get_review_insights(
    session: Session,
    *,
    user_id: UUID,
    trend_days: int = 30,
    forecast_days: int = 14,
    weak_limit: int = 10,
    timezone: str = "Asia/Shanghai",
    daily_limit: int = 10,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return explainable review analytics without changing scheduling state.

    Weakness and trends are derived from final ``review_rated`` events.  Workload
    dates are a presentation-only recommendation: FSRS ``due_at`` and
    ``scheduler_state`` are never changed here.
    """

    _require_range("trend_days", trend_days, minimum=1, maximum=365)
    _require_range("forecast_days", forecast_days, minimum=1, maximum=90)
    _require_range("weak_limit", weak_limit, minimum=0, maximum=100)
    _require_range("daily_limit", daily_limit, minimum=1, maximum=100)
    tz = _load_timezone(timezone)
    current = _as_utc(now or datetime.now(UTC))

    with session.no_autoflush:
        bundles = _load_active_card_bundles(session, user_id=user_id)
        rated_reviews = _load_rated_reviews(session, user_id=user_id)

    reviews_by_card = _reviews_by_card(rated_reviews)
    weakness_by_card = _weakness_by_card(bundles, reviews_by_card, current=current)
    ranked_weakness = sorted(
        weakness_by_card.values(),
        key=lambda item: (-item["weakness_score"], -item["reviewed_count"], str(item["card_id"])),
    )
    weak_cards = ranked_weakness[:weak_limit]
    trend = _build_trend(rated_reviews, days=trend_days, tz=tz, current=current)
    workload = _build_workload(
        bundles,
        rated_reviews,
        weakness_by_card,
        days=forecast_days,
        daily_limit=daily_limit,
        tz=tz,
        current=current,
    )
    due_count = sum(_as_utc(bundle.card.due_at) <= current for bundle in bundles)
    unreviewed_count = sum(bundle.card.id not in weakness_by_card for bundle in bundles)

    return {
        "generated_at": current,
        "timezone": timezone,
        "summary": {
            "active_card_count": len(bundles),
            "due_count": due_count,
            "reviewed_card_count": len(weakness_by_card),
            "unreviewed_count": unreviewed_count,
            "completed_in_period": trend["summary"]["completed_count"],
            "self_rated_mastery_rate": trend["summary"]["self_rated_mastery_rate"],
        },
        "weak_cards": weak_cards,
        "weak_tags": _aggregate_weak_tags(ranked_weakness)[:weak_limit],
        "trend": trend,
        "workload": workload,
        "methodology": {
            "weakness_scale": "0-100",
            "recent_rating_mapping": {"1": 1.0, "2": 0.65, "3": 0.15, "4": 0.0},
            "component_weights": {"recent_ratings": 0.6, "lapse_rate": 0.2, "fsrs_difficulty": 0.2},
            "unreviewed_cards_are_ranked": False,
            "overdue_affects_weakness": False,
            "workload_changes_fsrs_due_at": False,
        },
    }


def get_daily_plan(
    session: Session,
    *,
    user_id: UUID,
    daily_limit: int,
    timezone: str = "Asia/Shanghai",
    overdue_enabled: bool = True,
    include_overflow: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a soft-cap queue while preserving the canonical FSRS schedule."""

    _require_range("daily_limit", daily_limit, minimum=1, maximum=100)
    tz = _load_timezone(timezone)
    current = _as_utc(now or datetime.now(UTC))
    local_today = current.astimezone(tz).date()

    with session.no_autoflush:
        bundles = _load_active_card_bundles(session, user_id=user_id)
        rated_reviews = _load_rated_reviews(session, user_id=user_id)

    reviews_by_card = _reviews_by_card(rated_reviews)
    weakness_by_card = _weakness_by_card(bundles, reviews_by_card, current=current)
    completed_today = sum(
        review.reviewed_at.astimezone(tz).date() == local_today for review in rated_reviews
    )
    due = [bundle for bundle in bundles if _as_utc(bundle.card.due_at) <= current]
    overdue = [
        bundle
        for bundle in due
        if _as_utc(bundle.card.due_at).astimezone(tz).date() < local_today
    ]
    eligible = [
        bundle
        for bundle in due
        if overdue_enabled
        or _as_utc(bundle.card.due_at).astimezone(tz).date() == local_today
    ]
    excluded_overdue = [bundle for bundle in overdue if bundle not in eligible]
    must_do = sorted(
        (bundle for bundle in eligible if _is_learning(bundle.card)),
        key=lambda bundle: (_as_utc(bundle.card.due_at), str(bundle.card.id)),
    )
    ordinary = sorted(
        (bundle for bundle in eligible if not _is_learning(bundle.card)),
        key=lambda bundle: _plan_sort_key(bundle, weakness_by_card),
    )
    remaining_capacity = max(0, daily_limit - completed_today)
    ordinary_slots = max(0, remaining_capacity - len(must_do))
    planned = [*must_do, *ordinary[:ordinary_slots]]
    overflow = [*ordinary[ordinary_slots:], *excluded_overdue]
    overflow.sort(key=lambda bundle: _plan_sort_key(bundle, weakness_by_card))
    plan_payload = [
        _plan_card_payload(bundle, weakness_by_card, current=current, plan_status="planned")
        for bundle in planned
    ]
    overflow_payload = (
        [
            _plan_card_payload(bundle, weakness_by_card, current=current, plan_status="overflow")
            for bundle in overflow
        ]
        if include_overflow
        else []
    )
    soft_limit_exceeded = completed_today + len(planned) > daily_limit
    if not due:
        balance_status = "clear"
    elif overflow or soft_limit_exceeded:
        balance_status = "overloaded"
    else:
        balance_status = "within_limit"

    return {
        "date": local_today.isoformat(),
        "generated_at": current,
        "timezone": timezone,
        "daily_limit": daily_limit,
        "completed_today": completed_today,
        "remaining_capacity": remaining_capacity,
        "due_now_count": len(due),
        "overdue_backlog_count": len(overdue),
        "must_do_count": len(must_do),
        "planned_count": len(planned),
        "overflow_count": len(due) - len(planned),
        "soft_limit_exceeded": soft_limit_exceeded,
        "balance_status": balance_status,
        "overdue_enabled": overdue_enabled,
        "planned_cards": plan_payload,
        "overflow_cards": overflow_payload,
        "fsrs_schedule_changed": False,
    }


def _load_active_card_bundles(session: Session, *, user_id: UUID) -> list[_CardBundle]:
    rows = session.execute(
        select(ReviewCard, DraftUnit, KnowledgeDraft, Source)
        .join(DraftUnit, ReviewCard.draft_unit_id == DraftUnit.id)
        .join(KnowledgeDraft, DraftUnit.draft_id == KnowledgeDraft.id)
        .join(Source, KnowledgeDraft.source_id == Source.id)
        .where(
            ReviewCard.user_id == user_id,
            ReviewCard.status == "active",
            DraftUnit.status == "active",
            KnowledgeDraft.status == "confirmed",
        )
        .order_by(ReviewCard.due_at, ReviewCard.id)
    ).all()
    return [_CardBundle(card, unit, draft, source) for card, unit, draft, source in rows]


def _load_rated_reviews(session: Session, *, user_id: UUID) -> list[_RatedReview]:
    events = session.scalars(
        select(ReviewEvent)
        .where(ReviewEvent.user_id == user_id, ReviewEvent.event_type == "review_rated")
        .order_by(ReviewEvent.created_at, ReviewEvent.id)
    ).all()
    reviews: list[_RatedReview] = []
    for event in events:
        payload = event.payload_json or {}
        try:
            rating = int(payload.get("rating"))
        except (TypeError, ValueError):
            continue
        if rating not in _RATING_WEAKNESS:
            continue
        reviewed_at = _payload_datetime(payload.get("reviewed_at"), fallback=event.created_at)
        before = payload.get("scheduler_card_before") or {}
        before_state = _integer_or_none(before.get("state")) if isinstance(before, dict) else None
        reviews.append(
            _RatedReview(
                card_id=event.card_id,
                rating=rating,
                reviewed_at=reviewed_at,
                lapsed=rating == 1 and before_state == 2,
            )
        )
    return reviews


def _reviews_by_card(reviews: list[_RatedReview]) -> dict[UUID, list[_RatedReview]]:
    grouped: dict[UUID, list[_RatedReview]] = defaultdict(list)
    for review in reviews:
        grouped[review.card_id].append(review)
    for card_reviews in grouped.values():
        card_reviews.sort(key=lambda item: item.reviewed_at, reverse=True)
    return grouped


def _weakness_by_card(
    bundles: list[_CardBundle],
    reviews_by_card: dict[UUID, list[_RatedReview]],
    *,
    current: datetime,
) -> dict[UUID, dict[str, Any]]:
    weakness: dict[UUID, dict[str, Any]] = {}
    for bundle in bundles:
        reviews = reviews_by_card.get(bundle.card.id, [])
        if not reviews:
            continue
        recent = reviews[: len(_RECENT_REVIEW_WEIGHTS)]
        weights = _RECENT_REVIEW_WEIGHTS[: len(recent)]
        recent_component = sum(
            _RATING_WEAKNESS[item.rating] * weight
            for item, weight in zip(recent, weights, strict=True)
        ) / sum(weights)
        review_count = max(int(bundle.card.review_count or 0), len(reviews))
        lapse_component = min(1.0, max(0.0, float(bundle.card.lapse_count or 0) / review_count))
        difficulty = _fsrs_difficulty(bundle.card)
        components: list[tuple[str, float, float]] = [
            ("recent_ratings", recent_component, 0.6),
            ("lapse_rate", lapse_component, 0.2),
        ]
        if difficulty is not None:
            components.append(("fsrs_difficulty", (difficulty - 1.0) / 9.0, 0.2))
        total_weight = sum(weight for _name, _value, weight in components)
        raw_score = sum(value * weight for _name, value, weight in components) / total_weight
        struggle_count = sum(item.rating <= 2 for item in recent)
        reasons = [f"最近 {len(recent)} 次复习中有 {struggle_count} 次评为 1 或 2"]
        reasons.append(f"累计遗忘 {int(bundle.card.lapse_count or 0)} / {review_count} 次")
        if difficulty is not None:
            reasons.append(f"FSRS 难度 {difficulty:.1f} / 10")
        due_at = _as_utc(bundle.card.due_at)
        weakness[bundle.card.id] = {
            "card_id": bundle.card.id,
            "draft_unit_id": bundle.unit.id,
            "title": bundle.unit.title,
            "question": bundle.unit.question,
            "source_title": bundle.source.title,
            "tags": list(bundle.unit.tags or []),
            "weakness_score": round(raw_score * 100, 1),
            "confidence": _weakness_confidence(len(reviews)),
            "reviewed_count": len(reviews),
            "recent_struggle_count": struggle_count,
            "lapse_count": int(bundle.card.lapse_count or 0),
            "fsrs_difficulty": round(difficulty, 3) if difficulty is not None else None,
            "components": {
                name: round(value * 100, 1) for name, value, _weight in components
            },
            "reasons": reasons,
            "due_at": due_at,
            "is_due": due_at <= current,
            "overdue_days": round(max(0.0, (current - due_at).total_seconds() / 86_400), 1),
        }
    return weakness


def _aggregate_weak_tags(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for card in cards:
        for raw_tag in card["tags"]:
            tag = str(raw_tag).strip()
            if tag:
                grouped[tag].append(card)
    result = []
    for tag, members in grouped.items():
        total_reviews = sum(item["reviewed_count"] for item in members)
        result.append(
            {
                "tag": tag,
                "weakness_score": round(
                    sum(item["weakness_score"] for item in members) / len(members), 1
                ),
                "sample_cards": len(members),
                "reviewed_count": total_reviews,
                "confidence": _tag_confidence(len(members), total_reviews),
            }
        )
    return sorted(result, key=lambda item: (-item["weakness_score"], item["tag"]))


def _build_trend(
    reviews: list[_RatedReview], *, days: int, tz: ZoneInfo, current: datetime
) -> dict[str, Any]:
    local_today = current.astimezone(tz).date()
    start_date = local_today - timedelta(days=days - 1)
    points: dict[date, dict[str, Any]] = {
        start_date + timedelta(days=offset): {
            "date": (start_date + timedelta(days=offset)).isoformat(),
            "completed_count": 0,
            "mastered_count": 0,
            "struggle_count": 0,
            "lapse_count": 0,
            "rating_counts": {"1": 0, "2": 0, "3": 0, "4": 0},
            "_cards": set(),
        }
        for offset in range(days)
    }
    for review in reviews:
        local_date = review.reviewed_at.astimezone(tz).date()
        point = points.get(local_date)
        if point is None:
            continue
        point["completed_count"] += 1
        point["rating_counts"][str(review.rating)] += 1
        point["mastered_count"] += int(review.rating >= 3)
        point["struggle_count"] += int(review.rating <= 2)
        point["lapse_count"] += int(review.lapsed)
        point["_cards"].add(review.card_id)

    daily: list[dict[str, Any]] = []
    all_cards: set[UUID] = set()
    for local_date in sorted(points):
        point = points[local_date]
        completed = point["completed_count"]
        cards = point.pop("_cards")
        all_cards.update(cards)
        point["unique_cards"] = len(cards)
        point["self_rated_mastery_rate"] = (
            round(point["mastered_count"] / completed, 4) if completed else 0.0
        )
        daily.append(point)

    completed_count = sum(item["completed_count"] for item in daily)
    mastered_count = sum(item["mastered_count"] for item in daily)
    struggle_count = sum(item["struggle_count"] for item in daily)
    active_days = sum(item["completed_count"] > 0 for item in daily)
    comparison_days = min(7, days)
    recent_completed = sum(item["completed_count"] for item in daily[-comparison_days:])
    previous_slice_start = max(0, len(daily) - 2 * comparison_days)
    previous_completed = sum(
        item["completed_count"] for item in daily[previous_slice_start:-comparison_days]
    )
    return {
        "days": days,
        "summary": {
            "completed_count": completed_count,
            "mastered_count": mastered_count,
            "struggle_count": struggle_count,
            "lapse_count": sum(item["lapse_count"] for item in daily),
            "unique_cards": len(all_cards),
            "active_days": active_days,
            "current_streak": _current_streak(daily),
            "average_reviews_per_day": round(completed_count / days, 2),
            "self_rated_mastery_rate": (
                round(mastered_count / completed_count, 4) if completed_count else 0.0
            ),
            "recent_period_days": comparison_days,
            "recent_completed": recent_completed,
            "previous_completed": previous_completed,
        },
        "daily": daily,
    }


def _build_workload(
    bundles: list[_CardBundle],
    reviews: list[_RatedReview],
    weakness_by_card: dict[UUID, dict[str, Any]],
    *,
    days: int,
    daily_limit: int,
    tz: ZoneInfo,
    current: datetime,
) -> dict[str, Any]:
    local_today = current.astimezone(tz).date()
    dates = [local_today + timedelta(days=offset) for offset in range(days)]
    completed_today = sum(
        review.reviewed_at.astimezone(tz).date() == local_today for review in reviews
    )
    capacity = {day: daily_limit for day in dates}
    capacity[local_today] = max(0, daily_limit - completed_today)
    canonical = {day: 0 for day in dates}
    recommended = {day: 0 for day in dates}
    mandatory: list[tuple[date, _CardBundle]] = []
    ordinary: list[tuple[date, _CardBundle]] = []
    backlog_count = 0
    due_now_count = 0

    for bundle in bundles:
        due_at = _as_utc(bundle.card.due_at)
        due_day = due_at.astimezone(tz).date()
        due_now_count += int(due_at <= current)
        backlog_count += int(due_at <= current and due_day < local_today)
        if due_day in canonical:
            canonical[due_day] += 1
        start_day = max(local_today, due_day)
        if start_day > dates[-1]:
            continue
        if _is_learning(bundle.card):
            mandatory.append((start_day, bundle))
        else:
            ordinary.append((start_day, bundle))

    for planned_day, _bundle in mandatory:
        recommended[planned_day] += 1
    remaining = {
        day: max(0, capacity[day] - recommended[day])
        for day in dates
    }
    ordinary.sort(
        key=lambda item: (
            item[0],
            *_plan_sort_key(item[1], weakness_by_card),
        )
    )
    carry_over_count = 0
    for earliest_day, bundle in ordinary:
        allocated = False
        for candidate in dates:
            if candidate < earliest_day or remaining[candidate] <= 0:
                continue
            recommended[candidate] += 1
            remaining[candidate] -= 1
            allocated = True
            break
        if not allocated:
            carry_over_count += 1

    day_payloads = [
        {
            "date": day.isoformat(),
            "capacity": capacity[day],
            "canonical_due_count": canonical[day],
            "recommended_count": recommended[day],
            "canonical_overload_count": max(0, canonical[day] - capacity[day]),
            "recommended_overload_count": max(0, recommended[day] - capacity[day]),
        }
        for day in dates
    ]
    return {
        "days": days,
        "daily_limit": daily_limit,
        "completed_today": completed_today,
        "remaining_capacity_today": capacity[local_today],
        "due_now_count": due_now_count,
        "backlog_count": backlog_count,
        "carry_over_count": carry_over_count,
        "daily": day_payloads,
        "fsrs_schedule_changed": False,
    }


def _plan_card_payload(
    bundle: _CardBundle,
    weakness_by_card: dict[UUID, dict[str, Any]],
    *,
    current: datetime,
    plan_status: str,
) -> dict[str, Any]:
    weak = weakness_by_card.get(bundle.card.id)
    due_at = _as_utc(bundle.card.due_at)
    return {
        "id": bundle.card.id,
        "draft_unit_id": bundle.unit.id,
        "title": bundle.unit.title,
        "question": bundle.unit.question,
        "source_title": bundle.source.title,
        "tags": list(bundle.unit.tags or []),
        "due_at": due_at,
        "review_count": int(bundle.card.review_count or 0),
        "lapse_count": int(bundle.card.lapse_count or 0),
        "scheduler_version": bundle.card.scheduler_version,
        "scheduler_state_name": _scheduler_state_name(bundle.card),
        "weakness_score": weak["weakness_score"] if weak else None,
        "weakness_confidence": weak["confidence"] if weak else "unreviewed",
        "weakness_reasons": list(weak["reasons"]) if weak else [],
        "overdue_days": round(max(0.0, (current - due_at).total_seconds() / 86_400), 1),
        "plan_status": plan_status,
    }


def _plan_sort_key(
    bundle: _CardBundle, weakness_by_card: dict[UUID, dict[str, Any]]
) -> tuple[datetime, float, str]:
    weak_score = weakness_by_card.get(bundle.card.id, {}).get("weakness_score")
    return (
        _as_utc(bundle.card.due_at),
        -float(weak_score if weak_score is not None else -1.0),
        str(bundle.card.id),
    )


def _fsrs_difficulty(card: ReviewCard) -> float | None:
    state = card.scheduler_state or {}
    if not isinstance(state, dict):
        return None
    fsrs_card = state.get("card")
    raw = fsrs_card.get("difficulty") if isinstance(fsrs_card, dict) else state.get("difficulty")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return min(10.0, max(1.0, value))


def _scheduler_state(card: ReviewCard) -> int | None:
    state = card.scheduler_state or {}
    if not isinstance(state, dict):
        return None
    fsrs_card = state.get("card")
    raw = fsrs_card.get("state") if isinstance(fsrs_card, dict) else state.get("state")
    return _integer_or_none(raw)


def _scheduler_state_name(card: ReviewCard) -> str:
    return {1: "learning", 2: "review", 3: "relearning"}.get(
        _scheduler_state(card), "unknown"
    )


def _is_learning(card: ReviewCard) -> bool:
    return _scheduler_state(card) in _LEARNING_STATES


def _weakness_confidence(review_count: int) -> str:
    if review_count <= 2:
        return "low"
    if review_count <= 4:
        return "medium"
    return "high"


def _tag_confidence(card_count: int, review_count: int) -> str:
    if card_count < 2 or review_count < 5:
        return "low"
    if card_count < 4 or review_count < 12:
        return "medium"
    return "high"


def _current_streak(daily: list[dict[str, Any]]) -> int:
    streak = 0
    for point in reversed(daily):
        if point["completed_count"] <= 0:
            break
        streak += 1
    return streak


def _payload_datetime(value: Any, *, fallback: datetime) -> datetime:
    if isinstance(value, datetime):
        return _as_utc(value)
    if isinstance(value, str):
        try:
            return _as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            pass
    return _as_utc(fallback)


def _integer_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _load_timezone(value: str) -> ZoneInfo:
    try:
        return ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown timezone: {value}") from exc


def _require_range(name: str, value: int, *, minimum: int, maximum: int) -> None:
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
