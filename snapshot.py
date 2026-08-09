import os, requests, time
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.environ["MERAKI_API_KEY"]
SERIAL  = os.environ["MERAKI_SERIAL"]
H = {"X-Cisco-Meraki-API-Key": API_KEY, "Content-Type": "application/json"}

def get_snapshot():
    r = requests.post(
        f"https://api.meraki.com/api/v1/devices/{SERIAL}/camera/generateSnapshot",
        headers=H, timeout=10)
    r.raise_for_status()
    url = r.json()["url"]

    for _ in range(15):                 # poll until the image is live
        img = requests.get(url, timeout=10)
        if img.status_code == 200:
            return img.content
        time.sleep(1)
    raise TimeoutError("snapshot never became available")

t0 = time.time()
jpeg = get_snapshot()
print(f"got {len(jpeg)} bytes in {time.time()-t0:.1f}s")
open("/home/cqlm/door/photos/snapshot_test.jpg", "wb").write(jpeg)
