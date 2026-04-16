from datetime import UTC, datetime

from bhiksha.tools.provider_divergence import _compact_features, _session_label, _summarize


def test_provider_divergence_labels_regular_and_extended_sessions() -> None:
    assert _session_label(datetime(2026, 4, 15, 14, 30, tzinfo=UTC)) == "regular"
    assert _session_label(datetime(2026, 4, 15, 12, 59, tzinfo=UTC)) == "extended"


def test_provider_divergence_summarizes_price_and_volume_separately() -> None:
    rows = [
        {
            "open_pct": 0.0,
            "high_pct": 0.0,
            "low_pct": 0.0,
            "close_pct": 0.0005,
            "volume_pct": 0.25,
            "directional_mass_pct": 0.4,
        },
        {
            "open_pct": 0.002,
            "high_pct": 0.0,
            "low_pct": 0.0,
            "close_pct": 0.0,
            "volume_pct": 0.0,
            "directional_mass_pct": 0.0,
        },
    ]

    summary = _summarize(rows, ["directional_mass"], pct_tol=0.001)

    assert summary["price_divergent"] == 1
    assert summary["volume_divergent"] == 1
    assert summary["feature_divergent"] == 1
    assert summary["max_price_pct"] == 0.002
    assert summary["max_volume_pct"] == 0.25
    assert summary["worst_feature_name"] == "directional_mass"


def test_provider_divergence_compacts_relevant_features() -> None:
    compact = _compact_features(
        {
            "close": 194.804999999,
            "vpoc_4h": 194.74,
            "directional_mass": 41652.7272727273,
            "ignored": "noise",
        }
    )

    assert compact == {
        "close": 194.805,
        "vpoc_4h": 194.74,
        "directional_mass": 41652.727273,
    }
