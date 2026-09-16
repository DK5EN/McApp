# This suite intentionally exercises WeatherService's private pure-logic helpers
# (_fuse_weather_data, _validate_data_age, _calculate_humidity_from_dewpoint,
# _calculate_cloud_coverage_description, _wind_direction_to_compass) directly —
# they have no public wrapper and testing them is this file's entire purpose.
# A single file-level suppression here stands in for the SLF001 exemption that
# pyproject.toml already grants to **/tests.py and **/test_*.py; this filename
# just doesn't match either glob.
"""Built-in test suite for the pure, network-free logic in meteo.py.

Covers weather-formatting computations that never touch the network:
data-source fusion precedence, data-age validation, dewpoint→humidity (Magnus
formula), cloud-cover percent→okta conversion, 16-point compass bucketing, and
the LoRa message formatter (including its length-based fallback format).

Also covers the Wave 3 (2026-07-30) cold-start-seed fix: the unified
`is_valid_position` "no position" sentinel (None AND 0/0), that a 0/0 or
missing position defers weather without ever reaching the network,
`update_location`'s cache-invalidation contract, and `_make_request`'s
retry policy (permanent 4xx fails fast, transient 408/429 and 5xx/timeout
still retry, `Retry-After` honoured but clamped) — offline via a stubbed
`httpx.get`/`time.sleep`, never a real request.

Only tz handling (_messzeitpunkt_to_utc / _is_daytime) and the negative-cache
behaviour of get_weather_data() are covered elsewhere, in
commands/tests.py::test_meteo_timezone_validators/test_meteo_negative_cache.
"""

import contextlib
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from .meteo import (
    _MAX_LORA_MSG_LEN,
    RETRY_AFTER_MAX_S,
    RETRY_DELAY_S,
    WEATHER_CACHE_TTL_S,
    WEATHER_ERROR_CACHE_TTL_S,
    WeatherService,
    WeatherServiceError,
    _retry_delay_s,
    is_valid_position,
)

results: list[tuple[str, str]] = []


def _check(label: str, actual: object, expected: object) -> None:
    ok = actual == expected
    status = "✅ PASS" if ok else "❌ FAIL"
    results.append((status, label))
    print(f"{status} | {label}")
    if not ok:
        print(f"     actual={actual!r} expected={expected!r}")


def _test_fuse_weather_data() -> None:
    ws = WeatherService(lat=48.15, lon=11.58, stat_name="FuseTest")

    dwd_data: dict[str, Any] = {
        "temperatur_celsius": 10.0,
        "luftdruck_hpa": 1000.0,
        "windgeschwindigkeit_kmh": None,
        "windrichtung_grad": 180,
        "windboeen_kmh": None,
        "wolkenbedeckung_prozent": 50,
        "sichtweite_meter": None,
        "niederschlag_mm": 0.0,
        "luftfeuchtigkeit_prozent": 60,
    }
    openmeteo_data: dict[str, Any] = {
        "temperatur_celsius": 99.0,  # not in the supplement list -> must be ignored
        "windgeschwindigkeit_kmh": 20.0,  # DWD None -> supplemented
        "windrichtung_grad": 270,  # DWD has a value -> DWD wins
        "windboeen_kmh": 5.0,  # DWD None -> supplemented
        "wolkenbedeckung_prozent": 80,  # DWD has a value -> DWD wins
        "sichtweite_meter": 1000,  # DWD None -> supplemented
        "niederschlag_mm": None,  # DWD has a value (0.0, not None) -> DWD wins
        "luftfeuchtigkeit_prozent": None,  # DWD has a value -> DWD wins
    }

    fused = ws._fuse_weather_data(dwd_data, openmeteo_data)

    _check(
        "fuse: field outside supplement list keeps DWD value even if OpenMeteo differs",
        fused["temperatur_celsius"],
        10.0,
    )
    _check(
        "fuse: DWD None + OpenMeteo value -> supplemented",
        fused["windgeschwindigkeit_kmh"],
        20.0,
    )
    _check(
        "fuse: DWD value + OpenMeteo value -> DWD kept (wind direction)",
        fused["windrichtung_grad"],
        180,
    )
    _check(
        "fuse: DWD None + OpenMeteo value -> supplemented (gusts)",
        fused["windboeen_kmh"],
        5.0,
    )
    _check(
        "fuse: DWD value + OpenMeteo value -> DWD kept (clouds)",
        fused["wolkenbedeckung_prozent"],
        50,
    )
    _check(
        "fuse: DWD None + OpenMeteo value -> supplemented (visibility)",
        fused["sichtweite_meter"],
        1000,
    )
    _check(
        "fuse: DWD 0.0 (not None) + OpenMeteo None -> DWD's 0.0 kept, not overwritten",
        fused["niederschlag_mm"],
        0.0,
    )
    _check(
        "fuse: DWD value + OpenMeteo None -> DWD kept (humidity)",
        fused["luftfeuchtigkeit_prozent"],
        60,
    )
    _check(
        "fuse: supplemented_parameters lists exactly the OpenMeteo-filled fields, in order",
        fused["supplemented_parameters"],
        ["Wind-Geschwindigkeit", "Windböen", "Sichtweite"],
    )
    _check(
        "fuse: data_source names the supplemented fields",
        fused["data_source"],
        "DWD_BrightSky + OpenMeteo (Wind-Geschwindigkeit, Windböen, Sichtweite)",
    )
    _check(
        "fuse: all 5 quality-critical params present -> top quality label",
        fused["data_quality"],
        "Exzellent (alle Parameter)",
    )

    # Fully-DWD-complete case: nothing to supplement.
    complete_dwd: dict[str, Any] = {
        "windgeschwindigkeit_kmh": 5.0,
        "windrichtung_grad": 90,
        "windboeen_kmh": 8.0,
        "wolkenbedeckung_prozent": 20,
        "sichtweite_meter": 20000,
        "niederschlag_mm": 0.0,
        "luftfeuchtigkeit_prozent": 70,
    }
    fused_complete = ws._fuse_weather_data(complete_dwd, openmeteo_data)
    _check(
        "fuse: DWD fully populated -> nothing supplemented",
        fused_complete["supplemented_parameters"],
        [],
    )
    _check(
        "fuse: DWD fully populated -> data_source says complete",
        fused_complete["data_source"],
        "DWD_BrightSky (vollständig)",
    )


