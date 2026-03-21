import re
import httpx
import json
import urllib.parse
import html

async def fetch_and_compress_miro(miro_url: str, access_token: str):
    print(f"🚨 DEBUG: fetch_and_compress_miro triggered!")
    
    if not access_token:
        raise ValueError("Missing Miro Access Token. User must link their account.")

    # 1. Safely Extract Board ID
    # Miro URLs look like: https://miro.com/app/board/uXjVxxxxxxx=/
    board_id_match = re.search(r"board/([^/?]+)", miro_url)
    if not board_id_match:
        raise ValueError("Invalid Miro URL format. Could not find the board ID.")
    
    board_id = board_id_match.group(1)

    # 2. Fetch Items from Miro API
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {access_token}"
    }
    
    # Grabbing up to 100 items (you can implement pagination later if users have massive boards)
    api_url = f"https://api.miro.com/v2/boards/{board_id}/items?limit=100"
    
    async with httpx.AsyncClient() as client:
        print(f"🧠 Fetching Miro Board {board_id}...")
        resp = await client.get(api_url, headers=headers, timeout=15.0)
        
        if resp.status_code != 200:
            raise Exception(f"Miro API Error ({resp.status_code}): {resp.text}")
            
        data = resp.json()
        
    items = data.get("data", [])
    if not items:
        raise Exception("Could not find any items on this Miro board. Is it empty?")

    # ====================================================================
    # 🗜️ 3. THE "ARCHITECTURE" COMPRESSOR 
    # ====================================================================
    compressed_items = []
    
    for item in items:
        item_type = item.get("type", "unknown")
        
        clean_item = {
            "type": item_type,
            "id": item.get("id"),
        }
        
        # Capture Coordinates & Size (Helps the AI understand what is grouped together)
        if "position" in item:
            clean_item["x"] = round(item["position"].get("x", 0))
            clean_item["y"] = round(item["position"].get("y", 0))
        if "geometry" in item:
            clean_item["w"] = round(item["geometry"].get("width", 0))
            clean_item["h"] = round(item["geometry"].get("height", 0))
            
        # Capture Content & Shapes
        item_data = item.get("data", {})
        
        if "content" in item_data:
            # Miro sends content as HTML (e.g., "<p>Login Flow</p>"). We MUST strip this so we don't blow up the AI context window.
            clean_text = re.sub(r'<[^<]+?>', '', item_data["content"])
            clean_item["text"] = html.unescape(clean_text).strip()
            
        if "shape" in item_data:
            clean_item["shape"] = item_data["shape"]
            
        # Lines/Connectors (Helps the AI understand flowchart paths)
        if item_type == "connector":
            start = item.get("startItem", {}).get("id")
            end = item.get("endItem", {}).get("id")
            if start and end:
                clean_item["connects"] = f"{start} -> {end}"

        # Only keep items that actually have content or connections (ignore blank decorative shapes)
        if "text" in clean_item or "connects" in clean_item or clean_item.get("type") in ["frame"]:
            compressed_items.append(clean_item)

    print("🗜️ Running Miro Data Compression...")
    
    # Sort by Y and then X coordinate so the AI reads the flowchart top-to-bottom, left-to-right
    compressed_items.sort(key=lambda i: (i.get("y", 0), i.get("x", 0)))
    
    # Strip whitespace to save tokens
    json_output = json.dumps(compressed_items, separators=(',', ':'))
    
    print(f"✅ Miro extraction complete! ({len(json_output)} chars)")
    
    return json_output