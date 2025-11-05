from supabase import create_client
from dotenv import load_dotenv
import os
from pathlib import Path

# Resolve the project root (two levels up if needed)
project_root = Path(__file__).resolve().parent.parent
env_path = project_root / ".env"

# get env to connect to supabase
if env_path.exists():
    from dotenv import load_dotenv
    load_dotenv()

# read config
url = os.environ["SUPABASE_URL"]
key = os.environ["SUPABASE_KEY"]

# connect to supabase LeukerbaDB project
supabase = create_client(url, key)

supabase.table("web_content").select('page_url').limit(1).execute()

print("Client created successfully.")