import os
from dotenv import load_dotenv
load_dotenv(override=True)
print(f"ENV SHIOAJI_CA_PATH: {os.getenv('SHIOAJI_CA_PATH')}")
print(f"Exists: {os.path.exists(os.getenv('SHIOAJI_CA_PATH', ''))}")
print(f"Is Dir: {os.path.isdir(os.getenv('SHIOAJI_CA_PATH', ''))}")
