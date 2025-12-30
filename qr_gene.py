import pandas as pd
import qrcode
from pathlib import Path

# Load spreadsheet
df = pd.read_csv("items.csv")  # or read_excel("items.xlsx")

out_dir = Path("qr_codes")
out_dir.mkdir(exist_ok=True)

BASE_URL = "https://yourapp.domain/request?item="

for _, row in df.iterrows():
    item_id = row["item_id"]

    qr = qrcode.QRCode(
        version=2,
        error_correction=qrcode.constants.ERROR_CORRECT_Q,
        box_size=10,
        border=4,
    )

    qr.add_data(f"{BASE_URL}{item_id}")
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")
    img.save(out_dir / f"{item_id}.png")

print("QR codes generated.")
