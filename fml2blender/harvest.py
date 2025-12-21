"""
Asset harvester for Floorplanner FML files.

Parses FML files to extract asset IDs, resolves them via the Floorplanner API,
and downloads all required GLB models and texture files.
"""

import json
import os
import time
from glob import glob

import requests

# =============================================================================
# API CONFIGURATION
# =============================================================================

API_PRODUCTS_URL = "https://search.floorplanner.com/products/ids"
API_MATERIALS_URL = "https://search.floorplanner.com/materials/ids"
CDN_GLB_BASE_URL = "https://fp-gltf-lq-cdn.floorplanner.com/"
CDN_MATERIALS_BASE_URL = "https://d2bi8gvwsa8xa3.cloudfront.net/cdb/textures/floor_and_wall/original/"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Origin": "https://floorplanner.com",
    "Referer": "https://floorplanner.com/"
}


def parse_fml_files(project_dir: str) -> tuple[list, list]:
    """
    Parse all FML files in the project directory to extract asset IDs.
    
    Returns:
        Tuple of (product_ids, material_ids)
    """
    fml_files = glob(os.path.join(project_dir, "*.fml")) + \
                glob(os.path.join(project_dir, "*.fml.json"))
    
    if not fml_files:
        print("No .fml files found!")
        return [], []

    product_ids = set()
    material_ids = set()
    
    print(f"Parsing {len(fml_files)} FML files...")
    
    def extract_from_design(design):
        """Extract IDs from a single design/floor."""
        items_count = 0
        walls_count = 0
        
        # Furniture items
        items = design.get('objects', []) + design.get('items', [])
        for item in items:
            for key in ['refid', 'asset_id', 'model_id']:
                if key in item and item[key]:
                    product_ids.add(item[key])
                    items_count += 1
                    break

        # Walls & Openings
        lines = design.get('lines', []) + design.get('walls', [])
        walls_count = len(lines)
        for line in lines:
            # Wall textures
            decor = line.get('decor', {})
            if decor:
                for side in ['left', 'right']:
                    if decor.get(side) and 'refid' in decor[side]:
                        material_ids.add(decor[side]['refid'])

            # Windows/Doors
            for opening in line.get('openings', []):
                for key in ['refid', 'asset_id', 'model_id']:
                    if key in opening and opening[key]:
                        product_ids.add(opening[key])
                        break

        # Floor textures
        for area in design.get('areas', []):
            if 'refid' in area:
                material_ids.add(area['refid'])
            if 'decor' in area and 'refid' in area['decor']:
                material_ids.add(area['decor']['refid'])
        
        # Surface textures
        for surface in design.get('surfaces', []):
            if 'refid' in surface:
                material_ids.add(surface['refid'])
        
        return items_count, walls_count
    
    for fml_path in fml_files:
        try:
            with open(fml_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            total_items = 0
            total_walls = 0
            
            # Check if this is a nested project file (has 'floors' array)
            if 'floors' in data:
                for floor in data['floors']:
                    for design in floor.get('designs', []):
                        items, walls = extract_from_design(design)
                        total_items += items
                        total_walls += walls
            else:
                # Flat structure (single floor)
                items, walls = extract_from_design(data)
                total_items += items
                total_walls += walls

            print(f"  {os.path.basename(fml_path)}: {total_items} items, {total_walls} walls")
                
        except Exception as e:
            print(f"  Error parsing {fml_path}: {e}")

    return list(product_ids), list(material_ids)


def resolve_assets(id_list: list, asset_type: str, api_url: str) -> tuple[dict, dict]:
    """
    Resolve asset IDs to download URLs via the Floorplanner API.
    
    Returns:
        Tuple of (url_map, metadata)
    """
    if not id_list:
        return {}, {}

    resolved_urls = {}
    metadata = {}
    chunk_size = 50
    
    clean_ids = list(set([str(i).replace('rs-', '') for i in id_list]))
    
    print(f"Resolving {len(clean_ids)} {asset_type}s...")

    for i in range(0, len(clean_ids), chunk_size):
        chunk = clean_ids[i:i + chunk_size]
        
        try:
            response = requests.post(api_url, json={"ids": chunk}, headers=HEADERS)
            
            if response.status_code == 200:
                hits = response.json().get('hits', {}).get('hits', [])
                
                for hit in hits:
                    source = hit.get('_source', {})
                    original_id = hit['_id']
                    
                    if asset_type == 'product':
                        model_str = source.get('model')
                        if model_str:
                            resolved_urls[original_id] = f"{CDN_GLB_BASE_URL}{model_str}.glb"
                            metadata[original_id] = {
                                "name": source.get('name'),
                                "width": source.get('width'),
                                "height": source.get('height'),
                                "depth": source.get('depth'),
                                "bbox_min": source.get('bbox_min'),
                                "bbox_max": source.get('bbox_max'),
                            }
                            
                    elif asset_type == 'material':
                        texture_file = source.get('texture')
                        if texture_file:
                            url = f"{CDN_MATERIALS_BASE_URL}{texture_file}"
                            resolved_urls[original_id] = url
                            resolved_urls[f"rs-{original_id}"] = url
            else:
                print(f"  API error: {response.status_code}")
        
        except Exception as e:
            print(f"  Request failed: {e}")
            
        time.sleep(0.3)

    return resolved_urls, metadata


def download_assets(url_map: dict, assets_dir: str) -> dict:
    """
    Download assets to local directory.
    
    Returns:
        Manifest mapping asset IDs to local file paths.
    """
    os.makedirs(assets_dir, exist_ok=True)
        
    manifest = {}
    downloaded = 0
    skipped = 0
    
    print(f"Downloading {len(url_map)} assets...")
    
    for original_id, url in url_map.items():
        filename = url.split('/')[-1]
        local_path = os.path.join(assets_dir, filename)
        manifest[original_id] = os.path.abspath(local_path)
        
        if os.path.exists(local_path):
            skipped += 1
            continue
            
        try:
            r = requests.get(url, headers=HEADERS, stream=True)
            if r.status_code == 200:
                with open(local_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
                downloaded += 1
            else:
                print(f"  Failed: {filename} ({r.status_code})")
        except Exception as e:
            print(f"  Error downloading {filename}: {e}")
    
    print(f"  Downloaded: {downloaded}, Skipped (cached): {skipped}")
    return manifest


def harvest(project_dir: str, assets_dir: str = None) -> tuple[dict, dict]:
    """
    Main harvest function - parse FML files and download all assets.
    
    Args:
        project_dir: Directory containing FML files
        assets_dir: Directory to save assets (default: project_dir/assets)
    
    Returns:
        Tuple of (manifest, products_metadata)
    """
    if assets_dir is None:
        assets_dir = os.path.join(project_dir, "assets")
    
    print(f"\n=== Harvesting assets from {project_dir} ===\n")
    
    product_ids, material_ids = parse_fml_files(project_dir)
    
    manifest = {}
    products = {}

    # Products (furniture, windows, doors)
    if product_ids:
        prod_urls, prod_meta = resolve_assets(product_ids, 'product', API_PRODUCTS_URL)
        manifest.update(download_assets(prod_urls, assets_dir))
        products.update(prod_meta)

    # Materials (textures)
    if material_ids:
        mat_urls, _ = resolve_assets(material_ids, 'material', API_MATERIALS_URL)
        manifest.update(download_assets(mat_urls, assets_dir))
    
    # Save manifest and metadata
    manifest_path = os.path.join(project_dir, "manifest.json")
    products_path = os.path.join(project_dir, "products.json")
    
    with open(manifest_path, 'w') as f:
        json.dump(manifest, f, indent=2)
    
    with open(products_path, 'w') as f:
        json.dump(products, f, indent=2)
    
    print(f"\nSaved manifest.json ({len(manifest)} assets)")
    print(f"Saved products.json ({len(products)} products)")
    
    return manifest, products


if __name__ == "__main__":
    import sys
    project_dir = sys.argv[1] if len(sys.argv) > 1 else "."
    harvest(project_dir)
