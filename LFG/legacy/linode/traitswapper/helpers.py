import requests
import json
from xrpl.asyncio.clients import AsyncWebsocketClient
from xrpl.models.requests import AccountNFTs
from xrpl.wallet import Wallet
from xrpl.models.transactions import NFTokenBurn, NFTokenMint, NFTokenCreateOffer, NFTokenCreateOfferFlag, Payment
from xrpl.models.requests import AccountInfo
from xrpl.clients import JsonRpcClient
from xrpl.transaction import send_reliable_submission, safe_sign_and_autofill_transaction, get_transaction_from_hash
from xrpl.utils import str_to_hex
from xrpl.models import IssuedCurrencyAmount
from PIL import Image
import os
import cv2
import moviepy.editor as mp
import time
import asyncio


JSON_RPC_URL = "https://s2.ripple.com:51234/"
WS_URL = "wss://s1.ripple.com/" 
# TOP_TRAITS = ['Wavy Eyes','Rainbow Puke','Laser Eyes']
TOP_TRAITS = [
    {
        "trait_type": "Eyes",
        "value": "Wavy"
    },
    {
        "trait_type": "Mouth",
        "value": "Rainbow Puke"
    },
    {
        "trait_type": "Eyes",
        "value": "Laser Eyes"
    }
]

async def get_nft_metadata(uri):
    try:
        ascii_uri = bytes.fromhex(uri).decode('ascii')
        ascii_uri = ascii_uri.lower()
        ascii_uri = ascii_uri.replace("ipfs://", "")
        # print(ascii_uri)
        parts = ascii_uri.split("/") #link is https://bafybeiahdnp4q3fntlnfk544zmqiersjbmrzwir4l4xqzbeip3wx4vwz74/475.json, make it https://bafybeiahdnp4q3fntlnfk544zmqiersjbmrzwir4l4xqzbeip3wx4vwz74.ipfs.dweb.link/475.json (append .ipfs.dweb.link)
        # ascii_uri = "https://" + parts[0] + ".ipfs.dweb.link/" + parts[1]
        if len(parts) == 2:
            ascii_uri = "https://" + parts[0] + ".ipfs.dweb.link/" + parts[1]
        else:
            ascii_uri = "https://" + parts[0] + ".ipfs.dweb.link/"
        response = requests.get(ascii_uri)
        return response.json()
    except Exception as e:
        print(e)
        return None

def register_user(user, address):
    with open("users.json", "r") as f:
        users = json.load(f)["users"] # list of dicts of users
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
        users = json.load(f)["users"] # list of dicts of users
    for userr in users:
        if userr["id"] == str(user.id):
            return userr  

async def get_nfts(address,issuer):
    marker = True
    markerVal = None
    nfts = []
    nftIds = []
    # taxons = []
    async with AsyncWebsocketClient(WS_URL) as websocket:
        while marker == True:
          account_nfts_request = AccountNFTs(
              account=address,
              marker=markerVal,
              limit=400
          )
          account_nfts_response = await websocket.request(account_nfts_request)
          a1 = account_nfts_response.to_dict()

          for nft in a1['result']['account_nfts']:
            if (nft['Issuer'] != issuer):
              continue
            nfts.append(nft["URI"])
            nftIds.append(nft["NFTokenID"])
            # taxons.append(nft["NFTokenTaxon"])

          if 'marker' in a1["result"]:
            markerVal = a1["result"]["marker"]
          else:
            marker = False
    return nfts, nftIds

