import requests
import json
from xrpl.wallet import Wallet
from xrpl.models.transactions import (
    NFTokenBurn,
    NFTokenMint,
    NFTokenCreateOffer,
    NFTokenCreateOfferFlag,
    Payment,
)
from xrpl.models.requests import AccountInfo, AccountNFTs
from xrpl.clients import JsonRpcClient
from xrpl.asyncio.clients import AsyncWebsocketClient
from xrpl.transaction import (
    reliable_submission,
    safe_sign_and_autofill_transaction,
    get_transaction_from_hash,
)
from xrpl.models import IssuedCurrencyAmount
from PIL import Image
import os

import cv2
import moviepy.editor as mp
import time

import ffmpeg
import pprint

from bunnycdnpython import BunnyCDNStorage
from BunnyCDN.CDN import CDN


JSON_RPC_URL = "https://s1.ripple.com:51234/"
WS_URL = "wss://s2.ripple.com/"
SEED = "sEdVTowejzt4oLvYn2Q4cGHmk5XrvoB"
# TOP_TRAITS = ['Wavy Eyes','Rainbow Puke','Laser Eyes']
TOP_TRAITS = [
    {"trait_type": "Eyes", "value": "Wavy"},
    {"trait_type": "Mouth", "value": "Rainbow Puke"},
    {"trait_type": "Eyes", "value": "Laser Eyes"},
    {"trait_type": "Eyes", "value": "Laser"},
]

MED_TRAITS = [
    {"trait_type": "Accessory", "value": "Girls Best Friend"},
]


async def get_nft_metadata(uri):
    try:
        ascii_uri = bytes.fromhex(uri).decode("ascii").lower()

        if ascii_uri.startswith("ipfs://"):    
            ascii_uri = ascii_uri.replace("ipfs://", "")
            # print(ascii_uri)
            parts = ascii_uri.split(
                "/"
            )  # link is https://bafybeiahdnp4q3fntlnfk544zmqiersjbmrzwir4l4xqzbeip3wx4vwz74/475.json, make it https://bafybeiahdnp4q3fntlnfk544zmqiersjbmrzwir4l4xqzbeip3wx4vwz74.ipfs.dweb.link/475.json (append .ipfs.dweb.link)
            # ascii_uri = "https://" + parts[0] + ".ipfs.dweb.link/" + parts[1]
            print(parts)
            if len(parts) == 2:
                print("2 parts")
                ascii_uri = "https://" + parts[0] + ".ipfs.dweb.link/" + parts[1]
            else:
                print("3 parts")
                ascii_uri = "https://" + parts[0] + ".ipfs.dweb.link/"
        
        # Otherwise, assume it's a BunnyCDN link or a valid HTTP URL
        else:
            print("Assuming BunnyCDN or HTTP URL")
            # If BunnyCDN, the URI should be a valid URL already
            ascii_uri = uri  # No modification needed for BunnyCDN or HTTP URLs
        
        print(ascii_uri)
        response = requests.get(ascii_uri)
        return response.json()
    except Exception as e:
        print(e)
        return None


def register_user(user, address):
    with open("users.json", "r") as f:
        users = json.load(f)["users"]  # list of dicts of users
    if str(user.id) not in [user["id"] for user in users]:
        users.append({"id": str(user.id), "address": address})
    else:
        for userr in users:
            if userr["id"] == str(user.id):
                userr["address"] = address
    with open("users.json", "w") as f:
        json.dump({"users": users}, f, indent=2)


def get_user(user):
    with open("users.json", "r") as f:
        users = json.load(f)["users"]  # list of dicts of users
    for userr in users:
        if userr["id"] == str(user.id):
            return userr


async def get_nfts(address, issuer):
    marker = True
    markerVal = None
    nfts = []
    nftIds = []
    try:
        print(f"Getting nfts for {address}")
        async with AsyncWebsocketClient(WS_URL) as websocket:
            while marker:
                account_nfts_request = AccountNFTs(
                    account=address, marker=markerVal, limit=400
                )
                account_nfts_response = await websocket.request(account_nfts_request)
                a1 = account_nfts_response.to_dict()
                print(f"Got {len(a1['result']['account_nfts'])} nfts")

                for nft in a1["result"]["account_nfts"]:
                    if nft["Issuer"] != issuer:
                        continue
                    nfts.append(nft["URI"])
                    nftIds.append(nft["NFTokenID"])

                if "marker" in a1["result"]:
                    markerVal = a1["result"]["marker"]
                    print(f"Marker: {markerVal}")
                else:
                    marker = False
                    print("No more markers")
    except Exception as e:
        print(f"Error occurred: {e}")
    return nfts, nftIds


