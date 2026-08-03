from __future__ import annotations

import inspect

import control_bot


def test_four_avito_results_are_available_but_market_is_insufficient():
    status = control_bot._classify_avito_status(4, 4, 4)

    assert status["provider_available"] is True
    assert status["provider_status"] == "provider_ok_with_results"
    assert status["market_analysis_enabled"] is False
    assert status["reason"] == "market_analysis_insufficient_data"

    source = inspect.getsource(control_bot.do_search_for_user)
    assert "Avito работает, но данных для надёжной оценки рынка пока недостаточно" in source
    insufficient_branch = source.split("elif _avito_available:", 1)[1].split("else:", 1)[0]
    assert "Авито недоступен" not in insufficient_branch


def test_avito_provider_error_is_distinct_from_empty_success():
    failed = control_bot._classify_avito_status(0, 0, 0, provider_error=True)
    empty = control_bot._classify_avito_status(0, 0, 0)
    assert failed["provider_status"] == "provider_error"
    assert empty["provider_status"] == "provider_ok_no_results"
