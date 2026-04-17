"""
SharePoint Excel integration via Microsoft Graph API.
Appends claimed sales as rows to a shared Excel workbook.
"""

import os
import httpx

TENANT_ID = os.getenv("AZURE_TENANT_ID", "")
CLIENT_ID = os.getenv("AZURE_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("AZURE_CLIENT_SECRET", "")

# SharePoint site and file identifiers (resolved on first call)
SITE_HOST = "alarmprotection.sharepoint.com"
SITE_PATH = "/sites/insidesalesph"
SHEET_NAME = "Sales Data Cove"

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

    url = (
        f"https://graph.microsoft.com/v1.0/drives/{drive_id}/items/{item_id}"
        f"/workbook/worksheets/{SHEET_NAME}/tables"
    )

    async with httpx.AsyncClient() as client:
        # First, try to find an existing table on the sheet
        tables_resp = await client.get(url, headers=headers)

        if tables_resp.status_code == 200 and tables_resp.json().get("value"):
            # Table exists — append row to it
            table_id = tables_resp.json()["value"][0]["id"]
            add_url = (
                f"https://graph.microsoft.com/v1.0/drives/{drive_id}/items/{item_id}"
                f"/workbook/worksheets/{SHEET_NAME}/tables/{table_id}/rows"
            )
            resp = await client.post(add_url, headers=headers, json={"values": row})
        else:
            # No table — use the used range to find the next empty row and write directly
            range_url = (
                f"https://graph.microsoft.com/v1.0/drives/{drive_id}/items/{item_id}"
                f"/workbook/worksheets/{SHEET_NAME}/usedRange"
            )
            range_resp = await client.get(range_url, headers=headers)

            if range_resp.status_code == 200:
                used = range_resp.json()
                # rowCount tells us how many rows are used; next row is rowCount + 1
                next_row = used.get("rowCount", 1) + 1
            else:
                next_row = 2  # fallback: assume row 1 is header

            cell_range = f"A{next_row}:E{next_row}"
            write_url = (
                f"https://graph.microsoft.com/v1.0/drives/{drive_id}/items/{item_id}"
                f"/workbook/worksheets/{SHEET_NAME}/range(address='{cell_range}')"
            )
            resp = await client.patch(write_url, headers=headers, json={"values": row})

        if resp.status_code in (200, 201):
            print(f"[sharepoint] row added for {sale['rep']} | {sale['account_id']}")
            return {"ok": True}
        else:
            error = resp.text
            print(f"[sharepoint] error: {resp.status_code} {error}")
            return {"ok": False, "error": error}


def is_configured() -> bool:
    """Check if Azure credentials are present."""
    return bool(TENANT_ID and CLIENT_ID and CLIENT_SECRET)
