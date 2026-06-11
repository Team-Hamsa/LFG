import os
import requests
from pathlib import Path

class BunnyCDNStorage:
    def __init__(self):
        self.apikey       = '{{PASSWORD_FROM_FTP_API_ACCESS}}'
        self.storage_zone = '{{STORAGE_ZONE_NAME}}'
        self.pull_zone    = '{{PULL_ZONE_NAME}}'

        self.base_url     = f'https://storage.bunnycdn.com/{ self.storage_zone }/'
        self.headers      = {
            'AccessKey'    : self.apikey,
            'Content-Type' : 'application/json',
            'Accept'       : 'applcation/json'
        }
    
    def download_file(self, file_path, destination_path):
        file_url      = f'{ self.base_url }{ file_path }'
        file_name     = file_url.split("/")[-1]
        download_path = Path(destination_path, file_name)

        try:
            response = requests.get(file_url, headers=self.headers, stream=True) # Stream prevents loading whole object into memory immediatly.
            response.raise_for_status()                                          # Raises HTTPError object if an error has occurred.
            with open(download_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=1024):
                    if chunk:
                        f.write(chunk)  
                return  response.status_code
        except Exception as error:
            return error

    def upload_file(self, storage_path, file_content, file_name):
        try:
            storage_url = f'{ self.base_url }{ storage_path }{ file_name }'
            response    = requests.put(storage_url, data=file_content, headers=self.headers)
            cdn_url     = f'https://{ self.pull_zone }.b-cdn.net/{ storage_path }{ file_name }'
            response.raise_for_status()
            return cdn_url
        except Exception as error:
            return error

    def object_exists(self, file_path):
        file_url = f'{ self.base_url }{ file_path }'
        response = requests.get(file_url, headers=self.headers)
        return response.status_code == 200                        

    def delete_object(self, file_path):                 
        try:
            file_url = f'{ self.base_url }{ file_path }'
            response = requests.delete(file_url, headers=self.headers)
            response.raise_for_status()         
            return response.status_code                                    
        except Exception as error:
            return error
