"""Unit tests of the execution-accuracy comparison used by the eval runner."""

from __future__ import annotations

from evals.compare import ResultTable, compare_results, normalize_value, values_equal


def table(columns: list[str], *rows: list[object]) -> ResultTable:
    return ResultTable(columns, [list(row) for row in rows])


def test_identical_results_match() -> None:
    expected = table(["a", "b"], [1, "x"], [2, "y"])
    assert compare_results(expected, expected).match


def test_column_order_and_names_are_ignored() -> None:
    expected = table(["Name", "Total"], ["Ava", 10.5], ["Bo", 3])
    actual = table(["total_amount", "customer"], [3, "Bo"], [10.5, "Ava"])
    assert compare_results(expected, actual).match


def test_extra_generated_columns_are_allowed_and_counted() -> None:
    expected = table(["Name"], ["Ava"], ["Bo"])
    actual = table(["ID", "Name", "City"], [1, "Ava", "Austin"], [2, "Bo", "Boston"])
    result = compare_results(expected, actual)
    assert result.match and result.extra_columns == 2


def test_missing_column_or_wrong_values_do_not_match() -> None:
    expected = table(["Name", "Total"], ["Ava", 10], ["Bo", 3])
    assert not compare_results(expected, table(["Name"], ["Ava"], ["Bo"])).match
    assert not compare_results(expected, table(["Name", "Total"], ["Ava", 10], ["Bo", 4])).match


def test_values_must_stay_together_in_rows() -> None:
    expected = table(["Name", "Total"], ["Ava", 10], ["Bo", 3])
    swapped = table(["Name", "Total"], ["Ava", 3], ["Bo", 10])
    assert not compare_results(expected, swapped).match


def test_row_count_must_match() -> None:
    result = compare_results(table(["a"], [1], [2]), table(["a"], [1]))
    assert not result.match and "row count" in result.reason


def test_rows_are_a_multiset_unless_ordered() -> None:
    expected = table(["a"], [1], [2], [2])
    shuffled = table(["a"], [2], [1], [2])
    assert compare_results(expected, shuffled).match
    ordered = compare_results(expected, shuffled, ordered=True)
    assert not ordered.match and "order" in ordered.reason
    assert not compare_results(expected, table(["a"], [1], [1], [2])).match


def test_numbers_are_compared_at_two_decimals_with_tolerance() -> None:
    assert compare_results(table(["v"], [79055.5555]), table(["v"], [79055.56])).match
    assert compare_results(table(["v"], [216491.554]), table(["v"], [216491.5])).match
    assert not compare_results(table(["v"], [5.75]), table(["v"], [0.0575])).match
    assert not compare_results(table(["n"], [45]), table(["n"], [46])).match


def test_numeric_strings_equal_numbers() -> None:
    assert compare_results(table(["year", "n"], ["2025", 3]), table(["y", "count"], [2025, 3.0])).match
    assert normalize_value(" 2025 ") == 2025.0 and normalize_value("2025-01-01") == "2025-01-01"


def test_a_full_name_column_can_stand_for_first_and_last_name() -> None:
    expected = table(["FirstName", "LastName", "Total"], ["Ava", "Smith", 5], ["Mary Ann", "Lee", 2])
    actual = table(["EmployeeName", "Total"], ["Ava Smith", 5], ["Mary Ann Lee", 2])
    assert compare_results(expected, actual).match


def test_nulls_compare_equal_only_to_nulls() -> None:
    assert compare_results(table(["a"], [None], [1]), table(["b"], [1], [None])).match
    assert not compare_results(table(["a"], [None]), table(["a"], [0])).match
    assert not values_equal(None, 0.0)


def test_mismatch_reason_shows_a_missing_row() -> None:
    result = compare_results(table(["a", "b"], [1, "x"], [2, "y"]), table(["a", "b"], [1, "y"], [2, "x"]))
    assert not result.match and "missing" in result.reason