def _test_validate_data_age() -> None:
    ws = WeatherService(lat=48.15, lon=11.58, stat_name="AgeTest", max_age_minutes=30)
    now = datetime.now(UTC)

    fresh = (now - timedelta(minutes=5)).isoformat()
    fresh_result = ws._validate_data_age({"messzeitpunkt": fresh})
    _check("validate_data_age: 5 min old (< 30 min max) is valid", fresh_result["valid"], True)

    stale = (now - timedelta(minutes=60)).isoformat()
    stale_result = ws._validate_data_age({"messzeitpunkt": stale})
    _check("validate_data_age: 60 min old (> 30 min max) is invalid", stale_result["valid"], False)
    _check(
        "validate_data_age: stale reason mentions the max-age threshold",
        "> 30 Min" in stale_result["reason"],
        True,
    )

    future = (now + timedelta(minutes=10)).isoformat()
    future_result = ws._validate_data_age({"messzeitpunkt": future})
    _check("validate_data_age: 10 min in the future is invalid", future_result["valid"], False)
    _check(
        "validate_data_age: future reason names it a forecast",
        "Forecast" in future_result["reason"],
        True,
    )

    missing_result = ws._validate_data_age({})
    _check("validate_data_age: missing messzeitpunkt is invalid", missing_result["valid"], False)
    _check(
        "validate_data_age: missing messzeitpunkt age is +inf",
        missing_result["age_minutes"],
        float("inf"),
    )

    sentinel_result = ws._validate_data_age({"messzeitpunkt": "unbekannt"})
    _check(
        "validate_data_age: 'unbekannt' sentinel is invalid like missing",
        sentinel_result["valid"],
        False,
    )


def _test_calculate_humidity_from_dewpoint() -> None:
    ws = WeatherService(lat=48.15, lon=11.58, stat_name="HumidityTest")

    # Magnus formula, temp=20.0C dewpoint=10.0C -> raw RH ~= 52.5658...%, rounds to 53.
    rh = ws._calculate_humidity_from_dewpoint(20.0, 10.0)
    _check("humidity_from_dewpoint: 20.0C/10.0C dewpoint rounds to 53%", rh, 53)

    # Saturated: dewpoint == temperature -> exactly 100%.
    rh_saturated = ws._calculate_humidity_from_dewpoint(15.0, 15.0)
    _check("humidity_from_dewpoint: dewpoint == temperature -> 100%", rh_saturated, 100)

    # Dewpoint above temperature is unphysical but the formula still clamps to <= 100.
    rh_clamped = ws._calculate_humidity_from_dewpoint(5.0, 20.0)
    _check("humidity_from_dewpoint: dewpoint > temperature is clamped to 100%", rh_clamped, 100)


def _test_okta_conversion() -> None:
    ws = WeatherService(lat=48.15, lon=11.58, stat_name="OktaTest")
    day_ts = "2026-07-15T12:00:00+00:00"
    night_ts = "2026-07-15T00:00:00+00:00"

    _check(
        "okta: None cloud cover -> unbekannt",
        ws._calculate_cloud_coverage_description(None, day_ts),
        "unbekannt",
    )
    _check(
        "okta: 0% at day -> sonnig (0/8, banker's rounding of 0.5 -> 0)",
        ws._calculate_cloud_coverage_description(0, day_ts),
        "sonnig",
    )
    _check(
        "okta: 0% at night -> klar",
        ws._calculate_cloud_coverage_description(0, night_ts),
        "klar",
    )
    # NOTE: the 5 calls below pass genuinely fractional percentages
    # (6.25/12.5/37.5/87.5) because the okta boundary math
    # (`cloud_percent / _PERCENT_PER_OKTA`, _PERCENT_PER_OKTA == 12.5) only
    # lands exactly on a rounding boundary — the case these tests exist to
    # cover — for non-integer inputs; no int can be 12.5*n + 6.25 for integer
    # n, so the banker's-rounding boundary (6.25 -> exactly 0.5) is only
    # reachable with a float. `_calculate_cloud_coverage_description`'s
    # parameter is therefore typed `cloud_percent: float | None` (the
    # algorithm was always float-based; production call sites still feed it
    # `_safe_int(...)` output, which int-widens cleanly to float).
    _check(
        "okta: 6.25% (round(0.5)==0 banker's rounding) still rounds to 0/8 -> sonnig",
        ws._calculate_cloud_coverage_description(6.25, day_ts),
        "sonnig",
    )
    _check(
        "okta: 12.5% -> 1/8 boundary, day phrasing",
        ws._calculate_cloud_coverage_description(12.5, day_ts),
        "1/8 (heiter)",
    )
    _check(
        "okta: 12.5% -> 1/8 boundary, night phrasing",
        ws._calculate_cloud_coverage_description(12.5, night_ts),
        "1/8 (überwiegend klar)",
    )
    _check(
        "okta: 37.5% -> 3/8 (aufgelockert bewölkt)",
        ws._calculate_cloud_coverage_description(37.5, day_ts),
        "3/8 (aufgelockert bewölkt)",
    )
    _check(
        "okta: 75% -> 6/8 (teilweise bewölkt), upper boundary of that bucket",
        ws._calculate_cloud_coverage_description(75, day_ts),
        "6/8 (teilweise bewölkt)",
    )
    _check(
        "okta: 87.5% -> 7/8 rounds past the 'teilweise bewölkt' bucket -> bewölkt",
        ws._calculate_cloud_coverage_description(87.5, day_ts),
        "bewölkt",
    )
    _check(
        "okta: 100% -> 8/8 -> bewölkt",
        ws._calculate_cloud_coverage_description(100, day_ts),
        "bewölkt",
    )