# def makeNft(nft1,nft2,gender,userWallet):
#     #nft1 contains traits for nft1 and nft2 contains traits for nft2. List[dict] of traits
#     folder = f"{gender}" #main folder, traits are inside, i.e folder/Background/trait.png, ...
#     #first draw nft1
#     # Load each trait image
#     bg = None
#     for trait in nft1:
#         if trait["trait_type"] == "Background":
#             bg = Image.open(f'{folder}/Background/{trait["value"]}.png')
#             break
#     #order of traits to put images in: ['Clothing', 'Mouth', 'Eyebrows', 'Eyes', 'Head', 'Accessory']
#     traits = ['Body','Clothing', 'Mouth', 'Eyebrows', 'Eyes', 'Head', 'Accessory']
#     #if any of the traits are in top traits, put them last(so its on top of other traits)
#     for trait in TOP_TRAITS:
#         if trait in nft1:
#             traits.remove(trait["trait_type"])
#             traits.append(trait["trait_type"])
#             break
#     for trait in traits:
#         for t in nft1:
#             if t["trait_type"] == trait:
#                 bg.paste(Image.open(f'{folder}/{trait}/{t["value"]}.png'), (0, 0), mask=Image.open(f'{folder}/{trait}/{t["value"]}.png'))
#                 break
#     bg.save(f'generated/{userWallet}_nft1.png')
#     #now draw nft2
#     bg = None
#     for trait in nft2:
#         if trait["trait_type"] == "Background":
#             bg = Image.open(f'{folder}/Background/{trait["value"]}.png')
#             break
#     #order of traits to put images in: ['Clothing', 'Mouth', 'Eyebrows', 'Eyes', 'Head', 'Accessory']
#     traits = ['Body','Clothing', 'Mouth', 'Eyebrows', 'Eyes', 'Head', 'Accessory']
#     #if any of the traits are in top traits, put them last(so its on top of other traits)
#     for trait in TOP_TRAITS:
#         if trait in nft2:
#             traits.remove(trait["trait_type"])
#             traits.append(trait["trait_type"])
#             break
#     for trait in traits:
#         for t in nft2:
#             if t["trait_type"] == trait:
#                 bg.paste(Image.open(f'{folder}/{trait}/{t["value"]}.png'), (0, 0), mask=Image.open(f'{folder}/{trait}/{t["value"]}.png'))
#                 break
#     bg.save(f'generated/{userWallet}_nft2.png')
#     return f'generated/{userWallet}_nft1.png', f'generated/{userWallet}_nft2.png'


