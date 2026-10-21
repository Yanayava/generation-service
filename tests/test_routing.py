from uuid import uuid4

from app.routing import LegChoice, select_leg


def leg(provider: str, priority: int, max_ref_images: int, skip: bool = False) -> LegChoice:
    return LegChoice(
        id=uuid4(),
        provider=provider,
        priority=priority,
        max_ref_images=max_ref_images,
        skip_on_moderation=skip,
    )


def test_image_fast_prefers_alpha_then_beta():
    alpha = leg("alpha", 0, 4)
    beta = leg("beta", 1, 10)
    legs = [beta, alpha]

    assert select_leg(legs, ref_count=0, tried=set(), previous_error=None) == alpha
    assert select_leg(legs, ref_count=4, tried=set(), previous_error=None) == alpha
    assert select_leg(legs, ref_count=5, tried=set(), previous_error=None) == beta
    assert select_leg(legs, ref_count=11, tried=set(), previous_error=None) is None
    assert select_leg(legs, ref_count=0, tried={alpha.id}, previous_error="technical") == beta


def test_video_long_skips_alpha_only_after_moderation():
    beta = leg("beta", 0, 2)
    alpha = leg("alpha", 1, 1, skip=True)
    legs = [alpha, beta]

    assert select_leg(legs, ref_count=1, tried=set(), previous_error=None) == beta
    assert select_leg(legs, ref_count=1, tried={beta.id}, previous_error="technical") == alpha
    assert select_leg(legs, ref_count=1, tried={beta.id}, previous_error="moderation") is None
    assert select_leg(legs, ref_count=2, tried={beta.id}, previous_error="technical") is None
    assert select_leg(legs, ref_count=3, tried=set(), previous_error=None) is None
