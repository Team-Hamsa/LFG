# LFG Bot - XRPL NFT Minting and Token Trading Bot

![Discord](https://img.shields.io/badge/Discord-Bot-blue) ![XRPL](https://img.shields.io/badge/XRPL-NFT-green) ![XUMM](https://img.shields.io/badge/XUMM-Integration-orange)

The **LFG Bot** is a Discord bot that allows users to mint NFTs on the XRP Ledger (XRPL) and trade tokens (`LFGO`) using the XUMM app. The bot dynamically generates NFT images by combining traits from different layers, uploads them to BunnyCDN, and mints them on the XRPL. It also supports token trading via AMM pools.

---

## Table of Contents

1. [Features](#features)
2. [Prerequisites](#prerequisites)
3. [Installation](#installation)
4. [Directory Structure](#directory-structure)
5. [Configuration](#configuration)
6. [Usage](#usage)
7. [Commands](#commands)
8. [Contributing](#contributing)
9. [License](#license)
10. [Acknowledgments](#acknowledgments)

---

## Features

- **Dynamic NFT Generation**: Randomly selects traits from directories, combines them into an image using `ffmpeg`, and generates metadata.
- **BunnyCDN Integration**: Uploads generated NFT images and metadata files to BunnyCDN.
- **XRPL NFT Minting**: Mints NFTs on the XRPL using XUMM for secure transaction signing.
- **Token Trading**: Allows users to trade `XRP` or `BRIX` for `LFGO` tokens via AMM pools.
- **Interactive Buttons**: Provides an intuitive interface with buttons for actions like "Mint NFT" and "Buy LFGO".
- **Scalable Trait System**: Dynamically loads traits from directories, making it easy to add new traits without modifying the code.

---

## Prerequisites

Before running the bot, ensure you have the following:

- Python 3.8 or higher
- A Discord bot token from the [Discord Developer Portal](https://discord.com/developers/applications)
- XUMM API credentials (API Key and Secret) from the [XUMM Developer Console](https://apps.xumm.dev/)
- An XRPL testnet account (for testing purposes)
- BunnyCDN credentials (Access Key and Storage Zone)
- `ffmpeg` installed on your system for image processing
- Basic knowledge of XRPL, XUMM, and BunnyCDN

---

## Installation

1. **Clone the Repository**

   ```bash
   git clone https://github.com/yourusername/lfg-bot.git
   cd lfg-bot
   ```

2. **Install Dependencies**

   Run the setup script to install both Python dependencies and system dependencies (like ffmpeg):

   ```bash
   ./setup.sh
   ```

   Or manually install:

   ```bash
   # Install system dependency (ffmpeg)
   sudo apt-get update && sudo apt-get install -y ffmpeg  # For Debian/Ubuntu
   
   # Install Python dependencies
   pip install -r requirements.txt
   ```

3. **Set Up Environment Variables**

   Create a `.env` file in the root directory and add the following variables:

   ```plaintext
   DISCORD_BOT_TOKEN=YOUR_DISCORD_BOT_TOKEN
   XUMM_API_KEY=YOUR_XUMM_API_KEY
   XUMM_API_SECRET=YOUR_XUMM_API_SECRET
   BUNNY_CDN_ACCESS_KEY=YOUR_BUNNY_CDN_ACCESS_KEY
   BUNNY_CDN_STORAGE_ZONE=YOUR_BUNNY_CDN_STORAGE_ZONE
   ```

4. **Prepare Trait Layers**

   Create a `trait_layers` directory in the root folder. Inside this directory, create subdirectories for each trait layer (e.g., `background`, `body`, `eyes`). Add image files for each trait in the respective directories.

   Example structure:

   ```
   /trait_layers/
       /background/
           background1.png
           background2.png
       /body/
           body1.png
           body2.png
       /eyes/
           eyes1.png
           eyes2.png
   ```

5. **Run the Bot**

   Start the bot using the following command:

   ```bash
   python bot.py
   ```

---

## Directory Structure

```
lfg-bot/
├── bot.py                # Main bot script
├── .env                  # Environment variables (DO NOT COMMIT TO GIT)
├── trait_layers/         # Directory containing trait layers
│   ├── background/       # Background trait images
│   ├── body/             # Body trait images
│   ├── eyes/             # Eyes trait images
│   └── ...               # Add more layers as needed
├── output_nft.png        # Generated NFT image (temporary)
├── metadata.json         # Generated metadata file (temporary)
└── README.md             # This file
```

---

## Configuration

### Environment Variables

| Variable                | Description                                      |
|-------------------------|--------------------------------------------------|
| `DISCORD_BOT_TOKEN`     | Your Discord bot token                           |
| `XUMM_API_KEY`          | XUMM API key                                     |
| `XUMM_API_SECRET`       | XUMM API secret                                  |
| `BUNNY_CDN_ACCESS_KEY`  | BunnyCDN access key                              |
| `BUNNY_CDN_STORAGE_ZONE`| BunnyCDN storage zone name                       |

### XRPL Testnet Account

Ensure you have a funded XRPL testnet account for testing. You can get testnet funds from the [XRPL Faucet](https://xrpl.org/xrp-testnet-faucet.html).

---

## Usage

1. **Invite the Bot to Your Server**

   Use the Discord Developer Portal to invite the bot to your server.

2. **Interact with the Bot**

   Use the `/LFG` slash command to interact with the bot. The bot will present buttons for "Mint NFT" and "Buy LFGO".

3. **Mint NFT**

   - Click the "Mint NFT" button.
   - The bot will generate an NFT image by randomly selecting traits, upload it to BunnyCDN, and mint it on the XRPL.
   - Follow the QR code or deep link provided by XUMM to complete the minting process.

4. **Buy LFGO Tokens**

   - Click the "Buy LFGO" button.
   - Select the currency (`XRP` or `BRIX`) to trade.
   - Follow the QR code or deep link provided by XUMM to complete the trade.

---

## Commands

### `/LFG`

- **Description**: Displays an embed with buttons for "Mint NFT" and "Buy LFGO".
- **Usage**: `/LFG`

---

## Contributing

Contributions are welcome! If you'd like to contribute to this project, please follow these steps:

1. Fork the repository.
2. Create a new branch (`git checkout -b feature/YourFeatureName`).
3. Commit your changes (`git commit -m 'Add some feature'`).
4. Push to the branch (`git push origin feature/YourFeatureName`).
5. Open a pull request.

Please ensure your code adheres to the project's coding standards and includes appropriate documentation.

---

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.

---

## Acknowledgments

- [XUMM SDK](https://github.com/XRPL-Labs/XUMM-SDK): For secure transaction signing on the XRPL.
- [XRPL-Py](https://github.com/XRPLF/xrpl-py): For interacting with the XRPL.
- [BunnyCDN](https://bunny.net/): For hosting NFT images and metadata.
- [FFmpeg](https://ffmpeg.org/): For combining images into NFTs.
