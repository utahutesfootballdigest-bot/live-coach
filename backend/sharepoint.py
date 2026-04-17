"""
SharePoint Excel integration via Microsoft Graph API.
Appends claimed sales as rows to a shared Excel workbook.
"""

import os
import httpx
from urllib.parse import quote

TENANT_ID = os.getenv("AZURE_TENANT_ID", "")
CLIENT_ID = os.getenv("AZURE_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("AZURE_CLIENT_SECRET", "")

# SharePoint site and file identifiers (resolved on first call)
SITE_HOST = "alarmprotection.sharepoint.com"
SITE_PATH = "/sites/insidesalesph"
SHEET_NAME = "SALES DATA | COVE"

_token_cache = {"access_token": None, "expires_at": 0}
_file_info_cache = {"drive_id": None, "item_id": None}


async def _get_token() -> str:
    """Get an app-only access token using client credentials flow."""
    import time
    if _token_cache["access_token"] and time.time() < _token_cache["expires_at"] - 60:
        return _token_cache["access_token"]

    url = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
    data = {
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "scope": "https://graph.microsoft.com/.default",
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, data=data)
        resp.raise_for_status()
        body = resp.json()

    _token_cache["access_token"] = body["access_token"]
    _token_cache["expires_at"] = time.time() + body.get("expires_in", 3600)
    return _token_cache["access_token"]


async def _get_file_info(token: str) -> tuple[str, str]:
    """Resolve the SharePoint site ID, drive ID, and item ID for the Excel file."""
    if _file_info_cache["drive_id"] and _file_info_cache["item_id"]:
        return _file_info_cache["drive_id"], _file_info_cache["item_id"]

    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient() as client:
        # Get site ID
        site_resp = await client.get(
            f"https://graph.microsoft.com/v1.0/sites/{SITE_HOST}:{SITE_PATH}",
            headers=headers,
        )
        site_resp.raise_for_status()
        site_id = site_resp.json()["id"]

        # Get default document library drive
        drives_resp = await client.get(
            f"https://graph.microsoft.com/v1.0/sites/{site_id}/drives",
            headers=headers,
        )
        drives_resp.raise_for_status()
        drives = drives_resp.json()["value"]
        drive_id = drives[0]["id"]  # default document library

        # Search for the Excel file in the drive
        search_resp = await client.get(
            f"https://graph.microsoft.com/v1.0/drives/{drive_id}/root/search(q='Sales Data')",
            headers=headers,
        )
        search_resp.raise_for_status()
        items = search_resp.json().get("value", [])

        item_id = None
        for item in items:
            name = item.get("name", "")
            if name.endswith(".xlsx") or name.endswith(".xls"):
                item_id = item["id"]
                break

        if not item_id:
            raise RuntimeError(
                f"Could not find Excel file in SharePoint drive. "
                f"Found {len(items)} items but none were .xlsx files. "
                f"Items: {[i.get('name') for i in items[:5]]}"
            )

        _file_info_cache["drive_id"] = drive_id
        _file_info_cache["item_id"] = item_id
        print(f"[sharepoint] resolved file: drive={drive_id}, item={item_id}")
        return drive_id, item_id


async def append_sale_row(sale: dict) -> dict:
    """
    Append a sale as a new row to the 'Sales Data Cove' sheet.
    Columns: SALES REP | DATE | ACCOUNT ID | PHONE NUMBER | SALES CHANNEL
    """
    if not all([TENANT_ID, CLIENT_ID, CLIENT_SECRET]):
        return {"ok": False, "error": "Azure credentials not configured"}

    token = await _get_token()
    drive_id, item_id = await _get_file_info(token)

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    # Build the row values in column order
    row = [[
        sale["rep"],
        sale["date"],
        sale["account_id"],
        sale["phone"],
        sale["channel"],
    ]]

    encoded_sheet = quote(SHEET_NAME, safe="")
    base = f"https://graph.microsoft.com/v1.0/drives/{drive_id}/items/{item_id}/workbook/worksheets('{encoded_sheet}')"

    async with httpx.AsyncClient(timeout=30.0) as client:
        # Find the next empty row by checking a batch of cells near the expected end.
        # Read column A from a range that should cover the last entries.
        # Start from a high row and scan downward in chunks to find the last used row.
        next_row = 2  # fallback

        # Check a range of rows to find where data ends.
        # Start by reading A4750:A5000 — should cover current data ending ~4799
        scan_start = 4750
        scan_end = 5200
        scan_range = f"A{scan_start}:A{scan_end}"
        scan_url = f"{base}/range(address='{scan_range}')"
        scan_resp = await client.get(scan_url, headers=headers)

        if scan_resp.status_code == 200:
            values = scan_resp.json().get("values", [])
            # Find the last non-empty cell in this range
            last_used_offset = -1
            for i, cell_row in enumerate(values):
                if cell_row[0] not in (None, "", " "):
                    last_used_offset = i
            if last_used_offset >= 0:
                next_row = scan_start + last_used_offset + 1
            else:
                # All empty in this range — data might be earlier, try A1:A100
                early_resp = await client.get(f"{base}/range(address='A1:A100')", headers=headers)
                if early_resp.status_code == 200:
                    early_vals = early_resp.json().get("values", [])
                    for i, cell_row in enumerate(early_vals):
                        if cell_row[0] not in (None, "", " "):
                            last_used_offset = i
                    next_row = last_used_offset + 2 if last_used_offset >= 0 else 2
            print(f"[sharepoint] scan found next_row={next_row}")
        else:
            print(f"[sharepoint] scan failed: {scan_resp.status_code} {scan_resp.text[:300]}")
            next_row = 4800  # best guess based on screenshot

        cell_range = f"A{next_row}:E{next_row}"
        write_url = f"{base}/range(address='{cell_range}')"
        print(f"[sharepoint] writing to {cell_range}")
        resp = await client.patch(write_url, headers=headers, json={"values": row})

        print(f"[sharepoint] write response: {resp.status_code} {resp.text[:500]}")
        if resp.status_code in (200, 201):
            print(f"[sharepoint] row added for {sale['rep']} | {sale['account_id']}")
            return {"ok": True}
        else:
            print(f"[sharepoint] error: {resp.status_code} {resp.text[:500]}")
            return {"ok": False, "error": resp.text}


def is_configured() -> bool:
    """Check if Azure credentials are present."""
    return bool(TENANT_ID and CLIENT_ID and CLIENT_SECRET)
