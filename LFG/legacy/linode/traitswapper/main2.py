import discord
import logging
from discord import app_commands
import xumm 
import random
import asyncio
import aiohttp
import helpers4 as helpers
import pprint
from PIL import Image
import requests
import os
import json
import time
import datetime
import pprint
from BunnyCDN.Storage import Storage
from BunnyCDN.CDN import CDN

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
        if not self.synced:
            await tree.sync()
            self.synced=True
        print(f'Logged in as {self.user}')

client = aclient()
tree = app_commands.CommandTree(client)

client.currently_users = {}

# Constants
TRAITS = ['Background', 'Back', 'Clothing', 'Mouth', 'Eyebrows', 'Eyes', 'Head', 'Accessory']
BACK = ["Angel Wings","Angel Wings Open"]
NFT_URL_PREFIX = "https://lfgo.b-cdn.net/LFGO/"
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
        self.season = 1

    def getAttributes(self,nft,nftdata): #returns all attributes of an nft
        for n in nftdata:
            if n['name'] == nft:
                # pprint.pprint(n)
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
                elif 'Ape' in n['value']:
                    nft1Gender = 'ape'
                else:
                    nft1Gender = 'skeleton'
        for n in self.oldTraits2:
            if n['trait_type'] == 'Body':
                if 'Straight' in n['value']:
                    nft2Gender = 'male'
                elif 'Curved' in n['value']:
                    nft2Gender = 'female'
                elif 'Ape' in n['value']:
                    nft2Gender = 'ape'
                else:
                    nft2Gender = 'skeleton'
        if nft1Gender != nft2Gender:
            return False
        self.gender = nft1Gender
        return True

