"""Read-only Gumroad sales access for Company Mode revenue tracking.

Gumroad's API can't create products or upload files (dashboard-only), but it CAN
read your products and their sales, which is all we need to close the money loop:
pull per-product sales_count + revenue and compare against what the company spent.

Auth: a Gumroad access token (Settings -> Advanced -> Applications -> generate an
access token) in GUMROAD_ACCESS_TOKEN. Config is read at call time (not import) so a
value from a .env loaded after import still works - same pattern as github_helpers.
"""
import os

import requests

API_ROOT = "https://api.gumroad.com/v2"
NOT_CONFIGURED = "Gumroad isn't configured (set GUMROAD_ACCESS_TOKEN)."


def _token():
    return os.environ.get("GUMROAD_ACCESS_TOKEN", "")


def is_configured():
    return bool(_token())


def list_products():
    """Fetch the seller's products with sales figures. Returns (products, None) on
    success or (None, error_message) on failure, where each product is a dict with
    id, name, short_url, sales_count, sales_usd_cents, published."""
    token = _token()
    if not token:
        return None, NOT_CONFIGURED

    try:
        resp = requests.get(f"{API_ROOT}/products", params={"access_token": token}, timeout=15)
    except Exception as e:
        return None, f"Couldn't reach Gumroad ({e})."

    if resp.status_code != 200:
        return None, f"Gumroad error {resp.status_code}: {resp.text[:200]}"

    try:
        data = resp.json()
    except ValueError:
        return None, "Gumroad returned an unreadable response."

    if not data.get("success"):
        return None, f"Gumroad returned an error: {str(data)[:200]}"

    products = []
    for p in data.get("products", []):
        products.append({
            "id": p.get("id"),
            "name": p.get("name", ""),
            "short_url": p.get("short_url", ""),
            "sales_count": p.get("sales_count", 0) or 0,
            "sales_usd_cents": p.get("sales_usd_cents", 0) or 0,
            "published": bool(p.get("published", False)),
        })
    return products, None