def makeNft(traits, gender, wallet, num):
    # each file is stored in "trait_type/trait_value.png" or "trait_type/trait_value.gif" or "trait_type/trait_value.mp4"
    # check if all files are png
    isAllPng = True
    anyGif = False
    anyMp4 = False
    # if all files are png, use pngs
    # order of traits to put images in: ['Clothing', 'Mouth', 'Eyebrows', 'Eyes', 'Head', 'Accessory']
    traitsInOrder = [
        "Background",
        "Back",
        "Body",
        "Clothing",
        "Mouth",
        "Eyebrows",
        "Eyes",
        "Head",
        "Accessory",
    ]

    medTraitOrder = [
        "Background",
        "Back",
        "Body",
        "Clothing",
        "Accessory",
        "Mouth",
        "Eyebrows",
        "Eyes",
        "Head",
    ]

    # put traits in order
    traits = sorted(traits, key=lambda k: traitsInOrder.index(k["trait_type"]))
    # medFlag = False
    # for t in traits:
    #     for medTrait in MED_TRAITS:
    #         if t["trait_type"] == medTrait["trait_type"]:
    #             medFlag = True
    #             break
    # if medFlag:
    #     traits = sorted(traits, key=lambda k: medTraitOrder.index(k["trait_type"]))
    # else:
    #     traits = sorted(traits, key=lambda k: traitsInOrder.index(k["trait_type"]))
    # check if .png files are present for each trait
    pprint.pprint(traits)
    for trait in traits:
        if not os.path.isfile(
            gender + "/" + trait["trait_type"] + "/" + trait["value"] + ".png"
        ):
            print(
                gender
                + "/"
                + trait["trait_type"]
                + "/"
                + trait["value"]
                + ".png"
                + " not found"
            )
            isAllPng = False
        if os.path.isfile(
            gender + "/" + trait["trait_type"] + "/" + trait["value"] + ".gif"
        ):
            anyGif = True
        if os.path.isfile(
            gender + "/" + trait["trait_type"] + "/" + trait["value"] + ".mp4"
        ):
            anyMp4 = True
        # check if any trait is in top traits, if so, put it in the front, i.e the last index so that its rendered on top of other traits
        for topTrait in TOP_TRAITS:
            if (
                topTrait["trait_type"] == trait["trait_type"]
                and topTrait["value"] == trait["value"]
            ):
                print(f"Top trait found: {trait['trait_type']}")
                traits.remove(trait)
                traits.append(trait)
                traitsInOrder.remove(trait["trait_type"])
                traitsInOrder.append(trait["trait_type"])
                break
        
    pprint.pprint(traits)
    print("isAllPng: " + str(isAllPng))
    print("anyGif: " + str(anyGif))
    print("anyMp4: " + str(anyMp4))

    # # check for top traits, if any, put them in the front
    # for trait in traits:
    #     t = trait["trait_type"]
    #     val = trait["value"]
    #     for topTrait in TOP_TRAITS:
    #         if topTrait["trait_type"] == t and topTrait["value"] == val:
    #             traitsInOrder.remove(t)
    #             traitsInOrder.append(t)
    #             break
    # print(traitsInOrder)
    # print(traits)
    if isAllPng and not anyGif and not anyMp4:
        # create a list of all png files
        pngFiles = []
        for trait in traitsInOrder:
            # print(f"Checking for {trait}")
            # print(f"Index: {traitsInOrder.index(trait)}")
            # print(json.dumps(traits, indent=2))
            pngFiles.append(
                gender
                + "/"
                + trait
                + "/"
                + traits[traitsInOrder.index(trait)]["value"]
                + ".png"
            )
        # combine png files
        (
            ffmpeg.input(pngFiles[0])
            .overlay(ffmpeg.input(pngFiles[1]))
            .overlay(ffmpeg.input(pngFiles[2]))
            .overlay(ffmpeg.input(pngFiles[3]))
            .overlay(ffmpeg.input(pngFiles[4]))
            .overlay(ffmpeg.input(pngFiles[5]))
            .overlay(ffmpeg.input(pngFiles[6]))
            .overlay(ffmpeg.input(pngFiles[7]))
            .overlay(ffmpeg.input(pngFiles[8]))
            .output(
                f"generated/{wallet}_nft_{num}.png"
            )  # automatically say yes to overwrite
            .overwrite_output()
            .run()
        )
        print("output.png created")
        return f"generated/{wallet}_nft_{num}.png"
    elif anyGif and not anyMp4:
        print("gif")
        # create a list of all gif files
        gifFiles = []
        for trait in traitsInOrder:
            if os.path.isfile(
                gender
                + "/"
                + trait
                + "/"
                + traits[traitsInOrder.index(trait)]["value"]
                + ".gif"
            ):
                gifFiles.append(
                    gender
                    + "/"
                    + trait
                    + "/"
                    + traits[traitsInOrder.index(trait)]["value"]
                    + ".gif"
                )
            else:
                gifFiles.append(
                    gender
                    + "/"
                    + trait
                    + "/"
                    + traits[traitsInOrder.index(trait)]["value"]
                    + ".png"
                )
        # combine gif files
        (
            ffmpeg.input(gifFiles[0])
            .overlay(ffmpeg.input(gifFiles[1]))
            .overlay(ffmpeg.input(gifFiles[2]))
            .overlay(ffmpeg.input(gifFiles[3]))
            .overlay(ffmpeg.input(gifFiles[4]))
            .overlay(ffmpeg.input(gifFiles[5]))
            .overlay(ffmpeg.input(gifFiles[6]))
            .overlay(ffmpeg.input(gifFiles[7]))
            .overlay(ffmpeg.input(gifFiles[8]))
            .output(
                f"generated/{wallet}_nft_{num}.mp4"
            )  # automatically say yes to overwrite
            .overwrite_output()
            .run()
        )
        print("output.gif created")
        return f"generated/{wallet}_nft_{num}.mp4"
    elif anyMp4 and not anyGif:
        print("mp4")
        # create a list of all mp4 files, also have the audio ported over to the final mp4
        mp4Files = []
        print(traits)
        for trait in traitsInOrder:
            print(f"Checking for {trait}")
            if os.path.isfile(
                gender
                + "/"
                + trait
                + "/"
                + traits[traitsInOrder.index(trait)]["value"]
                + ".mp4"
            ):
                print(f"Video file found for {trait}")
                mp4Files.append(
                    gender
                    + "/"
                    + trait
                    + "/"
                    + traits[traitsInOrder.index(trait)]["value"]
                    + ".mp4"
                )
                # extract audio and save it
                clip = mp.VideoFileClip(
                    gender
                    + "/"
                    + trait
                    + "/"
                    + traits[traitsInOrder.index(trait)]["value"]
                    + ".mp4"
                )
                clip.audio.write_audiofile(f"generated/{wallet}_audio_{num}.mp3")
            else:
                print(f"Image file found for {trait}")
                mp4Files.append(
                    gender
                    + "/"
                    + trait
                    + "/"
                    + traits[traitsInOrder.index(trait)]["value"]
                    + ".png"
                )

        # combine mp4 files
        (
            ffmpeg.input(mp4Files[0])
            .overlay(ffmpeg.input(mp4Files[1]))
            .overlay(ffmpeg.input(mp4Files[2]))
            .overlay(ffmpeg.input(mp4Files[3]))
            .overlay(ffmpeg.input(mp4Files[4]))
            .overlay(ffmpeg.input(mp4Files[5]))
            .overlay(ffmpeg.input(mp4Files[6]))
            .overlay(ffmpeg.input(mp4Files[7]))
            .overlay(ffmpeg.input(mp4Files[8]))
            .output(
                f"generated/{wallet}_nft_{num}.mp4"
            )  # automatically say yes to overwrite
            .overwrite_output()
            .run()
        )

        # add audio to mp4 using ffmpeg
        audio = ffmpeg.input(f"generated/{wallet}_audio_{num}.mp3")
        video = ffmpeg.input(f"generated/{wallet}_nft_{num}.mp4")
        ffmpeg.concat(video, audio, v=1, a=1).output(
            f"generated/{wallet}_nft_video_{num}.mp4"
        ).overwrite_output().run()
        print("output.mp4 created")
        return f"generated/{wallet}_nft_video_{num}.mp4"
    elif anyGif and anyMp4:
        print("mp4 + gif")
        allFiles = []
        for trait in traitsInOrder:
            if os.path.isfile(
                gender
                + "/"
                + trait
                + "/"
                + traits[traitsInOrder.index(trait)]["value"]
                + ".png"
            ):
                allFiles.append(
                    gender
                    + "/"
                    + trait
                    + "/"
                    + traits[traitsInOrder.index(trait)]["value"]
                    + ".png"
                )
            elif os.path.isfile(
                gender
                + "/"
                + trait
                + "/"
                + traits[traitsInOrder.index(trait)]["value"]
                + ".mp4"
            ):
                allFiles.append(
                    gender
                    + "/"
                    + trait
                    + "/"
                    + traits[traitsInOrder.index(trait)]["value"]
                    + ".mp4"
                )
                # extract audio and save it
                clip = mp.VideoFileClip(
                    gender
                    + "/"
                    + trait
                    + "/"
                    + traits[traitsInOrder.index(trait)]["value"]
                    + ".mp4"
                )
                clip.audio.write_audiofile(f"generated/{wallet}_audio_{num}.mp3")
            elif os.path.isfile(
                gender
                + "/"
                + trait
                + "/"
                + traits[traitsInOrder.index(trait)]["value"]
                + ".gif"
            ):
                allFiles.append(
                    gender
                    + "/"
                    + trait
                    + "/"
                    + traits[traitsInOrder.index(trait)]["value"]
                    + ".gif"
                )
            else:
                print(
                    "Error: No file found for "
                    + trait
                    + "/"
                    + traits[traitsInOrder.index(trait)]["value"]
                )
                return
        # combine mp4 files
        (
            ffmpeg.input(allFiles[0])
            .overlay(ffmpeg.input(allFiles[1]))
            .overlay(ffmpeg.input(allFiles[2]))
            .overlay(ffmpeg.input(allFiles[3]))
            .overlay(ffmpeg.input(allFiles[4]))
            .overlay(ffmpeg.input(allFiles[5]))
            .overlay(ffmpeg.input(allFiles[6]))
            .overlay(ffmpeg.input(allFiles[7]))
            .overlay(ffmpeg.input(allFiles[8]))
            # .output(f"generated/{wallet}_output.mp4") #automatically say yes to overwrite
            # copy audio from first mp4 file
            .output(f"generated/{wallet}_output_{num}.mp4")
            .overwrite_output()
            .run()
        )
        # add audio to mp4 using ffmpeg
        audio = ffmpeg.input(f"generated/{wallet}_audio_{num}.mp3")
        video = ffmpeg.input(f"generated/{wallet}_output_{num}.mp4")
        ffmpeg.concat(video, audio, v=1, a=1).output(
            f"generated/{wallet}_nft_video_{num}.mp4"
        ).overwrite_output().run()
        print("output.mp4 created")
        return f"generated/{wallet}_nft_video_{num}.mp4"
    else:
        print("Error: No files found")
        return


