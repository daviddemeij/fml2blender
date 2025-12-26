"""
Asset harvester for Floorplanner FML files.

Parses FML files to extract asset IDs, resolves them via the Floorplanner API,
and downloads all required GLB models and texture files.
"""

import base64
import json
import os
import shutil
import subprocess
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
MATERIAL_MAP_FIELDS = ("texture", "bump", "reflection", "gloss")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Origin": "https://floorplanner.com",
    "Referer": "https://floorplanner.com/"
}


def convert_glb_v1_to_v2(local_path: str, asset_id: str) -> None:
    """Upgrade a GLB v1 file to GLB v2 via gltf-pipeline."""
    npx = shutil.which("npx")
    if not npx:
        raise RuntimeError(
            "GLB v1 detected but npx is not available. Install Node.js and gltf-pipeline: "
            "npm i -g gltf-pipeline"
        )

    tmp_path = f"{local_path}.tmp.glb"
    cmd = [npx, "-y", "gltf-pipeline", "-i", local_path, "-o", tmp_path]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            "Failed to convert GLB v1 for asset "
            f"{asset_id} using gltf-pipeline. Install gltf-pipeline via: npm i -g gltf-pipeline. "
            f"Command: {' '.join(cmd)}. stdout: {result.stdout.strip()} stderr: {result.stderr.strip()}"
        )

    shutil.move(tmp_path, local_path)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to convert GLB v1 for asset {asset_id}. Command: {' '.join(cmd)}. "
            f"stdout: {result.stdout.strip()} stderr: {result.stderr.strip()}"
        )


def validate_glb_version(local_path: str, asset_id: str) -> None:
    """Ensure GLB header advertises version 2; auto-upgrade from v1 when possible."""
    if not local_path.lower().endswith(".glb"):
        return

    def read_header() -> tuple[bytes, int]:
        with open(local_path, "rb") as f:
            header = f.read(12)
        if len(header) < 12:
            raise ValueError("missing GLB header")
        return header[:4], int.from_bytes(header[4:8], "little")

    magic, version = read_header()
    if magic != b"glTF":
        raise ValueError("missing glTF magic")

    if version == 2:
        return

    if version == 1:
        convert_glb_v1_to_v2(local_path, asset_id)
        magic, version = read_header()
        if magic == b"glTF" and version == 2:
            return
        raise ValueError(f"conversion did not produce GLB v2 (version={version})")

    raise ValueError(f"GLB version must be 2; got {version}")


def ensure_products_present(product_ids: list, manifest: dict) -> None:
    """Validate that every requested product exists on disk and is a GLB v2."""
    missing = []
    invalid = []

    for pid in set(product_ids):
        keys = [str(pid), str(pid).replace('rs-', '')]
        path = None
        for key in keys:
            if key in manifest:
                path = manifest[key]
                break

        if not path or not os.path.exists(path):
            missing.append(str(pid))
            continue

        try:
            validate_glb_version(path, pid)
        except Exception as exc:
            invalid.append(f"{pid} ({exc})")

    if missing or invalid:
        parts = []
        if missing:
            parts.append(f"missing: {', '.join(sorted(missing))}")
        if invalid:
            parts.append(f"invalid: {', '.join(sorted(invalid))}")
        raise RuntimeError("Unusable assets – " + "; ".join(parts))


def parse_fml_files(project_dir: str) -> tuple[list, list, dict]:
    """
    Parse all FML files in the project directory to extract asset IDs.
    
    Returns:
        Tuple of (product_ids, material_ids, opening_types)
        opening_types maps refid -> "window" | "door" when seen in openings
    """
    fml_files = glob(os.path.join(project_dir, "*.fml")) + \
                glob(os.path.join(project_dir, "*.fml.json"))
    
    if not fml_files:
        print("No .fml files found!")
        return [], [], {}

    product_ids = set()
    material_ids = set()
    opening_types = {}
    
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
                        if 'type' in opening and opening[key]:
                            opening_types.setdefault(str(opening[key]), opening.get('type'))
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

    return list(product_ids), list(material_ids), opening_types


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
                                "level": source.get('level'),
                            "has_opening": 'opening' in source,  # True for doors/windows
                            }
                            
                    elif asset_type == 'material':
                        texture_file = source.get('texture')
                        material_meta = dict(source)
                        material_meta["id"] = original_id
                        if texture_file:
                            url = build_material_url(texture_file)
                            resolved_urls[original_id] = url
                            resolved_urls[f"rs-{original_id}"] = url
                            material_meta["texture_url"] = url
                        for field in MATERIAL_MAP_FIELDS:
                            filename = source.get(field)
                            if filename:
                                material_meta[f"{field}_url"] = build_material_url(filename)
                        metadata[original_id] = material_meta
            else:
                print(f"  API error: {response.status_code}")
        
        except Exception as e:
            print(f"  Request failed: {e}")
            
        time.sleep(0.3)

    return resolved_urls, metadata