def _test_compass_conversion() -> None:
    ws = WeatherService(lat=48.15, lon=11.58, stat_name="CompassTest")

    _check("compass: None -> empty string", ws._wind_direction_to_compass(None), "")
    _check("compass: 0deg -> N", ws._wind_direction_to_compass(0), "N")
    _check("compass: 11deg -> NNE", ws._wind_direction_to_compass(11), "NNE")
    _check(
        "compass: 22deg -> NNE (still within the NNE bucket)",
        ws._wind_direction_to_compass(22),
        "NNE",
    )
    _check(
        "compass: 23deg -> NE (first degree of the NE bucket)",
        ws._wind_direction_to_compass(23),
        "NE",
    )
    _check("compass: 45deg -> NE", ws._wind_direction_to_compass(45), "NE")
    _check("compass: 203deg -> SW", ws._wind_direction_to_compass(203), "SW")
    _check(
        "compass: 225deg -> SW (last degree of the SW bucket)",
        ws._wind_direction_to_compass(225),
        "SW",
    )
    _check(
        "compass: 226deg -> WSW (first degree of the WSW bucket)",
        ws._wind_direction_to_compass(226),
        "WSW",
    )
    _check("compass: 337deg -> NNW", ws._wind_direction_to_compass(337), "NNW")
    _check(
        "compass: 338deg -> N (wraps into the N bucket early)",
        ws._wind_direction_to_compass(338),
        "N",
    )
    _check("compass: 359deg -> N", ws._wind_direction_to_compass(359), "N")
    _check("compass: 360deg normalizes to 0 -> N", ws._wind_direction_to_compass(360), "N")
    _check("compass: 370deg normalizes to 10 -> NNE", ws._wind_direction_to_compass(370), "NNE")
    _check("compass: -10deg normalizes to 350 -> N", ws._wind_direction_to_compass(-10), "N")


def _test_format_for_lora() -> None:
    ws = WeatherService(lat=48.15, lon=11.58, stat_name="TestStation")

    # Normal case, mirrors the canned payload used by commands/tests.py's !WX test.
    canned: dict[str, Any] = {
        "temperatur_celsius": 21.5,
        "luftfeuchtigkeit_prozent": 55,
        "luftdruck_hpa": 1013.2,
        "windgeschwindigkeit_kmh": 0,  # below the calm threshold -> "windstill"
        "timestamp": "test",
    }
    normal = ws.format_for_lora(canned)
    _check(
        "format_for_lora: normal case matches production formatting exactly",
        normal,
        "🌤️ WX TestStation: 21.5C 55% rF, 1013.2hPa, windstill, unbekannt",
    )

    # Error passthrough, truncated to _ERROR_PREVIEW_LEN (25) chars.
    err_msg = "Alle Wetter-APIs nicht verfügbar wegen Netzwerkfehler und Timeout etc etc"
    err_out = ws.format_for_lora({"error": err_msg})
    _check(
        "format_for_lora: error case previews the error message to 25 chars",
        err_out,
        "WX ERR: Alle Wetter-APIs nicht ve",
    )

    # Rich payload whose emoji-format rendering exceeds _MAX_LORA_MSG_LEN chars,
    # forcing the shorter, emoji-free fallback format. Chosen so the *fallback* itself
    # still fits within the cap.
    rich_ws = WeatherService(
        lat=48.15, lon=11.58, stat_name="LONGSTATION123456", max_age_minutes=30
    )
    rich_data: dict[str, Any] = {
        "temperatur_celsius": -12.34,
        "luftfeuchtigkeit_prozent": 87,
        "luftdruck_hpa": 987.65,
        "windgeschwindigkeit_kmh": 45.6,
        "windrichtung_grad": 225,
        "wolkenbedeckung_prozent": 90,
        "niederschlag_mm": 12.3,
        "messzeitpunkt": "2026-07-15T12:00:00+00:00",
    }
    prefix_text = "Hallo Freunde vom Ham Radio Club heute" + "X" * 23
    truncated = rich_ws.format_for_lora(rich_data, prefix_text=prefix_text)

    _check(
        "format_for_lora: rich payload triggers the >149-char fallback format",
        truncated,
        "Hallo Freunde vom Ham Radio Club heuteXXXXXXXXXXXXXXXXXXXXXXX "
        "WX LONGSTATION123456: -12.3C 87%rF 987.6hPa Wind 45.6km/h SW bewölkt, 12.3mm rain",
    )
    _check(
        "format_for_lora: fallback format has no emoji (production checks len(), not bytes)",
        "\U0001f324" in truncated,
        False,
    )
    _check(
        "format_for_lora: fallback format fits within the _MAX_LORA_MSG_LEN char cap",
        len(truncated) <= _MAX_LORA_MSG_LEN,
        True,
    )
    _check(
        "format_for_lora: fallback is still well-formed (station, temp, humidity, "
        "pressure, wind, clouds, rain all present)",
        all(
            token in truncated
            for token in (
                "LONGSTATION123456",
                "-12.3C",
                "87%rF",
                "987.6hPa",
                "Wind 45.6km/h SW",
                "bewölkt",
                "12.3mm rain",
            )
        ),
        True,
    )


