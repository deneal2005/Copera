from __future__ import annotations

import re

PAGE_RANGE = re.compile(r"^\d+(?:-\d+)?(?:\s*,\s*\d+(?:-\d+)?)*$")


def selected_page_count(selection: str, total_pages: int | None) -> int | None:
    if total_pages is None or total_pages < 1:
        return None
    if not selection or selection.strip().lower() == "all":
        return total_pages
    value = selection.replace(" ", "")
    if not PAGE_RANGE.fullmatch(value):
        raise ValueError("Enter pages like 1-5, 8, 10-12, or choose all pages.")
    pages: set[int] = set()
    for segment in value.split(","):
        ends = [int(number) for number in segment.split("-")]
        start, end = ends[0], ends[-1]
        if start < 1 or end < start or end > total_pages:
            raise ValueError(f"Page selection must stay between 1 and {total_pages}.")
        pages.update(range(start, end + 1))
    return len(pages)


def _integer(value: object, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{label} must be a whole number of at least {minimum} paise.")
    return value


def quote_order(pricing: dict, documents: list[dict]) -> dict:
    """Return a server-side quote in integer paise using owner-configured rates."""
    if not documents:
        raise ValueError("Add at least one document before requesting a price.")
    rates = pricing.get("rates", {})
    line_items: list[dict] = []
    subtotal = 0
    needs_review = False
    for doc in documents:
        settings = doc.get("settings", {})
        pages = selected_page_count(settings.get("page_selection", "all"), doc.get("page_count"))
        if pages is None:
            needs_review = True
            line_items.append({"document_id": doc.get("id"), "name": doc.get("original_name", "Document"),
                               "pages": None, "copies": settings.get("copies", 1), "print_units": None,
                               "unit_rate_paise": 0, "print_paise": 0, "add_ons": [], "add_ons_paise": 0,
                               "total_paise": 0, "paper_size": settings.get("paper_size", "A4"),
                               "color": settings.get("color", "bw"), "sides": settings.get("sides", "single"),
                               "needs_review": True})
            continue
        copies = settings.get("copies", 1)
        if isinstance(copies, bool) or not isinstance(copies, int) or not 1 <= copies <= 99:
            raise ValueError("Copies must be a whole number between 1 and 99.")
        color = str(settings.get("color", "bw")).lower()
        side = str(settings.get("sides", "single")).lower()
        paper = str(settings.get("paper_size", "A4")).upper()
        if color not in ("bw", "color") or side not in ("single", "double"):
            raise ValueError("Choose supported color and side settings.")
        paper_rates = rates.get(paper)
        rate_name = f"{color}_{'duplex_per_sheet' if side == 'double' else 'simplex_per_side'}"
        if not isinstance(paper_rates, dict) or rate_name not in paper_rates:
            raise ValueError(f"The shop has not configured {paper} {color.upper()} {side}-side pricing.")
        unit_rate = _integer(paper_rates[rate_name], f"{paper} {rate_name}")
        units_per_copy = (pages + 1) // 2 if side == "double" else pages
        print_units = units_per_copy * copies
        print_amount = unit_rate * print_units
        add_ons = settings.get("add_ons", [])
        if not isinstance(add_ons, list) or any(item not in ("binding", "stapling") for item in add_ons):
            raise ValueError("This document has an unsupported additional service.")
        unique_add_ons = sorted(set(add_ons))
        extra_amount = 0
        for service in unique_add_ons:
            extra_rate = _integer(pricing.get(f"{service}_per_copy_paise", 0), service)
            extra_amount += extra_rate * copies
        amount = print_amount + extra_amount
        subtotal += amount
        line_items.append({
            "document_id": doc.get("id"), "name": doc.get("original_name", "Document"),
            "pages": pages, "copies": copies, "print_units": print_units,
            "unit_rate_paise": unit_rate, "print_paise": print_amount,
            "add_ons": unique_add_ons, "add_ons_paise": extra_amount, "total_paise": amount,
            "paper_size": paper, "color": color, "sides": side,
        })
    minimum = _integer(pricing.get("minimum_order_paise", 0), "Minimum order")
    service_fee = _integer(pricing.get("service_fee_paise", 0), "Service fee")
    base_subtotal = max(subtotal, minimum)
    discount_bps = pricing.get("discount_rate_bps", 0)
    if isinstance(discount_bps, bool) or not isinstance(discount_bps, int) or not 0 <= discount_bps <= 10000:
        raise ValueError("Discount rate must be between 0 and 100 percent.")
    discount = (base_subtotal * discount_bps + 5000) // 10000
    taxable = base_subtotal - discount + service_fee
    tax_bps = pricing.get("tax_rate_bps", 0)
    if isinstance(tax_bps, bool) or not isinstance(tax_bps, int) or not 0 <= tax_bps <= 10000:
        raise ValueError("Tax rate must be between 0 and 100 percent.")
    tax = (taxable * tax_bps + 5000) // 10000
    total = taxable + tax
    return {
        "items": line_items, "subtotal_paise": base_subtotal,
        "minimum_adjustment_paise": base_subtotal - subtotal,
        "discount_rate_bps": discount_bps, "discount_paise": discount,
        "service_fee_paise": service_fee, "tax_paise": tax,
        "total_paise": total, "needs_review": needs_review,
    }