def makeNft(nft1,nft2,gender,userWallet):
    #nft1 contains traits for nft1 and nft2 contains traits for nft2. List[dict] of traits
    folder = f"{gender}" #main folder, traits are inside, i.e folder/Background/trait.png, ...
    #first draw nft1
    # Load each trait image
    bg = None
    for trait in nft1:
        if trait["trait_type"] == "Background":
            bg = Image.open(f'{folder}/Background/{trait["value"]}.png')
            break
    #order of traits to put images in: ['Clothing', 'Mouth', 'Eyebrows', 'Eyes', 'Head', 'Accessory']
    traits = ['Body','Clothing', 'Mouth', 'Eyebrows', 'Eyes', 'Head', 'Accessory']
    #if any of the traits are in top traits, put them last(so its on top of other traits)
    for trait in TOP_TRAITS:
        if trait in nft1:
            traits.remove(trait["trait_type"])
            traits.append(trait["trait_type"])
            break
    for trait in traits:
        for t in nft1:
            if t["trait_type"] == trait:
                bg.paste(Image.open(f'{folder}/{trait}/{t["value"]}.png'), (0, 0), mask=Image.open(f'{folder}/{trait}/{t["value"]}.png'))
                break
    bg.save(f'generated/{userWallet}_nft1.png')
    #now draw nft2
    bg = None
    for trait in nft2:
        if trait["trait_type"] == "Background":
            bg = Image.open(f'{folder}/Background/{trait["value"]}.png')
            break
    #order of traits to put images in: ['Clothing', 'Mouth', 'Eyebrows', 'Eyes', 'Head', 'Accessory']
    traits = ['Body','Clothing', 'Mouth', 'Eyebrows', 'Eyes', 'Head', 'Accessory']
    #if any of the traits are in top traits, put them last(so its on top of other traits)
    for trait in TOP_TRAITS:
        if trait in nft2:
            traits.remove(trait["trait_type"])
            traits.append(trait["trait_type"])
            break
    for trait in traits:
        for t in nft2:
            if t["trait_type"] == trait:
                bg.paste(Image.open(f'{folder}/{trait}/{t["value"]}.png'), (0, 0), mask=Image.open(f'{folder}/{trait}/{t["value"]}.png'))
                break
    bg.save(f'generated/{userWallet}_nft2.png')
    return f'generated/{userWallet}_nft1.png', f'generated/{userWallet}_nft2.png'

def makeNftSingle(nft,gender,userWallet):
    folder = f"{gender}"
    bg = None
    for trait in nft:
        if trait["trait_type"] == "Background":
            bg = Image.open(f'{folder}/Background/{trait["value"]}.png')
            break
    #order of traits to put images in: ['Clothing', 'Mouth', 'Eyebrows', 'Eyes', 'Head', 'Accessory']
    traits = ['Body','Clothing', 'Mouth', 'Eyebrows', 'Eyes', 'Head', 'Accessory']
    #if any of the traits are in top traits, put them last(so its on top of other traits)
    for trait in TOP_TRAITS:
        if trait in nft:
            traits.remove(trait["trait_type"])
            traits.append(trait["trait_type"])
            break
    for trait in traits:
        for t in nft:
            if t["trait_type"] == trait:
                bg.paste(Image.open(f'{folder}/{trait}/{t["value"]}.png'), (0, 0), mask=Image.open(f'{folder}/{trait}/{t["value"]}.png'))
                break
    bg.save(f'generated/{userWallet}_nft1.png')
    return f'generated/{userWallet}_nft1.png'