def _test_is_valid_position() -> None:
    _check("is_valid_position: None/None is invalid", is_valid_position(None, None), False)
    _check("is_valid_position: lat None, lon set is invalid", is_valid_position(None, 11.75), False)
    _check("is_valid_position: lat set, lon None is invalid", is_valid_position(48.4, None), False)
    _check(
        "is_valid_position: 0/0 (no-fix GPS sentinel) is invalid",
        is_valid_position(0, 0),
        False,
    )
    _check(
        "is_valid_position: 0.0/0.0 (float sentinel) is invalid",
        is_valid_position(0.0, 0.0),
        False,
    )
    _check(
        "is_valid_position: a real position is valid",
        is_valid_position(48.4031, 11.7497),
        True,
    )
    _check(
        "is_valid_position: lat==0 alone (equator) is valid — only the (0,0) PAIR is the sentinel",
        is_valid_position(0, 11.75),
        True,
    )
    _check(
        "is_valid_position: lon==0 alone (prime meridian) is valid",
        is_valid_position(48.4, 0),
        True,
    )


def _test_no_position_defers_without_network() -> None:
    """0/0 and None must both defer (return the 'no GPS' error) and never
    reach the network. Verified by stubbing `_get_brightsky_weather` /
    `_get_openmeteo_weather` to raise if called at all — the early-return
    guard in `_fetch_weather_data` must trip before either is invoked.
    """
    for lat, lon, label in ((0, 0, "0/0"), (None, None, "None/None"), (0.0, 0.0, "0.0/0.0")):
        ws = WeatherService(lat=lat, lon=lon, stat_name="NoPosTest")

        def _unreachable() -> dict[str, Any]:
            raise AssertionError("network fetch must not be attempted without a valid position")

        ws._get_brightsky_weather = _unreachable  # type: ignore[method-assign]  # offline test double
        ws._get_openmeteo_weather = _unreachable  # type: ignore[method-assign]  # offline test double

        result = ws.get_weather_data()
        _check(
            f"no-position ({label}): get_weather_data returns an error, not weather",
            "error" in result,
            True,
        )
        _check(
            f"no-position ({label}): the error names the missing GPS, not an API failure",
            "GPS" in result.get("error", ""),
            True,
        )


def _test_update_location_same_place_keeps_cache() -> None:
    """F2 follow-up: the node re-announces its own GPS every beacon cycle and
    `_cache_gps` forwards each one. An UNCHANGED location must not bump the
    generation, or every cycle throws the good cache away and the next poll
    pays a cold upstream fetch (the 806 ms `/api/weather` row of 2026-09-16
    09:37). A move beyond LOCATION_EPSILON_DEG still invalidates.
    """
    ws = WeatherService(lat=48.4078, lon=11.738, stat_name="SameTest")
    fetch_count = 0

    def fake_fetch() -> dict[str, Any]:
        nonlocal fetch_count
        fetch_count += 1
        return {"temperatur_celsius": 20.0, "timestamp": "test"}

    ws._fetch_weather_data = fake_fetch  # type: ignore[method-assign] # offline cache double
    first = ws.get_weather_data()
    gen = ws._cache_generation
    ws.update_location(48.4078, 11.738)
    ws.update_location(48.40781, 11.73801)  # 1e-5 deg, ~1 m of GPS jitter
    _check("same-place update_location: generation unchanged", ws._cache_generation, gen)
    _check(
        "same-place update_location: cache still served",
        ws.get_weather_data() is first and fetch_count == 1,
        True,
    )
    ws.update_location(48.4078, 11.738, "SameTest")  # same name, no bump
    _check("same-name update_location: generation unchanged", ws._cache_generation, gen)
    ws.update_location(48.41, 11.738)
    _check("real move: generation bumped", ws._cache_generation, gen + 1)
    ws.update_location(48.41, 11.738, "Renamed")
    _check("rename alone: generation bumped", ws._cache_generation, gen + 2)


def _test_update_location_bumps_generation_and_invalidates_cache() -> None:
    """REGRESSION guard for the incident meteo.py's update_location docstring
    documents: a stale cache must not keep serving weather for the OLD
    location after a GPS update. update_location() is deliberately
    lock-free (bumps `_cache_generation`), so this proves the counter
    actually invalidates a populated cache rather than just changing.
    """
    ws = WeatherService(lat=48.15, lon=11.58, stat_name="GenTest")
    fetch_count = 0
    seen_positions: list[tuple[float | None, float | None]] = []

    def fake_fetch() -> dict[str, Any]:
        nonlocal fetch_count
        fetch_count += 1
        seen_positions.append((ws.lat, ws.lon))
        return {"temperatur_celsius": 20.0, "timestamp": "test"}

    ws._fetch_weather_data = fake_fetch  # type: ignore[method-assign] # offline cache-invalidation double

    first = ws.get_weather_data()
    _check("update_location: initial fetch populates the cache", fetch_count == 1, True)
    second = ws.get_weather_data()
    _check(
        "update_location: a second call before any location change is served from cache",
        second is first and fetch_count == 1,
        True,
    )

    generation_before = ws._cache_generation
    ws.update_location(48.40, 11.75, "MovedTest")
    _check(
        "update_location: bumps _cache_generation",
        ws._cache_generation == generation_before + 1,
        True,
    )
    _check("update_location: updates lat/lon", (ws.lat, ws.lon), (48.40, 11.75))
    _check("update_location: updates stat_name", ws.stat_name, "MovedTest")

    third = ws.get_weather_data()
    _check(
        "update_location: the next fetch is NOT served from the stale cache "
        "(a fresh fetch runs for the new location)",
        third is not first and fetch_count == 2,
        True,
    )
    _check(
        "update_location: the fresh fetch actually ran against the NEW coordinates",
        seen_positions[-1] == (48.40, 11.75),
        True,
    )