def makeNftSingle(nft, gender, userWallet):
    folder = f"{gender}"
    bg = None
    for trait in nft:
        if trait["trait_type"] == "Background":
            bg = Image.open(f'{folder}/Background/{trait["value"]}.png')
            break
    # order of traits to put images in: ['Clothing', 'Mouth', 'Eyebrows', 'Eyes', 'Head', 'Accessory']
    traits = ["Body", "Clothing", "Mouth", "Eyebrows", "Eyes", "Head", "Accessory"]
    # if any of the traits are in top traits, put them last(so its on top of other traits)
    for trait in TOP_TRAITS:
        if trait in nft:
            traits.remove(trait["trait_type"])
            traits.append(trait["trait_type"])
            break
    for trait in traits:
        for t in nft:
            if t["trait_type"] == trait:
                bg.paste(
                    Image.open(f'{folder}/{trait}/{t["value"]}.png'),
                    (0, 0),
                    mask=Image.open(f'{folder}/{trait}/{t["value"]}.png'),
                )
                break
    bg.save(f"generated/{userWallet}_nft1.png")
    return f"generated/{userWallet}_nft1.png"




# Initialize BunnyCDN storage
obj_storage = Storage('370842b6-f9e4-4e0f-b98b13a49bbe-a5a7-43c6','lfgo','ny')

