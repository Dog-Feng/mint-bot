from mint_engine.treasury.distribute import _build_preview_items


def test_preview_total_blocks_when_sum_exceeds_balance():
    source = "0x0000000000000000000000000000000000000001"
    targets = [
        "0x0000000000000000000000000000000000000002",
        "0x0000000000000000000000000000000000000003",
    ]
    quote_rows = [(10**18, 10**15), (10**18, 10**15)]
    balance = 2 * 10**18 + 10**15  # exactly one transfer + fee short for two
    items, budget = _build_preview_items(
        source_address=source,
        targets=targets,
        gap_min=0,
        gap_max=0,
        balance_wei=balance,
        quote_rows=quote_rows,
        native_symbol="ETH",
    )
    assert budget["sufficient"] is False
    assert all(row["status"] == "INSUFFICIENT_BALANCE" for row in items)


def test_preview_total_allows_when_sum_within_balance():
    source = "0x0000000000000000000000000000000000000001"
    targets = ["0x0000000000000000000000000000000000000002"]
    quote_rows = [(10**17, 10**14)]
    items, budget = _build_preview_items(
        source_address=source,
        targets=targets,
        gap_min=0,
        gap_max=0,
        balance_wei=10**18,
        quote_rows=quote_rows,
        native_symbol="ETH",
    )
    assert budget["sufficient"] is True
    assert items[0]["status"] == "READY"


def test_build_preview_items_preserves_custom_indices():
    source = "0x0000000000000000000000000000000000000001"
    targets = [
        "0x0000000000000000000000000000000000000002",
        "0x0000000000000000000000000000000000000003",
    ]
    quote_rows = [(10**17, 10**14), (10**17, 10**14)]
    items, budget = _build_preview_items(
        source_address=source,
        targets=targets,
        gap_min=0,
        gap_max=0,
        balance_wei=10**18,
        quote_rows=quote_rows,
        native_symbol="ETH",
        indices=[1, 3],
    )
    assert budget["sufficient"] is True
    assert [row["index"] for row in items] == [1, 3]
