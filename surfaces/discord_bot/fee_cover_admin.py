"""Discord /admin sub-panel for the marketplace fee-cover campaign.

Spec: docs/superpowers/specs/2026-09-14-marketplace-fee-cover-design.md
(§Admin panel). The service is the authority: this module parses the
operator's form, calls the service, and renders its status. The admin gate
and the audit-log poster are passed in, so this module never imports admin.py
(which imports it).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from decimal import Decimal, DecimalException
from typing import Any, cast

import discord
from discord import Embed
from discord.ui import Button, Modal, TextInput, View

from surfaces._client.errors import ServiceError
from surfaces.discord_bot.bot import svc

AdminCheck = Callable[[discord.Interaction], Awaitable[bool]]
AdminLog = Callable[[Any, str], Awaitable[None]]

# Upper bounds enforced BEFORE formatting a Decimal to a string — an
# unbounded scientific-notation input like "1e999999999" must never reach
# `format(value.normalize(), "f")`, which would build a billion-digit string
# in the bot process. XRP_TOTAL_SUPPLY matches the service's own bound.
XRP_TOTAL_SUPPLY = Decimal(100_000_000_000)
MAX_DURATION_HOURS = Decimal(87_600)
# XRP's drop precision. Also closes the mirror-image hardening gap: a large
# NEGATIVE exponent like "1e-9999999999" is finite, positive, and under every
# maximum, but would still reach `format(...)` unless rejected first.
MAX_DECIMAL_PLACES = 6


def _xrp(drops: Any) -> str:
    return format((Decimal(int(drops)) / Decimal(1_000_000)).normalize(), "f")


def parse_form(
    *, coverage: str, budget: str, wallet_cap: str, min_bid: str, duration: str
) -> dict[str, str]:
    """Validate the modal's text fields into the service payload. Raises
    ValueError with an operator-readable message."""

    def number(
        label: str, raw: str, *, allow_zero: bool = False, maximum: Decimal | None = None
    ) -> str:
        try:
            value = Decimal((raw or "").strip())
        except DecimalException:
            raise ValueError(f"{label} must be a number") from None
        if not value.is_finite() or value < 0 or (value == 0 and not allow_zero):
            raise ValueError(
                f"{label} must be {'zero or more' if allow_zero else 'greater than zero'}"
            )
        if maximum is not None and value > maximum:
            raise ValueError(f"{label} must be at most {maximum}")
        # Read the exponent off the AS-PARSED value, not a normalized one:
        # normalize() is a context operation, and the default decimal
        # context's Emin (-999999) silently underflows an extreme negative
        # exponent — e.g. "1e-9999999999" — to Decimal('0'), which would
        # sail straight past a check performed on the normalized result
        # (verified: Decimal("1e-9999999999").normalize() == Decimal("0")).
        # `.as_tuple().exponent` on the raw value is an O(1) attribute read
        # regardless of magnitude — is_finite() above guarantees it's always
        # an int here, never the 'n'/'N'/'F' special-value markers — so this
        # check is both cheap and exact, and runs before any formatting.
        exponent = cast(int, value.as_tuple().exponent)
        if exponent < -MAX_DECIMAL_PLACES:
            raise ValueError(f"{label} has more than {MAX_DECIMAL_PLACES} decimal places")
        return format(value.normalize(), "f")

    out = {
        "coverage_pct": number("Coverage %", coverage, allow_zero=True, maximum=Decimal(100)),
        "budget_xrp": number("Budget", budget, maximum=XRP_TOTAL_SUPPLY),
        "wallet_cap_xrp": number("Per-wallet cap", wallet_cap, maximum=XRP_TOTAL_SUPPLY),
        "min_bid_xrp": number("Minimum bid", min_bid, allow_zero=True, maximum=XRP_TOTAL_SUPPLY),
    }
    if duration and duration.strip():
        out["duration_hours"] = number("Duration hours", duration, maximum=MAX_DURATION_HOURS)
    return out


def modal_defaults(campaign: dict[str, Any] | None) -> dict[str, str]:
    """The Update modal's prefill, read off the live campaign.

    Every field of the campaign is replaced on Update — the service takes a
    complete set of knobs — so an operator who opens the form against the
    START defaults and edits one field silently resets the other three (a 25
    XRP budget becomes 50). Prefilling makes "change one field" mean what it
    looks like. Returns {} when there is no campaign to read, and the caller
    must then not open the form at all."""
    if not campaign:
        return {}
    return {
        "coverage": f"{int(campaign['coverage_bps']) / 100:g}",
        "budget": _xrp(campaign["budget_drops"]),
        "wallet_cap": _xrp(campaign["wallet_cap_drops"]),
        "min_bid": _xrp(campaign["min_bid_drops"]),
    }


def fee_cover_status_embed(status: dict[str, Any]) -> Embed:
    embed = Embed(title="💸 Fee Cover Status", color=0x9C84EF)
    embed.add_field(
        name="Campaign",
        value=str(status.get("state", "unknown")).replace("_", " ").title(),
        inline=True,
    )
    campaign = status.get("campaign")
    if campaign:
        embed.add_field(
            name="Coverage",
            value=f"{int(campaign['coverage_bps']) / 100:g}% of broker fee",
            inline=True,
        )
        embed.add_field(
            name="Budget",
            value=f"{_xrp(status.get('committed_drops', 0))} / {_xrp(campaign['budget_drops'])} XRP committed",
            inline=True,
        )
        embed.add_field(name="Paid", value=f"{_xrp(status.get('paid_drops', 0))} XRP", inline=True)
        embed.add_field(
            name="Remaining", value=f"{_xrp(status.get('remaining_drops', 0))} XRP", inline=True
        )
        embed.add_field(
            name="Per-wallet cap",
            value=f"{_xrp(campaign['wallet_cap_drops'])} XRP / {int(campaign['wallet_window_seconds']) // 86_400}d",
            inline=True,
        )
        embed.add_field(name="Min bid", value=f"{_xrp(campaign['min_bid_drops'])} XRP", inline=True)
        ends_at = campaign.get("ends_at")
        embed.add_field(name="Ends", value=f"<t:{ends_at}:f>" if ends_at else "No end", inline=True)
    embed.add_field(name="Open promises", value=str(status.get("open_promises", 0)), inline=True)
    refunds = status.get("refunds_by_state") or {}
    embed.add_field(
        name="Refunds",
        value=" · ".join(f"{k} {v}" for k, v in sorted(refunds.items())) or "None",
        inline=False,
    )
    declines = status.get("declines_by_reason") or {}
    embed.add_field(
        name="Declines",
        value=" · ".join(f"{k} {v}" for k, v in sorted(declines.items())) or "None",
        inline=False,
    )
    top = status.get("top_wallets") or []
    embed.add_field(
        name="Top wallets",
        value="\n".join(f"`{w['wallet'][:10]}…` {_xrp(w['drops'])} XRP" for w in top) or "None",
        inline=False,
    )
    balance = status.get("issuer_balance_drops")
    embed.add_field(
        name="Issuer XRP balance",
        value=f"{_xrp(balance)} XRP" if balance is not None else "Balance lookup failed",
        inline=True,
    )
    return embed


class FeeCoverModal(Modal, title="Fee cover campaign"):
    coverage: TextInput[Any] = TextInput(
        label="Coverage (% of marketplace fee)", default="100", max_length=6
    )
    budget: TextInput[Any] = TextInput(label="Budget (XRP)", default="50", max_length=14)
    wallet_cap: TextInput[Any] = TextInput(
        label="Per-wallet cap (XRP per 30 days)", default="5", max_length=14
    )
    min_bid: TextInput[Any] = TextInput(label="Minimum bid (XRP)", default="1", max_length=14)
    duration: TextInput[Any] = TextInput(
        label="Duration hours (Start only; blank = no end)", required=False, max_length=6
    )

    def __init__(
        self,
        mode: str,
        check: AdminCheck,
        log: AdminLog,
        defaults: dict[str, str] | None = None,
    ):
        super().__init__()
        self.mode = mode
        self._check = check
        self._log = log
        # discord.py gives every Modal instance its own copy of the class-level
        # TextInputs (verified on 2.7.1), so this never leaks into the Start
        # form's defaults.
        for name, value in (defaults or {}).items():
            field = cast(TextInput[Any], getattr(self, name))
            field.default = value

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await self._check(interaction):
            return
        try:
            fields = parse_form(
                coverage=self.coverage.value,
                budget=self.budget.value,
                wallet_cap=self.wallet_cap.value,
                min_bid=self.min_bid.value,
                duration=self.duration.value if self.mode == "start" else "",
            )
        except ValueError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        actor = f"discord:{interaction.user.id}"
        try:
            if self.mode == "start":
                status = await svc.fee_cover_start(actor, **fields)
            else:
                status = await svc.fee_cover_update(actor, **fields)
        except ServiceError as e:
            logging.error("fee cover %s failed: %s", self.mode, e)
            await interaction.followup.send(
                f"❌ Fee cover {self.mode} failed: {e.message}", ephemeral=True
            )
            return
        await self._log(
            interaction.client,
            f"💸 Fee cover {self.mode} ({status.get('result')}) by {actor}: {fields}",
        )
        await interaction.followup.send(embed=fee_cover_status_embed(status), ephemeral=True)


class FeeCoverView(View):
    def __init__(self, check: AdminCheck, log: AdminLog):
        super().__init__(timeout=600)
        self._check = check
        self._log = log

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self._check(interaction)

    @discord.ui.button(label="▶️ Start", style=discord.ButtonStyle.success)
    async def start_button(self, interaction: discord.Interaction, button: Button[Any]) -> None:
        await interaction.response.send_modal(FeeCoverModal("start", self._check, self._log))

    @discord.ui.button(label="✏️ Update", style=discord.ButtonStyle.primary)
    async def update_button(self, interaction: discord.Interaction, button: Button[Any]) -> None:
        """Update replaces every knob, so the form is prefilled from the live
        campaign. It is never opened against the Start defaults: that would
        turn "change the coverage" into "reset the budget, cap and minimum
        bid". If the campaign can't be read, say so instead."""
        try:
            status = await svc.fee_cover_status()
        except ServiceError as e:
            await interaction.response.send_message(
                f"❌ Fee cover status failed, nothing to update against: {e.message}",
                ephemeral=True,
            )
            return
        defaults = modal_defaults(status.get("campaign"))
        if not defaults:
            await interaction.response.send_message(
                "❌ No campaign to update — use ▶️ Start.", ephemeral=True
            )
            return
        await interaction.response.send_modal(
            FeeCoverModal("update", self._check, self._log, defaults)
        )

    @discord.ui.button(label="⏹️ Stop", style=discord.ButtonStyle.danger)
    async def stop_button(self, interaction: discord.Interaction, button: Button[Any]) -> None:
        await interaction.response.defer(ephemeral=True)
        actor = f"discord:{interaction.user.id}"
        try:
            status = await svc.fee_cover_stop(actor)
        except ServiceError as e:
            logging.error("fee cover stop failed: %s", e)
            await interaction.followup.send(
                f"❌ Fee cover stop failed: {e.message}", ephemeral=True
            )
            return
        await self._log(
            interaction.client, f"💸 Fee cover stop ({status.get('result')}) by {actor}"
        )
        await interaction.followup.send(embed=fee_cover_status_embed(status), ephemeral=True)

    @discord.ui.button(label="🔄 Refresh", style=discord.ButtonStyle.secondary)
    async def refresh_button(self, interaction: discord.Interaction, button: Button[Any]) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            status = await svc.fee_cover_status()
        except ServiceError as e:
            await interaction.followup.send(
                f"❌ Fee cover status failed: {e.message}", ephemeral=True
            )
            return
        await interaction.followup.send(embed=fee_cover_status_embed(status), ephemeral=True)
