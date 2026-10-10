#!/usr/bin/env python3
"""Crypto Treasury & Liquidity Manager — invoice guardrails + Reap payloads.

Reads a simulated invoice JSON, checks merchant allowlists and a per-transaction
USD cap, and if approved prepares the HTTP requests for Reap:

  POST /agentic/enrollments     store the sandbox card (EXTERNAL hosted page)
  POST /cards/                  optional VIRTUAL card if REAP_USER_ID is set

Credentials load from `.env` (gitignored). Before Reap, the agent reads
Ethereum Sepolia USDC and Ink Sepolia ETH (gas). Declared fallback if RPC
or the wallet is missing: 20 USDC, 3 ETH Sepolia, 0.1 ETH Ink.
Dry-run is the default; pass --live to POST, or --probe to GET /users/.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import zipfile
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent
MERCHANT_XLSX = ROOT / "Reap - List of Supported Merchant.xlsx"
ENV_FILE = ROOT / ".env"

MAX_TRANSACTION_USD = Decimal(os.environ.get("MAX_TRANSACTION_USD", "700.00"))


def load_dotenv(path: Path = ENV_FILE) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        os.environ.setdefault(key, value)


load_dotenv()

REAP_VERSION = os.environ.get("REAP_VERSION", "2025-02-14")
REAP_API_BASE = os.environ.get(
    "REAP_API_BASE_URL", "https://sg.sandbox.api.reap.global"
).rstrip("/")

# Demo treasury rails: USDC lives on Ethereum Sepolia; Ink Sepolia is gas only.
TREASURY_RAILS = {
    "gas_network": "Ink Sepolia",
    "settlement_network": "Ethereum Sepolia",
    "settlement_asset": "USDC",
}
INK_SEPOLIA_RPC = os.environ.get(
    "INK_SEPOLIA_RPC", "https://rpc-gel-sepolia.inkonchain.com"
)
INK_CHAIN_ID = int(os.environ.get("INK_CHAIN_ID", "763373"))
ETH_SEPOLIA_RPC = os.environ.get(
    "ETH_SEPOLIA_RPC", "https://ethereum-sepolia-rpc.publicnode.com"
)
ETH_SEPOLIA_CHAIN_ID = int(os.environ.get("ETH_SEPOLIA_CHAIN_ID", "11155111"))
# Circle USDC on Ethereum Sepolia
SEPOLIA_USDC_ADDRESS = os.environ.get(
    "SEPOLIA_USDC_ADDRESS", "0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238"
)
USDC_DECIMALS = int(os.environ.get("USDC_DECIMALS", "6"))
DECLARED_USDC = Decimal(os.environ.get("TREASURY_USDC", "20"))
MXN_PER_USD = Decimal(os.environ.get("MXN_PER_USD", "18.5"))
BLENDSTATION_CHECKOUT_URL = (
    "https://blendstation.com.mx/checkouts/cn/hWNHn6CJ0XA2shwTzYFqRx2H/es-mx"
    "?_r=AQABpoPXsvvBRxYmwIrQ1eblruI7N4HBURZLLy4o33mv59f4UB9n"
    "&attributes%5Bclick_id%5D=example_click_id&auto_redirect=false"
    "&cart_link_id=acQngpiI&edge_redirect=true&skip_shop_pay=true"
)
DECLARED_ETH_SEPOLIA = Decimal(os.environ.get("TREASURY_ETH_SEPOLIA", "3"))
DECLARED_ETH_INK = Decimal(os.environ.get("TREASURY_ETH_INK", "0.1"))
MIN_INK_GAS_ETH = Decimal(os.environ.get("MIN_INK_GAS_ETH", "0.001"))
# Sepolia ETH has no live USD price; this is the demo oracle for collateral.
ETH_USD_RATE = Decimal(os.environ.get("ETH_USD_RATE", "2500"))
ETH_COLLATERAL_LTV = Decimal(os.environ.get("ETH_COLLATERAL_LTV", "1"))

# B2B vendors that are in-scope for the treasury demo but are not Shopify
# storefronts on the Reap supported-merchant spreadsheet.
INTERNAL_APPROVED_MERCHANTS: dict[str, tuple[str, ...]] = {
    "aws": ("aws", "amazon web services", "amazonaws.com", "aws.amazon.com"),
    "google cloud": ("google cloud", "gcp", "cloud.google.com"),
    "stripe": ("stripe", "stripe.com"),
    "github": ("github", "github.com"),
    "openai": ("openai", "openai.com"),
    "anthropic": ("anthropic", "anthropic.com"),
    "cloudflare": ("cloudflare", "cloudflare.com"),
    "datadog": ("datadog", "datadoghq.com"),
    "vercel": ("vercel", "vercel.com"),
}

_XLSX_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_HOST_RE = re.compile(r"^(?:https?://)?(?:www\.)?([^/:]+)", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Domain objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Invoice:
    invoice_id: str
    merchant: str
    amount_usd: Decimal
    currency: str
    description: str
    received_at: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class MerchantMatch:
    approved: bool
    source: str | None
    canonical_name: str | None
    matched_on: str | None


@dataclass(frozen=True)
class GuardrailDecision:
    approved: bool
    reasons: tuple[str, ...]
    merchant: MerchantMatch
    amount_usd: Decimal
    max_amount_usd: Decimal


@dataclass
class ReapHttpRequest:
    """A fully assembled request. Swap the Bearer token and POST it."""

    name: str
    method: str
    url: str
    headers: dict[str, str]
    body: dict[str, Any] | None
    ready_for_live: bool
    notes: str
    client_context: dict[str, Any] = field(default_factory=dict)
    timeout_seconds: int = 25


@dataclass
class TreasurySnapshot:
    wallet: str
    usdc_before: Decimal
    usdc_after: Decimal | None
    ink_eth: Decimal
    sepolia_eth: Decimal
    source: str
    sufficient: bool
    rpc_error: str | None = None
    eth_usd_rate: Decimal = Decimal("0")
    eth_collateral_eth: Decimal = Decimal("0")
    eth_collateral_usd: Decimal = Decimal("0")
    usdc_used: Decimal = Decimal("0")
    capacity_usd: Decimal = Decimal("0")
    collateralized: bool = False
    sepolia_eth_after: Decimal | None = None


@dataclass(frozen=True)
class CatalogItem:
    merchant_door: str
    product: str
    variant_id: str
    price: Decimal
    currency: str
    cart_url: str
    market: str
    category: str


@dataclass
class AgentOutcome:
    invoice: Invoice
    decision: GuardrailDecision
    treasury: TreasurySnapshot | None = None
    catalog_item: CatalogItem | None = None
    reap_requests: list[ReapHttpRequest] = field(default_factory=list)
    live_responses: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "invoice": {
                **{k: v for k, v in asdict(self.invoice).items() if k != "raw"},
                "amount_usd": str(self.invoice.amount_usd),
            },
            "decision": {
                "approved": self.decision.approved,
                "reasons": list(self.decision.reasons),
                "merchant": asdict(self.decision.merchant),
                "amount_usd": str(self.decision.amount_usd),
                "max_amount_usd": str(self.decision.max_amount_usd),
            },
            "treasury": {
                **TREASURY_RAILS,
                **(
                    {
                        "wallet": self.treasury.wallet,
                        "usdc_before": str(self.treasury.usdc_before),
                        "usdc_after": (
                            str(self.treasury.usdc_after)
                            if self.treasury.usdc_after is not None
                            else None
                        ),
                        "ink_eth": str(self.treasury.ink_eth),
                        "sepolia_eth": str(self.treasury.sepolia_eth),
                        "eth_usd_rate": str(self.treasury.eth_usd_rate),
                        "eth_collateral_eth": str(self.treasury.eth_collateral_eth),
                        "eth_collateral_usd": str(self.treasury.eth_collateral_usd),
                        "usdc_used": str(self.treasury.usdc_used),
                        "capacity_usd": str(self.treasury.capacity_usd),
                        "collateralized": self.treasury.collateralized,
                        "sepolia_eth_after": (
                            str(self.treasury.sepolia_eth_after)
                            if self.treasury.sepolia_eth_after is not None
                            else None
                        ),
                        "source": self.treasury.source,
                        "sufficient": self.treasury.sufficient,
                        "rpc_error": self.treasury.rpc_error,
                    }
                    if self.treasury
                    else {}
                ),
            },
            "reap_requests": [
                {
                    "name": req.name,
                    "method": req.method,
                    "url": req.url,
                    "headers": _redact_headers(req.headers),
                    "body": req.body,
                    "client_context": req.client_context,
                    "ready_for_live": req.ready_for_live,
                    "notes": req.notes,
                }
                for req in self.reap_requests
            ],
            "live_responses": self.live_responses,
            "catalog": (
                {
                    **asdict(self.catalog_item),
                    "price": str(self.catalog_item.price),
                }
                if self.catalog_item
                else None
            ),
        }


SAMPLE_INVOICE: dict[str, Any] = {
    "invoice_id": "INV-AWS-2026-0412",
    "merchant": "AWS",
    "amount_usd": 15,
    "currency": "USD",
    "description": "Amazon Web Services — sandbox spend sized to ~20 USDC treasury",
    "received_at": "2026-10-09T16:00:00Z",
}


# ---------------------------------------------------------------------------
# Invoice parsing
# ---------------------------------------------------------------------------


def parse_invoice(payload: dict[str, Any] | str | Path) -> Invoice:
    data = _as_dict(payload)
    merchant = _first_str(
        data, "merchant", "vendor", "merchant_name", "payee", "supplier"
    )
    if not merchant:
        raise ValueError("Invoice JSON is missing a merchant / vendor name.")

    native = _first_amount(data)
    currency = (_first_str(data, "currency", "ccy") or "USD").upper()
    amount = _to_usd(native, currency)
    data = {**data, "amount_native": str(native)}

    invoice_id = (
        _first_str(data, "invoice_id", "id", "number", "invoiceNumber")
        or f"INV-{uuid.uuid4().hex[:8]}"
    )
    description = _first_str(data, "description", "memo", "line_item") or ""
    received_at = (
        _first_str(data, "received_at", "issued_at", "date")
        or datetime.now(timezone.utc).isoformat()
    )
    return Invoice(
        invoice_id=invoice_id,
        merchant=merchant,
        amount_usd=amount,
        currency=currency,
        description=description,
        received_at=received_at,
        raw=data,
    )


def _as_dict(payload: dict[str, Any] | str | Path) -> dict[str, Any]:
    if isinstance(payload, dict):
        return payload
    path = Path(payload)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, str):
        return json.loads(payload)
    raise TypeError("Invoice must be a dict, JSON string, or file path.")


def _first_str(data: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _first_amount(data: dict[str, Any]) -> Decimal:
    for key in ("amount_usd", "amount", "total", "total_usd", "gross"):
        if key in data and data[key] is not None:
            return _to_money(data[key])
    nested = data.get("total")
    if isinstance(nested, dict):
        for key in ("amount", "value", "usd"):
            if key in nested:
                return _to_money(nested[key])
    raise ValueError("Invoice JSON is missing an amount.")


def _to_usd(native: Decimal, currency: str) -> Decimal:
    if currency == "USD":
        return native
    if currency == "MXN":
        return (native / MXN_PER_USD).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    raise ValueError(f"Unsupported invoice currency {currency} (demo handles USD and MXN).")


def _to_money(value: Any) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Invoice amount is not a number: {value!r}") from exc
    if amount <= 0:
        raise ValueError("Invoice amount must be greater than zero.")
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Merchant allowlist (Excel + internal B2B list)
# ---------------------------------------------------------------------------


def load_excel_merchants(xlsx_path: Path = MERCHANT_XLSX) -> list[dict[str, str]]:
    """Parse 'Reap - List of Supported Merchant.xlsx' without extra packages."""
    if not xlsx_path.exists():
        return []

    with zipfile.ZipFile(xlsx_path) as archive:
        shared = _xlsx_shared_strings(archive)
        sheet = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))

    rows = sheet.findall("m:sheetData/m:row", _XLSX_NS)
    if not rows:
        return []

    header = _xlsx_row_values(rows[0], shared)
    merchants: list[dict[str, str]] = []
    for row in rows[1:]:
        values = _xlsx_row_values(row, shared)
        if not values:
            continue
        record = {
            (header.get(idx) or f"col_{idx}"): values[idx]
            for idx in values
        }
        door = (record.get("Merchant door") or "").strip()
        if door:
            merchants.append(record)
    return merchants


def _xlsx_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    strings: list[str] = []
    for si in root.findall("m:si", _XLSX_NS):
        strings.append("".join(t.text or "" for t in si.findall(".//{*}t")))
    return strings


def _xlsx_row_values(row: ET.Element, shared: list[str]) -> dict[int, str]:
    values: dict[int, str] = {}
    for cell in row.findall("m:c", _XLSX_NS):
        ref = cell.attrib.get("r", "")
        col = "".join(ch for ch in ref if ch.isalpha())
        idx = 0
        for ch in col:
            idx = idx * 26 + (ord(ch) - 64)
        idx -= 1
        cell_type = cell.attrib.get("t")
        if cell_type == "inlineStr":
            inline = cell.find("m:is", _XLSX_NS)
            text = (
                "".join(t.text or "" for t in inline.findall(".//{*}t"))
                if inline is not None
                else ""
            )
        else:
            node = cell.find("m:v", _XLSX_NS)
            if node is None or node.text is None:
                continue
            text = shared[int(node.text)] if cell_type == "s" else node.text
        if text:
            values[idx] = str(text).strip()
    return values


def match_merchant(
    merchant: str,
    excel_rows: list[dict[str, str]] | None = None,
) -> MerchantMatch:
    needle = _normalize_merchant(merchant)
    if not needle:
        return MerchantMatch(False, None, None, None)

    for canonical, aliases in INTERNAL_APPROVED_MERCHANTS.items():
        for alias in aliases:
            alias_n = _normalize_merchant(alias)
            if needle == alias_n or alias_n in needle or needle in alias_n:
                return MerchantMatch(True, "internal", canonical, alias)

    rows = excel_rows if excel_rows is not None else load_excel_merchants()
    for row in rows:
        door = row.get("Merchant door") or ""
        door_n = _normalize_merchant(door)
        if not door_n:
            continue
        if needle == door_n or door_n in needle or needle in door_n:
            return MerchantMatch(True, "reap_xlsx", door, door)
    return MerchantMatch(False, None, None, None)


def parse_catalog_item(row: dict[str, str]) -> CatalogItem | None:
    door = (row.get("Merchant door") or "").strip()
    cart_url = (row.get("Cart permalink") or "").strip()
    if not door or not cart_url:
        return None
    currency = (row.get("Currency") or "USD").upper()
    try:
        price = _to_money(row.get("Item price") or "0")
    except ValueError:
        return None
    return CatalogItem(
        merchant_door=door,
        product=(row.get("Test product") or "").strip(),
        variant_id=(row.get("Variant ID") or "").strip(),
        price=price,
        currency=currency,
        cart_url=cart_url,
        market=(row.get("Market") or "").strip(),
        category=(row.get("Category") or "").strip(),
    )


def find_catalog_item(
    query: str, excel_rows: list[dict[str, str]] | None = None
) -> CatalogItem | None:
    needle = _normalize_merchant(query)
    for row in excel_rows if excel_rows is not None else load_excel_merchants():
        item = parse_catalog_item(row)
        if not item:
            continue
        hay = _normalize_merchant(
            f"{item.merchant_door} {item.product} {item.category}"
        )
        if needle and (needle in hay or hay in needle):
            return item
    return None


def invoice_from_catalog(item: CatalogItem) -> dict[str, Any]:
    return {
        "invoice_id": f"CART-{item.variant_id or uuid.uuid4().hex[:8]}",
        "merchant": item.merchant_door,
        "amount": item.price,
        "currency": item.currency,
        "description": item.product,
        "cart_url": item.cart_url,
        "variant_id": item.variant_id,
    }


def _normalize_merchant(value: str) -> str:
    value = value.strip().lower()
    host_match = _HOST_RE.match(value)
    if host_match:
        value = host_match.group(1)
    value = value.replace("www.", "")
    return re.sub(r"[^a-z0-9.]+", "", value)


# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------


def evaluate_guardrails(
    invoice: Invoice,
    *,
    excel_rows: list[dict[str, str]] | None = None,
    max_amount_usd: Decimal = MAX_TRANSACTION_USD,
) -> GuardrailDecision:
    merchant = match_merchant(invoice.merchant, excel_rows)
    reasons: list[str] = []

    if not merchant.approved:
        reasons.append(
            f"Merchant '{invoice.merchant}' is not on the internal allowlist "
            "or Reap supported-merchant spreadsheet."
        )
    else:
        reasons.append(
            f"Merchant '{invoice.merchant}' matched "
            f"{merchant.source}:{merchant.canonical_name}."
        )

    if invoice.amount_usd > max_amount_usd:
        reasons.append(
            f"Amount ${invoice.amount_usd} exceeds the ${max_amount_usd} "
            "per-transaction cap."
        )
    else:
        reasons.append(
            f"Amount ${invoice.amount_usd} is within the ${max_amount_usd} cap."
        )

    approved = merchant.approved and invoice.amount_usd <= max_amount_usd
    return GuardrailDecision(
        approved=approved,
        reasons=tuple(reasons),
        merchant=merchant,
        amount_usd=invoice.amount_usd,
        max_amount_usd=max_amount_usd,
    )


# ---------------------------------------------------------------------------
# Reap payload builders (mock until API keys are present)
# ---------------------------------------------------------------------------


def _api_token() -> str | None:
    token = os.environ.get("REAP_API_KEY") or os.environ.get("REAP_ACCESS_TOKEN")
    if token and token.strip() and "YOUR_" not in token:
        return token.strip()
    return None


def _auth_header() -> tuple[str, bool]:
    token = _api_token()
    if token:
        return f"Bearer {token}", True
    return "Bearer YOUR_REAP_API_KEY", False


def _common_headers(idempotency_key: str) -> tuple[dict[str, str], bool]:
    authorization, ready = _auth_header()
    return (
        {
            "Authorization": authorization,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Idempotency-Key": idempotency_key,
            "Reap-Version": REAP_VERSION,
        },
        ready,
    )


def prepare_virtual_card_payload(invoice: Invoice) -> ReapHttpRequest:
    """POST /cards/ — issue a VIRTUAL card sized to the approved invoice."""
    idempotency_key = f"card-{invoice.invoice_id}-{uuid.uuid4()}"
    headers, ready = _common_headers(idempotency_key)
    user_id = os.environ.get("REAP_USER_ID", "YOUR_REAP_USER_ID")
    account_id = os.environ.get("REAP_ACCOUNT_ID", "YOUR_REAP_ACCOUNT_ID")
    body = {
        "userId": user_id,
        "accountId": account_id,
        "type": "VIRTUAL",
        "3dsChallengeMethod": "SMS",
    }
    ready = ready and "YOUR_" not in user_id and "YOUR_" not in account_id
    return ReapHttpRequest(
        name="create_virtual_card",
        method="POST",
        url=f"{REAP_API_BASE}/cards/",
        headers=headers,
        body=body,
        ready_for_live=ready,
        notes=(
            "Issues a Reap VIRTUAL card after guardrails pass. "
            "Set REAP_USER_ID and REAP_ACCOUNT_ID from the sandbox project. "
            "Card spend should be funded from Ethereum Sepolia USDC."
        ),
        client_context={
            "invoice_id": invoice.invoice_id,
            "merchant": invoice.merchant,
            "spend_intent_usd": str(invoice.amount_usd),
            "preferred_card_name": (
                f"Treasury · {invoice.merchant} · {invoice.invoice_id}"
            ),
            **TREASURY_RAILS,
        },
    )


def _sandbox_card_hint() -> dict[str, str]:
    """Hosted-page hints only. PAN/CVV never go in an API body or stdout dump."""
    pan = os.environ.get("REAP_SANDBOX_PAN", "")
    last4 = pan[-4:] if len(pan) >= 4 else ""
    hint = {
        "network_hint": "Visa test card",
        "expiry": os.environ.get("REAP_SANDBOX_EXPIRY", ""),
        "otp": os.environ.get("REAP_SANDBOX_OTP", "456789"),
        "entry": "Paste PAN/CVV only on Reap's hosted enrollment page, never in this agent.",
    }
    if last4:
        hint["last4"] = last4
    return hint


def prepare_enrollment_payload(
    invoice: Invoice,
    *,
    card_id: str | None = None,
) -> ReapHttpRequest:
    """POST /agentic/enrollments — store a card for later agentic checkout."""
    idempotency_key = f"enroll-{invoice.invoice_id}-{uuid.uuid4()}"
    headers, ready = _common_headers(idempotency_key)
    source = (
        os.environ.get("REAP_ENROLLMENT_SOURCE") or "EXTERNAL"
    ).upper()
    return_url = os.environ.get(
        "REAP_RETURN_URL", "https://example.com/cards/enrolled"
    )
    presentation = {"type": "REDIRECT", "returnUrl": return_url}

    if source == "REAP_CARD":
        resolved_card_id = (
            card_id or os.environ.get("REAP_CARD_ID") or "YOUR_REAP_CARD_ID"
        )
        body: dict[str, Any] = {
            "source": "REAP_CARD",
            "cardId": resolved_card_id,
            "presentation": presentation,
        }
        ready = ready and "YOUR_" not in resolved_card_id
        notes = (
            "Enrolls an existing Reap-issued card. Open nextAction.url to finish "
            "device binding, then charge only when status is ACTIVE."
        )
    else:
        owner_id = os.environ.get("REAP_OWNER_ID", "treasury-buildathon")
        owner_email = os.environ.get(
            "REAP_OWNER_EMAIL", "treasury@example.com"
        )
        body = {
            "source": "EXTERNAL",
            "owner": {
                "type": "CLIENT_REFERENCE",
                "id": owner_id,
                "email": owner_email,
            },
            "presentation": presentation,
        }
        notes = (
            "Opens Reap hosted card entry. Enter the sandbox Visa there "
            f"(last4 {_sandbox_card_hint().get('last4', '****')}, OTP "
            f"{_sandbox_card_hint().get('otp', '456789')}). Keep the enrollment "
            "id and wait for ACTIVE before checkout."
        )

    return ReapHttpRequest(
        name="create_enrollment",
        method="POST",
        url=f"{REAP_API_BASE}/agentic/enrollments",
        headers=headers,
        body=body,
        ready_for_live=ready,
        notes=notes,
        client_context={
            "invoice_id": invoice.invoice_id,
            "merchant": invoice.merchant,
            "amount_usd": str(invoice.amount_usd),
            "hosted_card": _sandbox_card_hint(),
        },
    )


def prepare_probe_request() -> ReapHttpRequest:
    headers, ready = _auth_header()
    return ReapHttpRequest(
        name="list_users",
        method="GET",
        url=f"{REAP_API_BASE}/users/",
        headers={
            "Authorization": headers,
            "Accept": "application/json",
            "Reap-Version": REAP_VERSION,
        },
        body=None,
        ready_for_live=ready,
        notes="Confirms the Singapore sandbox API key without creating resources.",
    )


def _shipping_address(*, country: str | None = None) -> dict[str, str]:
    resolved = (country or os.environ.get("REAP_SHIP_COUNTRY") or "US").upper()
    if resolved == "MX":
        defaults = {
            "firstName": "Maria",
            "lastName": "Hernandez",
            "phone": "+525512345678",
            "addressLine1": "Av. Paseo de la Reforma 222",
            "addressLine2": "Piso 1",
            "city": "Ciudad de Mexico",
            "region": "CMX",
            "postalCode": "06600",
            "country": "MX",
        }
    else:
        defaults = {
            "firstName": "Avery",
            "lastName": "Tan",
            "phone": "+14155552671",
            "addressLine1": "123 Market Street",
            "addressLine2": "Apt 4",
            "city": "San Francisco",
            "region": "CA",
            "postalCode": "94105",
            "country": "US",
        }
    address = {
        "firstName": os.environ.get("REAP_SHIP_FIRST_NAME", defaults["firstName"]),
        "lastName": os.environ.get("REAP_SHIP_LAST_NAME", defaults["lastName"]),
        "phone": os.environ.get("REAP_SHIP_PHONE", defaults["phone"]),
        "addressLine1": os.environ.get("REAP_SHIP_LINE1", defaults["addressLine1"]),
        "addressLine2": os.environ.get("REAP_SHIP_LINE2", defaults["addressLine2"]),
        "city": os.environ.get("REAP_SHIP_CITY", defaults["city"]),
        "region": os.environ.get("REAP_SHIP_REGION", defaults["region"]),
        "postalCode": os.environ.get("REAP_SHIP_POSTAL", defaults["postalCode"]),
        "country": os.environ.get("REAP_SHIP_COUNTRY", defaults["country"]),
    }
    return {key: value for key, value in address.items() if value}


def prepare_quote_payload(
    invoice: Invoice, item: CatalogItem
) -> ReapHttpRequest:
    idempotency_key = f"quote-{item.variant_id}-{uuid.uuid4()}"
    headers, ready = _common_headers(idempotency_key)
    email = os.environ.get("REAP_OWNER_EMAIL", "treasury@example.com")
    ship_country = "MX" if item.merchant_door.endswith(".mx") or item.currency == "MXN" else "US"
    body = {
        "externalCheckout": {
            "merchantDomain": item.merchant_door,
            "checkoutUrl": item.cart_url,
        },
        "email": email,
        "shippingAddress": _shipping_address(country=ship_country),
    }
    return ReapHttpRequest(
        name="create_quote",
        method="POST",
        url=f"{REAP_API_BASE}/agentic/quotes",
        headers=headers,
        body=body,
        ready_for_live=ready,
        timeout_seconds=90,
        notes=(
            "Prices the Excel cart permalink via POST /agentic/quotes "
            "(externalCheckout). Reap opens the merchant checkout URL as-is."
        ),
        client_context={
            "product": item.product,
            "list_price": str(item.price),
            "cart_url": item.cart_url,
        },
    )


def prepare_checkout_payload(
    invoice: Invoice, *, quote_id: str, enrollment_id: str
) -> ReapHttpRequest:
    idempotency_key = f"checkout-{invoice.invoice_id}-{uuid.uuid4()}"
    headers, ready = _common_headers(idempotency_key)
    if os.environ.get("REAP_SIMULATE_CHECKOUT", "COMPLETED"):
        headers["X-Simulate-Checkout"] = os.environ.get(
            "REAP_SIMULATE_CHECKOUT", "COMPLETED"
        )
    return_url = os.environ.get(
        "REAP_CHECKOUT_RETURN_URL",
        os.environ.get("REAP_RETURN_URL", "https://example.com/orders/done"),
    )
    body = {
        "quoteId": quote_id,
        "enrollmentId": enrollment_id,
        "presentation": {"type": "REDIRECT", "returnUrl": return_url},
    }
    ready = ready and "YOUR_" not in quote_id and "YOUR_" not in enrollment_id
    return ReapHttpRequest(
        name="create_checkout",
        method="POST",
        url=f"{REAP_API_BASE}/agentic/checkouts",
        headers=headers,
        body=body,
        ready_for_live=ready,
        timeout_seconds=90,
        notes=(
            "Charges an ACTIVE enrollment for the quote. Sandbox sends "
            "X-Simulate-Checkout: COMPLETED so the order can complete without "
            "the hosted approval page."
        ),
        client_context={"quote_id": quote_id, "enrollment_id": enrollment_id},
    )


def prepare_get_checkout(checkout_id: str) -> ReapHttpRequest:
    headers, ready = _common_headers(f"get-checkout-{checkout_id}")
    return ReapHttpRequest(
        name="get_checkout",
        method="GET",
        url=f"{REAP_API_BASE}/agentic/checkouts/{checkout_id}",
        headers=headers,
        body=None,
        ready_for_live=ready,
        timeout_seconds=30,
        notes="Poll checkout status after POST (sandbox confirmation).",
        client_context={"checkout_id": checkout_id},
    )


def prepare_reap_payloads(
    invoice: Invoice,
    *,
    card_id: str | None = None,
    catalog_item: CatalogItem | None = None,
    enrollment_id: str | None = None,
    skip_enroll: bool = False,
) -> list[ReapHttpRequest]:
    requests: list[ReapHttpRequest] = []
    user_id = os.environ.get("REAP_USER_ID", "")
    account_id = os.environ.get("REAP_ACCOUNT_ID", "")
    if user_id and account_id and "YOUR_" not in user_id:
        requests.append(prepare_virtual_card_payload(invoice))
    if not skip_enroll and not enrollment_id:
        requests.append(prepare_enrollment_payload(invoice, card_id=card_id))
    if catalog_item:
        requests.append(prepare_quote_payload(invoice, catalog_item))
    return requests


def submit_reap_request(req: ReapHttpRequest, *, live: bool) -> dict[str, Any]:
    """POST the payload. Dry-run unless live=True and credentials are ready."""
    if not live:
        return {
            "status": "mocked",
            "request": req.name,
            "message": "Dry-run. Re-run with --live once REAP_API_KEY is set.",
        }
    if not req.ready_for_live:
        return {
            "status": "blocked",
            "request": req.name,
            "message": (
                "Live mode is on but placeholders remain in headers/body. "
                "Insert REAP_API_KEY in .env. EXTERNAL enrollment only needs "
                "the key; POST /cards/ also needs REAP_USER_ID and REAP_ACCOUNT_ID."
            ),
        }

    # urllib can hang on this host's IPv6 path to Reap; curl (--ipv4) is reliable.
    command = [
        "curl",
        "--silent",
        "--show-error",
        "--ipv4",
        "--max-time",
        str(req.timeout_seconds),
        "-w",
        "\n__HTTP_STATUS__:%{http_code}",
        "-X",
        req.method,
        req.url,
    ]
    for key, value in req.headers.items():
        command.extend(["-H", f"{key}: {value}"])
    if req.body is not None:
        command.extend(["--data-binary", json.dumps(req.body)])
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=req.timeout_seconds + 15,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return {
            "status": "network_error",
            "request": req.name,
            "message": str(exc),
        }
    if completed.returncode != 0 and not completed.stdout:
        return {
            "status": "network_error",
            "request": req.name,
            "message": completed.stderr.strip() or f"curl exit {completed.returncode}",
        }
    stdout = completed.stdout
    status_line = "000"
    if "\n__HTTP_STATUS__:" in stdout:
        stdout, _, status_line = stdout.rpartition("\n__HTTP_STATUS__:")
    try:
        http_status = int(status_line.strip() or "0")
    except ValueError:
        http_status = 0
    try:
        parsed: Any = json.loads(stdout) if stdout.strip() else {}
    except json.JSONDecodeError:
        parsed = {"raw": stdout}
    if 200 <= http_status < 300:
        return {
            "status": "sent",
            "request": req.name,
            "http_status": http_status,
            "body": parsed,
        }
    return {
        "status": "http_error",
        "request": req.name,
        "http_status": http_status,
        "body": parsed,
        "stderr": completed.stderr.strip() or None,
    }


# ---------------------------------------------------------------------------
# Treasury: Ethereum Sepolia USDC + Ink Sepolia gas
# ---------------------------------------------------------------------------


def _treasury_wallet() -> str | None:
    raw = (
        os.environ.get("TREASURY_WALLET_ADDRESS")
        or os.environ.get("INK_WALLET_ADDRESS")
        or ""
    ).strip()
    if re.fullmatch(r"0x[a-fA-F0-9]{40}", raw):
        return raw
    return None


def _fmt_usdc(amount: Decimal) -> str:
    quantized = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if quantized == quantized.to_integral_value():
        return str(int(quantized))
    return format(quantized.normalize(), "f")


def _fmt_eth(amount: Decimal) -> str:
    quantized = amount.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    return format(quantized.normalize(), "f")


def _json_rpc(rpc_url: str, method: str, params: list[Any]) -> Any:
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    )
    command = [
        "curl",
        "--silent",
        "--show-error",
        "--ipv4",
        "--max-time",
        "15",
        "-H",
        "Content-Type: application/json",
        "--data-binary",
        payload,
        rpc_url,
    ]
    completed = subprocess.run(
        command, capture_output=True, text=True, timeout=20, check=False
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or f"RPC failed: {rpc_url}")
    body = json.loads(completed.stdout)
    if body.get("error"):
        raise RuntimeError(str(body["error"]))
    return body.get("result")


def _wei_to_eth(wei: int) -> Decimal:
    return (Decimal(wei) / Decimal(10) ** 18).quantize(
        Decimal("0.0001"), rounding=ROUND_HALF_UP
    )


def _erc20_balance(rpc_url: str, token: str, wallet: str) -> Decimal:
    data = "0x70a08231" + wallet[2:].lower().zfill(64)
    result = _json_rpc(rpc_url, "eth_call", [{"to": token, "data": data}, "latest"])
    if not isinstance(result, str) or not result.startswith("0x"):
        raise RuntimeError("Unexpected eth_call result for USDC balanceOf")
    raw = int(result, 16)
    return (Decimal(raw) / (Decimal(10) ** USDC_DECIMALS)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


def _native_eth(rpc_url: str, wallet: str, expected_chain: int) -> Decimal:
    chain_hex = _json_rpc(rpc_url, "eth_chainId", [])
    chain_id = int(chain_hex, 16) if isinstance(chain_hex, str) else None
    if chain_id != expected_chain:
        raise RuntimeError(f"RPC chainId {chain_id} != {expected_chain}")
    eth_hex = _json_rpc(rpc_url, "eth_getBalance", [wallet, "latest"])
    return _wei_to_eth(int(eth_hex, 16))


def read_treasury() -> TreasurySnapshot:
    wallet = _treasury_wallet()
    display_wallet = wallet or "(set TREASURY_WALLET_ADDRESS in .env)"
    if not wallet:
        return TreasurySnapshot(
            wallet=display_wallet,
            usdc_before=DECLARED_USDC,
            usdc_after=None,
            ink_eth=DECLARED_ETH_INK,
            sepolia_eth=DECLARED_ETH_SEPOLIA,
            source="declared",
            sufficient=True,
            rpc_error="No wallet in .env; using declared balances (20 USDC / 0.1 ETH Ink / 3 ETH Sepolia).",
        )

    errors: list[str] = []
    usdc = DECLARED_USDC
    ink_eth = DECLARED_ETH_INK
    sepolia_eth = DECLARED_ETH_SEPOLIA
    source = "declared"
    try:
        usdc = _erc20_balance(ETH_SEPOLIA_RPC, SEPOLIA_USDC_ADDRESS, wallet)
        sepolia_eth = _native_eth(ETH_SEPOLIA_RPC, wallet, ETH_SEPOLIA_CHAIN_ID)
        source = "onchain"
    except (RuntimeError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        errors.append(f"Ethereum Sepolia: {exc}")
    try:
        ink_eth = _native_eth(INK_SEPOLIA_RPC, wallet, INK_CHAIN_ID)
        if source != "onchain":
            source = "onchain-gas"
    except (RuntimeError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        errors.append(f"Ink Sepolia: {exc}")

    return TreasurySnapshot(
        wallet=display_wallet,
        usdc_before=usdc,
        usdc_after=None,
        ink_eth=ink_eth,
        sepolia_eth=sepolia_eth,
        source=source,
        sufficient=True,
        rpc_error="; ".join(errors) if errors else None,
    )


def evaluate_treasury(
    invoice: Invoice, *, sepolia_eth_override: Decimal | None = None
) -> TreasurySnapshot:
    snapshot = read_treasury()
    if sepolia_eth_override is not None:
        snapshot = TreasurySnapshot(
            wallet=snapshot.wallet,
            usdc_before=snapshot.usdc_before,
            usdc_after=None,
            ink_eth=snapshot.ink_eth,
            sepolia_eth=sepolia_eth_override,
            source=f"{snapshot.source}+eth-override",
            sufficient=True,
            rpc_error=snapshot.rpc_error,
        )
    amount = invoice.amount_usd
    usdc_used = min(snapshot.usdc_before, amount)
    shortfall = amount - usdc_used
    rate = ETH_USD_RATE
    ltv = ETH_COLLATERAL_LTV
    eth_needed = Decimal("0")
    eth_usd = Decimal("0")
    if shortfall > 0 and rate > 0 and ltv > 0:
        eth_needed = (shortfall / (rate * ltv)).quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_UP
        )
        eth_usd = (eth_needed * rate * ltv).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    collateralized = shortfall > 0 and snapshot.sepolia_eth >= eth_needed
    capacity = snapshot.usdc_before + (snapshot.sepolia_eth * rate * ltv)
    enough_gas = snapshot.ink_eth >= MIN_INK_GAS_ETH
    enough_spend = shortfall == 0 or collateralized
    return TreasurySnapshot(
        wallet=snapshot.wallet,
        usdc_before=snapshot.usdc_before,
        usdc_after=None,
        ink_eth=snapshot.ink_eth,
        sepolia_eth=snapshot.sepolia_eth,
        source=snapshot.source,
        sufficient=enough_spend and enough_gas,
        rpc_error=snapshot.rpc_error,
        eth_usd_rate=rate,
        eth_collateral_eth=eth_needed if collateralized else Decimal("0"),
        eth_collateral_usd=eth_usd if collateralized else Decimal("0"),
        usdc_used=usdc_used,
        capacity_usd=capacity.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        collateralized=collateralized,
        sepolia_eth_after=None,
    )


def apply_treasury_debit(
    snapshot: TreasurySnapshot, amount: Decimal
) -> TreasurySnapshot:
    usdc_after = snapshot.usdc_before - snapshot.usdc_used
    if usdc_after < 0:
        usdc_after = Decimal("0")
    eth_after = snapshot.sepolia_eth - snapshot.eth_collateral_eth
    if eth_after < 0:
        eth_after = Decimal("0")
    return TreasurySnapshot(
        wallet=snapshot.wallet,
        usdc_before=snapshot.usdc_before,
        usdc_after=usdc_after,
        ink_eth=snapshot.ink_eth,
        sepolia_eth=snapshot.sepolia_eth,
        source=snapshot.source,
        sufficient=snapshot.sufficient,
        rpc_error=snapshot.rpc_error,
        eth_usd_rate=snapshot.eth_usd_rate,
        eth_collateral_eth=snapshot.eth_collateral_eth,
        eth_collateral_usd=snapshot.eth_collateral_usd,
        usdc_used=snapshot.usdc_used,
        capacity_usd=snapshot.capacity_usd,
        collateralized=snapshot.collateralized,
        sepolia_eth_after=eth_after,
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def process_invoice(
    payload: dict[str, Any] | str | Path = SAMPLE_INVOICE,
    *,
    live: bool = False,
    card_id: str | None = None,
    catalog_item: CatalogItem | None = None,
    enrollment_id: str | None = None,
    skip_enroll: bool = False,
    sepolia_eth_override: Decimal | None = None,
) -> AgentOutcome:
    invoice = parse_invoice(payload)
    excel_rows = load_excel_merchants()
    if catalog_item is None:
        catalog_item = find_catalog_item(invoice.merchant, excel_rows)
    decision = evaluate_guardrails(invoice, excel_rows=excel_rows)
    outcome = AgentOutcome(
        invoice=invoice, decision=decision, catalog_item=catalog_item
    )

    if not decision.approved:
        return outcome

    outcome.treasury = evaluate_treasury(
        invoice, sepolia_eth_override=sepolia_eth_override
    )
    if not outcome.treasury.sufficient:
        return outcome

    saved_enrollment = enrollment_id or os.environ.get("REAP_ENROLLMENT_ID")
    outcome.reap_requests = prepare_reap_payloads(
        invoice,
        card_id=card_id,
        catalog_item=catalog_item,
        enrollment_id=saved_enrollment,
        skip_enroll=skip_enroll,
    )
    for req in outcome.reap_requests:
        outcome.live_responses.append(submit_reap_request(req, live=live))

    quote = _named_result(outcome, "create_quote")
    quote_id = None
    if quote and quote.get("status") == "sent":
        body = quote.get("body") if isinstance(quote.get("body"), dict) else {}
        quote_id = body.get("id")
        final = (
            ((body.get("amountBreakdown") or {}).get("finalAmount") or {}).get(
                "amount"
            )
        )
        if final is not None:
            try:
                invoice = Invoice(
                    invoice_id=invoice.invoice_id,
                    merchant=invoice.merchant,
                    amount_usd=_to_money(final),
                    currency=invoice.currency,
                    description=invoice.description,
                    received_at=invoice.received_at,
                    raw=invoice.raw,
                )
                outcome.invoice = invoice
            except ValueError:
                pass

    enroll_from_response = (_enrollment_result(outcome) or {}).get("enrollment_id")
    checkout_enrollment = saved_enrollment or enroll_from_response
    if quote_id and checkout_enrollment and live:
        checkout_req = prepare_checkout_payload(
            invoice, quote_id=quote_id, enrollment_id=checkout_enrollment
        )
        outcome.reap_requests.append(checkout_req)
        outcome.live_responses.append(submit_reap_request(checkout_req, live=True))
        checkout_body = outcome.live_responses[-1].get("body")
        checkout_id = (
            checkout_body.get("id") if isinstance(checkout_body, dict) else None
        )
        if checkout_id:
            time.sleep(2)
            poll = prepare_get_checkout(checkout_id)
            outcome.reap_requests.append(poll)
            outcome.live_responses.append(submit_reap_request(poll, live=True))

    enrollment = _enrollment_result(outcome)
    quote_ok = quote is not None and quote.get("status") in {"sent", "mocked"}
    enroll_ok = False
    if enrollment is not None:
        enroll_ok = enrollment["status"] in {"sent", "mocked"}
        if enrollment["status"] == "sent" and not enrollment.get("enrollment_id"):
            enroll_ok = False
    checkout = _named_result(outcome, "create_checkout")
    checkout_ok = checkout is not None and checkout.get("status") in {
        "sent",
        "mocked",
    }
    if enroll_ok or quote_ok or checkout_ok:
        outcome.treasury = apply_treasury_debit(
            outcome.treasury, invoice.amount_usd
        )
    return outcome


def _redact_headers(headers: dict[str, str]) -> dict[str, str]:
    redacted = dict(headers)
    auth = redacted.get("Authorization", "")
    if auth.startswith("Bearer ") and "YOUR_" not in auth:
        redacted["Authorization"] = "Bearer ***"
    return redacted


def _named_result(outcome: AgentOutcome, name: str) -> dict[str, Any] | None:
    for response, req in zip(outcome.live_responses, outcome.reap_requests):
        if req.name == name:
            return response
    return None


def _enrollment_result(outcome: AgentOutcome) -> dict[str, Any] | None:
    for response, req in zip(outcome.live_responses, outcome.reap_requests):
        if req.name != "create_enrollment":
            continue
        body = response.get("body") if isinstance(response.get("body"), dict) else {}
        next_action = body.get("nextAction") if isinstance(body, dict) else None
        hosted_url = None
        if isinstance(next_action, dict):
            hosted_url = next_action.get("url")
        return {
            "http_status": response.get("http_status"),
            "status": response.get("status"),
            "enrollment_id": body.get("id") if isinstance(body, dict) else None,
            "source": body.get("source") if isinstance(body, dict) else None,
            "hosted_url": hosted_url,
            "error": response,
        }
    return None


def _checkout_confirmation_lines(body: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    status = str(body.get("status") or "")
    if status == "COMPLETED":
        lines.append("✅ Confirmación: checkout COMPLETED (sandbox simulated).")
    elif status == "REQUIRES_ACTION":
        lines.append("⏳ Checkout espera acción (hosted collect / 3DS).")
    elif status:
        lines.append(f"ℹ️  Checkout status: {status}")
    order = body.get("order") or body.get("merchantOrder") or {}
    if isinstance(order, dict) and order:
        oid = order.get("id") or order.get("orderId") or order.get("number")
        if oid:
            lines.append(f"📦 Order: {oid}")
    amount = (
        body.get("amount")
        or ((body.get("amountBreakdown") or {}).get("finalAmount") or {})
        or {}
    )
    if isinstance(amount, dict) and amount.get("amount") is not None:
        lines.append(
            f"💵 Charged: {amount.get('amount')} {amount.get('currency', '')}".rstrip()
        )
    for key in ("quoteId", "enrollmentId", "paymentIntentId", "createdAt"):
        if body.get(key):
            lines.append(f"   {key}: {body.get(key)}")
    return lines


def render_run(outcome: AgentOutcome) -> str:
    invoice = outcome.invoice
    native = invoice.raw.get("amount_native") or invoice.raw.get("amount")
    if invoice.currency != "USD" and native:
        received = (
            f"📥 Factura recibida de {invoice.merchant} por {native} {invoice.currency}"
            f" (~${invoice.amount_usd} USD)"
        )
    else:
        received = (
            f"📥 Factura recibida de {invoice.merchant} por ${invoice.amount_usd} {invoice.currency}"
        )
    lines = [received]
    if outcome.catalog_item:
        item = outcome.catalog_item
        lines.append(
            f"🛒 Catalog: {item.product} @ {item.merchant_door} ({item.price} {item.currency})"
        )
        lines.append(f"🔗 Checkout page: {item.cart_url}")
    if outcome.decision.approved:
        lines.append(
            "✅ Guardrails aprobados (Merchant autorizado & Monto dentro del límite)"
        )
    else:
        lines.append("❌ Guardrails rechazados")
        for reason in outcome.decision.reasons:
            lines.append(f"   • {reason}")
        return "\n".join(lines)

    if outcome.treasury is not None:
        t = outcome.treasury
        lines.append(
            f"⛓️ Tesorería: USDC en Ethereum Sepolia + gas en Ink Sepolia ({t.source})"
        )
        if t.rpc_error:
            lines.append(f"   ℹ️  {t.rpc_error}")
        if t.collateralized:
            lines.append(
                f"💵 USDC {_fmt_usdc(t.usdc_before)} no cubre ${_fmt_usdc(invoice.amount_usd)} "
                f"(faltan ${_fmt_usdc(invoice.amount_usd - t.usdc_used)})"
            )
            lines.append(
                f"🔒 Colateral on-chain: lock {_fmt_eth(t.eth_collateral_eth)} ETH "
                f"en Ethereum Sepolia @ ${_fmt_usdc(t.eth_usd_rate)}/ETH "
                f"→ ${_fmt_usdc(t.eth_collateral_usd)}"
            )
            lines.append(
                "✅ Factura colateralizada. Ink Sepolia ETH se reserva solo para gas."
            )
        if not t.sufficient:
            lines.append("❌ Fondos insuficientes. No se llama a Reap.")
            if t.usdc_before < invoice.amount_usd and not t.collateralized:
                lines.append(
                    f"   USDC {_fmt_usdc(t.usdc_before)} + ETH Sepolia "
                    f"{_fmt_eth(t.sepolia_eth)} no cubren ${_fmt_usdc(invoice.amount_usd)}."
                )
            if t.ink_eth < MIN_INK_GAS_ETH:
                lines.append(
                    f"   Gas Ink Sepolia {_fmt_eth(t.ink_eth)} ETH es menor que {MIN_INK_GAS_ETH} ETH."
                )
            lines.extend(_treasury_bridge_lines(t, invoice, authorized=False))
            return "\n".join(lines)

    enrollment = _enrollment_result(outcome)
    quote = _named_result(outcome, "create_quote")
    checkout = _named_result(outcome, "create_checkout")
    if enrollment is None and quote is None:
        lines.append("⏸️  Payload listo (dry-run). Usa --live para enviarlo.")
        return "\n".join(lines)

    if enrollment:
        if enrollment["status"] == "mocked":
            lines.append("🌐 Enrolamiento armado (dry-run).")
        elif enrollment["status"] == "sent" and enrollment.get("enrollment_id"):
            lines.append("🌐 Enviando orden de enrolamiento a Reap API...")
            source = enrollment.get("source") or "EXTERNAL"
            lines.append(
                f"🎉 [{enrollment.get('http_status')} OK] Enrollment {source} generado con éxito!"
            )
            hosted = enrollment.get("hosted_url") or "(sin nextAction.url)"
            lines.append(f"🔗 Hosted page (Reap nextAction.url): {hosted}")
            lines.append(f"🆔 Enrollment ID: {enrollment['enrollment_id']}")
        else:
            lines.append(
                f"💥 Enrolamiento: {enrollment.get('status')} HTTP {enrollment.get('http_status')}"
            )

    if quote:
        if quote.get("status") == "mocked":
            lines.append("🧾 Quote armado (dry-run) con el cart permalink del Excel.")
        elif quote.get("status") == "sent":
            body = quote.get("body") if isinstance(quote.get("body"), dict) else {}
            final = ((body.get("amountBreakdown") or {}).get("finalAmount") or {})
            lines.append(
                f"🎉 [{quote.get('http_status')} OK] Quote {body.get('id', '')}"
            )
            if final.get("amount") is not None:
                lines.append(
                    f"💵 Total merchant: {final.get('amount')} {final.get('currency', '')}"
                )
        else:
            lines.append(
                f"💥 Quote: {quote.get('status')} HTTP {quote.get('http_status')}"
            )
            if isinstance(quote.get("body"), dict):
                err = quote.get("body").get("error") or quote.get("body")
                lines.append(json.dumps(err, indent=2)[:1200])
                code = (quote.get("body").get("error") or {}).get("code")
                if code == "QUOTE_UNFULFILLABLE" and outcome.catalog_item:
                    lines.append(
                        "   Abre el checkout page del Excel en el browser; "
                        "Reap no pudo fondear ese cart (address/SKU/allowlist)."
                    )

    if checkout:
        if checkout.get("status") == "mocked":
            lines.append("🛍️  Checkout armado (dry-run).")
        elif checkout.get("status") == "sent":
            body = checkout.get("body") if isinstance(checkout.get("body"), dict) else {}
            polled = _named_result(outcome, "get_checkout")
            polled_body = (
                polled.get("body")
                if polled and isinstance(polled.get("body"), dict)
                else {}
            )
            confirmed = polled_body or body
            status = confirmed.get("status") or body.get("status")
            lines.append(
                f"🎉 [{checkout.get('http_status')} OK] Checkout {status} id={body.get('id')}"
            )
            lines.extend(_checkout_confirmation_lines(confirmed))
            next_action = confirmed.get("nextAction") or body.get("nextAction") or {}
            if isinstance(next_action, dict) and next_action.get("url"):
                lines.append(f"🔗 Approval page: {next_action.get('url')}")
            if polled and polled.get("status") == "sent":
                lines.append(
                    f"📬 Poll GET /agentic/checkouts/{body.get('id')} → {status}"
                )
        else:
            lines.append(
                f"💥 Checkout: {checkout.get('status')} HTTP {checkout.get('http_status')}"
            )
            if isinstance(checkout.get("body"), dict):
                lines.append(json.dumps(checkout.get("body"), indent=2)[:1200])

    if (
        outcome.treasury is not None
        and outcome.treasury.usdc_after is not None
    ):
        lines.append("")
        lines.append("—— Puente On-Chain → Fiat ——")
        lines.extend(_treasury_bridge_lines(outcome.treasury, invoice, authorized=True))
    return "\n".join(lines)


def _treasury_bridge_lines(
    treasury: TreasurySnapshot, invoice: Invoice, *, authorized: bool
) -> list[str]:
    pay = (
        f"💳 Pago Reap Autorizado: ${_fmt_usdc(invoice.amount_usd)} USD"
        if authorized
        else f"💳 Pago Reap solicitado: ${_fmt_usdc(invoice.amount_usd)} USD"
    )
    after = (
        treasury.usdc_after
        if treasury.usdc_after is not None
        else treasury.usdc_before
    )
    lines = [
        f"🔗 Wallet: {treasury.wallet}",
        f"⛽ Gas Ink Sepolia: {_fmt_eth(treasury.ink_eth)} ETH (no se usa de colateral)",
        f"💰 USDC pre: {_fmt_usdc(treasury.usdc_before)} · ETH Sepolia pre: {_fmt_eth(treasury.sepolia_eth)}",
        pay,
        f"📉 USDC post: {_fmt_usdc(after)} (Ethereum Sepolia)",
    ]
    if treasury.collateralized:
        eth_after = (
            treasury.sepolia_eth_after
            if treasury.sepolia_eth_after is not None
            else treasury.sepolia_eth
        )
        lines.append(
            f"🔒 ETH colateralizado: {_fmt_eth(treasury.eth_collateral_eth)} "
            f"(${_fmt_usdc(treasury.eth_collateral_usd)} @ ${_fmt_usdc(treasury.eth_usd_rate)}/ETH)"
        )
        lines.append(f"📉 ETH Sepolia post (libre): {_fmt_eth(eth_after)}")
    return lines


def _api_trace(outcome: AgentOutcome) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    decision = outcome.decision
    steps.append(
        {
            "layer": "agent",
            "title": "Guardrails",
            "ok": decision.approved,
            "detail": " ".join(decision.reasons),
        }
    )
    treasury = outcome.treasury
    if treasury is None:
        steps.append(
            {
                "layer": "onchain",
                "title": "Treasury / RPC",
                "ok": False,
                "skipped": True,
                "detail": "Skipped — policy rejected before reading the wallet.",
            }
        )
    else:
        steps.append(
            {
                "layer": "onchain",
                "title": "Treasury / RPC",
                "ok": treasury.sufficient,
                "detail": (
                    f"USDC {treasury.usdc_before} · ETH Sepolia {treasury.sepolia_eth} "
                    f"· capacity ${treasury.capacity_usd} @ ${treasury.eth_usd_rate}/ETH"
                    + (
                        f" · lock {treasury.eth_collateral_eth} ETH"
                        if treasury.collateralized
                        else ""
                    )
                ),
            }
        )
    paired = list(zip(outcome.reap_requests, outcome.live_responses))
    if not paired and decision.approved and treasury and not treasury.sufficient:
        steps.append(
            {
                "layer": "reap",
                "title": "POST /agentic/quotes",
                "ok": False,
                "skipped": True,
                "detail": "Not sent — USDC + Sepolia ETH cannot cover this invoice.",
            }
        )
    if not paired and not decision.approved:
        steps.append(
            {
                "layer": "reap",
                "title": "POST /agentic/quotes + /agentic/checkouts",
                "ok": False,
                "skipped": True,
                "detail": "Not sent — over the per-transaction expense cap.",
            }
        )
    for req, resp in paired:
        path = req.url
        if path.startswith(REAP_API_BASE):
            path = path[len(REAP_API_BASE) :] or "/"
        body = resp.get("body") if isinstance(resp.get("body"), dict) else {}
        preview: dict[str, Any] = {}
        if isinstance(body, dict):
            for key in ("id", "status", "quoteId", "enrollmentId", "createdAt"):
                if body.get(key) is not None:
                    preview[key] = body.get(key)
            final = ((body.get("amountBreakdown") or {}).get("finalAmount") or {})
            if final.get("amount") is not None:
                preview["finalAmount"] = final
            next_action = body.get("nextAction") or {}
            if isinstance(next_action, dict) and next_action.get("url"):
                preview["approvalUrl"] = next_action.get("url")
            err = body.get("error")
            if err:
                preview["error"] = err
        steps.append(
            {
                "layer": "reap",
                "title": f"{req.method} {path}",
                "name": req.name,
                "ok": resp.get("status") == "sent",
                "http_status": resp.get("http_status"),
                "status": resp.get("status"),
                "request": req.body,
                "response": preview or body,
                "detail": resp.get("message") or "",
            }
        )
    return steps


def _approval_url(outcome: AgentOutcome) -> str | None:
    for name in ("get_checkout", "create_checkout"):
        result = _named_result(outcome, name)
        if not result or not isinstance(result.get("body"), dict):
            continue
        next_action = result["body"].get("nextAction") or {}
        if isinstance(next_action, dict) and next_action.get("url"):
            return str(next_action["url"])
    return None


def public_run_summary(outcome: AgentOutcome, kind: str) -> dict[str, Any]:
    checkout = _named_result(outcome, "create_checkout")
    quote = _named_result(outcome, "create_quote")
    checkout_body = (
        checkout.get("body") if checkout and isinstance(checkout.get("body"), dict) else {}
    )
    quote_body = quote.get("body") if quote and isinstance(quote.get("body"), dict) else {}
    live_reap = any(
        response.get("status") == "sent"
        for response in outcome.live_responses
        if response.get("request") in {"create_quote", "create_checkout"}
    )
    treasury = outcome.treasury
    return {
        "case": kind,
        "log": render_run(outcome),
        "approved": outcome.decision.approved,
        "sufficient": bool(treasury and treasury.sufficient),
        "collateralized": bool(treasury and treasury.collateralized),
        "approval_url": _approval_url(outcome),
        "quote_id": quote_body.get("id"),
        "checkout_id": checkout_body.get("id"),
        "checkout_status": checkout_body.get("status"),
        "reap_called": live_reap,
        "cap_usd": str(MAX_TRANSACTION_USD),
        "api": _api_trace(outcome),
        "records": (
            "Reap sandbox stored a quote and checkout. Complete the hosted "
            "collect URL to finish. This agent does not write a local DB and "
            "does not broadcast an on-chain lock transaction."
            if live_reap
            else "No Reap API call. Policy or treasury stopped the run, so "
            "there is no quote, checkout, or collect session. The live wallet "
            "was not overridden."
        ),
    }


def _append_sandbox_run(summary: dict[str, Any]) -> None:
    path = ROOT / "sandbox-runs.jsonl"
    record = {
        "at": datetime.now(timezone.utc).isoformat(),
        **{k: v for k, v in summary.items() if k != "log"},
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


def run_demo_case(kind: str) -> dict[str, Any]:
    if kind == "pass":
        item = find_catalog_item("beautybakerie")
        if item is None:
            return {"error": "Catalog missing beautybakerie"}
        outcome = process_invoice(
            invoice_from_catalog(item),
            live=True,
            catalog_item=item,
            skip_enroll=True,
        )
    elif kind == "collateral":
        item = find_catalog_item("abchome")
        if item is None:
            return {"error": "Catalog missing abchome"}
        outcome = process_invoice(
            invoice_from_catalog(item),
            live=True,
            catalog_item=item,
            skip_enroll=True,
        )
    elif kind == "shortfall":
        item = find_catalog_item("headphones.com")
        if item is None:
            return {"error": "Catalog missing headphones.com"}
        outcome = process_invoice(
            invoice_from_catalog(item),
            live=False,
            catalog_item=item,
            skip_enroll=True,
        )
    elif kind in {"reject", "overcap"}:
        item = find_catalog_item("gmktec")
        if item is None:
            return {"error": "Catalog missing gmktec"}
        outcome = process_invoice(
            invoice_from_catalog(item),
            live=False,
            catalog_item=item,
            skip_enroll=True,
        )
    else:
        return {"error": f"Unknown demo case {kind!r}"}
    summary = public_run_summary(outcome, kind)
    _append_sandbox_run(summary)
    return summary


def serve_demo(host: str = "127.0.0.1", port: int = 8765) -> int:
    html_path = ROOT / "demo.html"

    class DemoHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path.split("?", 1)[0] in {"/", "/demo.html"}:
                data = html_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            self.send_error(404)

        def do_POST(self) -> None:
            if self.path != "/api/run":
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length") or "0")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                body = {}
            kind = str(body.get("case") or "")
            result = run_demo_case(kind)
            payload = json.dumps(result).encode("utf-8")
            status = 200 if "error" not in result else 400
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, fmt: str, *args: Any) -> None:
            sys.stderr.write("demo %s - %s\n" % (self.address_string(), fmt % args))

    server = ThreadingHTTPServer((host, port), DemoHandler)
    print(f"Demo: http://{host}:{port}/", flush=True)
    print("Run agent on pass/collateral calls Reap live and returns a collect URL.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped demo server.")
    return 0


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate an invoice and prepare Reap virtual-card payloads."
    )
    parser.add_argument(
        "--invoice",
        help="Path to invoice JSON. Defaults to the AWS sample (15 USD, sized to ~20 USDC).",
    )
    parser.add_argument(
        "--amount",
        help="Override invoice USD amount (150 will fail with a 20 USDC treasury).",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="POST to Reap sandbox. Requires REAP_API_KEY and related env vars.",
    )
    parser.add_argument(
        "--card-id",
        help="Existing Reap card UUID when REAP_ENROLLMENT_SOURCE=REAP_CARD.",
    )
    parser.add_argument(
        "--probe",
        action="store_true",
        help="GET /users/ against the Singapore sandbox to verify the API key.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the full machine-readable payload instead of the demo log.",
    )
    parser.add_argument(
        "--catalog",
        help="Buy the Excel test SKU (merchant door or product), e.g. beautybakerie.",
    )
    parser.add_argument(
        "--list-catalog",
        action="store_true",
        help="Print USD cart permalinks from the supported-merchant spreadsheet.",
    )
    parser.add_argument(
        "--skip-enroll",
        action="store_true",
        help="Skip POST /agentic/enrollments (use with --enrollment-id to checkout).",
    )
    parser.add_argument(
        "--sepolia-eth",
        help="Override Ethereum Sepolia ETH (depleted-collateral demo).",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Serve demo.html on http://127.0.0.1:8765 and POST /api/run.",
    )
    parser.add_argument(
        "--enrollment-id",
        help="ACTIVE enrollment UUID for POST /agentic/checkouts.",
    )
    parser.add_argument(
        "--checkout-url",
        help="Override the Excel cart permalink (e.g. Blendstation pickup checkout).",
    )
    args = parser.parse_args(argv)

    if args.serve:
        return serve_demo()

    if args.list_catalog:
        rows = load_excel_merchants()
        print("Test carts from Reap - List of Supported Merchant.xlsx")
        print(f"{'merchant':<28} {'price':>8} {'ccy':<4}  product")
        for row in rows:
            item = parse_catalog_item(row)
            if not item:
                continue
            print(
                f"{item.merchant_door:<28} {item.price:>8} {item.currency:<4}  {item.product[:48]}"
            )
            print(f"  {item.cart_url}")
        return 0

    if args.probe:
        probe = prepare_probe_request()
        result = {
            "probe": {
                "url": probe.url,
                "ready_for_live": probe.ready_for_live,
                "key_expires_at": os.environ.get("REAP_API_KEY_EXPIRES_AT"),
            },
            "response": submit_reap_request(probe, live=True),
        }
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0 if result["response"].get("status") == "sent" else 1

    catalog_item = None
    checkout_url = args.checkout_url
    if args.catalog:
        catalog_item = find_catalog_item(args.catalog)
        if catalog_item is None:
            sys.stderr.write(f"No catalog match for {args.catalog!r}.\n")
            return 2
        if checkout_url:
            catalog_item = replace(catalog_item, cart_url=checkout_url)
        payload = invoice_from_catalog(catalog_item)
        if args.amount:
            payload["amount_usd"] = args.amount
    elif checkout_url:
        host = _HOST_RE.match(checkout_url)
        domain = (host.group(1) if host else "").replace("www.", "")
        catalog_item = find_catalog_item(domain) if domain else None
        if catalog_item:
            catalog_item = replace(catalog_item, cart_url=checkout_url)
            payload = invoice_from_catalog(catalog_item)
        else:
            payload = {
                "invoice_id": "CART-CUSTOM",
                "merchant": domain or "unknown",
                "amount": args.amount or "16.22",
                "currency": "USD",
                "description": "External checkout URL",
                "cart_url": checkout_url,
            }
            catalog_item = CatalogItem(
                merchant_door=domain or "unknown",
                product="External checkout",
                variant_id="",
                price=_to_money(args.amount or "16.22"),
                currency="USD",
                cart_url=checkout_url,
                market="",
                category="",
            )
    elif args.invoice:
        payload = Path(args.invoice)
    else:
        payload = dict(SAMPLE_INVOICE)
        if args.amount:
            payload["amount_usd"] = args.amount
    eth_override = None
    if args.sepolia_eth:
        eth_override = Decimal(str(args.sepolia_eth))
    outcome = process_invoice(
        payload,
        live=args.live,
        card_id=args.card_id,
        catalog_item=catalog_item,
        enrollment_id=args.enrollment_id,
        skip_enroll=args.skip_enroll,
        sepolia_eth_override=eth_override,
    )
    if args.json:
        json.dump(outcome.to_dict(), sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(render_run(outcome) + "\n")
    if not outcome.decision.approved:
        return 2
    if outcome.treasury is not None and not outcome.treasury.sufficient:
        return 3
    enrollment = _enrollment_result(outcome)
    if args.live and enrollment and enrollment.get("status") != "sent":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
