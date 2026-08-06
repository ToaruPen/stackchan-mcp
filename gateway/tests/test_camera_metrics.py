from stackchan_mcp.camera_metrics import BoundedLatencyHistogram


def test_histogram_coalesces_values_to_two_significant_digit_buckets() -> None:
    histogram = BoundedLatencyHistogram(maximum_bucket=10_000_000)

    histogram.add(12_341)
    histogram.add(12_399)

    assert histogram.status() == {
        "count": 2,
        "p50": 13_000,
        "p95": 13_000,
        "p99": 13_000,
        "max": 13_000,
    }