def makeNftVideo(background_video, nft_traits, gender, userWallet):
    #load and save audio
    clip = mp.VideoFileClip(background_video)
    clip.audio.write_audiofile(f'generated/{userWallet}_audio.mp3')
    # Load the background video
    cap = cv2.VideoCapture(background_video)
    fps = cap.get(cv2.CAP_PROP_FPS)
    frame_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    frame_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
 
    # Create the VideoWriter to save the final video
    output_filename = f"generated/{userWallet}_nft_video.mp4"
    out = cv2.VideoWriter(output_filename, cv2.VideoWriter_fourcc(*'mp4v'), fps, (frame_width, frame_height))
    #order of traits to put images in: ['Clothing', 'Mouth', 'Eyebrows', 'Eyes', 'Head', 'Accessory']
    traitsOrder = ['Body','Clothing', 'Mouth', 'Eyebrows', 'Eyes', 'Head', 'Accessory']
    #if any of the traits are in top traits, put them last(so its on top of other traits)
    for trait in TOP_TRAITS:
        if trait in nft_traits:
            traitsOrder.remove(trait["trait_type"])
            traitsOrder.append(trait["trait_type"])
            break
    # Load each trait image
    trait_layers = []
    for trait in traitsOrder:
         for t in nft_traits:
            trait_type = t["trait_type"]
            trait_value = t["value"]
            if trait_type == trait:
                trait_image_path = f"{gender}/{trait_type}/{trait_value}.png"
                trait_image = cv2.imread(trait_image_path, cv2.IMREAD_UNCHANGED)
                trait_layers.append(trait_image)
                break

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        # Overlay each trait image on the frame
        for trait_layer in trait_layers:
            # Resize trait layers to match frame dimensions
            trait_layer = cv2.resize(trait_layer, (frame_width, frame_height))

            y1, y2 = 0, trait_layer.shape[0]
            x1, x2 = 0, trait_layer.shape[1]

            alpha_s = trait_layer[:, :, 3] / 255.0
            alpha_l = 1.0 - alpha_s

            for c in range(0, 3):
                frame[y1:y2, x1:x2, c] = (alpha_s * trait_layer[:, :, c] + alpha_l * frame[y1:y2, x1:x2, c])

        # Write the frame to the output video
        out.write(frame)

    # Release video objects
    cap.release()
    out.release()

    return output_filename

async def upload_image_to_nft_storage(file_path):
    print(file_path)
    url = 'https://api.nft.storage/upload'
    authKey = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJkaWQ6ZXRocjoweGY0MDVlQjc4ZTI0MTYwMEQxNzUzQzFDMkNBZjRhMTBGMUJBYjgzNmIiLCJpc3MiOiJuZnQtc3RvcmFnZSIsImlhdCI6MTY5MDQ0MzEyMDU5NiwibmFtZSI6ImxmZ28gc3dhcHBlciJ9.PPFDn8hyv4t38w5AJvP-66dJMxakVioS3lnw_mqEU1o"
    headers = {
        'accept': 'application/json',
        'Authorization': f"Bearer {authKey}"
    }

    with open(file_path, 'rb') as file:
        response = requests.post(url, headers=headers, data=file)

    res = response.json()
    if res['ok'] == True:
        # return res['value']['cid']
        return f"https://{res['value']['cid']}.ipfs.nftstorage.link", res['value']['cid']
    else:
        return None
    

async def upload_image_to_bunny_net(file_path):
    print(file_path)
    file_name = os.path.basename(file_path)
    url = f'https://storage.bunnycdn.com/lfgo/{file_name}'
    api_key = '370842b6-f9e4-4e0f-b98b13a49bbe-a5a7-43c6'
    
    headers = {
        'AccessKey': api_key,
        'Content-Type': 'application/octet-stream'
    }
    
    with open(file_path, 'rb') as file:
        response = requests.put(url, headers=headers, data=file)
    
    if response.status_code == 201:
        return response.json()
    else:
        print(f"Error: {response.status_code} - {response.text}")
        return None


async def combineAudio(nft_video, userWallet):
    audio = mp.AudioFileClip(f'generated/{userWallet}_audio.mp3')
    video = mp.VideoFileClip(nft_video,fps_source="fps")
    final_video = video.set_audio(audio)
    final_video.write_videofile(f"generated/{userWallet}_nft_video_full.mp4")
    return f"generated/{userWallet}_nft_video_full.mp4"

def burn_nft(nftid,owner):
    try:
        wallet = Wallet(seed=SEED, sequence=0)
        payment = NFTokenBurn(
            account=wallet.classic_address,
            nftoken_id=nftid,
            owner=owner
        )
        client = JsonRpcClient(JSON_RPC_URL)
        signed = safe_sign_and_autofill_transaction(payment, wallet, client)
        hashTxn = signed.get_hash()
        print("Identifying hash:", hashTxn)
        try:
            response = send_reliable_submission(signed, client)
        except:
            pass
        return hashTxn
    except Exception as e:
        print(e)
        return None