async def upload_to_bunnycdn(file_path):
    print(file_path)
    
    # Extract the file name from the file path
    file_name = os.path.basename(file_path)
    
    try:
        # Upload the file to BunnyCDN
        with open(file_path, "rb") as file:
            storage.put_file(f'/{file_name}', file)

        # Construct the file URL
        file_url = f'https://lfgo.b-cdn.net/{file_name}' 

        print(f"File uploaded successfully: {file_url}")
        return file_url

    except Exception as e:
        print(f"An error occurred while uploading the file: {e}")
        return None


async def upload_image_to_nft_storage(file_path):
    print(file_path)
    url = "https://api.nft.storage/upload"
    authKey = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJkaWQ6ZXRocjoweGY0MDVlQjc4ZTI0MTYwMEQxNzUzQzFDMkNBZjRhMTBGMUJBYjgzNmIiLCJpc3MiOiJuZnQtc3RvcmFnZSIsImlhdCI6MTY5MDQ0MzEyMDU5NiwibmFtZSI6ImxmZ28gc3dhcHBlciJ9.PPFDn8hyv4t38w5AJvP-66dJMxakVioS3lnw_mqEU1o"
    headers = {"accept": "application/json", "Authorization": f"Bearer {authKey}"}

    with open(file_path, "rb") as file:
        response = requests.post(url, headers=headers, data=file)

    res = response.json()
    if res["ok"] == True:
        # return res['value']['cid']
        print(res["value"]["cid"])
        return (
            f"https://{res['value']['cid']}.ipfs.nftstorage.link",
            res["value"]["cid"],
        )
    else:
        return None


# asyncio.run(upload_image_to_nft_storage("generated/test_nft_video.mp4"))
# asyncio.run(upload_image_to_nft_storage("generated/up.png"))
# asyncio.run(upload_image_to_nft_storage("meta.json"))


async def combineAudio(nft_video, userWallet):
    audio = mp.AudioFileClip(f"generated/{userWallet}_audio.mp3")
    video = mp.VideoFileClip(nft_video, fps_source="fps")
    final_video = video.set_audio(audio)
    final_video.write_videofile(f"generated/{userWallet}_nft_video_full.mp4")
    return f"generated/{userWallet}_nft_video_full.mp4"


