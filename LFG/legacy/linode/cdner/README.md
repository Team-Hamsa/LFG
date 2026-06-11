# XRPL NFT Metadata Fetcher

This script fetches and processes NFTs from the XRP Ledger based on a specific issuer and taxon. It retrieves NFT metadata from IPFS and stores the relevant attributes in an SQLite database.

## Features

- Connects to multiple WebSocket servers to fetch NFTs from the XRP Ledger.
- Handles pagination using the `marker` parameter to retrieve all NFTs in a collection.
- Retrieves metadata from IPFS, supporting multiple IPFS gateways with retries and exponential backoff.
- Stores NFT data, including specific attributes, in an SQLite database.

## Requirements

- Python 3.7+
- Required Python packages:
  - `asyncio`
  - `websockets`
  - `requests`
  - `sqlite3`

## Installation

1. **Clone the repository:**
   ```bash
   git clone https://github.com/yourusername/nft-metadata-fetcher.git
   cd nft-metadata-fetcher
   ```

2. **Create and activate a virtual environment (optional but recommended):**
   ```bash
   python3 -m venv xrplenv
   source xrplenv/bin/activate  # On Windows use `xrplenv\Scripts\activate`
   ```

3. **Install the required packages:**
   ```bash
   pip install asyncio websockets requests
   ```

4. **Run the script:**
   ```bash
   python fetcher.py
   ```

## Usage

1. **Configure the Script:**
   - Modify the `issuer_address` and `taxon` variables in the script to match the issuer and taxon of the NFTs you want to fetch.
   - The script will automatically handle pagination and process all NFTs in the specified collection.
   - Replace the trait1, trait2, etc with the names of the traits for the collection you're fetching.

2. **Database:**
   - The script uses an SQLite database (`nftdata.db` by default) to store the NFT data. It will create a table named `nfts` if it doesn't exist.
   - Each NFT's attributes are stored in individual columns, allowing for easy querying and analysis.

3. **IPFS Gateways:**
   - The script supports multiple IPFS gateways. If one gateway fails or is slow, the script will automatically retry with another.

4. **WebSocket Servers:**
   - The script rotates through a list of WebSocket servers (`wss://s1.ripple.com/`, `wss://xrplcluster.com/`, `wss://s2.ripple.com/`) to ensure reliable data retrieval from the XRP Ledger.

## Logging

- The script logs its operations at the `INFO` level by default. Detailed responses and errors are logged at the `DEBUG` level.
- You can modify the logging level in the script by adjusting the `logging.basicConfig(level=logging.INFO)` line.

## Troubleshooting

- **Not fetching all NFTs?**
  - Ensure that the `limit` parameter is set correctly and that the script is handling the `marker` parameter for pagination.
  
- **Connection Issues?**
  - If one of the WebSocket servers is down or not responding, the script will automatically try the next server in the list.

- **IPFS Issues?**
  - If IPFS metadata isn't being fetched, the script will retry using different gateways. Ensure you have a stable internet connection.

## Contributing

Contributions are welcome! Feel free to submit issues, feature requests, or pull requests.

## License

This project is licensed under the MIT License. See the `LICENSE` file for more details.
