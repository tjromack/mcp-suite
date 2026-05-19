"""Unit tests for get_trial_details: NCT validation + field extraction."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from clinical_trial_mcp.tools.get_trial_details import _CACHE, get_trial_details


@pytest.fixture(autouse=True)
def _clear_cache():
    """Isolate the process-local response cache between tests."""
    _CACHE.clear()
    yield
    _CACHE.clear()


_FAKE_STUDY = {
    "protocolSection": {
        "identificationModule": {
            "nctId": "NCT04280705",
            "briefTitle": "Adaptive COVID-19 Treatment Trial",
            "officialTitle": "A Multicenter, Adaptive, Randomized Trial",
        },
        "statusModule": {
            "overallStatus": "COMPLETED",
            "startDateStruct": {"date": "2020-02-21"},
        },
        "designModule": {"phases": ["PHASE3"]},
        "conditionsModule": {"conditions": ["COVID-19"]},
        "armsInterventionsModule": {"interventions": [{"name": "Remdesivir"}]},
        "descriptionModule": {"briefSummary": "A study of remdesivir."},
    }
}


@pytest.fixture
def _patch_fetch():
    with patch(
        "clinical_trial_mcp.tools.get_trial_details.fetch_study",
        new=AsyncMock(return_value=_FAKE_STUDY),
    ) as m:
        yield m


async def test_extracts_key_fields(_patch_fetch):
    result = await get_trial_details("NCT04280705")
    text = result[0].text
    assert "NCT04280705" in text
    assert "Adaptive COVID-19 Treatment Trial" in text
    assert "COMPLETED" in text
    assert "PHASE3" in text
    assert "COVID-19" in text
    assert "Remdesivir" in text
    assert "2020-02-21" in text


async def test_lowercase_id_normalized(_patch_fetch):
    result = await get_trial_details("nct04280705")
    assert "NCT04280705" in result[0].text
    _patch_fetch.assert_awaited_once_with("NCT04280705")


@pytest.mark.parametrize("bad", ["NCT123", "ABC04280705", "", "NCT0428070A"])
async def test_invalid_nct_id_rejected(bad):
    result = await get_trial_details(bad)
    assert "not a valid NCT ID" in result[0].text


async def test_not_found_returns_message():
    with patch(
        "clinical_trial_mcp.tools.get_trial_details.fetch_study",
        new=AsyncMock(return_value=None),
    ):
        result = await get_trial_details("NCT99999999")
    assert "No trial found" in result[0].text


async def test_network_error_returned_not_raised():
    with patch(
        "clinical_trial_mcp.tools.get_trial_details.fetch_study",
        new=AsyncMock(side_effect=RuntimeError("403 blocked")),
    ):
        result = await get_trial_details("NCT04280705")
    assert "Error running get_trial_details" in result[0].text


# --- response cache --------------------------------------------------------


async def test_cache_hit_skips_second_fetch(_patch_fetch):
    first = await get_trial_details("NCT04280705")
    second = await get_trial_details("NCT04280705")

    assert first[0].text == second[0].text
    _patch_fetch.assert_awaited_once()  # second call served from cache


async def test_404_is_not_cached():
    fetch = AsyncMock(side_effect=[None, _FAKE_STUDY])
    with patch("clinical_trial_mcp.tools.get_trial_details.fetch_study", new=fetch):
        miss = await get_trial_details("NCT04280705")
        hit = await get_trial_details("NCT04280705")

    assert "No trial found" in miss[0].text
    assert "Adaptive COVID-19 Treatment Trial" in hit[0].text  # re-fetched, not cached None
    assert fetch.await_count == 2


async def test_error_is_not_cached():
    fetch = AsyncMock(side_effect=[RuntimeError("transient"), _FAKE_STUDY])
    with patch("clinical_trial_mcp.tools.get_trial_details.fetch_study", new=fetch):
        err = await get_trial_details("NCT04280705")
        ok = await get_trial_details("NCT04280705")

    assert "Error running get_trial_details" in err[0].text
    assert "Adaptive COVID-19 Treatment Trial" in ok[0].text
    assert fetch.await_count == 2