def burn_nft(nftid, owner):
    try:
        for i in range(0, 5):
            wallet = Wallet(seed=SEED)
            payment = NFTokenBurn(
                account=wallet.classic_address, nftoken_id=nftid, owner=owner
            )
            client = JsonRpcClient(JSON_RPC_URL)
            signed = safe_sign_and_autofill_transaction(payment, wallet, client)
            hashTxn = signed.get_hash()
            print("Identifying hash:", hashTxn)
            try:
                response = send_reliable_submission(signed, client)
            except:
                pass
            # return hashTxn
            for _ in range(0, 5):
                try:
                    txn = get_transaction_from_hash(tx_hash=hashTxn, client=client)
                    break
                except:
                    time.sleep(2)
            res = txn.result
            if "meta" in res:
                if "TransactionResult" in res["meta"]:
                    if (
                        res["meta"]["TransactionResult"] == "tesSUCCESS"
                        or res["meta"]["TransactionResult"] == "terQUEUED"
                    ):
                        # return hashTxn
                        print("Success")
                        return hashTxn
    except Exception as e:
        print(e)
        return None


# burn_nft("00091B58D1AE1BC312BEF9C68233FB0C8CF6A338F7C227BE561254C200001187","rhA5i9dWtXprHXYiAzgUdRoyybGmfnV9Pj")


def convert_str_to_hex(string):
    return string.encode().hex().upper()


def get_nft_token(res):
    import pprint

    try:
        pprint.pprint(res)
        uri = res["URI"]
        for node in res["meta"]["AffectedNodes"]:
            nodeToCheck = None

            if "CreatedNode" in node:
                nodeToCheck = node["CreatedNode"]
            elif "ModifiedNode" in node:
                nodeToCheck = node["ModifiedNode"]

            if "FinalFields" in nodeToCheck:
                if "NFTokens" in nodeToCheck["FinalFields"]:
                    for nft in nodeToCheck["FinalFields"]["NFTokens"]:
                        if nft["NFToken"]["URI"] == uri:
                            return nft["NFToken"]["NFTokenID"]

            if "NewFields" in nodeToCheck:
                if "NFTokens" in nodeToCheck["NewFields"]:
                    for nft in nodeToCheck["NewFields"]["NFTokens"]:
                        if nft["NFToken"]["URI"] == uri:
                            return nft["NFToken"]["NFTokenID"]

    except Exception as e:
        print(e)
        return None


def mint_nft(uri, taxon, issuer):
    try:
        for i in range(0, 5):
            wallet = Wallet(seed=SEED, sequence=0)
            payment = NFTokenMint(
                account=wallet.classic_address,
                uri=convert_str_to_hex(uri),
                nftoken_taxon=taxon,
                issuer=issuer,
                transfer_fee=7000,
                flags=9,
            )
            client = JsonRpcClient(JSON_RPC_URL)
            signed = safe_sign_and_autofill_transaction(payment, wallet, client)
            hashTxn = signed.get_hash()
            print("Identifying hash:", hashTxn)
            try:
                response = send_reliable_submission(signed, client)
            except:
                pass
            # return hashTxn
            for _ in range(0, 5):
                try:
                    txn = get_transaction_from_hash(tx_hash=hashTxn, client=client)
                    break
                except:
                    time.sleep(2)
            res = txn.result
            if "meta" in res:
                if "TransactionResult" in res["meta"]:
                    if (
                        res["meta"]["TransactionResult"] == "tesSUCCESS"
                        or res["meta"]["TransactionResult"] == "terQUEUED"
                    ):
                        # return hashTxn
                        print("Success")
                        nftId = get_nft_token(res)
                        if nftId != None:
                            return nftId

    except Exception as e:
        print(e)
        return None


# Create thumbnail for video NFTs
async def extractFirstFrame(video_path, output_image_path):
    # Open the video file
    cap = cv2.VideoCapture(video_path)

    # Check if the video was opened successfully
    if not cap.isOpened():
        print("Error opening video file.")
        return

    # Read the first frame
    ret, frame = cap.read()

    # Check if a frame was read
    if not ret:
        print("Error reading video frame.")
        cap.release()
        return

    # Save the first frame as a PNG image
    cv2.imwrite(output_image_path, frame)

    # Release the video capture object
    cap.release()

    print(f"First frame extracted and saved as {output_image_path}.")
    return output_image_path


