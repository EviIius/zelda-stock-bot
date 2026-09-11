"""Validation and persistence for user-added product monitors."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import unquote, urlparse


ROOT = Path(__file__).resolve().parent
CUSTOM_PRODUCTS_FILE = Path(os.environ.get(
    "CUSTOM_PRODUCTS_FILE", str(ROOT / "custom_products.json")
))
PRODUCT_OVERRIDES_FILE = Path(os.environ.get(
    "PRODUCT_OVERRIDES_FILE", str(ROOT / "product_overrides.json")
))
MAX_CUSTOM_PRODUCTS = 50
RETAILERS = {
    "target.com": "Target",
    "walmart.com": "Walmart",
    "bestbuy.com": "Best Buy",
    "gamestop.com": "GameStop",
    "nintendo.com": "Nintendo Store",
    "costco.com": "Costco",
    "amazon.com": "Amazon",
}


def retailer_for_url(url: str) -> str:
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    for domain, name in RETAILERS.items():
        if host == domain or host.endswith("." + domain):
            return name
    return host.split(".")[0].replace("-", " ").title() or "Online store"


def title_for_url(url: str) -> str:
    parsed = urlparse(url)
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    ignored = {"ip", "p", "product", "products", "site", "store", "us", "-"}
    candidates = [part for part in parts if part.lower() not in ignored]
    if not candidates:
        return f"Product at {retailer_for_url(url)}"
    candidate = candidates[-1]
    if re.fullmatch(r"(?:A-)?\d+(?:\.html)?|[A-Z0-9]{8,}", candidate, re.I) and len(candidates) > 1:
        candidate = candidates[-2]
    candidate = re.sub(r"[-_]+", " ", candidate)
    candidate = re.sub(r"\s+", " ", candidate).strip()
    return candidate.title()[:120] or f"Product at {retailer_for_url(url)}"


def _http_url(value: object, field: str, required: bool = True) -> str | None:
    text = str(value or "").strip()
    if not text and not required:
        return None
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{field} must be a complete http:// or https:// link")
    return text


def _key(record: dict, url: str) -> str:
    supplied = re.sub(r"[^a-z0-9_-]", "", str(record.get("key", "")).lower())
    if supplied.startswith("custom_") and len(supplied) <= 60:
        return supplied
    identity = f"{url}|{record.get('kind')}|{record.get('phrase', '')}"
    return "custom_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def normalize_product(record: dict) -> dict:
    if not isinstance(record, dict):
        raise ValueError("each catalog entry must be an object")
    url = _http_url(record.get("url"), "Product URL")
    assert url is not None
    kind = str(record.get("kind", "product")).strip().lower()
    if kind not in {"product", "appears"}:
        raise ValueError("Monitor type must be product or appears")
    phrase = str(record.get("phrase", "")).strip()
    if kind == "appears" and not phrase:
        raise ValueError("A listing-appearance monitor needs a phrase to watch for")
    try:
        interval = int(record.get("interval", 60))
    except (TypeError, ValueError) as exc:
        raise ValueError("Check interval must be a number of seconds") from exc
    if not 15 <= interval <= 86400:
        raise ValueError("Check interval must be between 15 and 86,400 seconds")
    group = str(record.get("group", "console")).strip().lower()
    if group not in {"console", "controller"}:
        raise ValueError("Alert channel must be primary or secondary")

    product = {
        "key": _key(record, url),
        "name": str(record.get("name") or retailer_for_url(url)).strip()[:80],
        "product_name": str(record.get("product_name") or title_for_url(url)).strip()[:160],
        "group": group,
        "url": url,
        "kind": kind,
        "cart_url": _http_url(record.get("cart_url"), "Cart URL", required=False),
        "enabled": bool(record.get("enabled", True)),
        "interval": interval,
        "custom": True,
    }
    if kind == "appears":
        product["phrase"] = phrase[:200]

    host = (urlparse(url).hostname or "").lower()
    if "target.com" in host:
        match = re.search(r"(?:A-|/A-)(\d{7,})", url, re.I)
        if match:
            product["tcin"] = match.group(1)
    elif "walmart.com" in host:
        match = re.search(r"/(\d{8,})(?:[/?#.]|$)", url)
        if match:
            item_id = match.group(1)
            product.update({
                "item_id": item_id,
                "status_url": f"https://www.walmart.com/search?q={item_id}",
                "cart_url": product["cart_url"] or
                            f"https://affil.walmart.com/cart/addToCart?items={item_id}",
                "request_timeout": 12,
                "http_attempts": 0,
                "use_browser": False,
            })
    elif "bestbuy.com" in host:
        # Exact Best Buy pages are intentionally HTTP-only unless a numeric
        # SKU/API key is supplied manually; an optimistic catalog flag must
        # never become a false alert.
        product.update({"request_timeout": 8, "http_attempts": 0, "use_browser": False})
        sku = re.search(r"/(\d{7})(?:[/?#.]|$)", url)
        if sku:
            product["sku"] = sku.group(1)
    return product


def load_custom_products(path: Path | str = CUSTOM_PRODUCTS_FILE) -> tuple[list[dict], list[str]]:
    source = Path(path)
    if not source.exists():
        return [], []
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [], [f"Could not read {source.name}: {exc}"]
    if not isinstance(value, list):
        return [], [f"{source.name} must contain a JSON list"]
    products: list[dict] = []
    errors: list[str] = []
    seen: set[str] = set()
    for index, raw in enumerate(value[:MAX_CUSTOM_PRODUCTS], start=1):
        try:
            product = normalize_product(raw)
            if product["key"] in seen:
                raise ValueError("duplicates another catalog entry")
            seen.add(product["key"])
            products.append(product)
        except ValueError as exc:
            errors.append(f"Entry {index}: {exc}")
    if len(value) > MAX_CUSTOM_PRODUCTS:
        errors.append(f"Only the first {MAX_CUSTOM_PRODUCTS} custom products were loaded")
    return products, errors


def save_custom_products(products: list[dict], path: Path | str = CUSTOM_PRODUCTS_FILE) -> list[dict]:
    if len(products) > MAX_CUSTOM_PRODUCTS:
        raise ValueError(f"The catalog supports up to {MAX_CUSTOM_PRODUCTS} custom products")
    normalized = [normalize_product(product) for product in products]
    keys = [product["key"] for product in normalized]
    if len(keys) != len(set(keys)):
        raise ValueError("The catalog contains duplicate product links")
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps(normalized, indent=2, ensure_ascii=False) + "\n",
                         encoding="utf-8")
    os.replace(temporary, destination)
    return normalized


def load_product_overrides(path: Path | str = PRODUCT_OVERRIDES_FILE) -> dict[str, bool]:
    source = Path(path)
    if not source.exists():
        return {}
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {
        str(key): bool(enabled)
        for key, enabled in value.items()
        if re.fullmatch(r"[a-z0-9_-]{1,60}", str(key))
    }


def set_product_enabled(key: str, enabled: bool,
                        path: Path | str = PRODUCT_OVERRIDES_FILE) -> None:
    if not re.fullmatch(r"[a-z0-9_-]{1,60}", key):
        raise ValueError("Invalid built-in product key")
    destination = Path(path)
    overrides = load_product_overrides(destination)
    overrides[key] = bool(enabled)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps(overrides, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, destination)
