import discord
import logging
from discord import app_commands
import xumm 
import random
import asyncio
import helpers
import pprint
from PIL import Image
import requests
import os
import json
import time
import datetime

logging.basicConfig(level=logging.INFO)

class aclient(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        intents.messages = True
        super().__init__(intents=intents)
        self.synced = False
        
    async def on_ready(self):
        await self.wait_until_ready()
        if not self.synced:
            await tree.sync()
            self.synced=True
        print(f'Logged in as {self.user}')

client = aclient()
tree = app_commands.CommandTree(client)

client.currently_users = {}

# Constants
TRAITS = ['Background', 'Clothing', 'Mouth', 'Eyebrows', 'Eyes', 'Head', 'Accessory']
BACK = ["Angel Wings","Angel Wings Open"]
NFT_URL_PREFIX = "https://cloudflare-ipfs.com/ipfs/"
MAX_ITEMS = 20

class nftSwapper:
    def __init__(self, nft1, nft2):
        self.nft1 = nft1
        self.nft2 = nft2
        self.oldTraits1 = []
        self.oldTraits2 = []
        self.newTraits1 = []
        self.newTraits2 = []
        self.nft1VidFlag = False
        self.nft2VidFlag = False
        self.gender = None
        self.burnt1 = 0
        self.burnt2 = 0
        self.msg = None
        self.confirmation = None

    def getAttributes(self,nft,nftdata): #returns all attributes of an nft
        for n in nftdata:
            if n['name'] == nft:
                return n['attributes']
        return None
    
    def getAttrValue(self,attributes,attr): #returns value of an attribute
        for a in attributes:
            if a['trait_type'] == attr:
                return a['value']
        return None

    def genderCheck(self):
        nft1Gender = None
        nft2Gender = None
        for n in self.oldTraits1:
            if n['trait_type'] == 'Body':
                if 'Straight' in n['value']:
                    nft1Gender = 'male'
                elif 'Curved' in n['value']:
                    nft1Gender = 'female'
                else:
                    nft1Gender = 'skeleton'
        for n in self.oldTraits2:
            if n['trait_type'] == 'Body':
                if 'Straight' in n['value']:
                    nft2Gender = 'male'
                elif 'Curved' in n['value']:
                    nft2Gender = 'female'
                else:
                    nft2Gender = 'skeleton'
        if nft1Gender != nft2Gender:
            return False
        self.gender = nft1Gender
        return True

def save_files(interaction, img, img2):
    response = requests.get(img)
    file = open(f"{interaction.user.id}+nft1.png", "wb")
    file.write(response.content)
    file.close()
    response = requests.get(img2)
    file = open(f"{interaction.user.id}+nft2.png", "wb")
    file.write(response.content)
    file.close()

def collage_files(interaction, im1, im2, new_im):
    new_im.paste(im1, (0, 0))
    new_im.paste(im2, (512, 0))
    new_im.save(f"{interaction.user.id}+nft.png")

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
  try:
    if interaction.user.id != 739375301578194944:
        await interaction.followup.send("We are on a maintenance break, please try again later!")
        return
    wallet = helpers.get_user(interaction.user)['address']
    await interaction.response.defer(ephemeral=True)
    user = interaction.user
    timeNow = int(time.time())
    timeLastUsed = client.currently_users.get(user.id,0)
    print(timeNow)
    print(timeLastUsed)
    pprint.pprint(client.currently_users)
    if timeLastUsed != 0:
        print('Checking time')
        if timeNow - timeLastUsed > 60 * 8: #8 minutes past since last use, clear the user
            print('Clearing user')
            client.currently_users.pop(user.id)
        else:
            print('User is still using')
            await interaction.followup.send("You are already using the bot!", ephemeral=True)
            return
    else:
        print('User is not using')
        if user.id in client.currently_users:
            client.currently_users.pop(user.id)
    nfts,nftIds = await helpers.get_nfts(wallet,"rLfgoMintj3KBcs4s2XKtquvDwEte2kYfJ")
    nftsData = []
    for nft in nfts:
        data = await helpers.get_nft_metadata(nft)
        if data == None:
            continue
        if '#' not in data['name']:
            continue
        # check the number in the name, if its between 1-707
        num = int(data['name'].split("#")[1])
        if num < 1 or num > 707:
            continue
        data['nftid'] = nftIds[nfts.index(nft)]
        nftsData.append(data)
    if len(nftsData) == 0:
        await interaction.followup.send("No NFTs found in wallet")
        return

    #sort according to number present in name, eg: Let's Effing Go! #1
    nftsData.sort(key=lambda x: int(x['name'].split("#")[1]))

    pages = []
    page = []
    for nft in nftsData:
        if len(page) == MAX_ITEMS:
            pages.append(page)
            page = []
        page.append(nft)
    if len(page) > 0:
        pages.append(page)
    embeds = []
    for page in pages:
        embed = discord.Embed(title=f"Page {pages.index(page)+1}/{len(pages)}", description=f"Showing {len(page)} NFTs", color=random.randint(0, 0xFFFFFF))
        for nft in page:
            embed.add_field(name="Nft", value=nft['name'])
        embeds.append(embed)

    swap = nftSwapper(None, None)
    selectPage = discord.ui.Select(
        placeholder="Select a page",
        options=[discord.SelectOption(label=f"Page {pages.index(page)+1}", value=f"{pages.index(page)}") for page in pages],
        min_values=1,
        max_values=1
    )

    selectNft = discord.ui.Select(
        placeholder="Select an NFT",
        options=[discord.SelectOption(label=nft['name'], value=nft['name']) for nft in pages[0]],
        min_values=1,
        max_values=1
    )

    selectTrait = discord.ui.Select(
        placeholder="Select traits to swap",
        # options=[discord.SelectOption(label=trait, value=trait) for trait in TRAITS],
        #skip body trait
        options=[discord.SelectOption(label=trait, value=trait) for trait in TRAITS if trait != 'Body'],
        min_values=1,
        max_values=len(TRAITS)
    )

    async def selectPageCallback(interaction: discord.Interaction):
        if interaction.user != user:
            return
        await interaction.response.defer()
        item = int(selectPage.values[0])
        selectNft.options = [discord.SelectOption(label=nft['name'], value=nft['name']) for nft in pages[item]]
        # await interaction.message.edit(embed=embeds[item], view=view)
        await interaction.edit_original_response(embed=embeds[item], view=view)

    async def selectNftCallback(interaction: discord.Interaction):
        if interaction.user != user:
            return
        if swap.nft1 != None and swap.nft2 != None:
            await interaction.response.send_message("Already selected two NFTs", ephemeral=True)
            return
        await interaction.response.defer()
        nft = selectNft.values[0]
        if swap.nft1 == None:
            swap.nft1 = nft
            swap.oldTraits1 = swap.getAttributes(swap.nft1,nftsData)
            m = await interaction.channel.send(f"{interaction.user.mention}\nSelected {swap.nft1}, now select another NFT", delete_after=30)
        else:
            if nft == swap.nft1:
                await interaction.followup.send("Cannot select same NFT twice", ephemeral=True)
                return
            swap.nft2 = nft
            swap.oldTraits2 = swap.getAttributes(swap.nft2,nftsData)
            genderCheck = swap.genderCheck()
            if genderCheck == False:
                await interaction.followup.send(f"NFTs do not have same gender!")
                return
            selectTrait.options = [discord.SelectOption(label=trait,
                                                        description=f"{swap.getAttrValue(swap.oldTraits1,trait)} <-> {swap.getAttrValue(swap.oldTraits2,trait)}",
                                                        value=f"{trait}") for trait in TRAITS]
            embed = discord.Embed(title="NFTs selected",
                    description=f"You have selected {swap.nft1} and {swap.nft2}\nNow select the traits which you want to swap!",
                    color=random.randint(0, 0xFFFFFF))
            img,imgg = None,None
            burnt1,burnt2 = 0,0
            for nft in nftsData:
                if nft['name'] == swap.nft1:
                    img = nft['image']
                    # burnt1 = nft['burnCount']
                    if 'burnCount' in nft:
                        burnt1 = nft['burnCount']
                        swap.burnt1 = int(burnt1)
                    if 'video' in nft:
                        swap.nft1VidFlag = True
                elif nft['name'] == swap.nft2:
                    imgg = nft['image']
                    # burnt2 = nft['burnCount']
                    if 'burnCount' in nft:
                        burnt2 = nft['burnCount']
                        swap.burnt2 = int(burnt2)
                    if 'video' in nft:
                        swap.nft2VidFlag = True
            
            embed.add_field(name=f"NFT 1",value=f"{swap.nft1}\nBurn Count: {burnt1}")
            embed.add_field(name=f"NFT 2",value=f"{swap.nft2}\nBurn Count: {burnt2}")

            img = img.replace("ipfs://","https://cloudflare-ipfs.com/ipfs/")
            imgg = imgg.replace("ipfs://","https://cloudflare-ipfs.com/ipfs/")

            save_files(interaction, img, imgg)
            im = Image.open(f"{interaction.user.id}+nft1.png").resize((512,512))
            imm = Image.open(f"{interaction.user.id}+nft2.png").resize((512,512))
            new_im = Image.new('RGB', (1024,512))
            collage_files(interaction, im, imm, new_im)
            file = discord.File(f"{interaction.user.id}+nft.png", filename="nft.png")
            embed.set_image(url="attachment://nft.png")
            view = discord.ui.View()
            view.add_item(selectTrait)
            # await interaction.message.edit(embed=embed, view=view,attachments=[file])
            await interaction.edit_original_response(embed=embed, view=view,attachments=[file])

    async def selectTraitCallback(interaction: discord.Interaction):
        if user.id in client.currently_users:
            await interaction.response.send_message("You are already using the bot!", ephemeral=True)
            return
        client.currently_users[user.id] = int(time.time())
        # if user.id == 739375301578194944:
        #     return
        if interaction.user.id != user.id:
            return
        if swap.nft1 == None or swap.nft2 == None:
            await interaction.response.send_message("Select two NFTs first", ephemeral=True)
            return
        print("Trait selected")
        await interaction.response.defer()
        # await interaction.message.edit(content="Crafting new NFTs...", embed=None, view=None,attachments=[])
        # await interaction.edit_original_response(content="Crafting new NFTs... It will take a few minutes to process this request!", embed=None, view=None,attachments=[])
        traits = selectTrait.values
        #confirm if user wants to swap
        print("Confirming swap")
        embed = discord.Embed(title="Confirm swap", description=f"Are you sure you want to swap these traits?\n{swap.nft1} <-> {swap.nft2}", color=random.randint(0, 0xFFFFFF))
        for trait in traits:
            embed.add_field(name=trait, value=f"{swap.getAttrValue(swap.oldTraits1,trait)} <-> {swap.getAttrValue(swap.oldTraits2,trait)}")
        embed.set_footer(text="Requested by "+user.name, icon_url=user.avatar.url)
        print("Sending confirmation")
        viewww = discord.ui.View()
        confirm = discord.ui.Button(label="Confirm", style=discord.ButtonStyle.green)
        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.red)

        async def confirmCallback(interaction: discord.Interaction):
            if interaction.user != user:
                return
            swap.confirmation = True
            # await interaction.response.send_message(f"Confirmed swap!", ephemeral=True)
            await interaction.response.defer()
            await interaction.edit_original_response(content="Crafting new NFTs... It will take a few minutes to process this request!",embed=None,view=None)

        async def cancelCallback(interaction: discord.Interaction):
            if interaction.user != user:
                return
            swap.confirmation = False
            client.currently_users.pop(user.id)
            # await interaction.response.send_message(f"Cancelled swap!", ephemeral=True)
            await interaction.response.defer()
            await interaction.edit_original_response(content="Cancelled swap!",embed=None,view=None)
            return

        confirm.callback = confirmCallback
        cancel.callback = cancelCallback
        viewww.add_item(confirm)
        viewww.add_item(cancel)
        await interaction.edit_original_response(embed=embed, view=viewww,attachments=[])
        await asyncio.sleep(30)
        if swap.confirmation == False:
            return
        elif swap.confirmation == None:
            await asyncio.sleep(30)
            if swap.confirmation == False:
                return
            elif swap.confirmation == None:
                await interaction.followup.send("Confirmation timed out, please try again.", ephemeral=True)
                return
            
        #swap video flag if background trait is selected
        if 'Background' in traits:
            swap.nft1VidFlag,swap.nft2VidFlag = swap.nft2VidFlag,swap.nft1VidFlag

        #swap traits
        for trait in traits:
            for n in swap.oldTraits1:
                if n['trait_type'] == trait:
                    swap.newTraits2.append(n)
                    break
            for n in swap.oldTraits2:
                if n['trait_type'] == trait:
                    swap.newTraits1.append(n)
                    break
        #now append the rest of the traits
        for n in swap.oldTraits1:
            if n['trait_type'] not in traits:
                swap.newTraits1.append(n)
        for n in swap.oldTraits2:
            if n['trait_type'] not in traits:
                swap.newTraits2.append(n)
        desired_order = ['Background', 'Body', 'Clothing', 'Mouth', 'Eyebrows', 'Eyes', 'Head', 'Accessory']
        swap.newTraits1.sort(key=lambda x: desired_order.index(x['trait_type']))
        swap.newTraits2.sort(key=lambda x: desired_order.index(x['trait_type']))

        im1,im2 = None,None
        im3 = Image.new('RGB', (1024,512))
        if swap.nft1VidFlag == False and swap.nft2VidFlag == False:
            pathToNft1, pathToNft2 = helpers.makeNft(swap.newTraits1,swap.newTraits2,swap.gender,wallet)
            im1 = Image.open(pathToNft1).resize((512,512))
            im2 = Image.open(pathToNft2).resize((512,512))
            nft1Link, nft1cid = await helpers.upload_image_to_nft_storage(pathToNft1)
            nft2Link, nft2cid = await helpers.upload_image_to_nft_storage(pathToNft2)
            helpers.cleanup(wallet)
        else:
            if swap.nft1VidFlag == True and swap.nft2VidFlag == False:
                pathToVid = None
                for n in swap.newTraits1:
                    if n['trait_type'] == 'Background':
                        pathToVid = f"{swap.gender}/Background/{n['value']}.mp4"
                        break
                loop = asyncio.get_event_loop()
                vidPath = await loop.run_in_executor(None,helpers.makeNftVideo, pathToVid,swap.newTraits1,swap.gender,wallet)
                if vidPath:
                    vidPath = await helpers.combineAudio(nft_video=vidPath,userWallet=wallet)
                    pathToNft2 = helpers.makeNftSingle(swap.newTraits2,swap.gender,wallet)
                    im2 = Image.open(pathToNft2).resize((512,512))
                    nft1VidIpfs, nft1VidCid = await helpers.upload_image_to_nft_storage(vidPath)
                    nft2Link, nft2cid = await helpers.upload_image_to_nft_storage(pathToNft2)
                    nft1im = await helpers.extractFirstFrame(vidPath,f"{vidPath.split('.')[0]}.png")
                    im1 = Image.open(nft1im).resize((512,512))
                    nft1Link, nft1cid = await helpers.upload_image_to_nft_storage(nft1im)
                    helpers.cleanupVid(wallet)
                    helpers.cleanup(wallet)
            elif swap.nft1VidFlag == False and swap.nft2VidFlag == True:
                pathToVid = None
                for n in swap.newTraits2:
                    if n['trait_type'] == 'Background':
                        pathToVid = f"{swap.gender}/Background/{n['value']}.mp4"
                        break
                loop = asyncio.get_event_loop()
                vidPath = await loop.run_in_executor(None,helpers.makeNftVideo,pathToVid,swap.newTraits2,swap.gender,wallet)
                if vidPath:
                    vidPath = await helpers.combineAudio(nft_video=vidPath,userWallet=wallet)
                    pathToNft1 = helpers.makeNftSingle(swap.newTraits1,swap.gender,wallet)
                    im1 = Image.open(pathToNft1).resize((512,512))
                    nft2VidIpfs, nft2VidCid = await helpers.upload_image_to_nft_storage(vidPath)
                    nft1Link, nft1cid = await helpers.upload_image_to_nft_storage(pathToNft1)
                    nft2im = await helpers.extractFirstFrame(vidPath,f"{vidPath.split('.')[0]}.png")
                    im2 = Image.open(nft2im).resize((512,512))
                    nft2Link, nft2cid = await helpers.upload_image_to_nft_storage(nft2im)
                    helpers.cleanupVid(wallet)
                    helpers.cleanup(wallet)
            else:
                pathToVid = None
                pathToVid2 = None
                for n in swap.newTraits1:
                    if n['trait_type'] == 'Background':
                        pathToVid = f"{swap.gender}/Background/{n['value']}.mp4"
                        break
                for n in swap.newTraits2:
                    if n['trait_type'] == 'Background':
                        pathToVid2 = f"{swap.gender}/Background/{n['value']}.mp4"
                        break
                loop = asyncio.get_event_loop()
                vidPath1 = await loop.run_in_executor(None,helpers.makeNftVideo,pathToVid,swap.newTraits1,swap.gender,wallet)
                if vidPath1:
                    vidPath1 = await helpers.combineAudio(nft_video=vidPath1,userWallet=wallet)
                    nft1VidIpfs, nft1VidCid = await helpers.upload_image_to_nft_storage(vidPath1)
                    nft1im = await helpers.extractFirstFrame(vidPath1,f"{vidPath1.split('.')[0]}.png")
                    im1 = Image.open(nft1im).resize((512,512))
                    nft1Link, nft1cid = await helpers.upload_image_to_nft_storage(nft1im)
                    vidPath2 = await loop.run_in_executor(None,helpers.makeNftVideo,pathToVid2,swap.newTraits2,swap.gender,wallet)
                    if vidPath2:
                        vidPath2 = await helpers.combineAudio(nft_video=vidPath2,userWallet=wallet)
                        nft2VidIpfs, nft2VidCid = await helpers.upload_image_to_nft_storage(vidPath2)
                        nft2im = await helpers.extractFirstFrame(vidPath2,f"{vidPath2.split('.')[0]}.png")
                        im2 = Image.open(nft2im).resize((512,512))
                        nft2Link, nft2cid = await helpers.upload_image_to_nft_storage(nft2im)
                        helpers.cleanupVid(wallet)
                        helpers.cleanup(wallet)

        collage_files(interaction, im1, im2, im3)
        file = discord.File(f"{interaction.user.id}+nft.png", filename="nftCol.png")
        embed = discord.Embed(title="New NFTs crafted!", description=f"New NFTs have been crafted!\n\n**NFT 1**\n{swap.nft1}\n\n**NFT 2**\n{swap.nft2}", color=random.randint(0, 0xFFFFFF))
        embed.set_image(url="attachment://nftCol.png")
        await interaction.channel.send(content=f"{interaction.user.mention}",embed=embed, file=file,delete_after=60*5)

        #also send the new metadata for both nfts
        template = {
            "schema": "ipfs://QmNpi8rcXEkohca8iXu7zysKKSJYqCvBJn3xJwga8jXqWU",
            "name": "NFT",
            "description": "Season 1",
            "image": "image.png",
            "video": "video.mp4",
            "external_link": "https://letseffinggo.com",
            "collection": {
                "name": "Let's Effing Go!",
                "family": "Season 1",
                "image": "ipfs://bafkreidkycr647vfstujnzt3r74w5edsiacwpt5ik5jd5f6qsp7rxeokpa"
            },
            "edition": 1,
            "burnCount": 0,
            "attributes": []
        }
        nft1ToSend, nft2ToSend = template.copy(), template.copy()
        nft1ToSend['name'] = swap.nft1
        nft2ToSend['name'] = swap.nft2
        nft1ToSend['attributes'] = swap.newTraits1
        nft2ToSend['attributes'] = swap.newTraits2
        if swap.nft1VidFlag == True:
            nft1ToSend['video'] = f"ipfs://{nft1VidCid}"
        else:
            nft1ToSend.pop('video')
        if swap.nft2VidFlag == True:
            nft2ToSend['video'] = f"ipfs://{nft2VidCid}"
        else:
            nft2ToSend.pop('video')
        nft1ToSend['image'], nft2ToSend['image'] = f"ipfs://{nft1cid}", f"ipfs://{nft2cid}"
        nft1ToSend['burnCount'], nft2ToSend['burnCount'] = swap.burnt1 + 1, swap.burnt2 + 1
        saveFile = open(f"{interaction.user.id}+nft1.json", "w")
        saveFile.write(json.dumps(nft1ToSend, indent=2))
        saveFile.close()
        saveFile = open(f"{interaction.user.id}+nft2.json", "w")
        saveFile.write(json.dumps(nft2ToSend, indent=2))
        saveFile.close()
        #now save to ipfs
        nft1JsonLink, nft1JsonCid = await helpers.upload_image_to_nft_storage(f"{interaction.user.id}+nft1.json")
        nft2JsonLink, nft2JsonCid = await helpers.upload_image_to_nft_storage(f"{interaction.user.id}+nft2.json")
        # file1 = discord.File(f"{interaction.user.id}+nft1.json", filename="nft1.json")
        # file2 = discord.File(f"{interaction.user.id}+nft2.json", filename="nft2.json")
        # msg = await interaction.followup.send(content=f"Links:\nNFT 1: {nft1JsonLink}\nNFT 2: {nft2JsonLink}", files=[file1,file2], ephemeral=True)
        # await interaction.followup.send("Burning old NFTs...", ephemeral=True)
        nft1id = None
        nft2id = None
        for n in nftsData:
            if n['name'] == swap.nft1:
                nft1id = n['nftid']
            elif n['name'] == swap.nft2:
                nft2id = n['nftid']
        loop = asyncio.get_event_loop()
        burn1 = await loop.run_in_executor(None,helpers.burn_nft,nft1id,wallet)
        burn2 = await loop.run_in_executor(None,helpers.burn_nft,nft2id,wallet)
        if burn1 and burn2:
            #now remint the new nfts
            nft1 = await loop.run_in_executor(None,helpers.mint_nft,f"ipfs://{nft1JsonCid}",1760,"rLfgoMintj3KBcs4s2XKtquvDwEte2kYfJ")
            nft2 = await loop.run_in_executor(None,helpers.mint_nft,f"ipfs://{nft2JsonCid}",1760,"rLfgoMintj3KBcs4s2XKtquvDwEte2kYfJ")
            if nft1 and nft2:
                # nftoken1 = helpers.get_nft_token(nft1)
                nftoken1 = await loop.run_in_executor(None,helpers.get_nft_token,nft1)
                # nftoken2 = helpers.get_nft_token(nft2) 
                nftoken2 = await loop.run_in_executor(None,helpers.get_nft_token,nft2)
                if nftoken1 and nftoken2:
                    offer1 = await loop.run_in_executor(None,helpers.create_nft_offer,nftoken1,wallet)
                    offer2 = await loop.run_in_executor(None,helpers.create_nft_offer,nftoken2,wallet)
                    if offer1 and offer2:
                        offer1id = await loop.run_in_executor(None,helpers.get_offer_id,offer1)
                        offer2id = await loop.run_in_executor(None,helpers.get_offer_id,offer2)
                        if offer1id and offer2id:
                            xummLink1 = await loop.run_in_executor(None,helpers.gen_nft_accept_txn,offer1id)
                            xummLink2 = await loop.run_in_executor(None,helpers.gen_nft_accept_txn,offer2id)
                            if xummLink1 and xummLink2:
                                os.remove(f"{interaction.user.id}+nft1.png")
                                os.remove(f"{interaction.user.id}+nft2.png")
                                os.remove(f"{interaction.user.id}+nft.png")
                                os.remove(f"{interaction.user.id}+nft1.json")
                                os.remove(f"{interaction.user.id}+nft2.json")
                                embed = discord.Embed(title="New NFTs crafted!", description=f"New NFTs have been crafted and offered to you!\n\n**NFT 1**\n{swap.nft1}\n\n**NFT 2**\n{swap.nft2}", color=random.randint(0, 0xFFFFFF))
                                embed.set_footer(text="Requested by "+user.name, icon_url=user.avatar.url)
                                await interaction.channel.send(embed=embed,content=f"{xummLink1}\n{xummLink2}\n{user.mention}",delete_after=60*5)
                                client.currently_users.remove(user.id)


    selectTrait.callback = selectTraitCallback
    selectNft.callback = selectNftCallback
    selectPage.callback = selectPageCallback
    view = discord.ui.View()
    view.add_item(selectPage)
    view.add_item(selectNft)
    await interaction.followup.send(embed=embeds[0], view=view)
  except Exception as e:
    print(e)
    await interaction.channel.send(f"An error occured! {interaction.user.mention}")
    #clear user from currently_users
    if interaction.user.id in client.currently_users:
        client.currently_users.pop(interaction.user.id)


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


client.run('MTEyOTc5MDgxMTc0ODQ0MjExMg.GhGBGS.k6QWqEY5oVw2mB-Y6PS0M8ykHTk6GplIYWF4As')
