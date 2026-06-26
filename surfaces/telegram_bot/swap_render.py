# surfaces/telegram_bot/swap_render.py
# Pure InlineKeyboardMarkup / caption builders for the Telegram trait-swapper
# (#88). Mirrors the webapp Dressing Room's grid + trait-picker UX. No SDK,
# no XRPL, no `discord` — trivially unit-testable (returns InlineKeyboardMarkup
# / str). The view module (swap_view.py) owns conversation state and drives the
# SDK; this module only renders.
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

PAGE_SIZE = 8  # NFTs per grid page; >PAGE_SIZE paginates

# Fallback if the /api/nfts roster ever omits swappable_traits (it always sends
# them). Mirrors lfg_core.swap_meta.SWAPPABLE_TRAITS.
DEFAULT_SWAPPABLE_TRAITS = [
    "Background",
    "Back",
    "Clothing",
    "Mouth",
    "Eyebrows",
    "Eyes",
    "Head",
    "Accessory",
]


def _trait_value(nft: dict[str, Any], trait: str) -> str:
    """Look up a trait's value on a normalized NFT record."""
    for attr in nft.get("attributes", []):
        if attr.get("trait_type") == trait:
            return str(attr.get("value", "—"))
    return "—"


def nft_grid_keyboard(
    nfts: list[dict[str, Any]], *, gender: str | None = None, page: int = 0
) -> InlineKeyboardMarkup:
    """A 2-per-row grid of the user's NFTs as ``swap_pick_<nft_id>`` buttons.

    When ``gender`` is set (after the first pick), NFTs of a different body type
    are shown dimmed with a ❌ label and a no-op callback — they cannot be
    picked, enforcing the gender lock at the keyboard level. Pagination kicks in
    past ``PAGE_SIZE``; ◀/▶ nav buttons fire ``swap_page_<n>``.
    """
    start = page * PAGE_SIZE
    window = nfts[start : start + PAGE_SIZE]

    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for nft in window:
        nft_id = nft["nft_id"]
        label = f"{nft.get('name', nft_id)}"
        if gender is not None and nft.get("gender") != gender:
            # Dimmed / unpickable — a different body type. No-op callback.
            button = InlineKeyboardButton(f"❌ {label}", callback_data="swap_noop")
        else:
            button = InlineKeyboardButton(label, callback_data=f"swap_pick_{nft_id}")
        row.append(button)
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    # Pagination nav row (only when there's more than one page).
    total_pages = (len(nfts) + PAGE_SIZE - 1) // PAGE_SIZE
    if total_pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"swap_page_{page - 1}"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("Next ▶", callback_data=f"swap_page_{page + 1}"))
        if nav:
            rows.append(nav)

    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="swap_cancel")])
    return InlineKeyboardMarkup(rows)


def trait_picker_keyboard(
    nft1: dict[str, Any],
    nft2: dict[str, Any],
    swappable_traits: list[str],
    selected: set[str],
) -> InlineKeyboardMarkup:
    """One toggle button per swappable trait showing both avatars' values, e.g.
    ``☑ Eyes: Blue ↔ Green`` (``swap_trait_<TraitName>``), plus Confirm / Cancel.
    """
    rows: list[list[InlineKeyboardButton]] = []
    for trait in swappable_traits:
        box = "☑" if trait in selected else "☐"
        v1, v2 = _trait_value(nft1, trait), _trait_value(nft2, trait)
        rows.append(
            [
                InlineKeyboardButton(
                    f"{box} {trait}: {v1} ↔ {v2}", callback_data=f"swap_trait_{trait}"
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton("✅ Confirm", callback_data="swap_confirm"),
            InlineKeyboardButton("❌ Cancel", callback_data="swap_cancel"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def trait_picker_text(nft1: dict[str, Any], nft2: dict[str, Any], swap_fee: Any) -> str:
    """Help text above the trait picker, including the cost line."""
    name1 = nft1.get("name", "?")
    name2 = nft2.get("name", "?")
    return (
        f"Swapping traits between {name1} and {name2}.\n"
        f"{swap_cost_line(swap_fee)}\n\n"
        "Tap traits to swap, then Confirm."
    )


def swap_cost_line(swap_fee: Any) -> str:
    """Cost line from the /api/nfts swap_fee block (or 'free' when null)."""
    if not swap_fee:
        return "Cost: free 🎉"
    return f"Cost: {swap_fee.get('per_nft')} {swap_fee.get('pay_with')} per NFT"


def swap_payment_caption(fee_amount: str, pay_with: str) -> str:
    return (
        "💰 Swap Fee Required\n\n"
        f"Pay {fee_amount} {pay_with} to complete the swap:\n"
        "1. Scan the QR with your XRPL wallet (XUMM/Xaman)\n"
        "2. Approve the payment\n"
        "3. The swap proceeds automatically\n"
    )


def swap_result_caption(result: dict[str, Any]) -> str:
    name = result.get("name", "?")
    if result.get("modified"):
        return f"✅ {name} updated in your wallet — no action needed."
    return f"📨 Open in Xaman to claim {name}."
