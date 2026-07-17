# surfaces/discord_bot/admin.py
# Admin panel: AdminView, modals, burn helpers. Behavior unchanged (D1=B):
# still calls lfg_core/db_helpers/rarity locally.  Relocated from main.py.
import asyncio
import logging
import sqlite3
import traceback
from typing import Any

import discord
from discord import Embed, TextStyle, app_commands
from discord.ui import Button, Modal, TextInput, View
from xrpl.clients import JsonRpcClient
from xrpl.models.transactions import NFTokenBurn
from xrpl.transaction import submit_and_wait
from xrpl.wallet import Wallet

from lfg_core import config as core_config
from lfg_core import rarity as _rarity
from lfg_core.config import JSON_RPC_URL, SOURCE_TAG
from surfaces._client.errors import ServiceError
from surfaces.discord_bot import config
from surfaces.discord_bot.bot import svc, tree

SEED = config.SEED
# JSON_RPC_URL is the network-aware endpoint from lfg_core.config (resolves via
# XRPL_NETWORK / XRPL_JSON_RPC_URL) — same source lfg_core/xrpl_ops uses, so
# admin burns hit mainnet on a mainnet deploy. (Was hardcoded to testnet;
# Greptile P1 on #79.)
ADMIN_LOG_CHANNEL_ID = config.ADMIN_LOG_CHANNEL_ID


async def burn_nft(nft_id: str) -> bool:
    """Burn an NFT using the issuer's seed"""
    try:
        logging.info(f"Attempting to burn NFT: {nft_id}")

        wallet = Wallet.from_seed(SEED)
        client = JsonRpcClient(JSON_RPC_URL)

        # Create NFTokenBurn transaction. Account is SIGNING_ACCOUNT, not the
        # seed-derived address — on mainnet SEED holds the issuer's regular-key
        # seed and the tx must run as the issuer.
        burn_tx = NFTokenBurn(
            account=core_config.SIGNING_ACCOUNT,
            nftoken_id=nft_id,
            source_tag=SOURCE_TAG,
        )

        # Submit and wait for validation
        logging.info("Submitting burn transaction...")
        response = await asyncio.to_thread(submit_and_wait, burn_tx, client, wallet)

        if response.result.get("meta", {}).get("TransactionResult") == "tesSUCCESS":
            logging.info(f"Successfully burned NFT: {nft_id}")
            return True
        else:
            logging.error(f"Failed to burn NFT. Response: {response.result}")
            return False

    except Exception as e:
        logging.error(f"Error burning NFT: {e}")
        logging.error(f"Full traceback: {traceback.format_exc()}")
        return False


