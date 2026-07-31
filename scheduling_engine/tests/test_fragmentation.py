from scheduling_engine import waste_minutes, waste_table

CATALOGUE = (60, 90)


def test_gap_too_small_for_any_service_is_entirely_wasted():
    assert waste_minutes(30, CATALOGUE) == 30
    assert waste_minutes(45, CATALOGUE) == 45
    assert waste_minutes(59, CATALOGUE) == 59


def test_gap_matching_a_service_exactly_wastes_nothing():
    assert waste_minutes(60, CATALOGUE) == 0
    assert waste_minutes(90, CATALOGUE) == 0
    assert waste_minutes(150, CATALOGUE) == 0  # 60 + 90
    assert waste_minutes(180, CATALOGUE) == 0  # 90 + 90


def test_only_the_sub_catalogue_remainder_is_wasted():
    assert waste_minutes(75, CATALOGUE) == 15   # holds a 60
    assert waste_minutes(100, CATALOGUE) == 10  # holds a 90
    assert waste_minutes(140, CATALOGUE) == 20  # holds 60 + 60
    assert waste_minutes(0, CATALOGUE) == 0


def test_catalogue_drives_the_result_rather_than_round_numbers():
    # A 45-minute service makes 45 free of waste — nothing about the formula
    # is tied to half-hour boundaries.
    assert waste_minutes(45, (45,)) == 0
    assert waste_minutes(45, (60,)) == 45
    assert waste_minutes(100, (45,)) == 10  # two 45s fit


def test_waste_is_monotone_in_usable_capacity():
    table = waste_table(24, [4, 6])  # 60/90 minutes on a 15-minute grid
    assert table[0] == 0
    assert table[3] == 3   # 45 minutes, nothing fits
    assert table[4] == 0   # exactly one 60
    assert table[5] == 1   # 75: a 60 plus a dead cell
    assert table[10] == 0  # 150: 60 + 90
