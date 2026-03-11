import re
import httpx
import urllib.parse
import json
import os
import base64

async def fetch_and_compress_figma(figma_url: str, access_token: str):
    print(f"🚨 DEBUG: fetch_and_compress_figma triggered!")
    
    if not access_token:
        raise ValueError("Missing Figma Access Token. User must link their account.")

    # 1. Safely Extract File Key & Node ID
    file_key_match = re.search(r"figma\.com/(?:file|design)/([a-zA-Z0-9]{22,})", figma_url)
    if not file_key_match:
        raise ValueError("Invalid Figma URL format. Could not find the file key.")
    file_key = file_key_match.group(1)
    
    node_id_match = re.search(r"node-id=([^&#]+)", figma_url)
    if not node_id_match:
        raise ValueError("Please provide a link to a specific Frame, not the whole file.")
    
    node_id = node_id_match.group(1)
    node_id = urllib.parse.unquote(node_id).replace("-", ":")

    # 2. Fetch the Node from Figma API
    headers = {"Authorization": f"Bearer {access_token}"}
    api_url = f"https://api.figma.com/v1/files/{file_key}/nodes?ids={node_id}"
    
    async with httpx.AsyncClient() as client:
        print(f"🎨 Fetching Figma Node {node_id}...")
        resp = await client.get(api_url, headers=headers, timeout=15.0)
        
        if resp.status_code != 200:
            raise Exception(f"Figma API Error ({resp.status_code}): {resp.text}")
            
        data = resp.json()
        
    nodes = data.get("nodes", {})
    if not nodes or node_id not in nodes:
        raise Exception("Could not find that specific node in the file.")
        
    target_node = nodes[node_id].get("document", {})

    # ====================================================================
    # 🗜️ 3. THE "SMART FIDELITY" COMPRESSOR 
    # ====================================================================
    def rgb_to_hex(color_dict):
        """Helper to convert Figma RGB to Hex safely"""
        if not color_dict: return None
        return "#{:02x}{:02x}{:02x}".format(
            int(color_dict.get('r',0)*255), 
            int(color_dict.get('g',0)*255), 
            int(color_dict.get('b',0)*255)
        )

    def compress_node(node):
        if not isinstance(node, dict): return None
        
        # 1. Purge Invisible Layers instantly
        if node.get("visible") is False:
            return None
            
        node_type = node.get("type", "")
        compressed = {"type": node_type}
        if "name" in node: compressed["name"] = node["name"]

        # Capture exact Width and Height for sizing context
        bbox = node.get("absoluteBoundingBox", {})
        if bbox:
            if "width" in bbox: compressed["w"] = round(bbox["width"])
            if "height" in bbox: compressed["h"] = round(bbox["height"])
        
        # 2. Vector Translation (Keep dimensions, drop math)
        if node_type in ["VECTOR", "BOOLEAN_OPERATION", "STAR", "LINE", "ELLIPSE", "REGULAR_POLYGON"]:
            return {"type": "ICON", "name": compressed.get("name", "Graphic"), "w": compressed.get("w"), "h": compressed.get("h")}
            
        # 3. Typography Fidelity
        if node_type == "TEXT":
            compressed["text"] = node.get("characters", "").strip()
            style = node.get("style", {})
            if style:
                if "fontSize" in style: compressed["fontSize"] = style["fontSize"]
                if "fontWeight" in style: compressed["fontWeight"] = style["fontWeight"]
                if "textAlignHorizontal" in style: compressed["textAlign"] = style["textAlignHorizontal"]
            
            # Text Color
            if "fills" in node and isinstance(node["fills"], list):
                for f in node["fills"]:
                    if f.get("type") == "SOLID" and f.get("visible", True):
                        compressed["color"] = rgb_to_hex(f.get("color"))
                        break
            return compressed # Text has no children, return early
            
        # 4. Auto-Layout & Spacing (Crucial for Tailwind)
        if "layoutMode" in node and node["layoutMode"] != "NONE":
            compressed["layout"] = node["layoutMode"] # HORIZONTAL or VERTICAL
            if "primaryAxisAlignItems" in node: compressed["alignX"] = node["primaryAxisAlignItems"]
            if "counterAxisAlignItems" in node: compressed["alignY"] = node["counterAxisAlignItems"]
            if "itemSpacing" in node and node["itemSpacing"] > 0: compressed["gap"] = node["itemSpacing"]
            
            # Paddings
            px = node.get("paddingLeft", 0)
            py = node.get("paddingTop", 0)
            if px > 0: compressed["padX"] = px
            if py > 0: compressed["padY"] = py
            
        # 5. Visual Styling (Backgrounds, Borders, Shadows)
        if "cornerRadius" in node and node["cornerRadius"] > 0:
            compressed["radius"] = node["cornerRadius"]
            
        # Background Color
        if "fills" in node and isinstance(node["fills"], list):
            for f in node["fills"]:
                if f.get("type") == "SOLID" and f.get("visible", True):
                    compressed["bg"] = rgb_to_hex(f.get("color"))
                    break
                    
        # Borders
        if "strokes" in node and isinstance(node["strokes"], list) and len(node["strokes"]) > 0:
            for s in node["strokes"]:
                 if s.get("type") == "SOLID" and s.get("visible", True):
                     compressed["borderColor"] = rgb_to_hex(s.get("color"))
                     compressed["borderWidth"] = node.get("strokeWeight", 1)
                     break
                     
        # Drop Shadows
        if "effects" in node and isinstance(node["effects"], list):
            for e in node["effects"]:
                if e.get("type") == "DROP_SHADOW" and e.get("visible", True):
                    compressed["hasShadow"] = True
                    break

        # 6. Process Children recursively
        if "children" in node and isinstance(node["children"], list):
            valid_children = []
            for child in node["children"]:
                comp_child = compress_node(child)
                if comp_child:
                    valid_children.append(comp_child)
            
            if valid_children:
                # Group raw vectors into a single ICON wrapper so the AI knows it's a unified graphic
                if all(c.get("type") == "ICON" for c in valid_children):
                    return {"type": "COMPLEX_ICON", "name": node.get("name", "Graphic_Group"), "w": compressed.get("w"), "h": compressed.get("h")}
                
                compressed["children"] = valid_children
            else:
                # If a frame is completely empty (no bg, no border, no children), purge it
                if "bg" not in compressed and "borderColor" not in compressed and "text" not in compressed:
                    return None
                    
        return compressed

    print("🗜️ Running Smart Fidelity Compression...")
    optimized_tree = compress_node(target_node)
    
    # Strip whitespace to save tokens for the AI payload
    json_output = json.dumps(optimized_tree, separators=(',', ':')) 
    
    # ====================================================================
    # 🖼️ 4. FETCH FIGMA RENDERED IMAGE (For Snapshots & UI)
    # ====================================================================
    img_b64 = None
    img_api_url = f"https://api.figma.com/v1/images/{file_key}?ids={node_id}&format=jpg&scale=1"
    
    try:
        print(f"🖼️ Asking Figma to render Node {node_id} as an image...")
        async with httpx.AsyncClient() as client:
            img_resp = await client.get(img_api_url, headers=headers, timeout=15.0)
            
            if img_resp.status_code == 200:
                img_data = img_resp.json()
                img_url = img_data.get("images", {}).get(node_id)
                
                if img_url:
                    print(f"⬇️ Downloading rendered JPG from Figma S3...")
                    actual_img_resp = await client.get(img_url, timeout=15.0)
                    
                    if actual_img_resp.status_code == 200:
                        b64_bytes = base64.b64encode(actual_img_resp.content).decode('utf-8')
                        img_b64 = f"data:image/jpeg;base64,{b64_bytes}"
                        print("✅ Figma Image successfully converted to Base64!")
    except Exception as e:
        print(f"⚠️ Warning: Could not fetch Figma image preview: {e}")

    # --- DEBUG SAVER ---
    char_count = len(json_output)
    est_tokens = char_count // 4 
    
    print(f"\n✅ SMART COMPRESSION COMPLETE!")
    print(f"📊 Payload Size: {char_count:,} characters")
    print(f"🪙 Estimated Tokens: ~{est_tokens:,} tokens")
    
    debug_path = os.path.join(os.getcwd(), "debug_figma_payload.json")
    with open(debug_path, "w", encoding="utf-8") as f:
        f.write(json.dumps(optimized_tree, indent=2))
        
    return json_output, img_b64