def convert_str_to_hex(string):
    return string.encode().hex().upper()

def mint_nft(uri,taxon,issuer):
    try:
        wallet = Wallet(seed=SEED, sequence=0)
        payment = NFTokenMint(
            account=wallet.classic_address,
            uri=convert_str_to_hex(uri),
            nftoken_taxon=taxon,
            issuer=issuer,
            transfer_fee=7000,
            flags=9
        )
        client = JsonRpcClient(JSON_RPC_URL)
        signed = safe_sign_and_autofill_transaction(payment, wallet, client)
        hashTxn = signed.get_hash()
        print("Identifying hash:", hashTxn)
        try:
            response = send_reliable_submission(signed, client)
        except:
            pass
        return hashTxn
    except Exception as e:
        print(e)
        return None


if __name__ == "__main__":
    nft = \
[
    # {
    #   "trait_type": "Background",
    #   "value": "Claw Yellow"
    # },
    {
      "trait_type": "Body",
      "value": "Straight Gold"
    },
    {
      "trait_type": "Clothing",
      "value": "Letterman Black"
    },
    {
      "trait_type": "Mouth",
      "value": "Wise"
    },
    {
      "trait_type": "Eyebrows",
      "value": "Furious"
    },
    {
      "trait_type": "Eyes",
      "value": "Sticky Face"
    },
    {
      "trait_type": "Head",
      "value": "Big Afro Brown"
    },
    {
      "trait_type": "Accessory",
      "value": "Baseball Bat"
    }
  ]
    bg = "male/Background/Claw Yellow.mp4"

    vidPath = makeNftVideo(bg,nft,'male','test')
    print(vidPath)
    async def make(vidPath):
        return await combineAudio(vidPath,'test')
    async def main():
        return await make(vidPath)
    print(asyncio.run(main()))


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

def cleanup(userWallet):
    try:
        os.remove(f'generated/{userWallet}_nft1.png')
        os.remove(f'generated/{userWallet}_nft2.png')
    except:
        pass

def cleanupVid(userWallet):
    try:
        os.remove(f'generated/{userWallet}_nft_video.mp4')
        os.remove(f'generated/{userWallet}_audio.mp3')
        os.remove(f'generated/{userWallet}_nft_video_full.mp4')
    except:
        pass

def cleanupFiles(files):
    for file in files:
        try:
            os.remove(file)
        except:
            pass

def get_nft_token(hash):
    import pprint
    try:
        client = JsonRpcClient(JSON_RPC_URL)
        for i in range(0, 10):
            try:
                txn = get_transaction_from_hash(tx_hash=hash, client=client)
                break
            except:
                time.sleep(2)
        res = txn.result
        pprint.pprint(res)
        uri = res['URI']
        for node in res['meta']['AffectedNodes']:
            nodeToCheck = None

            if 'CreatedNode' in node:
                nodeToCheck = node['CreatedNode']
            elif 'ModifiedNode' in node:
                nodeToCheck = node['ModifiedNode']

            if 'FinalFields' in nodeToCheck:
                if 'NFTokens' in nodeToCheck['FinalFields']:
                    for nft in nodeToCheck['FinalFields']['NFTokens']:
                        if nft['NFToken']['URI'] == uri:
                            return nft['NFToken']['NFTokenID']
            
            if 'NewFields' in nodeToCheck:
                if 'NFTokens' in nodeToCheck['NewFields']:
                    for nft in nodeToCheck['NewFields']['NFTokens']:
                        if nft['NFToken']['URI'] == uri:
                            return nft['NFToken']['NFTokenID']

    except Exception as e:
        print(e)
        return None

