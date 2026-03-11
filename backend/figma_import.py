import re
import httpx
import urllib.parse
import json
import os

async def fetch_and_compress_figma(figma_url: str, access_token: str) -> str:
    print(f"🚨 DEBUG: fetch_and_compress_figma triggered!")
    print(f"🚨 DEBUG: URL received: {figma_url}")
    print(f"🚨 DEBUG: Token exists: {bool(access_token)}")
    """
    Parses a Figma URL, fetches the specific node data, and compresses it 
    into a lightweight JSON tree optimized for LLM context windows.
    """
    if not access_token:
        raise ValueError("Missing Figma Access Token. User must link their account.")

    # 1. Extract File Key and Node ID
    match = re.search(r"figma\.com/(?:file|design)/([a-zA-Z0-9]{22,}).*?(?:node-id=([^&]+))?", figma_url)
    if not match:
        raise ValueError("Invalid Figma URL format.")
        
    file_key = match.group(1)
    node_id = match.group(2)
    
    if node_id:
        node_id = urllib.parse.unquote(node_id).replace("-", ":")
    else:
        raise ValueError("Please provide a link to a specific Frame, not the whole file.")

    # 2. Fetch the Node from Figma API
    headers = {"Authorization": f"Bearer {access_token}"}
    api_url = f"https://api.figma.com/v1/files/{file_key}/nodes?ids={node_id}"
    
    async with httpx.AsyncClient() as client:
        print(f"🎨 Fetching Figma Node {node_id} from File {file_key}...")
        resp = await client.get(api_url, headers=headers, timeout=15.0)
        
        if resp.status_code != 200:
            raise Exception(f"Figma API Error ({resp.status_code}): {resp.text}")
            
        data = resp.json()
        
    nodes = data.get("nodes", {})
    if not nodes or node_id not in nodes:
        raise Exception("Could not find that specific node in the file.")
        
    target_node = nodes[node_id].get("document", {})

    # 3. The "Token-Saver" Compressor 
    # Figma JSON is massive. We recursively strip out vector math and keep only layout/CSS data.
    def compress_node(node):
        if not isinstance(node, dict): return node
        
        # We only care about layout, text, and styling
        keys_to_keep = [
            "name", "type", "characters", "fills", "strokes", 
            "layoutMode", "primaryAxisAlignItems", "counterAxisAlignItems",
            "paddingLeft", "paddingRight", "paddingTop", "paddingBottom", 
            "itemSpacing", "cornerRadius", "absoluteBoundingBox", "style"
        ]
        
        compressed = {k: node[k] for k in keys_to_keep if k in node}
        
        # Clean up fills (colors) to just hex codes if possible
        if "fills" in compressed and isinstance(compressed["fills"], list):
            simplified_fills = []
            for f in compressed["fills"]:
                if f.get("type") == "SOLID" and "color" in f:
                    c = f["color"]
                    hex_color = "#{:02x}{:02x}{:02x}".format(int(c.get('r',0)*255), int(c.get('g',0)*255), int(c.get('b',0)*255))
                    simplified_fills.append(hex_color)
            if simplified_fills:
                compressed["fills"] = simplified_fills
            else:
                del compressed["fills"]

        # Recurse through children
        if "children" in node and isinstance(node["children"], list):
            compressed_children = [compress_node(child) for child in node["children"]]
            # Filter out empty or invisible vector junk
            compressed["children"] = [c for c in compressed_children if c and c.get("type") not in ["VECTOR", "BOOLEAN_OPERATION"]]
            
        return compressed

    print("🗜️ Compressing Figma JSON for AI consumption...")
    optimized_tree = compress_node(target_node)
    
    json_output = json.dumps(optimized_tree, indent=2)
    
    # ====================================================================
    # 🛑 TOKEN SAFETY & DEBUGGING BLOCK
    # ====================================================================
    char_count = len(json_output)
    est_tokens = char_count // 4 # Rough rule of thumb: 4 chars = 1 token
    
    print(f"\n✅ COMPRESSION COMPLETE!")
    print(f"📊 Payload Size: {char_count:,} characters")
    print(f"🪙 Estimated LLM Tokens: ~{est_tokens:,} tokens")
    
    # Save it locally so you can open it in your IDE and verify it
    debug_path = os.path.join(os.getcwd(), "debug_figma_payload.json")
    with open(debug_path, "w", encoding="utf-8") as f:
        f.write(json_output)
    print(f"📁 Full JSON saved to: {debug_path}\n")
    # ====================================================================
    
    return json_output