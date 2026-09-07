from linkedin_automation.analytics import calculate_engagement_rate


def test_engagement_rate_includes_all_collected_interactions():
    assert calculate_engagement_rate(1000, 40, 5, 3, 12) == 6.0


def test_engagement_rate_is_zero_without_impressions():
    assert calculate_engagement_rate(0, 40, 5, 3, 12) == 0.0
    assert calculate_engagement_rate(-1, 40, 5, 3, 12) == 0.0