def _fake_status_response(
    status_code: int, headers: dict[str, str] | None = None
) -> httpx.Response:
    """A real httpx.Response (not a hand-rolled duck-type) whose
    raise_for_status() raises a real httpx.HTTPStatusError with
    `.response.status_code` set — so `_make_request`'s except clauses see
    exactly what production sees, built entirely offline.
    """
    request = httpx.Request("GET", "https://example.invalid/weather")
    return httpx.Response(status_code=status_code, headers=headers, request=request)


def _count_requests_for_status(status_code: int, headers: dict[str, str] | None = None) -> int:
    """Drive `_make_request` against a stubbed `httpx.get` that always returns
    `status_code`, and report how many HTTP attempts it made. `time.sleep` is
    stubbed to a no-op so a retried status costs no wall-clock time here.

    Fully offline: `httpx.get` is replaced for the duration, so no code path —
    including BrightSky's two-endpoint fallback, which is not exercised here
    because `_make_request` is called directly — can reach the network.
    """
    ws = WeatherService(lat=48.15, lon=11.58, stat_name="RetryTest")
    original_get = httpx.get
    original_sleep = time.sleep
    time.sleep = lambda *_args, **_kwargs: None  # no real delay in tests
    call_count = 0

    def fake_get(*_args: Any, **_kwargs: Any) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return _fake_status_response(status_code, headers)

    httpx.get = fake_get  # offline HTTP double
    try:
        with contextlib.suppress(WeatherServiceError):
            ws._make_request("https://example.invalid/weather", {"lat": 1, "lon": 2})
    finally:
        httpx.get = original_get  # restore
        time.sleep = original_sleep  # restore
    return call_count


def _test_make_request_4xx_fails_fast() -> None:
    """Wave 3 fix: a PERMANENT 4xx status fails fast — the request itself is
    wrong for this endpoint/params, so retrying an unchanged request cannot
    succeed. Exactly ONE request, no retry sleep.
    """
    ws = WeatherService(lat=48.15, lon=11.58, stat_name="RetryTest")
    original_get = httpx.get
    call_count = 0

    def fake_get(*_args: Any, **_kwargs: Any) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return _fake_status_response(404)

    httpx.get = fake_get  # offline HTTP double
    try:
        raised = False
        try:
            ws._make_request("https://example.invalid/weather", {"lat": 1, "lon": 2})
        except WeatherServiceError:
            raised = True
        _check("_make_request: a 404 raises (no silent success)", raised, True)
        _check(
            "_make_request: a 404 makes exactly ONE request (fails fast, no retry)",
            call_count,
            1,
        )
    finally:
        httpx.get = original_get  # restore

    # ADVISOR REGRESSION (Wave 3 defect 2): the fast-fail must NOT cover the
    # whole 4xx band. 408 and 429 mean "not right now", not "not ever", and
    # both upstreams here (api.brightsky.dev, api.open-meteo.com) are free
    # public APIs that rate-limit. A 429 that is never retried is simply no
    # weather. Guarded per code, so a future edit that widens the fast-fail
    # back to `400 <= code < 500` fails on the exact codes that matter.
    attempts = WeatherService(lat=0.0, lon=0.0, stat_name="x").max_retries + 1
    for retryable in (408, 429):
        _check(
            f"_make_request: a {retryable} IS retried — it is transient, not a permanent 4xx",
            _count_requests_for_status(retryable),
            attempts,
        )
    for permanent in (400, 403, 404, 422):
        _check(
            f"_make_request: a {permanent} still fails fast (exactly one request)",
            _count_requests_for_status(permanent),
            1,
        )