def download_assets(url_map: dict, assets_dir: str) -> dict:
    """
    Download assets to local directory, validating GLB files.
    
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

        if os.path.exists(local_path):
            skipped += 1
            validate_glb_version(local_path, original_id)
            manifest[original_id] = os.path.abspath(local_path)
            continue

        try:
            r = requests.get(url, headers=HEADERS, stream=True)
            if r.status_code != 200:
                raise RuntimeError(f"Failed to download {filename} ({r.status_code}) from {url}")

            with open(local_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
            downloaded += 1

            validate_glb_version(local_path, original_id)
            manifest[original_id] = os.path.abspath(local_path)

        except Exception as e:
            if os.path.exists(local_path):
                try:
                    os.remove(local_path)
                except OSError:
                    pass
            raise
    
    print(f"  Downloaded: {downloaded}, Skipped (cached): {skipped}")
    return manifest


def try_download_legacy_opening(asset_id: str, assets_dir: str) -> str | None:
    """Attempt to fetch legacy opening assets (numeric IDs) from known CDN patterns."""
    candidates = [
        f"https://d273csydae9vpp.cloudfront.net/assets/slices/{asset_id}/gltf/{asset_id}.glb",
        f"https://d1a6tkmtto0ap6.cloudfront.net/gltf/2.0/{asset_id}.glb",
    ]
    for url in candidates:
        filename = url.split('/')[-1]
        local_path = os.path.join(assets_dir, filename)

        r = requests.get(url, headers=HEADERS, stream=True)
        if r.status_code == 200:
            with open(local_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
            try:
                validate_glb_version(local_path, asset_id)
            except Exception:
                if os.path.exists(local_path):
                    os.remove(local_path)
                raise
            return os.path.abspath(local_path)



        if os.path.exists(local_path) and os.path.getsize(local_path) == 0:
            os.remove(local_path)
    return None


def build_material_url(filename: str) -> str:
    """Return an absolute URL for a material map filename."""
    if filename.startswith("http://") or filename.startswith("https://"):
        return filename
    return f"{CDN_MATERIALS_BASE_URL}{filename}"


def download_material_maps(materials_meta: dict, assets_dir: str) -> dict:
    """
    Download all referenced material map files (texture, bump, reflection, gloss).

    Returns:
        Updated materials_meta with local file paths under 'local_files'.
    """
    os.makedirs(assets_dir, exist_ok=True)
    cache: dict[str, str] = {}
    failed_urls: set[str] = set()

    def attempt_fetch(url: str, local_path: str) -> str | None:
        """Fetch a file, trying a fallback path when CloudFront returns 403/404."""
        try:
            r = requests.get(url, headers=HEADERS, stream=True)
        except Exception as exc:
            print(f"  Failed to fetch {url}: {exc}")
            return None

        if r.status_code == 200:
            with open(local_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
            return os.path.abspath(local_path)

        # Try a simple fallback when original path is blocked/missing
        if r.status_code in (403, 404) and "/original/" in url:
            alt_url = url.replace("/original/", "/")
            try:
                alt_resp = requests.get(alt_url, headers=HEADERS, stream=True)
                if alt_resp.status_code == 200:
                    with open(local_path, "wb") as f:
                        for chunk in alt_resp.iter_content(chunk_size=8192):
                            f.write(chunk)
                    print(f"  Fallback succeeded for {url} -> {alt_url}")
                    return os.path.abspath(local_path)
            except Exception:
                pass

        print(f"  Failed to download {os.path.basename(local_path)} ({r.status_code}) from {url}")
        return None

    for material_id, meta in materials_meta.items():
        local_files = {}
        missing_files = []
        fatal_missing = []
        for field in MATERIAL_MAP_FIELDS:
            url_key = f"{field}_url"
            url = meta.get(url_key)
            if not url:
                continue

            filename = url.split("/")[-1]
            local_path = os.path.join(assets_dir, filename)
            if filename.upper().startswith("WHITE_"):
                # Skip default white placeholders entirely (no download, no dummy)
                continue

            if url in cache:
                local_files[field] = cache[url]
                continue

            if os.path.exists(local_path):
                cache[url] = os.path.abspath(local_path)
                local_files[field] = cache[url]
                continue

            if url in failed_urls:
                missing_files.append(field)
                continue

            fetched = attempt_fetch(url, local_path)
            if fetched:
                cache[url] = fetched
                local_files[field] = fetched
            else:
                failed_urls.add(url)
                if os.path.exists(local_path):
                    try:
                        os.remove(local_path)
                    except OSError:
                        pass
                missing_files.append(field)
                # Only treat the primary texture as fatal; bump/reflection/gloss are optional
                if field == "texture":
                    fatal_missing.append((field, url))

        if local_files:
            meta["local_files"] = local_files
        if missing_files:
            meta["missing_files"] = sorted(set(missing_files))
            if fatal_missing:
                details = ", ".join(f"{field} ({url})" for field, url in fatal_missing)
                raise RuntimeError(f"Failed to download material maps: {material_id}: {details}")
    return materials_meta


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
    
    product_ids, material_ids, opening_types = parse_fml_files(project_dir)
    
    manifest = {}
    products = {}

    # Products (furniture, windows, doors)
    if product_ids:
        prod_urls, prod_meta = resolve_assets(product_ids, 'product', API_PRODUCTS_URL)
        manifest.update(download_assets(prod_urls, assets_dir))
        products.update(prod_meta)

        # Fail fast if any product ID could not be resolved/downloaded
        missing_products = []
        for pid in set(product_ids):
            base_id = str(pid).replace('rs-', '')
            if base_id not in manifest:
                missing_products.append(pid)

        # Try legacy CDN for numeric opening IDs
        if missing_products:
            os.makedirs(assets_dir, exist_ok=True)
            recovered = []
            for pid in list(missing_products):
                base_id = str(pid).replace('rs-', '')
                if not base_id.isdigit():
                    continue
                legacy_path = try_download_legacy_opening(base_id, assets_dir)
                if legacy_path:
                    manifest[base_id] = legacy_path
                    otype = opening_types.get(str(pid), "opening")
                    products[base_id] = {
                        "name": f"Legacy {otype} {base_id}",
                        "level": -1,
                        "has_opening": True,
                    }
                    recovered.append(pid)
            missing_products = [pid for pid in missing_products if pid not in recovered]

        if missing_products:
            raise RuntimeError(f"Missing assets for IDs: {', '.join(sorted(map(str, missing_products)))}")

        ensure_products_present(product_ids, manifest)

    # Materials (textures)
    if material_ids:
        mat_urls, mat_meta = resolve_assets(material_ids, 'material', API_MATERIALS_URL)
        manifest.update(download_assets(mat_urls, assets_dir))
        materials = download_material_maps(mat_meta, assets_dir)
    else:
        materials = {}
    
    # Save manifest and metadata
    manifest_path = os.path.join(project_dir, "manifest.json")
    products_path = os.path.join(project_dir, "products.json")
    materials_path = os.path.join(project_dir, "materials.json")
    
    with open(manifest_path, 'w') as f:
        json.dump(manifest, f, indent=2)
    
    with open(products_path, 'w') as f:
        json.dump(products, f, indent=2)
    
    with open(materials_path, 'w') as f:
        json.dump(materials, f, indent=2)
    
    print(f"\nSaved manifest.json ({len(manifest)} assets)")
    print(f"Saved products.json ({len(products)} products)")
    print(f"Saved materials.json ({len(materials)} materials)")
    
    return manifest, products


if __name__ == "__main__":
    import sys
    project_dir = sys.argv[1] if len(sys.argv) > 1 else "."
    harvest(project_dir)
