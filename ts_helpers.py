# SYSTEM LIBRARIES
import json
import os
import pprint
import re
import time
import traceback

import cv2
import ffmpeg
import requests
from dotenv import load_dotenv

# IMAGE LIBRARIES
from PIL import Image

try:
    import moviepy.editor as mp  # type: ignore[import-untyped]
except ImportError:
    mp = None  # type: ignore[assignment]

# CDN LIBRARIES
import aiohttp
from xrpl.asyncio.clients import AsyncWebsocketClient
from xrpl.clients import JsonRpcClient
from xrpl.models import IssuedCurrencyAmount

# XRPL LIBRARIES
from xrpl.models.requests import AccountInfo, AccountNFTs, Tx
from xrpl.models.transactions import (
    NFTokenBurn,
    NFTokenCreateOffer,
    NFTokenCreateOfferFlag,
    Payment,
)
from xrpl.transaction import autofill_and_sign
from xrpl.transaction.reliable_submission import submit_and_wait
from xrpl.wallet import Wallet

load_dotenv()

JSON_RPC_URL = "https://s1.ripple.com:51234/"
WS_URL = "wss://s2.ripple.com/"

# Environment Variables
SEED = os.getenv("SEED")
X_API_KEY = os.getenv("X_API_KEY")
X_API_SECRET = os.getenv("X_API_SECRET")
key = os.getenv("BUNNYCDN_ACCESS_KEY")


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
        ascii_uri = bytes.fromhex(uri).decode("ascii")
        print(f"Decoded URI: {ascii_uri}")

        if ascii_uri.startswith("ipfs://"):
            print("URI is hosted on IPFS")
            ascii_uri = ascii_uri.replace("ipfs://", "")
            # print(ascii_uri)
            parts = ascii_uri.split("/")
            # For example, if the link is ipfs://bafybeiahdnp4q3fntlnfk544zmqiersjbmrzwir4l4xqzbeip3wx4vwz74/475.json,
            # make it https://bafybeiahdnp4q3fntlnfk544zmqiersjbmrzwir4l4xqzbeip3wx4vwz74.ipfs.dweb.link/475.json (append .ipfs.dweb.link)
            # ascii_uri = "https://" + parts[0] + ".ipfs.dweb.link/" + parts[1]
            print(parts)
            if len(parts) == 2:
                print("2 parts")
                ascii_uri = "https://" + parts[0] + ".ipfs.dweb.link/" + parts[1] + "/"
            else:
                print("1 part")
                ascii_uri = "https://" + parts[0] + ".ipfs.dweb.link/"

        # Otherwise, assume it's a BunnyCDN link or a valid HTTP URL
        elif ascii_uri.startswith("https://") or ascii_uri.startswith("http://"):
            print("Assuming BunnyCDN or HTTP URL")
            # No modification needed for BunnyCDN or HTTP URLs
        else:
            raise ValueError("Invalid URL format")

        print(f"Final URI: {ascii_uri}")

        # Make the request to fetch the metadata
        response = requests.get(ascii_uri)
        response.raise_for_status()  # Raise an error for bad responses
        return response.json()

    except Exception as e:
        print(f"Error fetching metadata: {e}")
        return None


def register_user(user, address):
    with open("users.json") as f:
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
    with open("users.json") as f:
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
                account_nfts_request = AccountNFTs(account=address, marker=markerVal, limit=400)
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