def create_nft_offer(nftid,destination):
    try:
        client = JsonRpcClient(JSON_RPC_URL)
        wallet = Wallet(seed=SEED, sequence=0)
        payment = NFTokenCreateOffer(
            account=wallet.classic_address,
            destination=destination,
            amount=IssuedCurrencyAmount(
                currency="4252495800000000000000000000000000000000",
                issuer="rLfgoBriX5ZaMP32mtc7RUZJcjnisKh2Px",
                value="10"
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
        return hashTxn
    except Exception as e:
        print(e)
        return None
    
def get_offer_id(hashTxn):
    try:
        client = JsonRpcClient(JSON_RPC_URL)
        # txn = get_transaction_from_hash(tx_hash=hashTxn, client=client)
        for i in range(0, 10):
            try:
                txn = get_transaction_from_hash(tx_hash=hashTxn, client=client)
                break
            except:
                time.sleep(2)
        res = txn.result
        for node in res['meta']['AffectedNodes']:
            nodeToCheck = None

            if 'CreatedNode' in node:
                nodeToCheck = node['CreatedNode']
            elif 'ModifiedNode' in node:
                nodeToCheck = node['ModifiedNode']

            if nodeToCheck:
                if 'LedgerEntryType' in nodeToCheck:
                    if nodeToCheck['LedgerEntryType'] == 'NFTokenOffer':
                        return nodeToCheck['LedgerIndex']
    except Exception as e:
        print(e)
        return None
    
url = "https://xumm.app/api/v1/platform/payload"
headers = {
    "accept": "application/json",
    "content-type": "application/json",
    "X-API-Key": '3e468f17-7b7d-43eb-9a08-352b6a4d4638',
    "X-API-Secret": '3d3d0fc8-5108-4b9a-be54-feb2817f600e'
}

def gen_nft_accept_txn(offer):
    tjson = {
        "Account": "rJAFQ2d6mUTgHHtLogPx5BB5NRT97ASFDy",
        "NFTokenSellOffer": offer,
        "TransactionType": "NFTokenAcceptOffer"
    }
    payload = {
        "txjson": tjson,
        "options": {
            "pathfinding_fallback": False,
            "force_network": "N/A"
        }
    }

    response = requests.post(url, json=payload, headers=headers)
    res_json = response.json()
    # return res_json['uuid'], res_json['refs']['qr_png'], res_json['next']['always']
    return res_json['next']['always']


class wall(JsonRpcClient):
    def __init__(self, network_url: str, account_url: str, txn_url: str):
        self.network_url = network_url
        self.account_url = account_url
        self.txn_url = txn_url
        self.client = JsonRpcClient(network_url)
    def send_currency(self, sender_addr: str, sender_seed: str, receiver_addr: str, currency_code: str,
        currency_amount: str, currency_issuer: str) -> str:
        """send asset...
        max amount = 15 decimal places"""
        acc_info = AccountInfo(account=sender_addr, ledger_index="validated")
        response = self.client.request(acc_info).result
        sequence = response["account_data"]["Sequence"]
        sender_wallet = Wallet(seed=sender_seed, sequence=sequence)

        txn_payment = Payment(account=sender_addr, destination=receiver_addr,
        amount=IssuedCurrencyAmount(
            currency= currency_code,
            issuer=currency_issuer,
            value=currency_amount
        ))
        stxn_payment = safe_sign_and_autofill_transaction(
            transaction=txn_payment,
            wallet=sender_wallet,
            client=self.client
        )
        try:
          stxn_response = send_reliable_submission(stxn_payment, self.client)
        except Exception as e:
          pass

def send_brix(a,radd):
  wallet1 = wall(JSON_RPC_URL,"https://livenet.xrpl.org/accounts/", "https://livenet.xrpl.org/transactions/")
  # client = JsonRpcClient(JSON_RPC_URL)
  account1 = "rJAFQ2d6mUTgHHtLogPx5BB5NRT97ASFDy"
  seed = SEED
  wallet1 = wall(JSON_RPC_URL,"https://livenet.xrpl.org/accounts/", "https://livenet.xrpl.org/transactions/")
  wallet1.send_currency(account1,seed,radd,'4252495800000000000000000000000000000000',f'{a}','rLfgoBriX5ZaMP32mtc7RUZJcjnisKh2Px')


