import discord
import logging
from discord import app_commands
import xumm 
import random
import asyncio
import helpers
from user_db import create_users_table
import nft_db

from swap_service import SwapSessionTracker, run_swap

# logging.basicConfig(level=logging.INFO)
logging.basicConfig(filename='app.log', filemode='a', format='%(asctime)s - %(name)s - %(levelname)s | %(message)s',level=logging.INFO)

class aclient(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        intents.messages = True
        super().__init__(intents=intents)
        self.synced = False
        
    async def on_ready(self):
        await self.wait_until_ready()
        # Initialize database
        create_users_table()
        # Ensure mutable column exists in LFG table
        nft_db.ensure_mutable_column()
        if not self.synced:
            await tree.sync()
            self.synced=True
        print(f'Logged in as {self.user}')

client = aclient()
tree = app_commands.CommandTree(client)

swap_tracker = SwapSessionTracker()

@tree.command(name="register", description="Verify your XRP address")
async def verify(interaction: discord.Interaction):
    sdk = xumm.XummSdk('8099edd7-72d2-48ab-9e30-c0526e7b070f','63cdb6b4-3e90-4e0d-b579-2fb92a8238cd')
    payload = {
            "TransactionType": "SignIn"
        }
    response = sdk.payload.create(payload)
    uuid = response.uuid
    qrcode = response.refs.qr_png
    link = response.next.always
    embed = discord.Embed(title="Verify your XRP address", description=f"Scan the QR code or [click here]({link}) to verify your XRP address", color=random.randint(0, 0xFFFFFF))
    embed.set_image(url=qrcode)
    await interaction.response.send_message(embed=embed, ephemeral=True)
    await asyncio.sleep(60)
    response = sdk.payload.get(uuid)
    if response.meta.signed == True:
        account = response.response.account
    else:
        await asyncio.sleep(60)
        response = sdk.payload.get(uuid)
        if response.meta.signed == True:
            account = response.response.account
        else:
            await interaction.followup.send("Verification timed out, please try again.", ephemeral=True)
            return
    helpers.register_user(interaction.user, account)
    await interaction.followup.send(f"Your XRP address has been set to {account}", ephemeral=True)

@tree.command(name='swap')
async def mint(interaction: discord.Interaction):
    await run_swap(interaction, session_tracker=swap_tracker)


@tree.command(name='tip',description='Tip brix to other users')
@app_commands.checks.has_permissions(administrator=True)
async def tip(interaction: discord.Interaction, amount: int, user: discord.Member):
    await interaction.response.defer()
    #check if user is registered
    if helpers.get_user(user) == None:
        await interaction.followup.send("User is not registered!")
        return
    
    address = helpers.get_user(user)['address']
    if address == None:
        await interaction.followup.send("User is not registered!")
        return
    loop = asyncio.get_running_loop()
    await interaction.followup.send(f"Sending tip to {user.mention}...")
    try:
        found = await loop.run_in_executor(None, helpers.send_brix, amount, address)
        if found:
            await interaction.followup.send(f"Sent {amount} brix to {user.mention}!")
    except Exception as e:
        print(e)
        await interaction.followup.send("An error occured!")
        return

@tree.command(name='disconnect',description='Disconnect the bot')
@app_commands.checks.has_permissions(administrator=True)
async def disconnect(interaction: discord.Interaction):
    await interaction.response.defer()
    await interaction.followup.send("Disconnecting...")
    await client.close()
    exit()

client.run('MTEyOTc5MDgxMTc0ODQ0MjExMg.GhGBGS.k6QWqEY5oVw2mB-Y6PS0M8ykHTk6GplIYWF4As')