def create_nft_offer(nftid, destination):
    try:
        for i in range(0, 5):
            client = JsonRpcClient(JSON_RPC_URL)
            wallet = Wallet(seed=SEED, sequence=0)
            payment = NFTokenCreateOffer(
                account=wallet.classic_address,
                destination=destination,
                amount=IssuedCurrencyAmount(
                    currency="4252495800000000000000000000000000000000",
                    issuer="rLfgoBriX5ZaMP32mtc7RUZJcjnisKh2Px",
                    value="10",
                ),
                nftoken_id=nftid,
                flags=NFTokenCreateOfferFlag.TF_SELL_NFTOKEN,
            )
            signed = safe_sign_and_autofill_transaction(payment, wallet, client)
            hashTxn = signed.get_hash()
            print("Identifying hash:", hashTxn)
            try:
                response = send_reliable_submission(signed, client)
            except:
                pass
            # return hashTxn
            for _ in range(0, 5):
                try:
                    txn = get_transaction_from_hash(tx_hash=hashTxn, client=client)
                    break
                except:
                    time.sleep(2)
            res = txn.result
            if "meta" in res:
                if "TransactionResult" in res["meta"]:
                    if (
                        res["meta"]["TransactionResult"] == "tesSUCCESS"
                        or res["meta"]["TransactionResult"] == "terQUEUED"
                    ):
                        # return hashTxn
                        print("Success")
                        offerId = get_offer_id(res)
                        if offerId != None:
                            return offerId

    except Exception as e:
        print(e)
        return None


# create_nft_offer("00091B58D1AE1BC312BEF9C68233FB0C8CF6A338F7C227BEB1A990C60000118B","rhA5i9dWtXprHXYiAzgUdRoyybGmfnV9Pj")


def get_offer_id(res):
    try:
        for node in res["meta"]["AffectedNodes"]:
            nodeToCheck = None

            if "CreatedNode" in node:
                nodeToCheck = node["CreatedNode"]
            elif "ModifiedNode" in node:
                nodeToCheck = node["ModifiedNode"]

            if nodeToCheck:
                if "LedgerEntryType" in nodeToCheck:
                    if nodeToCheck["LedgerEntryType"] == "NFTokenOffer":
                        return nodeToCheck["LedgerIndex"]
    except Exception as e:
        print(e)
        return None


if __name__ == "__main__":
    # import asyncio
    # #  get_nfts("rKzFwNddQv4CtAzZbC4oHpJvy1Nzhotd5M", "rLfgoMintj3KBcs4s2XKtquvDwEte2kYfJ")
    # async def main():
    #        await get_nfts("rKzFwNddQv4CtAzZbC4oHpJvy1Nzhotd5M", "rLfgoMintj3KBcs4s2XKtquvDwEte2kYfJ")
    # asyncio.run(main())
    # pass
    nft = [
        {"trait_type": "Background", "value": "Red Devil"},
        {"trait_type": "Back", "value": "None"},
        {"trait_type": "Body", "value": "Straight Light"},
        {"trait_type": "Clothing", "value": "Zombie Suit"},
        {"trait_type": "Mouth", "value": "Fangs Overbite"},
        {"trait_type": "Eyebrows", "value": "Incensed"},
        {"trait_type": "Eyes", "value": "Laser"},
        {"trait_type": "Head", "value": "Banana Suit"},
        {"trait_type": "Accessory", "value": "Bone"},
    ]
    # create_nft_offer("00091B58D1AE1BC312BEF9C68233FB0C8CF6A338F7C227BE26D7C83E00001243","rhA5i9dWtXprHXYiAzgUdRoyybGmfnV9Pj")
    # create_nft_offer("00091B58D1AE1BC312BEF9C68233FB0C8CF6A338F7C227BE0FF1FB3D00001242","rhA5i9dWtXprHXYiAzgUdRoyybGmfnV9Pj")
    # mint_nft("ipfs://bafkreig6b4hbjcykcutpglxssrs75swfi2mpr7r47vcc7k7y7n43bxim5m",1760,"rLfgoMintj3KBcs4s2XKtquvDwEte2kYfJ")
    # burn_nft("00091B58D1AE1BC312BEF9C68233FB0C8CF6A338F7C227BE49B245A3000011A8","rk5dmsuTy4yaSzyqGoH3Uix6f4nJ4jRAN")
    # vidPath = makeNft(nft, "skeleton", "test", 1)
    # print(vidPath)
    # async def make():
    #     await upload_image_to_nft_storage("882277071685046383+nft1.json")
    #     await upload_image_to_nft_storage("882277071685046383+nft2.json")
    # asyncio.run(make())
    # extractFirstFrame("generated/test_nft_video.mp4","generated/test_nft_video.png")
    # print(asyncio.run(main()))
    makeNft(nft, "male", "test", 1)