def _test_retry_after_is_honoured_but_bounded() -> None:
    """A `Retry-After` on a retryable status is honoured — but clamped to
    RETRY_AFTER_MAX_S, because `_make_request` runs under `_cache_lock` with
    every queued weather caller parked behind it. A hostile or careless
    header must not be able to hold that lock for an unbounded time.

    `_retry_delay_s` is exercised directly (pure, no I/O), plus one end-to-end
    check that `_make_request` actually feeds the header through to sleep.
    """
    _check(
        "_retry_delay_s: no Retry-After header falls back to RETRY_DELAY_S",
        _retry_delay_s(_fake_status_response(429)),
        float(RETRY_DELAY_S),
    )
    _check(
        "_retry_delay_s: a server asking for LESS than our floor still waits RETRY_DELAY_S",
        _retry_delay_s(_fake_status_response(429, {"Retry-After": "0.2"})),
        float(RETRY_DELAY_S),
    )
    _check(
        "_retry_delay_s: a modest Retry-After is honoured verbatim",
        _retry_delay_s(_fake_status_response(429, {"Retry-After": "3"})),
        3.0,
    )
    _check(
        "_retry_delay_s: a long Retry-After is CLAMPED to RETRY_AFTER_MAX_S, never obeyed",
        _retry_delay_s(_fake_status_response(429, {"Retry-After": "3600"})),
        RETRY_AFTER_MAX_S,
    )
    for junk in ("Wed, 21 Oct 2015 07:28:00 GMT", "soon", "", "-5", "nan", "inf"):
        _check(
            f"_retry_delay_s: unusable Retry-After {junk!r} falls back to RETRY_DELAY_S",
            _retry_delay_s(_fake_status_response(429, {"Retry-After": junk})),
            float(RETRY_DELAY_S),
        )

    slept: list[float] = []
    original_sleep = time.sleep
    original_get = httpx.get

    def record_sleep(seconds: Any) -> None:
        slept.append(float(seconds))

    time.sleep = record_sleep  # record instead of blocking

    def fake_get(*_args: Any, **_kwargs: Any) -> httpx.Response:
        return _fake_status_response(429, {"Retry-After": "900"})

    httpx.get = fake_get  # offline HTTP double
    try:
        with contextlib.suppress(WeatherServiceError):
            ws = WeatherService(lat=48.15, lon=11.58, stat_name="RetryTest")
            ws._make_request("https://example.invalid/weather", {"lat": 1, "lon": 2})
    finally:
        httpx.get = original_get  # restore
        time.sleep = original_sleep  # restore
    _check(
        "_make_request: a 429 asking for a 15-minute backoff never sleeps longer than the cap",
        bool(slept) and max(slept) <= RETRY_AFTER_MAX_S,
        True,
    )


def _test_make_request_5xx_retries() -> None:
    """A persistent 5xx IS retried, up to max_retries + 1 attempts, unlike a
    4xx. `time.sleep` is stubbed to a no-op so this doesn't actually block
    the suite for RETRY_DELAY_S per retried attempt.
    """
    ws = WeatherService(lat=48.15, lon=11.58, stat_name="RetryTest")
    original_get = httpx.get
    original_sleep = time.sleep
    time.sleep = lambda *_args, **_kwargs: None  # no real delay in tests
    call_count = 0

    def fake_get(*_args: Any, **_kwargs: Any) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return _fake_status_response(500)

    httpx.get = fake_get  # offline HTTP double
    try:
        raised = False
        try:
            ws._make_request("https://example.invalid/weather", {"lat": 1, "lon": 2})
        except WeatherServiceError:
            raised = True
        _check("_make_request: a persistent 500 eventually raises", raised, True)
        _check(
            "_make_request: a 500 IS retried (max_retries + 1 attempts, unlike a 4xx)",
            call_count,
            ws.max_retries + 1,
        )
    finally:
        httpx.get = original_get  # restore
        time.sleep = original_sleep  # restore


def _test_make_request_timeout_retries() -> None:
    """A persistent timeout is retried the same way as a 5xx — the 4xx
    fast-fail change must not affect any other exception path.
    """
    ws = WeatherService(lat=48.15, lon=11.58, stat_name="RetryTest")
    original_get = httpx.get
    original_sleep = time.sleep
    time.sleep = lambda *_args, **_kwargs: None  # no real delay in tests
    call_count = 0

    def fake_get(*_args: Any, **_kwargs: Any) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        raise httpx.TimeoutException("simulated timeout")

    httpx.get = fake_get  # offline HTTP double
    try:
        raised = False
        try:
            ws._make_request("https://example.invalid/weather", {"lat": 1, "lon": 2})
        except WeatherServiceError:
            raised = True
        _check("_make_request: a persistent timeout eventually raises", raised, True)
        _check(
            "_make_request: a timeout IS retried (max_retries + 1 attempts)",
            call_count,
            ws.max_retries + 1,
        )
    finally:
        httpx.get = original_get  # restore
        time.sleep = original_sleep  # restore


def _test_make_request_recovers_on_retry() -> None:
    """A 5xx that recovers on the second attempt proves retrying actually
    helps recover a transient failure, not just that it's attempted.
    """
    ws = WeatherService(lat=48.15, lon=11.58, stat_name="RetryTest")
    original_get = httpx.get
    original_sleep = time.sleep
    time.sleep = lambda *_args, **_kwargs: None  # no real delay in tests
    call_count = 0

    def fake_get(*_args: Any, **_kwargs: Any) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _fake_status_response(500)
        return httpx.Response(
            status_code=200,
            json={"weather": {}},
            request=httpx.Request("GET", "https://example.invalid/weather"),
        )

    httpx.get = fake_get  # offline HTTP double
    try:
        response = ws._make_request("https://example.invalid/weather", {"lat": 1, "lon": 2})
        _check(
            "_make_request: a 500 that recovers on retry returns the successful response",
            response.status_code,
            200,
        )
        _check("_make_request: exactly 2 attempts were made (1 failure + 1 success)", call_count, 2)
    finally:
        httpx.get = original_get  # restore
        time.sleep = original_sleep  # restore


