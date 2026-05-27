#!/usr/bin/env python3
"""Minimal integration test for evaluate_candidates.py.

Verifies the scoring pipeline runs without crashing on dummy data.
"""
import sys, pathlib

# Allow import from scripts/
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

from evaluate_candidates import evaluate_items


def test_evaluate_items():
    """Test that evaluate_items processes items without crashing."""
    dummy = [
        {
            "source": "Yad2",
            "url": "https://www.yad2.co.il/realestate/item/test/123",
            "canonical_url": "https://www.yad2.co.il/realestate/item/test/123",
            "price": 6200,
            "rooms": 3,
            "city": "רמת גן",
            "entry_date": "2026-08-01",
            "entry_raw": "2026-08-01",
            "text": "דירה יפה 3 חדרים, מזגן, שקטה",
            "broker_status": "no_broker",
            "listing_type": "rental_apartment",
            "contract_type": "regular",
            "half_room_status": "closed",
            "source_platform": "Yad2",
            "id": "test-123",
        },
        {
            "source": "Facebook",
            "url": "https://www.facebook.com/groups/test/permalink/456/",
            "canonical_url": "https://www.facebook.com/groups/test/permalink/456/",
            "price": 7000,
            "rooms": 2.5,
            "city": "גבעתיים",
            "entry_date": "מיידית",
            "entry_raw": "מיידית",
            "text": "כניסה מיידית, מתווך",
            "broker_status": "broker",
            "listing_type": "rental_apartment",
            "contract_type": "regular",
            "half_room_status": "open",
            "source_platform": "Facebook",
            "id": "test-456",
        },
    ]

    try:
        results = evaluate_items(dummy)
        assert isinstance(results, list), f"Expected list, got {type(results)}"
        assert len(results) == 2, f"Expected 2 results, got {len(results)}"

        # Each result should have expected fields
        for r in results:
            assert "price" in r, "Missing price"
            assert "rooms" in r, "Missing rooms"

        print(f"✅ evaluate_items: {len(results)} items processed")
        return True

    except Exception as e:
        print(f"❌ evaluate_items failed: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    ok = test_evaluate_items()
    sys.exit(0 if ok else 1)