# asyncio.run(extractFirstFrame("generated/test_nft_video.mp4","generated/test_nft_video.png"))


def cleanup(userWallet):
    try:
        # os.remove(f'generated/{userWallet}_nft1.png')
        # os.remove(f'generated/{userWallet}_nft2.png')
        # check in generated folder for files with userWallet in it
        files = []
        for file in os.listdir("generated"):
            if userWallet in file:
                files.append(f"generated/{file}")
        for (
            file
        ) in os.listdir():  # check in root folder for files with userWallet in it
            if userWallet in file:
                files.append(file)
        cleanupFiles(files)
    except:
        pass


def cleanupVid(userWallet):
    try:
        os.remove(f"generated/{userWallet}_nft_video.mp4")
        os.remove(f"generated/{userWallet}_audio.mp3")
        os.remove(f"generated/{userWallet}_nft_video_full.mp4")
    except:
        pass


def cleanupFiles(files):
    for file in files:
        try:
            os.remove(file)
        except:
            pass


url = "https://xumm.app/api/v1/platform/payload"
headers = {
    "accept": "application/json",
    "content-type": "application/json",
    "X-API-Key": "3e468f17-7b7d-43eb-9a08-352b6a4d4638",
    "X-API-Secret": "3d3d0fc8-5108-4b9a-be54-feb2817f600e",
}


def gen_nft_accept_txn(offer):
    tjson = {
        "Account": "rJAFQ2d6mUTgHHtLogPx5BB5NRT97ASFDy",
        "NFTokenSellOffer": offer,
        "TransactionType": "NFTokenAcceptOffer",
    }
    payload = {
        "txjson": tjson,
        "options": {"pathfinding_fallback": False, "force_network": "N/A"},
    }

    response = requests.post(url, json=payload, headers=headers)
    res_json = response.json()
    # return res_json['uuid'], res_json['refs']['qr_png'], res_json['next']['always']
    return res_json["next"]["always"]


class wall(JsonRpcClient):
    def __init__(self, network_url: str, account_url: str, txn_url: str):
        self.network_url = network_url
        self.account_url = account_url
        self.txn_url = txn_url
        self.client = JsonRpcClient(network_url)

    def send_currency(
        self,
        sender_addr: str,
        sender_seed: str,
        receiver_addr: str,
        currency_code: str,
        currency_amount: str,
        currency_issuer: str,
    ) -> str:
        """send asset...
        max amount = 15 decimal places"""
        acc_info = AccountInfo(account=sender_addr, ledger_index="validated")
        response = self.client.request(acc_info).result
        sequence = response["account_data"]["Sequence"]
        sender_wallet = Wallet(seed=sender_seed, sequence=sequence)

        txn_payment = Payment(
            account=sender_addr,
            destination=receiver_addr,
            amount=IssuedCurrencyAmount(
                currency=currency_code, issuer=currency_issuer, value=currency_amount
            ),
        )
        stxn_payment = safe_sign_and_autofill_transaction(
            transaction=txn_payment, wallet=sender_wallet, client=self.client
        )
        try:
            stxn_response = send_reliable_submission(stxn_payment, self.client)
        except Exception as e:
            pass


def send_brix(a, radd):
    wallet1 = wall(
        JSON_RPC_URL,
        "https://livenet.xrpl.org/accounts/",
        "https://livenet.xrpl.org/transactions/",
    )
    # client = JsonRpcClient(JSON_RPC_URL)
    account1 = "rJAFQ2d6mUTgHHtLogPx5BB5NRT97ASFDy"
    seed = SEED
    wallet1 = wall(
        JSON_RPC_URL,
        "https://livenet.xrpl.org/accounts/",
        "https://livenet.xrpl.org/transactions/",
    )
    wallet1.send_currency(
        account1,
        seed,
        radd,
        "4252495800000000000000000000000000000000",
        f"{a}",
        "rLfgoBriX5ZaMP32mtc7RUZJcjnisKh2Px",
    )