def _fake_weather_get_response(
    url: str, call_log: list[str], *, dwd_temp: float = 20.0
) -> httpx.Response:
    """A real, fully-populated httpx.Response for either upstream (BrightSky's
    `current_weather` or Open-Meteo's `/forecast`), routed by URL, so a
    stubbed `httpx.get` can drive `_fetch_weather_data()` end-to-end (fusion
    included) without ever reaching the network. Appends to `call_log` so
    tests can count upstream hits precisely.
    """
    call_log.append(url)
    now_iso = datetime.now(UTC).isoformat()
    if "brightsky" in url:
        payload: dict[str, Any] = {
            "weather": {
                "temperature": dwd_temp,
                "dew_point": 10.0,
                "relative_humidity": 55,
                "pressure_msl": 1013.0,
                "wind_speed": 10.0,
                "wind_direction": 180,
                "cloud_cover": 50,
                "visibility": 10000,
                "precipitation": 0.0,
                "condition": "dry",
                "timestamp": now_iso,
            },
            "sources": [{"station_name": "TestStation"}],
        }
    else:
        payload = {
            "current": {
                "temperature_2m": dwd_temp,
                "relative_humidity_2m": 55,
                "pressure_msl": 1013.0,
                "wind_speed_10m": 10.0,
                "wind_direction_10m": 180,
                "wind_gusts_10m": 5.0,
                "cloud_cover": 50,
                "visibility": 10000,
                "precipitation": 0.0,
                "time": now_iso,
            }
        }
    return httpx.Response(status_code=200, json=payload, request=httpx.Request("GET", url))