'''
def save_files(interaction, img, img2):
    response = requests.get(img)
    file = open(f"{nft[name]}+{burnCount}.png", "wb")
    file.write(response.content)
    file.close()
    response = requests.get(img2)
    file = open(f"{nft[name]}+{burnCount}.png", "wb")
    file.write(response.content)
    file.close()
'''

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
    # if interaction.user.id != 739375301578194944:
    #     await interaction.followup.send(random.choice(maintainance_quotes), ephemeral=True)
    #     return
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
        if timeNow - timeLastUsed > 60 * 5: #8 minutes past since last use, clear the user
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
    print(f"NFTs: {nfts}")
    nftsData = []
    swap = nftSwapper(None, None)
    logging.info(f"Session started with {user.name} | {user.id}")

    for nft in nfts:
        print(f"Getting metadata for {nft}")
        data = await helpers.get_nft_metadata(nft)
        if data == None:
            continue
        if '#' not in data['name']:
            continue
        # pprint.pprint(data)
        #layout [{'trait_type': 'Background', 'value': 'Temptress'},
                # {'trait_type': 'Back', 'value': 'None'},
                # {'trait_type': 'Body', 'value': 'Straight Medium'},
                # {'trait_type': 'Clothing', 'value': 'Monster Garb'},
                # {'trait_type': 'Mouth', 'value': 'Freckled Freak'},
                # {'trait_type': 'Eyebrows', 'value': 'Skeptical'},
                # {'trait_type': 'Eyes', 'value': 'Hypno'},
                # {'trait_type': 'Head', 'value': 'Frankensteins Monster'},
                # {'trait_type': 'Accessory', 'value': 'None'}]
        orderTraits = ['Background', 'Back', 'Body', 'Clothing', 'Mouth', 'Eyebrows', 'Eyes', 'Head', 'Accessory']
        attri = data['attributes'] 
        #traverse through attributes, if Accesory, change it to Accessory. Also check if all traits are present, if any is missing, put that trait_type in with None as value. Also check if any value is present in back and is in Accessory, if yes, change it to Back
        #first check for typo
        for a in attri:
            if a['trait_type'] == 'Accesory':
                a['trait_type'] = 'Accessory'
        #now check if all traits are present
        for trait in orderTraits:
            found = False
            for a in attri:
                if a['trait_type'] == trait:
                    found = True
                    break
            if found == False:
                attri.append({'trait_type':trait,'value':'None'})
        #order the traits
        attri.sort(key=lambda x: orderTraits.index(x['trait_type']))
        #now check if any value is present in back and is in Accessory, if yes, change it to Back
        for a in attri:
            trtype = a['trait_type']
            val = a['value']
            if val in BACK and trtype == 'Accessory':
                #swap back and accessory
                a['value'] = 'None'
                for a in attri:
                    if a['trait_type'] == 'Back':
                        a['value'] = val
                        break
        # pprint.pprint(data)
        data['attributes'] = attri
        # check the number in the name, if its between 1-707
        num = int(data['name'].split("#")[1])
        if num < 1 or num > 3535:
            continue
        if num >= 1 and num <= 707:
            swap.season = 1
        elif num >= 708 and num <= 2121:
            swap.season = 2
        elif num >= 2122 and num <= 3535:
            swap.season = 3
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
        options=[discord.SelectOption(label=trait, value=trait) for trait in TRAITS],
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
            
            await interaction.channel.send(f"{interaction.user.mention}\nSelected {swap.nft1}, now select another NFT", delete_after=30)
        else:
            if nft == swap.nft1:
                await interaction.followup.send("Cannot select same NFT twice", ephemeral=True)
                return
            
            swap.nft2 = nft
            swap.oldTraits2 = swap.getAttributes(swap.nft2,nftsData)
            genderCheck = swap.genderCheck()
            if genderCheck == False:
                await interaction.followup.send(f"NFTs do not have same gender!")
                logging.info(f"Session ended with {user.name} | {user.id} | Reason: Invalid nfts selected")
                return
            
            logging.info(f"Session nfts selected with {user.name} | {user.id} | {swap.nft1} <-> {swap.nft2}")
            
            selectTrait.options = [
                discord.SelectOption(
                    label=trait,
                    description=f"{swap.getAttrValue(swap.oldTraits1,trait)} <-> {swap.getAttrValue(swap.oldTraits2,trait)}",
                    value=f"{trait}"
                    ) for trait in TRAITS
                    ]
            
            embed = discord.Embed(
                title="NFTs selected",
                description=f"You have selected {swap.nft1} and {swap.nft2}\nNow select the traits which you want to swap!",
                color=random.randint(0, 0xFFFFFF)
                )
            
            img,imgg = None,None
            burnt1,burnt2 = 0,0
            for nft in nftsData:
                if nft['name'] == swap.nft1:
                    img = nft['image']
                    # burnt1 = nft['burnCount']
                    if 'burnCount' in nft:
                        burnt1 = nft['burnCount']
                        swap.burnt1 = int(burnt1)
                    # if 'video' in nft and nft['video'] != None and nft['video'] != "null":
                    #     swap.nft1VidFlag = True
                elif nft['name'] == swap.nft2:
                    imgg = nft['image']
                    # burnt2 = nft['burnCount']
                    if 'burnCount' in nft:
                        burnt2 = nft['burnCount']
                        swap.burnt2 = int(burnt2)

            embed.add_field(name=f"NFT 1",value=f"{swap.nft1}\nBurn Count: {burnt1}")
            embed.add_field(name=f"NFT 2",value=f"{swap.nft2}\nBurn Count: {burnt2}")

            # Resolve IPFS URLs if needed
            def resolve_ipfs_uri(uri):
                if uri.startswith("ipfs://"):
                    print("URI is hosted on IPFS")
                    ascii_uri = uri.replace("ipfs://", "")
                    parts = ascii_uri.split("/")
                    
                    if len(parts) == 2:
                        print("2 parts")
                        return "https://" + parts[0] + ".ipfs.dweb.link/" + parts[1]
                    else:
                        print("1 part")
                        return "https://" + parts[0] + ".ipfs.dweb.link/"
                return uri

            img_resolved = resolve_ipfs_uri(img)
            imgg_resolved = resolve_ipfs_uri(imgg)

            logging.info(f"Resolved img URL: {img_resolved}")
            logging.info(f"Resolved imgg URL: {imgg_resolved}")

            # Download an image from a URL and save it locally.
            async def download_image(url, local_path):
                logging.info(f"Starting download: {url}")
                async with aiohttp.ClientSession() as session:
                    async with session.get(url) as response:
                        if response.status == 200:
                            content = await response.read()
                            with open(local_path, 'wb') as f:
                                f.write(content)
                            logging.info(f"Image saved to {local_path}")
                        else:
                            raise ValueError(f"Failed to download image from {url}, status code: {response.status}")

            file1 = f"{swap.nft1}_{swap.burnt1}.png"
            file2 = f"{swap.nft2}_{swap.burnt2}.png"

            # Start downloading both images concurrently
            download_tasks = [
                download_image(img_resolved, file1),
                download_image(imgg_resolved, file2)
            ]
            await asyncio.gather(*download_tasks)

            # Function to wait until the file size is above a certain threshold
            async def wait_for_file(file_path, min_size_kb=10, timeout=30):
                start_time = time.time()
                while True:
                    if os.path.exists(file_path):
                        size = os.path.getsize(file_path)
                        if size >= min_size_kb * 1024:
                            logging.info(f"File {file_path} has reached the minimum size: {size} bytes.")
                            return
                    if time.time() - start_time > timeout:
                        raise TimeoutError(f"File {file_path} is not fully downloaded after {timeout} seconds.")
                    await asyncio.sleep(1)

            # Wait for the files to be fully downloaded
            await asyncio.gather(
                wait_for_file(file1),
                wait_for_file(file2)
            )

	        # Process the downloaded images
            im = Image.open(file1).resize((512,512))
            imm = Image.open(file2).resize((512,512))
            new_im = Image.new('RGB', (1024,512))
            collage_files(interaction, im, imm, new_im)
            file = discord.File(f"{interaction.user.id}+nft.png", filename="nft.png")

            # Send the final image
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
        # print("Trait selected")
        await interaction.response.defer()
        # await interaction.message.edit(content="Crafting new NFTs...", embed=None, view=None,attachments=[])
        # await interaction.edit_original_response(content="Crafting new NFTs... It will take a few minutes to process this request!", embed=None, view=None,attachments=[])
        traits = selectTrait.values
        #confirm if user wants to swap
        # print("Confirming swap")
        embed = discord.Embed(title="Confirm swap", description=f"Are you sure you want to swap these traits?\n{swap.nft1} <-> {swap.nft2}", color=random.randint(0, 0xFFFFFF))
        for trait in traits:
            embed.add_field(name=trait, value=f"{swap.getAttrValue(swap.oldTraits1,trait)} <-> {swap.getAttrValue(swap.oldTraits2,trait)}")
        embed.set_footer(text="Requested by "+user.name, icon_url=user.avatar.url)
        # print("Sending confirmation")
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
            logging.info(f"Session confirmed with {user.name} | {user.id} | Traits selected: {traits}")

        async def cancelCallback(interaction: discord.Interaction):
            if interaction.user != user:
                return
            swap.confirmation = False
            client.currently_users.pop(user.id)
            # await interaction.response.send_message(f"Cancelled swap!", ephemeral=True)
            await interaction.response.defer()
            await interaction.edit_original_response(content="Cancelled swap!",embed=None,view=None)
            logging.info(f"Session cancelled with {user.name} | {user.id} | Traits selected: {traits}")
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
                logging.info(f"Session timed out with {user.name} | {user.id} | Reason: Confirmation timed out")
                return
            
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
        # pprint.pprint(swap.newTraits1)
        # pprint.pprint(swap.newTraits2)
        desired_order = ['Background', 'Back', 'Body', 'Clothing', 'Mouth', 'Eyebrows', 'Eyes', 'Head', 'Accessory']
        swap.newTraits1.sort(key=lambda x: desired_order.index(x['trait_type']))
        swap.newTraits2.sort(key=lambda x: desired_order.index(x['trait_type']))

        im1,im2 = None,None
        im3 = Image.new('RGB', (1024,512))
        nft1VidUrl, nft2VidUrl = None, None
        print(swap.nft1VidFlag)
        print(swap.nft2VidFlag)

        loop = asyncio.get_event_loop()
        pathToNft1 = await loop.run_in_executor(None,helpers.makeNft,swap.newTraits1,swap.gender,wallet,swap.nft1,swap.burnt1)
        pathToNft2 = await loop.run_in_executor(None,helpers.makeNft,swap.newTraits2,swap.gender,wallet,swap.nft2,swap.burnt2)
        print(f"Path to nft1: {pathToNft1}\nPath to nft2: {pathToNft2}")
        #if .mp4 is present, then its a video
        if pathToNft1.split('.')[-1] == 'mp4':
            swap.nft1VidFlag = True
        if pathToNft2.split('.')[-1] == 'mp4':
            swap.nft2VidFlag = True
        if swap.nft1VidFlag == False and swap.nft2VidFlag == False:
            im1 = Image.open(pathToNft1).resize((512,512))
            im2 = Image.open(pathToNft2).resize((512,512))
            nft1Link = await helpers.upload_to_bunnycdn(pathToNft1,swap.nft1,swap.burnt1)
            nft2Link = await helpers.upload_to_bunnycdn(pathToNft2,swap.nft2,swap.burnt2)
            helpers.cleanup(wallet)
        else:
            if swap.nft1VidFlag == True and swap.nft2VidFlag == False:
                im2 = Image.open(pathToNft2).resize((512,512))
                nft1VidUrl = await helpers.upload_to_bunnycdn(pathToNft1,swap.nft1,swap.burnt1)
                nft2Link = await helpers.upload_to_bunnycdn(pathToNft2,swap.nft2,swap.burnt2)
                nft1im = await helpers.extractFirstFrame(pathToNft1,f"{pathToNft1.split('.')[0]}.png")
                im1 = Image.open(nft1im).resize((512,512))
                nft1Link = await helpers.upload_to_bunnycdn(nft1im,swap.nft1,swap.burnt1)
                helpers.cleanupVid(wallet)
                helpers.cleanup(wallet)

            elif swap.nft1VidFlag == False and swap.nft2VidFlag == True:
                im1 = Image.open(pathToNft1).resize((512,512))
                nft2VidUrl = await helpers.upload_to_bunnycdn(pathToNft2,swap.nft2,swap.burnt2)
                nft1Link = await helpers.upload_to_bunnycdn(pathToNft1,swap.nft1,swap.burnt1)
                nft2im = await helpers.extractFirstFrame(pathToNft2,f"{pathToNft2.split('.')[0]}.png")
                im2 = Image.open(nft2im).resize((512,512))
                nft2Link = await helpers.upload_to_bunnycdn(nft2im,swap.nft2,swap.burnt2)
                helpers.cleanupVid(wallet)
                helpers.cleanup(wallet)
            else:
                nft1VidUrl = await helpers.upload_to_bunnycdn(pathToNft1,swap.nft1,swap.burnt1)
                nft2VidUrl = await helpers.upload_to_bunnycdn(pathToNft2,swap.nft2,swap.burnt2)
                nft1im = await helpers.extractFirstFrame(pathToNft1,f"{pathToNft1.split('.')[0]}.png")
                im1 = Image.open(nft1im).resize((512,512))
                nft1Link = await helpers.upload_to_bunnycdn(nft1im,swap.nft1,swap.burnt1)
                nft2im = await helpers.extractFirstFrame(pathToNft2,f"{pathToNft2.split('.')[0]}.png")
                im2 = Image.open(nft2im).resize((512,512))
                nft2Link = await helpers.upload_to_bunnycdn(nft2im,swap.nft2,swap.burnt2)
                helpers.cleanupVid(wallet)
                helpers.cleanup(wallet)

        logging.info(f"Session status: {user.name} | {user.id} | Status: uploaded to CDN")

        collage_files(interaction, im1, im2, im3)
        file = discord.File(f"{interaction.user.id}+nft.png", filename="nftCol.png")
        embed = discord.Embed(title="New NFTs crafted!", description=f"New NFTs have been crafted!\n\n**NFT 1**\n{swap.nft1}\n\n**NFT 2**\n{swap.nft2}", color=random.randint(0, 0xFFFFFF))
        embed.set_image(url="attachment://nftCol.png")
        await interaction.channel.send(content=f"{interaction.user.mention}",embed=embed, file=file,delete_after=60*5)

        #also send the new metadata for both nfts
        template = {
            "schema": "ipfs://QmNpi8rcXEkohca8iXu7zysKKSJYqCvBJn3xJwga8jXqWU",
            "name": "NFT",
            "description": f"Season {swap.season}",
            "image": "image.png",
            "video": "video.mp4",
            "external_link": "https://letseffinggo.com",
            "collection": {
                "name": "Let's Effing Go!",
                "family": f"Season {swap.season}",
                "image": "https://lfgo.b-cdn.net/LFGO_square_logo.png"
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
            nft1ToSend['video'] = nft1VidUrl
        else:
            nft1ToSend.pop('video')
        if swap.nft2VidFlag == True:
            nft2ToSend['video'] = nft2VidUrl
        else:
            nft2ToSend.pop('video')
        nft1ToSend['image'], nft2ToSend['image'] = nft1Link, nft2Link
        nft1ToSend['burnCount'], nft2ToSend['burnCount'] = swap.burnt1 + 1, swap.burnt2 + 1

        # Use regex to get nftNumbers
        import re
        def extract_nft_number(nft_name):
            # Regular expression to find the number after '#'
            pattern = r"#(\d+)"
            
            # Search for the pattern in the name
            match = re.search(pattern, nft_name)
            
            if match:
                return match.group(1)  # The first capturing group, which is the number
            else:
                return None  # Handle case where no number is found

        # Extract the NFT numbers from the names
        nftNumber1 = extract_nft_number(swap.nft1)  # Extract the number from swap.nft1
        nftNumber2 = extract_nft_number(swap.nft2)  # Extract the number from swap.nft2

        # Save json files
        saveFile = open(f"{nftNumber1}_{swap.burnt1}.json", "w")
        saveFile.write(json.dumps(nft1ToSend, indent=2))
        saveFile.close()
        saveFile = open(f"{nftNumber2}_{swap.burnt2}.json", "w")
        saveFile.write(json.dumps(nft2ToSend, indent=2))
        saveFile.close()


        # Upload to BunnyNet
        nft1JsonLink = await helpers.upload_to_bunnycdn(f"{nftNumber1}_{swap.burnt1}.json",swap.nft1,swap.burnt1)
        nft2JsonLink = await helpers.upload_to_bunnycdn(f"{nftNumber2}_{swap.burnt2}.json",swap.nft2,swap.burnt2)
        print(nft1JsonLink)
        print(nft2JsonLink)
        file1 = discord.File(f"{nftNumber1}_{swap.burnt1}.json", filename=f"{swap.nft1}.json")
        file2 = discord.File(f"{nftNumber2}_{swap.burnt2}.json", filename=f"{swap.nft2}.json")
        msg = await interaction.followup.send(content=f"Links:\nNFT 1: {nft1JsonLink}\nNFT 2: {nft2JsonLink}", files=[file1,file2], ephemeral=True)

        # Burning old NFTs
        await interaction.followup.send("Burning old NFTs...", ephemeral=True)
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
            logging.info(f"Session status: {user.name} | {user.id} | Status: burnt old nfts")
            #now remint the new nfts
            nftoken1 = await loop.run_in_executor(None,helpers.mint_nft,nft1JsonLink,1760,"rLfgoMintj3KBcs4s2XKtquvDwEte2kYfJ")
            nftoken2 = await loop.run_in_executor(None,helpers.mint_nft,nft2JsonLink,1760,"rLfgoMintj3KBcs4s2XKtquvDwEte2kYfJ")
            if nftoken1 and nftoken2:
                    logging.info(f"Session status: {user.name} | {user.id} | Status: reminted new nfts | NFT1: {nftoken1} | NFT2: {nftoken2}")
                    offer1id = await loop.run_in_executor(None,helpers.create_nft_offer,nftoken1,wallet)
                    offer2id = await loop.run_in_executor(None,helpers.create_nft_offer,nftoken2,wallet)
                    if offer1id and offer2id:
                            logging.info(f"Session status: {user.name} | {user.id} | Status: created nft offers | NFT1: {offer1id} | NFT2: {offer2id}")
                            xummLink1 = await loop.run_in_executor(None,helpers.gen_nft_accept_txn,offer1id)
                            xummLink2 = await loop.run_in_executor(None,helpers.gen_nft_accept_txn,offer2id)
                            if xummLink1 and xummLink2:
                                # os.remove(f"{nft[name]}+{burnCount}.png")
                                # os.remove(f"{interaction.user.id}+nft2.png")
                                # os.remove(f"{interaction.user.id}+nft.png")
                                # os.remove(f"{interaction.user.id}+nft1.json")
                                # os.remove(f"{interaction.user.id}+nft2.json")
                                #get all png files and json files, and check for user ids in name if so, delete them
                                for file in os.listdir():
                                    if str(interaction.user.id) in file:
                                        os.remove(file)
                                embed = discord.Embed(title="New NFTs crafted!", description=f"New NFTs have been crafted and offered to you!\n\n**NFT 1**\n{swap.nft1}\n\n**NFT 2**\n{swap.nft2}", color=random.randint(0, 0xFFFFFF))
                                embed.set_footer(text="Requested by "+user.name, icon_url=user.avatar.url)
                                await interaction.channel.send(embed=embed,content=f"{xummLink1}\n{xummLink2}\n{user.mention}",delete_after=60*5)
                                del client.currently_users[user.id]
                                logging.info(f"Session ended with {user.name} | {user.id} | Status: sent xumm links")

 
    selectTrait.callback = selectTraitCallback
    selectNft.callback = selectNftCallback
    selectPage.callback = selectPageCallback
    view = discord.ui.View()
    view.add_item(selectPage)
    view.add_item(selectNft)
    await interaction.followup.send(embed=embeds[0], view=view)
  except Exception as e:
    logging.info(f"Session Error with {user.name} | {user.id} | Error: {e}")
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

@tree.command(name='disconnect',description='Disconnect the bot')
@app_commands.checks.has_permissions(administrator=True)
async def disconnect(interaction: discord.Interaction):
    await interaction.response.defer()
    await interaction.followup.send("Disconnecting...")
    await client.close()
    exit()

client.run('MTEyOTc5MDgxMTc0ODQ0MjExMg.GhGBGS.k6QWqEY5oVw2mB-Y6PS0M8ykHTk6GplIYWF4As')
