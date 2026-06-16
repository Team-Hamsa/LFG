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

logging.basicConfig(level=logging.INFO)

cache = []

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

def delete_nft_images(user_id):
    os.remove(f"{user_id}+nft1.png")
    os.remove(f"{user_id}+nft2.png")
    os.remove(f"{user_id}+nft.png")

def collage_files(interaction, im1, im2, new_im):
    new_im.paste(im1, (0, 0))
    new_im.paste(im2, (512, 0))
    new_im.save(f"{interaction.user.id}+nft.png")

def save_files(interaction, img, img2):
    response = requests.get(img)
    file = open(f"{interaction.user.id}+nft1.png", "wb")
    file.write(response.content)
    file.close()
    response = requests.get(img2)
    file = open(f"{interaction.user.id}+nft2.png", "wb")
    file.write(response.content)
    file.close()

@tree.command(name="verify", description="Verify your XRP address")
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

@tree.command(name="nfts", description="View your NFTs")
async def nfts(interaction: discord.Interaction,wallet: str):
    await interaction.response.defer()
    mainUser = interaction.user.id
    u_nfts, u_nft_ids = await helpers.get_nfts(wallet, "rLfgoMintj3KBcs4s2XKtquvDwEte2kYfJ")
    #there are some None values in the list, so we need to remove them
    u_nfts = [nft for nft in u_nfts if nft != None]
    nfts_data = []
    for nfft in u_nfts:
        if nfft in cache:
            nfts_data.append(cache[cache.index(nfft)])
            continue

        print(f"Getting metadata for {u_nfts.index(nfft) + 1} out of {len(u_nfts)}")
        data = await helpers.get_nft_metadata(nfft)
        if data == None:
            continue
        nfts_data.append(data)
        cache.append(data)
    #sort according to the number present in the name, eg name format: Let's Effing Go! #216
    nfts_data.sort(key=lambda x: int(x["name"].split("#")[1]))

    # pprint.pprint(nfts_data)
    pages = []
    page = []
    for nft in nfts_data:
        if len(page) == 24:
            pages.append(page)
            page = []
        page.append(nft)
    pages.append(page)
    print(len(pages))
    embeds = []
    for page in pages:
        embed = discord.Embed(title=f"Page {pages.index(page) + 1}", description=f"Here are your NFTs!", color=random.randint(0, 0xFFFFFF))
        for nft in page:
            if nft == None:
                continue
            embed.add_field(name="NFT", value=nft["name"])
        embeds.append(embed)


    selectPage = discord.ui.Select(
        placeholder="Select a page",
        options=[discord.SelectOption(label=f"View page {i + 1}", value=i) for i in range(len(embeds))],
        min_values=1,
        max_values=1
    )
    try:
        # selectNft = discord.ui.Select(
        #     placeholder="Select NFTs",
        #     options=[discord.SelectOption(label=nft['name'], value=nft["name"]) for nft in pages[0]],
        #     min_values=1,
        #     max_values=1
        # )
        selectNft = discord.ui.Select(placeholder="Select NFTs", min_values=1, max_values=1)
        for nft in pages[0]:
            if nft == None:
                continue
            selectNft.add_option(label=nft['name'], value=nft["name"])
    except Exception as e:
        print(e)
        

    async def selectPageCallback(interaction: discord.Interaction):
        if interaction.user.id != mainUser:
            await interaction.response.send_message("You cannot use this button!", ephemeral=True)
            return
        item_number = int(selectPage.values[0])
        # selectNft.options = [discord.SelectOption(label=nft['name'], value=nft["name"]) for nft in pages[item_number]]
        selectNft.options = []
        for nft in pages[item_number]:
            if nft == None:
                continue
            selectNft.add_option(label=nft['name'], value=nft["name"])
        view = discord.ui.View()
        view.add_item(selectNft)
        view.add_item(selectPage)
        await interaction.response.edit_message(embed=embeds[item_number], view=view)

    async def selectNftCallback(interaction: discord.Interaction):
        await interaction.response.defer()
        if interaction.user.id != mainUser:
            await interaction.followup.send("You cannot use this button!", ephemeral=True)
            return
        nft1 = selectNft.values[0]
        # nft2_options = [discord.SelectOption(label=nft['name'], value=nft["name"]) for nft in pages[0]]
        nft2_options = []
        for nft in pages[0]:
            if nft == None:
                continue
            nft2_options.append(discord.SelectOption(label=nft['name'], value=nft["name"]))
        
        selectNft2 = discord.ui.Select(
            placeholder="Select the second NFT",
            options=nft2_options,
            min_values=1,
            max_values=1
        )

        selectPage2 = discord.ui.Select(
            placeholder="Select a page",
            options=[discord.SelectOption(label=f"View page {i + 1}", value=i) for i in range(len(embeds))],
            min_values=1,
            max_values=1
        )

        async def selectPage2Callback(interaction: discord.Interaction):
            if interaction.user.id != mainUser:
                await interaction.response.send_message("You cannot use this button!", ephemeral=True)
                return
            item_number = int(selectPage2.values[0])
            # selectNft.options = [discord.SelectOption(label=nft['name'], value=nft["name"]) for nft in pages[item_number]]
            selectNft2.options = []
            for nft in pages[item_number]:
                if nft == None:
                    continue
                selectNft2.add_option(label=nft['name'], value=nft["name"])
            view = discord.ui.View()
            view.add_item(selectNft2)
            view.add_item(selectPage2)
            await interaction.response.edit_message(embed=embeds[item_number], view=view)

        async def selectNft2Callback(interaction: discord.Interaction):
            if interaction.user.id != mainUser:
                await interaction.response.send_message("You cannot use this button!", ephemeral=True)
                return
            await interaction.response.edit_message(content="Please wait while we generate your nfts...", embed=None,view=None)
            nft = nft1
            nft2 = selectNft2.values[0]
            if nft == nft2:
                await interaction.followup.send("You cannot combine the same NFT!", ephemeral=True)
                return
            traits = ['Background', 'Clothing', 'Mouth', 'Eyebrows', 'Eyes', 'Head', 'Accessory']
            nft1Traits = []
            nft2Traits = []
            nft1Gender = None
            nft2Gender = None
            videoFlag = {"nft1": False, "nft2": False}
            for nftt in nfts_data:
                if nftt["name"] == nft:
                    if 'video' in nftt:
                        videoFlag["nft1"] = True
                    attr = nftt["attributes"]
                    for a in attr:
                        if a["trait_type"] in traits:
                            nft1Traits.append(a['value'])
                        if a["trait_type"] == "Body":
                            if "Straight" in a['value']:
                                nft1Gender = "male"
                            elif "Curved" in a['value']:
                                nft1Gender = "female"
                            else:
                                nft1Gender = "skeleton"
                elif nftt["name"] == nft2:
                    if 'video' in nftt:
                        videoFlag["nft2"] = True
                    attr = nftt["attributes"]
                    for a in attr:
                        if a["trait_type"] in traits:
                            nft2Traits.append(a['value'])
                        if a["trait_type"] == "Body":
                            if "Straight" in a['value']:
                                nft2Gender = "male"
                            elif "Curved" in a['value']:
                                nft2Gender = "female"
                            else:
                                nft2Gender = "skeleton"
            
            if nft1Gender != nft2Gender:
                await interaction.followup.send("Please select nfts with same gender!", ephemeral=True)
                return

            selectTrait = discord.ui.Select(
                placeholder="Select traits to swap",
                options=[
                    discord.SelectOption(label=f"{trait}",
                                        description=f"{nft1Traits[traits.index(trait)]} <-> {nft2Traits[traits.index(trait)]}",
                                        value=f"{trait}-{nft1Gender}-{videoFlag}-{nft}-{nft2}") for trait in traits
                ],
                min_values=1,
                max_values=len(traits)
            )

            async def selectTraitCallback(interaction: discord.Interaction):
                if interaction.user.id != mainUser:
                    await interaction.response.send_message("You cannot use this button!", ephemeral=True)
                    return
                # await interaction.response.edit_message(content="Please wait while we swap the traits...", embed=None,view=None,)
                await interaction.message.delete()
                await interaction.channel.send("Please wait while we swap the traits...",delete_after=120)
                traits = selectTrait.values
                nft1Gender = traits[0].split("-")[1]
                videoFlag = traits[0].split("-")[2] #string {nft1: bool, nft2: bool}
                #convert to dict
                videoFlag = eval(videoFlag)
                nft1 = traits[0].split("-")[3]
                nft2 = traits[0].split("-")[4]
                nft1Traits = []
                nft2Traits = []
                for nftt in nfts_data:
                    if nftt["name"] == nft1:
                        nft1Traits.append(nftt["attributes"])
                    elif nftt["name"] == nft2:
                        nft2Traits.append(nftt["attributes"])
                #swap the traits which were selected
                newNft1Traits = [] #append all the existing traits WHICH WERE NOT SELECTED and the new traits
                newNft2Traits = []
                traitTypesAppended = []
                for trait in traits:
                    traitToSwap = trait.split("-")[0]
                    for nft1Trait in nft1Traits[0]:
                        if nft1Trait["trait_type"] == traitToSwap:
                            newNft2Traits.append(nft1Trait)
                            traitTypesAppended.append(traitToSwap)
                    for nft2Trait in nft2Traits[0]:
                        if nft2Trait["trait_type"] == traitToSwap:
                            newNft1Traits.append(nft2Trait)
                            traitTypesAppended.append(traitToSwap)
                for nft1Trait in nft1Traits[0]:
                    if nft1Trait["trait_type"] not in traitTypesAppended:
                        newNft1Traits.append(nft1Trait)
                for nft2Trait in nft2Traits[0]:
                    if nft2Trait["trait_type"] not in traitTypesAppended:
                        newNft2Traits.append(nft2Trait)
                if videoFlag["nft1"] == True and videoFlag["nft2"] == True:
                    pass
                else:
                    #if bg is selected, swap the videoflag
                    if 'Background' in traitTypesAppended:
                        if videoFlag["nft1"] == True:
                            print("Flipping video flag")
                            videoFlag["nft1"] = False
                            videoFlag["nft2"] = True
                        elif videoFlag["nft2"] == True:
                            print("Flipping video flag")
                            videoFlag["nft1"] = True
                            videoFlag["nft2"] = False
                if videoFlag["nft1"] == False and videoFlag["nft2"] == False:
                    pathToNft1,pathToNft2 = helpers.makeNft(newNft1Traits,newNft2Traits,nft1Gender,wallet)
                    #upload the images to ipfs
                    nft1Ipfs,_nft1 = await helpers.upload_image_to_nft_storage(pathToNft1)
                    nft2Ipfs,_nft2 = await helpers.upload_image_to_nft_storage(pathToNft2)
                    #create a new image with the two images side by side
                    im1 = Image.open(pathToNft1).resize((512, 512))
                    im2 = Image.open(pathToNft2).resize((512, 512))
                    new_im = Image.new('RGB', (1024, 512))
                    collage_files(interaction, im1, im2, new_im)
                    file = discord.File(f"{interaction.user.id}+nft.png", filename="nft.png")
                    embed = discord.Embed(title="NFTs swapped",
                                            description=f"You have swapped {nft1} and {nft2}!\nHere is your new NFT!",
                                            color=random.randint(0, 0xFFFFFF))
                    embed.set_image(url="attachment://nft.png")
                    await interaction.channel.send(content="Enjoy your new nfts!", embed=embed, file=file)
                    helpers.cleanup(wallet)
                else:
                    if videoFlag["nft1"] == True and videoFlag["nft2"] == False:
                        pathToVid = None
                        #get the Background trait of nft1
                        for nft1Trait in newNft1Traits:
                            if nft1Trait["trait_type"] == "Background":
                                pathToVid = f"{nft1Gender}/Background/{nft1Trait['value']}.mp4"
                                break
                        # vidPath = await helpers.makeNftVideo(pathToVid,newNft1Traits,nft1Gender,wallet)
                        loop = asyncio.get_event_loop()
                        vidPath = await loop.run_in_executor(None, helpers.makeNftVideo, pathToVid, newNft1Traits, nft1Gender, wallet)
                        if vidPath:
                            vidPath = await helpers.combineAudio(vidPath,wallet)
                            #now make second nft, normal image
                            pathToNft2 = helpers.makeNftSingle(newNft2Traits,nft1Gender,wallet)
                            #upload the file to ipfs
                            nft1VidIpfs,vidnft1 = await helpers.upload_image_to_nft_storage(vidPath)
                            nft2Ipfs,_nft2 = await helpers.upload_image_to_nft_storage(pathToNft2)
                            nft1im = await helpers.extractFirstFrame(vidPath,f"{vidPath.split('.')[0]}.png")
                            nft1Ipfs,_nft1 = await helpers.upload_image_to_nft_storage(nft1im)
                            #now send the image and video
                            file = discord.File(f"{vidPath}", filename="nft.mp4")
                            file2 = discord.File(pathToNft2, filename="nft.png")
                            embed = discord.Embed(title="NFTs swapped",
                                                description=f"You have swapped {nft1} and {nft2}!\nHere is your new NFT!",
                                                color=random.randint(0, 0xFFFFFF))
                            embed.set_image(url="attachment://nft.png")
                            await interaction.channel.send(content="Enjoy your new nfts!", embed=embed, files=[file,file2])
                            helpers.cleanupVid(wallet)
                            helpers.cleanup(wallet)
                    elif videoFlag["nft1"] == False and videoFlag["nft2"] == True:
                        pathToVid = None
                        #get the Background trait of nft2
                        for nft2Trait in newNft2Traits:
                            if nft2Trait["trait_type"] == "Background":
                                pathToVid = f"{nft1Gender}/Background/{nft2Trait['value']}.mp4"
                                break
                        # vidPath = await helpers.makeNftVideo(pathToVid,newNft2Traits,nft1Gender,wallet)
                        loop = asyncio.get_event_loop()
                        vidPath = await loop.run_in_executor(None, helpers.makeNftVideo, pathToVid, newNft2Traits, nft1Gender, wallet)
                        if vidPath:
                            vidPath = await helpers.combineAudio(vidPath,wallet)
                            #now make second nft, normal image
                            pathToNft1 = helpers.makeNftSingle(newNft1Traits,nft1Gender,wallet)
                            #upload the file to ipfs
                            nft1Ipfs,_nft1 = await helpers.upload_image_to_nft_storage(vidPath)
                            nft2VidIpfs,vidnft2 = await helpers.upload_image_to_nft_storage(pathToNft1)
                            nft2im = await helpers.extractFirstFrame(vidPath,f"{vidPath.split('.')[0]}.png")
                            try:
                                print(f"nft2im: {nft2im}\nvidPath: {vidPath}\npathToNft1: {pathToNft1}")
                                nft2Ipfs,_nft2 = await helpers.upload_image_to_nft_storage(nft2im)
                            except Exception as e:
                                print(e)
                            #now send the image and video
                            file = discord.File(f"{vidPath}", filename="nft.mp4")
                            file2 = discord.File(pathToNft1, filename="nft.png")
                            embed = discord.Embed(title="NFTs swapped",
                                                description=f"You have swapped {nft1} and {nft2}!\nHere is your new NFT!",
                                                color=random.randint(0, 0xFFFFFF))
                            embed.set_image(url="attachment://nft.png")
                            await interaction.channel.send(content="Enjoy your new nfts!", embed=embed, files=[file,file2])
                            # helpers.cleanupVid(wallet)
                            # helpers.cleanup(wallet)
                    else:
                        pathToVid1 = None
                        pathToVid2 = None
                        #get the Background trait of nft1
                        for nft1Trait in newNft1Traits:
                            if nft1Trait["trait_type"] == "Background":
                                pathToVid1 = f"{nft1Gender}/Background/{nft1Trait['value']}.mp4"
                                break
                        #get the Background trait of nft2
                        for nft2Trait in newNft2Traits:
                            if nft2Trait["trait_type"] == "Background":
                                pathToVid2 = f"{nft1Gender}/Background/{nft2Trait['value']}.mp4"
                                break
                        # vidPath1 = await helpers.makeNftVideo(pathToVid1,newNft1Traits,nft1Gender,wallet)
                        # vidPath2 = await helpers.makeNftVideo(pathToVid2,newNft2Traits,nft1Gender,wallet)
                        loop = asyncio.get_event_loop()
                        vidPath1 = await loop.run_in_executor(None, helpers.makeNftVideo, pathToVid1, newNft1Traits, nft1Gender, wallet)
                        if vidPath1:
                            vidPath1 = await helpers.combineAudio(vidPath1,wallet)
                            # file = discord.File(f"{vidPath1}", filename="nft1.mp4")
                            nft1VidIpfs,vidnft1 = await helpers.upload_image_to_nft_storage(vidPath1)
                            im1nft = await helpers.extractFirstFrame(vidPath1,f"{vidPath1.split('.')[0]}.png")
                            nft1Ipfs,_nft1 = await helpers.upload_image_to_nft_storage(im1nft)
                            await interaction.channel.send(content=f"Nft 1 is ready!\n{nft1VidIpfs}")
                            vidPath2 = await loop.run_in_executor(None, helpers.makeNftVideo, pathToVid2, newNft2Traits, nft1Gender, wallet)
                            if vidPath2:
                                vidPath2 = await helpers.combineAudio(vidPath2,wallet)
                                #upload the file to ipfs
                                nft2VidIpfs,vidnft2 = await helpers.upload_image_to_nft_storage(vidPath2)
                                im2nft = await helpers.extractFirstFrame(vidPath2,f"{vidPath2.split('.')[0]}.png")
                                nft2Ipfs,_nft2 = await helpers.upload_image_to_nft_storage(im2nft)
                                #now send the two videos
                                embed = discord.Embed(title="NFTs swapped",
                                                    description=f"You have swapped {nft1} and {nft2}!\nHere are your new NFTs!",
                                                    color=random.randint(0, 0xFFFFFF))
                                await interaction.channel.send(content=f"Nft 2 is ready!\n{nft2VidIpfs}")
                                await interaction.channel.send(content="Enjoy your new nfts!", embed=embed)
                                helpers.cleanupVid(wallet)
                                helpers.cleanup(wallet)

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
                #use the new metadata to create a new nft metadata
                nft1ToSend = template.copy()
                nft2ToSend = template.copy()
                nft1ToSend["name"] = nft
                nft2ToSend["name"] = nft2
                #sort the traits, order: Background,Body, Clothing, Mouth, Eyebrows, Eyes, Head, Accessory. Its not alphabetical
                desiredOrder = ['Background', 'Body', 'Clothing', 'Mouth', 'Eyebrows', 'Eyes', 'Head', 'Accessory']
                newNft1Traits.sort(key=lambda x: desiredOrder.index(x['trait_type']))
                newNft2Traits.sort(key=lambda x: desiredOrder.index(x['trait_type']))
                nft1ToSend["attributes"] = newNft1Traits
                nft2ToSend["attributes"] = newNft2Traits
                #include `video` field if the nft is a video
                if videoFlag["nft1"] == True:
                    nft1ToSend["video"] = f"ipfs://{vidnft1}"
                else:
                    nft1ToSend.pop("video",None)
                if videoFlag["nft2"] == True:
                    nft2ToSend["video"] = f"ipfs://{vidnft2}"
                else:
                    nft2ToSend.pop("video",None)
                nft1ToSend["image"] = f"ipfs://{_nft1}"
                nft2ToSend["image"] = f"ipfs://{_nft2}"
                nft1ToSend["burnCount"] = int(burnt1) + 1
                nft2ToSend["burnCount"] = int(burnt2) + 1
                saveFile = open(f"{interaction.user.id}+nft1.json", "w")
                saveFile.write(json.dumps(nft1ToSend, indent=2))
                saveFile.close()
                saveFile = open(f"{interaction.user.id}+nft2.json", "w")
                saveFile.write(json.dumps(nft2ToSend, indent=2))
                saveFile.close()
                #now upload the metadata to ipfs
                nft1Ipfs,nft1 = await helpers.upload_image_to_nft_storage(f"{interaction.user.id}+nft1.json")
                nft2Ipfs,nft2 = await helpers.upload_image_to_nft_storage(f"{interaction.user.id}+nft2.json")
                #now send to discord
                file = discord.File(f"{interaction.user.id}+nft1.json", filename="nft1.json")
                file2 = discord.File(f"{interaction.user.id}+nft2.json", filename="nft2.json")
                await interaction.channel.send(content=f"Here are the new metadata for your nfts!\n{nft1Ipfs}\n{nft2Ipfs}", files=[file,file2])
                os.remove(f"{interaction.user.id}+nft1.json")
                os.remove(f"{interaction.user.id}+nft2.json")    

            selectTrait.callback = selectTraitCallback
            view = discord.ui.View()
            view.add_item(selectTrait)
            embed = discord.Embed(title="NFTs selected",
                                description=f"You have selected {nft} and {nft2}\nNow select the traits which you want to swap!",
                                color=random.randint(0, 0xFFFFFF))
            #get the image of both nfts from the metadata
            img, img2 = None, None
            #get the number of times nfts have been burnt (only if a `burnt` field exists in the metadata)
            burnt1, burnt2 = 0, 0
            for nftt in nfts_data:
                if nftt["name"] == nft:
                    img = nftt["image"]
                    if "burnCount" in nftt:
                        burnt1 = int(nftt["burnCount"])
                elif nftt["name"] == nft2:
                    img2 = nftt["image"]
                    if "burnCount" in nftt:
                        burnt2 = int(nftt["burnCount"])
            
            embed.add_field(name="NFT 1", value=f"{nft}\nBurnt {burnt1} times")
            embed.add_field(name="NFT 2", value=f"{nft2}\nBurnt {burnt2} times")

            img = img.replace("ipfs://", "https://cloudflare-ipfs.com/ipfs/")
            img2 = img2.replace("ipfs://", "https://cloudflare-ipfs.com/ipfs/")

            # create a new image with the two images side by side
            save_files(interaction, img, img2)
            im1 = Image.open(f"{interaction.user.id}+nft1.png").resize((512, 512))
            im2 = Image.open(f"{interaction.user.id}+nft2.png").resize((512, 512))
            new_im = Image.new('RGB', (1024, 512))
            collage_files(interaction, im1, im2, new_im)
            file = discord.File(f"{interaction.user.id}+nft.png", filename="nft.png")
            embed.set_image(url="attachment://nft.png")
            await interaction.channel.send(content=None, embed=embed, file=file, view=view)
            # delete the images
            delete_nft_images(interaction.user.id)


        selectNft2.callback = selectNft2Callback
        selectPage2.callback = selectPage2Callback
        view = discord.ui.View()
        view.add_item(selectNft2)
        if len(pages) > 1:
            view.add_item(selectPage2)
        await interaction.message.edit(content=f"{nft1} selected!\nNow select the second NFT to swap with!", view=None, embed=None)
        await interaction.followup.send(embed=embeds[0], view=view,content="Select the second NFT to swap with!")

    selectPage.callback = selectPageCallback
    selectNft.callback = selectNftCallback
    view = discord.ui.View()
    view.add_item(selectNft)
    if len(pages) > 1:
        view.add_item(selectPage)
    await interaction.followup.send(embed=embeds[0], view=view)

@tree.command(name="disconnect", description="disconnect this bot")
async def disconnect(interaction: discord.Interaction):
    await interaction.response.send_message("Disconnecting...", ephemeral=True)
    await client.close()
    
client.run('MTEyOTc5MDgxMTc0ODQ0MjExMg.GhGBGS.k6QWqEY5oVw2mB-Y6PS0M8ykHTk6GplIYWF4As')