class BurnNFTModal(Modal, title="Burn NFT"):
    nft_number: TextInput[Any] = TextInput(
        label="Enter NFT Number to Burn",
        placeholder="e.g., 3535",
        required=True,
        min_length=1,
        max_length=10,
    )

    reason: TextInput[Any] = TextInput(
        label="Reason for Burning",
        placeholder="Enter reason for audit purposes",
        required=True,
        style=TextStyle.paragraph,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        try:
            nft_num = int(self.nft_number.value)

            # Get NFT details from database
            conn = sqlite3.connect(core_config.DB_PATH)
            cursor = conn.cursor()

            cursor.execute(
                """
                SELECT nft_id, discord_id
                FROM LFG
                WHERE nft_number = ?
            """,
                (nft_num,),
            )

            result = cursor.fetchone()

            if not result or not result[0]:
                await interaction.followup.send(
                    f"❌ NFT #{nft_num} not found or hasn't been minted.", ephemeral=True
                )
                return

            nft_id = result[0]
            discord_id = result[1]

            # Confirm burn with a button
            confirm_embed = Embed(
                title="🔥 Confirm NFT Burn",
                description=(
                    f"Are you sure you want to burn NFT #{nft_num}?\n\n"
                    f"**NFT ID:** {nft_id}\n"
                    f"**Owner:** <@{discord_id}>\n"
                    f"**Reason:** {self.reason.value}\n\n"
                    "⚠️ This action cannot be undone!"
                ),
                color=0xFF0000,  # Red color for warning
            )

            # Create confirmation view
            view = BurnConfirmView(nft_num, nft_id, self.reason.value)
            await interaction.followup.send(embed=confirm_embed, view=view, ephemeral=True)

        except ValueError:
            await interaction.followup.send("❌ Please enter a valid NFT number.", ephemeral=True)
        except Exception as e:
            logging.error(f"Error in burn modal: {e}")
            await interaction.followup.send(
                "❌ Error processing burn request. Check logs for details.", ephemeral=True
            )
        finally:
            if "conn" in locals():
                conn.close()


class BurnConfirmView(View):
    def __init__(self, nft_number: int, nft_id: str, reason: str):
        super().__init__(timeout=60)  # 1 minute timeout
        self.nft_number = nft_number
        self.nft_id = nft_id
        self.reason = reason

    @discord.ui.button(label="Confirm Burn", style=discord.ButtonStyle.danger)
    async def confirm_burn(self, interaction: discord.Interaction, button: Button[Any]):
        await interaction.response.defer(ephemeral=True)

        try:
            # Attempt to burn the NFT
            success = await burn_nft(self.nft_id)

            if success:
                conn = sqlite3.connect(core_config.DB_PATH)
                cursor = conn.cursor()

                # Get all data from LFG table before deleting
                cursor.execute(
                    """
                    SELECT nft_number, nft_id, discord_id, created_at
                    FROM LFG
                    WHERE nft_number = ?
                """,
                    (self.nft_number,),
                )
                nft_data = cursor.fetchone()

                # Insert into burned_nfts with original data
                cursor.execute(
                    """
                    INSERT INTO burned_nfts (
                        nft_number, nft_id, discord_id, burned_by,
                        reason, original_mint_time
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                """,
                    (
                        nft_data[0],  # nft_number
                        nft_data[1],  # nft_id
                        nft_data[2],  # original discord_id
                        str(interaction.user.id),  # burned_by
                        self.reason,  # reason
                        nft_data[3],  # original_mint_time
                    ),
                )

                # Remove from LFG table
                cursor.execute("DELETE FROM LFG WHERE nft_number = ?", (self.nft_number,))

                conn.commit()

                try:
                    rarity_conn = _rarity.connect()
                    try:
                        _rarity.recalculate_rarity(rarity_conn)
                    finally:
                        rarity_conn.close()
                except Exception as e:
                    logging.error(f"rarity recalc after burn failed: {e}")

                await interaction.followup.send(
                    f"✅ Successfully burned NFT #{self.nft_number}", ephemeral=True
                )

                # Log the burn in the admin channel
                try:
                    guild = interaction.guild
                    log_channel = guild.get_channel(ADMIN_LOG_CHANNEL_ID) if guild else None
                    if isinstance(log_channel, discord.TextChannel):
                        log_embed = Embed(
                            title="🔥 NFT Burned",
                            description=(
                                f"**NFT #{self.nft_number}** was burned\n\n"
                                f"**Originally minted by:** <@{nft_data[2]}>\n"
                                f"**Burned by:** {interaction.user.mention}\n"
                                f"**Reason:** {self.reason}\n"
                                f"**NFT ID:** {self.nft_id}"
                            ),
                            color=0xFF0000,
                        )
                        await log_channel.send(embed=log_embed)
                except Exception as e:
                    logging.error(f"Failed to send burn log: {e}")

            else:
                await interaction.followup.send(
                    f"❌ Failed to burn NFT #{self.nft_number}. Check logs for details.",
                    ephemeral=True,
                )

        except Exception as e:
            logging.error(f"Error in burn confirmation: {e}")
            logging.error(f"Full traceback: {traceback.format_exc()}")
            await interaction.followup.send(
                "❌ Error processing burn confirmation. Check logs for details.", ephemeral=True
            )
        finally:
            if "conn" in locals():
                conn.close()
            self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_burn(self, interaction: discord.Interaction, button: Button[Any]):
        await interaction.response.send_message("❌ NFT burn cancelled.", ephemeral=True)
        self.stop()


async def log_admin_action(client: discord.Client, message: str) -> None:
    """Send a one-liner to the admin log channel. Best-effort — errors logged."""
    try:
        ch = client.get_channel(ADMIN_LOG_CHANNEL_ID)
        if isinstance(ch, discord.TextChannel):
            await ch.send(message)
    except Exception as e:
        logging.error(f"log_admin_action failed: {e}")


class RarityOddsModal(Modal, title="View Rarity Odds"):
    body: TextInput[Any] = TextInput(
        label="Body (* for legacy/Body Type)", default="*", max_length=20
    )
    category: TextInput[Any] = TextInput(
        label="Category (Background, Head, Body Type, ...)", max_length=30
    )

    async def on_submit(self, interaction: discord.Interaction):
        conn = _rarity.connect()
        try:
            rows = _rarity.get_odds(conn, self.body.value.strip(), self.category.value.strip())
        finally:
            conn.close()
        if not rows:
            await interaction.response.send_message(
                "No rarity rows for that body/category.", ephemeral=True
            )
            return
        lines = [
            f"`{t:24.24s}` n={c:<5d} {s:5.1f}% w={w:.4f}  {st}" for t, c, s, w, st in rows[:25]
        ]
        embed = Embed(
            title=f"Odds — {self.body.value} / {self.category.value}",
            description="\n".join(lines),
            color=0x00FF00,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class RarityBoostModal(Modal, title="Arm Trait Boost"):
    body: TextInput[Any] = TextInput(label="Body (* for legacy)", default="*", max_length=20)
    category: TextInput[Any] = TextInput(label="Category", max_length=30)
    trait: TextInput[Any] = TextInput(label="Trait value", max_length=60)
    initial: TextInput[Any] = TextInput(label="Boost multiplier", default="7", max_length=5)
    confirm: TextInput[Any] = TextInput(
        label="Type CONFIRM if trait already has mints", required=False, max_length=10
    )

    async def on_submit(self, interaction: discord.Interaction):
        from lfg_core import config as _cfg

        conn = _rarity.connect()
        try:
            row = conn.execute(
                """SELECT live_count FROM trait_rarity WHERE network=?
                   AND body=? AND category=? AND trait=?""",
                (
                    _cfg.XRPL_NETWORK,
                    self.body.value.strip(),
                    self.category.value.strip(),
                    self.trait.value.strip(),
                ),
            ).fetchone()
            if row is None:
                await interaction.response.send_message(
                    "Unknown trait — it must exist in the rarity table (mint once or run seed).",
                    ephemeral=True,
                )
                return
            if row[0] > 0 and self.confirm.value.strip() != "CONFIRM":
                await interaction.response.send_message(
                    f"'{self.trait.value}' already has {row[0]} mints. "
                    "Re-submit with CONFIRM to arm a comeback boost.",
                    ephemeral=True,
                )
                return
            _rarity.arm_boost(
                conn,
                self.body.value.strip(),
                self.category.value.strip(),
                self.trait.value.strip(),
                boost_initial=float(self.initial.value),
            )
        finally:
            conn.close()
        await interaction.response.send_message(
            f"Boost armed for **{self.trait.value}** "
            f"({self.initial.value}×, dormant until first organic mint).",
            ephemeral=True,
        )
        await log_admin_action(
            interaction.client,
            f"🎚️ Boost armed by {interaction.user}: "
            f"{self.body.value}/{self.category.value}/{self.trait.value} "
            f"@ {self.initial.value}x",
        )


class RarityDisableModal(Modal, title="Toggle Trait"):
    body: TextInput[Any] = TextInput(label="Body (* for legacy)", default="*", max_length=20)
    category: TextInput[Any] = TextInput(label="Category", max_length=30)
    trait: TextInput[Any] = TextInput(label="Trait value", max_length=60)
    action: TextInput[Any] = TextInput(label="Action (DISABLE or ENABLE)", max_length=10)

    async def on_submit(self, interaction: discord.Interaction):
        val = self.action.value.strip().upper()
        if val not in ("DISABLE", "ENABLE"):
            await interaction.response.send_message(
                "Action must be exactly DISABLE or ENABLE.", ephemeral=True
            )
            return
        enabled = val == "ENABLE"
        conn = _rarity.connect()
        try:
            _rarity.set_enabled(
                conn,
                self.body.value.strip(),
                self.category.value.strip(),
                self.trait.value.strip(),
                enabled,
            )
        finally:
            conn.close()
        state = "enabled" if enabled else "disabled"
        await interaction.response.send_message(f"**{self.trait.value}** {state}.", ephemeral=True)
        await log_admin_action(
            interaction.client,
            f"🚫 Trait {state} by {interaction.user}: "
            f"{self.body.value}/{self.category.value}/{self.trait.value}",
        )


def _x_toggle_label(paused: bool) -> str:
    """Button label mirrors the *next* action, not the current state name."""
    return "▶️ Resume X posting" if paused else "⏸️ Pause X posting"


def _x_status_embed(status: dict[str, Any]) -> Embed:
    embed = Embed(title="📡 X Posting Status", color=0x9C84EF)
    embed.add_field(
        name="Posting", value="⏸️ Paused" if status["paused"] else "▶️ Running", inline=True
    )
    embed.add_field(
        name="This Month", value=f"{status['month_posts']} / {status['budget']}", inline=True
    )
    embed.add_field(
        name="Enabled", value="✅ Yes" if status["enabled"] else "❌ No (dark)", inline=True
    )
    return embed


# Add burn button to AdminView
class AdminView(View):
    def __init__(self):
        super().__init__(timeout=600)  # 10 minute timeout
        logging.info("Initializing AdminView")

    @discord.ui.button(label="📊 View Stats", style=discord.ButtonStyle.primary)
    async def stats_button(self, interaction: discord.Interaction, button: Button[Any]):
        await interaction.response.defer(ephemeral=True)
        logging.info(f"Stats button pressed by {interaction.user}")

        try:
            conn = sqlite3.connect(core_config.DB_PATH)
            cursor = conn.cursor()

            # Get total NFTs minted
            cursor.execute("SELECT COUNT(*) FROM LFG WHERE nft_id IS NOT NULL")
            total_minted = cursor.fetchone()[0]

            # Get total unique users
            cursor.execute(
                "SELECT COUNT(DISTINCT discord_id) FROM LFG WHERE discord_id IS NOT NULL"
            )
            unique_users = cursor.fetchone()[0]

            # Get recent mints
            cursor.execute("""
                SELECT nft_number, discord_id, created_at
                FROM LFG
                WHERE nft_id IS NOT NULL
                ORDER BY created_at DESC
                LIMIT 5
            """)
            recent_mints = cursor.fetchall()

            # Get burned NFTs count
            cursor.execute("SELECT COUNT(*) FROM burned_nfts")
            burned_count = cursor.fetchone()[0]

            stats_embed = Embed(title="📊 Minting Statistics", color=0x9C84EF)

            stats_embed.add_field(name="Total NFTs Minted", value=str(total_minted), inline=True)

            stats_embed.add_field(name="Unique Users", value=str(unique_users), inline=True)

            stats_embed.add_field(name="Burned NFTs", value=str(burned_count), inline=True)

            if recent_mints:
                recent_mints_text = "\n".join(
                    f"#{num} by <@{uid}> on {date[:10]}" for num, uid, date in recent_mints
                )
                stats_embed.add_field(name="Recent Mints", value=recent_mints_text, inline=False)

            await interaction.followup.send(embed=stats_embed, ephemeral=True)

        except Exception as e:
            logging.error(f"Error in stats button: {e}")
            await interaction.followup.send(
                "❌ Error retrieving statistics. Check logs for details.", ephemeral=True
            )
        finally:
            if "conn" in locals():
                conn.close()

    @discord.ui.button(label="🔍 Lookup NFT", style=discord.ButtonStyle.primary)
    async def lookup_button(self, interaction: discord.Interaction, button: Button[Any]):
        logging.info(f"Lookup button pressed by {interaction.user}")
        await interaction.response.send_modal(NFTLookupModal())

    @discord.ui.button(label="🔥 Burn NFT", style=discord.ButtonStyle.danger)
    async def burn_button(self, interaction: discord.Interaction, button: Button[Any]):
        logging.info(f"Burn button pressed by {interaction.user}")
        await interaction.response.send_modal(BurnNFTModal())

    @discord.ui.button(label="View Odds", style=discord.ButtonStyle.secondary, emoji="🎲", row=1)
    async def view_odds(self, interaction: discord.Interaction, button: Button[Any]):
        await interaction.response.send_modal(RarityOddsModal())

    @discord.ui.button(label="Boost Trait", style=discord.ButtonStyle.primary, emoji="🚀", row=1)
    async def boost_trait(self, interaction: discord.Interaction, button: Button[Any]):
        await interaction.response.send_modal(RarityBoostModal())

    @discord.ui.button(label="Toggle Trait", style=discord.ButtonStyle.danger, emoji="🚫", row=1)
    async def toggle_trait(self, interaction: discord.Interaction, button: Button[Any]):
        await interaction.response.send_modal(RarityDisableModal())

    @discord.ui.button(label="⏸️ Pause X posting", style=discord.ButtonStyle.secondary, row=2)
    async def x_toggle_button(self, interaction: discord.Interaction, button: Button[Any]):
        await interaction.response.defer(ephemeral=True)
        logging.info(f"X posting toggle pressed by {interaction.user}")

        try:
            status = await svc.x_status()
            if status["paused"]:
                result = await svc.x_resume()
            else:
                result = await svc.x_pause()
            status["paused"] = result["paused"]
        except ServiceError as e:
            logging.error(f"X posting toggle failed: {e}")
            await interaction.followup.send(
                f"❌ Failed to toggle X posting: {e.message}", ephemeral=True
            )
            return

        button.label = _x_toggle_label(status["paused"])
        await interaction.followup.send(embed=_x_status_embed(status), ephemeral=True)

        action = "paused" if status["paused"] else "resumed"
        await log_admin_action(interaction.client, f"📡 X posting {action} by {interaction.user}")


@tree.command(name="admin", description="Admin control panel for NFT management")
@app_commands.checks.has_permissions(administrator=True)  # Add explicit permission check
async def admin_command(interaction: discord.Interaction):
    """Admin control panel for NFT management"""

    logging.info(f"Admin command triggered by {interaction.user}")

    # Create the admin panel embed
    embed = Embed(
        title="🔧 Admin Control Panel",
        description=(
            "Welcome to the NFT Admin Panel!\n\n"
            "**Available Actions:**\n"
            "• 📊 View Stats - Check minting statistics\n"
            "• 🔍 Lookup NFT - View details of specific NFT\n"
            "• 🔥 Burn NFT - Burn a specific NFT"
        ),
        color=0x9C84EF,
    )

    embed.set_footer(text="Admin panel will timeout after 10 minutes")

    # Create view with admin buttons
    view = AdminView()
    # Best-effort: reflect the current pause/resume state on the button label
    # when the panel opens. A failed/dark lfg_service must not block the
    # whole admin panel — fall back to the view's default label.
    try:
        status = await svc.x_status()
        view.x_toggle_button.label = _x_toggle_label(status["paused"])
    except ServiceError as e:
        logging.warning(f"x_status failed while building admin panel: {e}")

    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class NFTLookupModal(Modal, title="NFT Lookup"):
    nft_number: TextInput[Any] = TextInput(
        label="Enter NFT Number",
        placeholder="e.g., 3535",
        required=True,
        min_length=1,
        max_length=10,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        logging.info(
            f"NFT lookup requested for number {self.nft_number.value} by {interaction.user}"
        )

        try:
            nft_num = int(self.nft_number.value)
            conn = sqlite3.connect(core_config.DB_PATH)
            cursor = conn.cursor()

            # Check main NFT table
            cursor.execute(
                """
                SELECT nft_number, nft_id, discord_id, created_at
                FROM LFG
                WHERE nft_number = ?
            """,
                (nft_num,),
            )

            result = cursor.fetchone()

            # Check if NFT was burned
            cursor.execute(
                """
                SELECT burned_by, reason, burned_at
                FROM burned_nfts
                WHERE nft_number = ?
            """,
                (nft_num,),
            )

            burn_info = cursor.fetchone()

            if result:
                nft_embed = Embed(title=f"🔍 NFT #{result[0]} Details", color=0x9C84EF)

                nft_embed.add_field(name="NFT ID", value=result[1] or "Not minted", inline=True)

                if result[2]:  # If discord_id exists
                    nft_embed.add_field(name="Minted By", value=f"<@{result[2]}>", inline=True)

                nft_embed.add_field(
                    name="Minted On", value=result[3][:10] if result[3] else "N/A", inline=True
                )

                # Add burn information if it exists
                if burn_info:
                    nft_embed.add_field(
                        name="🔥 Burn Status",
                        value=(
                            f"Burned by: <@{burn_info[0]}>\n"
                            f"Reason: {burn_info[1]}\n"
                            f"Date: {burn_info[2][:10]}"
                        ),
                        inline=False,
                    )

                await interaction.followup.send(embed=nft_embed, ephemeral=True)
            else:
                await interaction.followup.send(
                    f"❌ NFT #{nft_num} not found in database.", ephemeral=True
                )

        except ValueError:
            await interaction.followup.send("❌ Please enter a valid NFT number.", ephemeral=True)
        except Exception as e:
            logging.error(f"Error in NFT lookup: {e}")
            await interaction.followup.send(
                "❌ Error looking up NFT. Check logs for details.", ephemeral=True
            )
        finally:
            if "conn" in locals():
                conn.close()
