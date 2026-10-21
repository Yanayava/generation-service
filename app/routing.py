from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class LegChoice:
    id: UUID
    provider: str
    priority: int
    max_ref_images: int
    skip_on_moderation: bool


def select_leg(
    legs: list[LegChoice],
    *,
    ref_count: int,
    tried: set[UUID],
    previous_error: str | None,
) -> LegChoice | None:
    """Следующая нога, которую ещё не пробовали и которая принимает референсы.

    previous_error == "moderation" пропускает ноги с skip_on_moderation.
    Для первого выбора previous_error is None.
    """
    ordered = sorted(legs, key=lambda leg: (leg.priority, str(leg.id)))
    for leg in ordered:
        if leg.id in tried:
            continue
        if ref_count > leg.max_ref_images:
            continue
        if previous_error == "moderation" and leg.skip_on_moderation:
            continue
        return leg
    return None