def _wait_until(predicate: Any, *, timeout_s: float = 5.0) -> bool:
    """Poll `predicate()` until it is true or `timeout_s` elapses. Used to wait
    for a background refresh thread to finish without a fixed sleep count.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def _test_weather_cache_warm_hits_upstream_once() -> None:
    """F2: a warm cache serves the fully assembled result verbatim — the
    second call must not touch the network at all.
    """
    ws = WeatherService(lat=48.15, lon=11.58, stat_name="CacheTest")
    call_log: list[str] = []
    original_get = httpx.get
    httpx.get = lambda url, **_kw: _fake_weather_get_response(url, call_log)  # offline HTTP double
    try:
        first = ws.get_weather_data()
        calls_after_first = len(call_log)
        second = ws.get_weather_data()
    finally:
        httpx.get = original_get  # restore

    _check("warm cache: first call fetches without error", "error" in first, False)
    _check(
        "warm cache: first call hits upstream exactly twice (BrightSky + OpenMeteo)",
        calls_after_first,
        2,
    )
    _check("warm cache: second call hits upstream ZERO more times", len(call_log), 2)
    _check(
        "warm cache: second call returns the identical cached object (no recompute)",
        second is first,
        True,
    )


def _test_weather_stale_while_revalidate() -> None:
    """F2: once the TTL expires, a caller gets the OLD data immediately and
    exactly one background thread refreshes the cache; an overlapping caller
    during that refresh gets the same stale data, never a second fetch.
    """
    ws = WeatherService(lat=48.15, lon=11.58, stat_name="SWRTest")
    call_log: list[str] = []
    original_get = httpx.get

    # 1. Populate a good, warm cache (dwd_temp=20.0).
    httpx.get = lambda url, **_kw: _fake_weather_get_response(url, call_log, dwd_temp=20.0)
    try:
        first = ws.get_weather_data()
    finally:
        httpx.get = original_get
    _check("SWR setup: initial fetch succeeds", "error" in first, False)
    calls_before_refresh = len(call_log)

    # 2. Age the cache past WEATHER_CACHE_TTL_S, and install a gated stub
    #    (dwd_temp=99.0) that blocks mid-fetch until the test releases it —
    #    proving the refresh runs off-thread rather than blocking the caller.
    ws._cache_time -= WEATHER_CACHE_TTL_S + 1
    started_event = threading.Event()
    release_event = threading.Event()

    def gated_get(*_args: Any, **_kwargs: Any) -> httpx.Response:
        url = str(_args[0]) if _args else str(_kwargs["url"])
        response = _fake_weather_get_response(url, call_log, dwd_temp=99.0)
        started_event.set()
        release_event.wait(timeout=5)
        return response

    httpx.get = gated_get  # offline HTTP double
    try:
        stale = ws.get_weather_data()
        _check(
            "SWR: a stale call returns the OLD data immediately (no block on refresh)",
            stale.get("temperatur_celsius"),
            20.0,
        )
        _check(
            "SWR: a stale call marks a refresh in flight",
            ws._refresh_in_flight,
            True,
        )

        # Wait for the background thread to actually issue its first upstream
        # call (and block on it) before checking overlap — otherwise the
        # overlapping-call assertions below would race the thread scheduler.
        _check(
            "SWR: the background refresh actually starts fetching",
            _wait_until(started_event.is_set, timeout_s=2),
            True,
        )

        # 3. Overlap a second stale call while the refresh is still gated: it
        #    must get the same stale data and must NOT start a second fetch.
        overlapping = ws.get_weather_data()
        _check(
            "SWR: an overlapping stale call also gets the OLD data",
            overlapping.get("temperatur_celsius"),
            20.0,
        )
        _check(
            "SWR: an overlapping stale call triggers no additional upstream hit yet",
            len(call_log),
            calls_before_refresh + 1,  # only the gated fetch's first (BrightSky) call so far
        )

        # 4. Let the background refresh finish and wait for it (poll, no sleep-by-count).
        release_event.set()
        finished = _wait_until(lambda: not ws._refresh_in_flight)
        _check("SWR: the background refresh completes", finished, True)
    finally:
        httpx.get = original_get  # restore

    _check(
        "SWR: exactly one background fetch ran (2 upstream calls, not 4)",
        len(call_log),
        calls_before_refresh + 2,
    )

    refreshed = ws.get_weather_data()
    _check(
        "SWR: the following call returns the refreshed payload",
        refreshed.get("temperatur_celsius"),
        99.0,
    )


def _test_weather_failed_refresh_keeps_good_data() -> None:
    """F2: a background refresh that fails must not clobber good stale data
    with an error payload — the last good result keeps being served, and the
    error TTL throttles the next retry attempt instead of hammering on every
    call.
    """
    ws = WeatherService(lat=48.15, lon=11.58, stat_name="FailRefreshTest")
    call_log: list[str] = []
    original_get = httpx.get

    httpx.get = lambda url, **_kw: _fake_weather_get_response(url, call_log, dwd_temp=20.0)
    try:
        good = ws.get_weather_data()
    finally:
        httpx.get = original_get
    _check("failed-refresh setup: initial fetch succeeds", "error" in good, False)
    calls_before_refresh = len(call_log)

    ws._cache_time -= WEATHER_CACHE_TTL_S + 1

    def failing_get(*_args: Any, **_kwargs: Any) -> httpx.Response:
        raise httpx.ConnectError("simulated upstream outage")

    original_sleep = time.sleep
    time.sleep = lambda *_a, **_kw: None  # no real retry delay in this test
    httpx.get = failing_get  # offline HTTP double
    try:
        stale = ws.get_weather_data()
        _check(
            "failed-refresh: the stale call still returns the last good data",
            stale.get("temperatur_celsius"),
            20.0,
        )
        finished = _wait_until(lambda: not ws._refresh_in_flight)
        _check("failed-refresh: the background refresh attempt completes", finished, True)
    finally:
        httpx.get = original_get  # restore
        time.sleep = original_sleep  # restore

    _check(
        "failed-refresh: the served cache is still the good data, not an error",
        "error" in ws._cache if ws._cache is not None else True,
        False,
    )

    # A second call, immediately after, must NOT spawn another refresh attempt
    # (error-TTL throttling) — no further upstream hits.
    httpx.get = lambda url, **_kw: _fake_weather_get_response(url, call_log, dwd_temp=42.0)
    try:
        again = ws.get_weather_data()
    finally:
        httpx.get = original_get
    _check(
        "failed-refresh: a call right after the failure keeps serving the good data",
        again.get("temperatur_celsius"),
        20.0,
    )
    _check(
        "failed-refresh: no additional upstream hit before WEATHER_ERROR_CACHE_TTL_S elapses",
        len(call_log),
        calls_before_refresh,
    )

    # After the error TTL, a retry IS attempted and succeeds.
    ws._refresh_retry_after -= WEATHER_ERROR_CACHE_TTL_S + 1
    httpx.get = lambda url, **_kw: _fake_weather_get_response(url, call_log, dwd_temp=42.0)
    try:
        ws.get_weather_data()
        finished = _wait_until(lambda: not ws._refresh_in_flight)
    finally:
        httpx.get = original_get
    _check("failed-refresh: the retry after the error TTL completes", finished, True)
    recovered = ws.get_weather_data()
    _check(
        "failed-refresh: the retried fetch eventually replaces the stale data",
        recovered.get("temperatur_celsius"),
        42.0,
    )


def _test_weather_cache_location_change_fetches_anew() -> None:
    """F2: `update_location()` must invalidate the new (finished-result)
    cache exactly like it did the old raw-fetch cache — the next call after
    a location change is a fresh synchronous fetch, not stale-while-revalidate.
    """
    ws = WeatherService(lat=48.15, lon=11.58, stat_name="MoveTest")
    call_log: list[str] = []
    original_get = httpx.get
    httpx.get = lambda url, **_kw: _fake_weather_get_response(url, call_log, dwd_temp=20.0)
    try:
        first = ws.get_weather_data()
        calls_before_move = len(call_log)

        ws.update_location(48.40, 11.75, "MovedTest")
        second = ws.get_weather_data()
    finally:
        httpx.get = original_get

    _check("location change: initial fetch succeeds", "error" in first, False)
    _check(
        "location change: the next call fetches anew (2 more upstream hits)",
        len(call_log) - calls_before_move,
        2,
    )
    _check(
        "location change: the new fetch is a distinct result, not the stale one",
        second is not first,
        True,
    )


def run_meteo_tests() -> bool:
    """Run all pure-logic meteo tests. Returns True iff every case passed."""
    results.clear()  # allow repeated invocations within one process (e.g. re-runs)

    print("Testing meteo.py pure logic:")
    print("=" * 50)

    _test_fuse_weather_data()
    _test_validate_data_age()
    _test_calculate_humidity_from_dewpoint()
    _test_okta_conversion()
    _test_compass_conversion()
    _test_format_for_lora()
    _test_is_valid_position()
    _test_no_position_defers_without_network()
    _test_update_location_bumps_generation_and_invalidates_cache()
    _test_update_location_same_place_keeps_cache()
    _test_make_request_4xx_fails_fast()
    _test_retry_after_is_honoured_but_bounded()
    _test_make_request_5xx_retries()
    _test_make_request_timeout_retries()
    _test_make_request_recovers_on_retry()
    _test_weather_cache_warm_hits_upstream_once()
    _test_weather_stale_while_revalidate()
    _test_weather_failed_refresh_keeps_good_data()
    _test_weather_cache_location_change_fetches_anew()

    passed = sum(1 for status, _ in results if status.startswith("✅"))
    total = len(results)
    all_ok = passed == total

    print("=" * 50)
    print(f"Test Summary: {passed}/{total} tests passed")
    print(f"meteo: {'PASS' if all_ok else 'FAIL'}")

    return all_ok


if __name__ == "__main__":
    sys.exit(0 if run_meteo_tests() else 1)