def makeNft(traits, gender, wallet, name, burnCount):
    # Parse NFT name to get the number for saving
    pattern = r"#(\d+)"
    match = re.search(pattern, name)
    if match:
        nftNumber = match.group(1)
        print(f"Extracted NFT number: {nftNumber}")
    else:
        print("No NFT number found")
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
        if not os.path.isfile(gender + "/" + trait["trait_type"] + "/" + trait["value"] + ".png"):
            print(gender + "/" + trait["trait_type"] + "/" + trait["value"] + ".png" + " not found")
            isAllPng = False
        if os.path.isfile(gender + "/" + trait["trait_type"] + "/" + trait["value"] + ".gif"):
            anyGif = True
        if os.path.isfile(gender + "/" + trait["trait_type"] + "/" + trait["value"] + ".mp4"):
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
                gender + "/" + trait + "/" + traits[traitsInOrder.index(trait)]["value"] + ".png"
            )
        # combine png files dynamically
        input_stream = ffmpeg.input(pngFiles[0])  # start with the first input
        for _, file in enumerate(pngFiles[1:], start=1):
            input_stream = input_stream.overlay(
                ffmpeg.input(file)
            )  # add overlay for each additional file

        # output the final combined file
        (
            input_stream.output(f"generated/{nftNumber}_{burnCount}.png")
            .overwrite_output()  # automatically say yes to overwrite
            .run()
        )
        print("output.png created")
        return f"generated/{nftNumber}_{burnCount}.png"

    elif anyGif and not anyMp4:
        print("gif")
        # create a list of all gif files
        gifFiles = []
        for trait in traitsInOrder:
            if os.path.isfile(
                gender + "/" + trait + "/" + traits[traitsInOrder.index(trait)]["value"] + ".gif"
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
        # combine gif files dynamically
        input_stream = ffmpeg.input(gifFiles[0])  # start with the first input
        for _, file in enumerate(gifFiles[1:], start=1):
            input_stream = input_stream.overlay(
                ffmpeg.input(file)
            )  # add overlay for each additional file

        # output the final combined file
        (
            input_stream.output(f"generated/{nftNumber}_{burnCount}.mp4")
            .overwrite_output()  # automatically say yes to overwrite
            .run()
        )
        print("output.gif created")
        return f"generated/{nftNumber}_{burnCount}.mp4"

    elif anyMp4 and not anyGif:
        print("mp4")
        # create a list of all mp4 files, also have the audio ported over to the final mp4
        mp4Files = []
        print(traits)
        for trait in traitsInOrder:
            print(f"Checking for {trait}")
            if os.path.isfile(
                gender + "/" + trait + "/" + traits[traitsInOrder.index(trait)]["value"] + ".mp4"
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
                clip.audio.write_audiofile(f"generated/{nftNumber}_audio_{burnCount}.mp3")
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

        # combine mp4 files dynamically
        input_stream = ffmpeg.input(mp4Files[0])  # start with the first input
        for _, file in enumerate(mp4Files[1:], start=1):
            input_stream = input_stream.overlay(
                ffmpeg.input(file)
            )  # add overlay for each additional file

        # output the final combined file
        (
            input_stream.output(f"generated/{nftNumber}_{burnCount}.mp4")
            .overwrite_output()  # automatically say yes to overwrite
            .run()
        )

        # add audio to mp4 using ffmpeg
        audio = ffmpeg.input(f"generated/{nftNumber}_audio_{burnCount}.mp3")
        video = ffmpeg.input(f"generated/{nftNumber}_{burnCount}.mp4")
        ffmpeg.concat(video, audio, v=1, a=1).output(
            f"generated/{nftNumber}_nft_video_{burnCount}.mp4"
        ).overwrite_output().run()
        print("output.mp4 created")
        return f"generated/{nftNumber}_nft_video_{burnCount}.mp4"
    elif anyGif and anyMp4:
        print("mp4 + gif")
        allFiles = []
        for trait in traitsInOrder:
            if os.path.isfile(
                gender + "/" + trait + "/" + traits[traitsInOrder.index(trait)]["value"] + ".png"
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
                gender + "/" + trait + "/" + traits[traitsInOrder.index(trait)]["value"] + ".mp4"
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
                clip.audio.write_audiofile(f"generated/{nftNumber}_audio_{burnCount}.mp3")
            elif os.path.isfile(
                gender + "/" + trait + "/" + traits[traitsInOrder.index(trait)]["value"] + ".gif"
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

        # combine mp4 files dynamically
        input_stream = ffmpeg.input(allFiles[0])  # start with the first input
        for _, file in enumerate(allFiles[1:], start=1):
            input_stream = input_stream.overlay(
                ffmpeg.input(file)
            )  # add overlay for each additional file

        # output the final combined file
        (
            input_stream.output(f"generated/{nftNumber}_{burnCount}.mp4")
            .overwrite_output()  # automatically say yes to overwrite
            .run()
        )
        """
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
            # .output(f"generated/{nftNumber}_output.mp4") #automatically say yes to overwrite
            # copy audio from first mp4 file
            .output(f"generated/{nftNumber}_output_{burnCount}.mp4")
            .overwrite_output()
            .run()
        )
        """

        # add audio to mp4 using ffmpeg
        audio = ffmpeg.input(f"generated/{nftNumber}_audio_{burnCount}.mp3")
        video = ffmpeg.input(f"generated/{nftNumber}_{burnCount}.mp4")
        ffmpeg.concat(video, audio, v=1, a=1).output(
            f"generated/{nftNumber}_nft_video_{burnCount}.mp4"
        ).overwrite_output().run()
        print("output.mp4 created")
        return f"generated/{nftNumber}_nft_video_{burnCount}.mp4"
    else:
        print("Error: No files found")
        return


def makeNftSingle(nft, gender, userWallet):
    folder = f"{gender}"
    bg = None
    for trait in nft:
        if trait["trait_type"] == "Background":
            bg = Image.open(f"{folder}/Background/{trait['value']}.png")
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
                    Image.open(f"{folder}/{trait}/{t['value']}.png"),
                    (0, 0),
                    mask=Image.open(f"{folder}/{trait}/{t['value']}.png"),
                )
                break
    bg.save(f"generated/{userWallet}_nft1.png")
    return f"generated/{userWallet}_nft1.png"


async def upload_to_bunnycdn(file_path, name, burnCount):
    # Parse NFT name to get the number for saving
    pattern = r"#(\d+)"
    match = re.search(pattern, name)
    if match:
        nftNumber = match.group(1)
    else:
        print("No NFT number found")

    key = os.getenv("BUNNYCDN_ACCESS_KEY")  # Bunny Access Key
    base_url = "https://storage.bunnycdn.com/lfgo/LFGO/"  # Base URL for CDN

    file_name = os.path.basename(file_path)

    file_extension = file_name.split(".")[-1].lower()

    with open(file_path, "rb") as file:
        file_bytes = file.read()

    content_types = {"png": "image/png", "mp4": "video/mp4", "json": "application/json"}
    if file_extension not in content_types:
        raise ValueError("Unsupported file type. Only PNG, MP4 and json are allowed.")

    async with aiohttp.ClientSession() as session:
        url = f"{base_url}{nftNumber}/{nftNumber}_{burnCount}.{file_extension}"
        print(f"Passing URL: {url}")

        headers = {
            "AccessKey": key,
            "Content-Type": content_types.get(file_extension),
        }
        async with session.put(url, headers=headers, data=file_bytes) as response:
            if response.status == 201:
                print("File uploaded")
                publicUrl = f"https://lfgo.b-cdn.net/LFGO/{nftNumber}/{nftNumber}_{burnCount}.{file_extension}"
                return publicUrl
            else:
                print(traceback.format_exc())
                return None


"""
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
"""


async def combineAudio(nft_video, userWallet):
    audio = mp.AudioFileClip(f"generated/{userWallet}_audio.mp3")
    video = mp.VideoFileClip(nft_video, fps_source="fps")
    final_video = video.set_audio(audio)
    final_video.write_videofile(f"generated/{userWallet}_nft_video_full.mp4")
    return f"generated/{userWallet}_nft_video_full.mp4"


def burn_nft(nftid, owner):
    try:
        wallet = Wallet.from_seed(SEED)
        payment = NFTokenBurn(account=wallet.classic_address, nftoken_id=nftid, owner=owner)
        client = JsonRpcClient(JSON_RPC_URL)

        retries = 5  # Number of retries for submission
        for attempt in range(1, retries + 1):
            try:
                # Submit the transaction and wait for response
                payment_response = submit_and_wait(payment, client, wallet)
                print(f"Transaction was submitted: {payment_response.result['hash']}")
                break  # Exit the loop if submission is successful
            except Exception as e:
                print(f"Attempt {attempt} failed with error: {e}")
                if attempt < retries:
                    print(f"Retrying in 5 seconds... ({attempt}/{retries})")
                    time.sleep(5)  # Wait for 5 seconds before retrying
                else:
                    print("All attempts failed. Giving up.")
                    return None  # Exit if all retries fail

        # Now check for the transaction response
        hashTxn = payment_response.result["hash"]  # Get the transaction hash

        # Retry mechanism to check the transaction status
        for check_attempt in range(1, retries + 1):
            try:
                txn = client.request(Tx(transaction=hashTxn))  # Query the transaction by its hash
                print(f"Transaction {hashTxn} status: {txn.result}")
                break  # Exit the loop if successful
            except Exception as e:
                print(f"Error fetching transaction: {e}")
                if check_attempt < retries:
                    print(f"Retrying in 5 seconds... ({check_attempt}/{retries})")
                    time.sleep(5)  # Wait for 5 seconds before retrying
                else:
                    print("Failed to fetch transaction status. Giving up.")
                    return None  # Exit if all retries fail

        # Access the transaction result
        res = txn.result
        if "meta" in res and "TransactionResult" in res["meta"]:
            if res["meta"]["TransactionResult"] in ["tesSUCCESS", "terQUEUED"]:
                print("Transaction successful!")
                return hashTxn  # Return the transaction hash if successful
        else:
            print("Transaction failed or not yet validated.")
            return None

    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        return None


# burn_nft("00091B58D1AE1BC312BEF9C68233FB0C8CF6A338F7C227BE561254C200001187","rhA5i9dWtXprHXYiAzgUdRoyybGmfnV9Pj")


def convert_str_to_hex(string):
    return string.encode().hex().upper()


"""
def get_nft_token(res):
    try:
        # Extract the URI from the transaction response
        if "tx_json" in res and "URI" in res["tx_json"]:
            uri = res["tx_json"]["URI"]
        else:
            print("URI not found in transaction response.")
            return None

        # Iterate over the AffectedNodes to find the corresponding NFTokenID
        for node in res["meta"]["AffectedNodes"]:
            nodeToCheck = None

            # Check if the node contains a CreatedNode or ModifiedNode
            if "CreatedNode" in node:
                nodeToCheck = node["CreatedNode"]
            elif "ModifiedNode" in node:
                nodeToCheck = node["ModifiedNode"]

            # Look for the NFToken in FinalFields or NewFields
            if nodeToCheck:
                if "FinalFields" in nodeToCheck:
                    if "NFTokens" in nodeToCheck["FinalFields"]:
                        for nft in nodeToCheck["FinalFields"]["NFTokens"]:
                            if nft["NFToken"]["URI"] == uri:
                                print(f"Found matching NFTokenID: {nft['NFToken']['NFTokenID']}")
                                return nft["NFToken"]["NFTokenID"]

                if "NewFields" in nodeToCheck:
                    if "NFTokens" in nodeToCheck["NewFields"]:
                        for nft in nodeToCheck["NewFields"]["NFTokens"]:
                            if nft["NFToken"]["URI"] == uri:
                                print(f"Found matching NFTokenID: {nft['NFToken']['NFTokenID']}")
                                return nft["NFToken"]["NFTokenID"]

        print("NFTokenID not found.")
        return None

    except Exception as e:
        print(f"Error in get_nft_token: {e}")
        return None
"""


def mint_nft(metadata_url, nft_token_taxon, issuer):
    """
    Mint an NFT on the XRPL using the provided metadata URL, NFT token taxon, and issuer.

    Parameters:
        metadata_url (str): The URL of the uploaded metadata.
        nft_token_taxon (int): The taxon value for the NFT.
        issuer (str): The XRPL address of the issuer.

    Returns:
        str: The NFT ID if the minting was successful; otherwise, None.
    """
    # Dummy implementation for now:
    print(
        f"Minting NFT with metadata URL: {metadata_url}, taxon: {nft_token_taxon}, issuer: {issuer}"
    )
    # Here, add your XRPL minting transaction logic (using xrpl-py, XUMM SDK, etc.)
    # For now, let's just return a dummy NFT ID
    return "dummy_nft_id"


# Create thumbnail for video NFTs
async def extractFirstFrame(video_path, output_image_path):
    print(video_path)
    print(output_image_path)

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


def create_nft_offer(nftId, destination):
    try:
        for _ in range(3):
            client = JsonRpcClient(JSON_RPC_URL)
            wallet = Wallet.from_seed(SEED)
            payment = NFTokenCreateOffer(
                account=wallet.classic_address,
                destination=destination,
                amount=IssuedCurrencyAmount(
                    currency="4252495800000000000000000000000000000000",
                    issuer="rLfgoBriX5ZaMP32mtc7RUZJcjnisKh2Px",
                    value="10",
                ),
                nftoken_id=nftId,
                flags=NFTokenCreateOfferFlag.TF_SELL_NFTOKEN,
            )

            payment_response = submit_and_wait(payment, client, wallet)
            print(f"Transaction was submitted: {payment_response.result['hash']}")
            hashTxn = payment_response.result["hash"]

            for _ in range(0, 3):
                try:
                    txn = client.request(
                        Tx(transaction=hashTxn)
                    )  # Query the transaction by its hash
                    res = txn.result
                    print(f"Transaction {hashTxn} status:\n{json.dumps(res, indent=4)}")
                    if "meta" in res and "TransactionResult" in res["meta"]:
                        if res["meta"]["TransactionResult"] == "tesSUCCESS":
                            print("Transaction succeeded")
                            offerId = res["meta"]["offer_id"]
                            print(offerId)
                            # offerId = get_offer_id(res)
                            if offerId:
                                return offerId  # Return the offer ID if successful
                        elif res["meta"]["TransactionResult"] == "terQUEUED":
                            print("Transaction queued, waiting for final result")
                    break  # Break after getting a valid response
                except Exception as e:
                    print(f"Error fetching transaction status: {e}")
                    time.sleep(5)  # Wait 5 seconds before retrying

            return None  # Return None if unable to get a successful response

    except Exception as e:
        print(f"An error occurred: {e}")
        return None


# create_nft_offer("00091B58D1AE1BC312BEF9C68233FB0C8CF6A338F7C227BEB1A990C60000118B","rhA5i9dWtXprHXYiAzgUdRoyybGmfnV9Pj")

"""
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
"""

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
        for file in os.listdir():  # check in root folder for files with userWallet in it
            if userWallet in file:
                files.append(file)
        cleanupFiles(files)
    except Exception:
        pass


def cleanupVid(userWallet):
    try:
        os.remove(f"generated/{userWallet}_nft_video.mp4")
        os.remove(f"generated/{userWallet}_audio.mp3")
        os.remove(f"generated/{userWallet}_nft_video_full.mp4")
    except Exception:
        pass


def cleanupFiles(files):
    for file in files:
        try:
            os.remove(file)
        except Exception:
            pass


url = "https://xumm.app/api/v1/platform/payload"
headers = {
    "accept": "application/json",
    "content-type": "application/json",
    "X-API-Key": f"{X_API_KEY}",
    "X-API-Secret": f"{X_API_SECRET}",
}


def gen_nft_accept_txn(offer):
    tjson = {
        "Account": "rJAFQ2d6mUTgHHtLogPx5BB5NRT97ASFDy",
        "NFTokenSellOffer": offer,
        "TransactionType": "NFTokenAcceptOffer",
    }
    payload = {
        "txjson": tjson,
    }

    response = requests.post(url, json=payload, headers=headers)
    res_json = response.json()
    # return res_json['uuid'], res_json['refs']['qr_png'], res_json['next']['always']
    return res_json["next"]["always"]


# Send BRIX to a user
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
        self.client.request(acc_info)
        sender_wallet = Wallet(seed=sender_seed)

        txn_payment = Payment(
            account=sender_addr,
            destination=receiver_addr,
            amount=IssuedCurrencyAmount(
                currency=currency_code, issuer=currency_issuer, value=currency_amount
            ),
        )
        stxn_payment = autofill_and_sign(
            transaction=txn_payment, wallet=sender_wallet, client=self.client
        )
        try:
            submit_and_wait(stxn_payment, self.client)
        except Exception:
            pass


def example_request():
    url = "https://api.xrpldata.com/docs/static/index.html"
    req = requests.get(url)
    data = req.json()
    print(data)


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


if __name__ == "__main__":
    example_request()
