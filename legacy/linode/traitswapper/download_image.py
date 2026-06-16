def download_image(url, local_path):
            # Download an image from a URL and save it locally.
                response = requests.get(url)
                if response.status_code == 200:
                    with open(local_path, 'wb') as f:
                        f.write(response.content)
                else:
                    raise ValueError(f"Failed to download image from {url}, status code: {response.status_code}")


            # Function to handle IPFS logic
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
                return uri  # Return as-is if not IPFS


            # Download images from the resolved URLs
            img_resolved = resolve_ipfs_uri(img)
            imgg_resolved = resolve_ipfs_uri(imgg)

            download_image(img_resolved, f"{swap.nft1}+{swap.burnt1}.png")
            download_image(imgg_resolved, f"{swap.nft2}+{swap.burnt2}.png")
            # save_files(interaction, img, imgg)


            # Function to wait until the file size is above a certain threshold
            def wait_for_file(file_path, min_size_kb=10, timeout=30):
                start_time = time.time()
                while os.path.getsize(file_path) < min_size_kb * 1024:
                    if time.time() - start_time > timeout:
                        raise TimeoutError(f"File {file_path} is not fully downloaded after {timeout} seconds.")
                    time.sleep(1)

            # Wait for the files to be fully downloaded
            wait_for_file(f"{swap.nft1}+{swap.burnt1}.png")
            wait_for_file(f"{swap.nft2}+{swap.burnt2}.png")