"""
Blender scene builder for Floorplanner FML files.

Converts FML floor plan data to Blender 3D scenes with:
- Walls with proper materials and textures
- Floors and surfaces (roofs, platforms)
- Windows and doors with wall cutouts
- Furniture and decorations
- Glass materials for windows
"""

import json
import os
import math

# Blender imports (only available when running inside Blender)
try:
    import bpy
    from mathutils import Vector, Matrix
    HAS_BLENDER = True
except ImportError:
    HAS_BLENDER = False

# =============================================================================
# CONFIGURATION
# =============================================================================

SCALE = 0.01  # FML uses centimeters, Blender uses meters
DEFAULT_WALL_THICKNESS = 0.15
LEVEL_HEIGHT = 2.8

# Asset IDs to skip (non-visible items)
SKIP_ASSET_IDS = {
    "200",       # Simple door (cutout marker only)
    "rs-2689",   # Wall finish texture
    "2689",
}

# Object name patterns to skip
SKIP_OBJECT_PATTERNS = {
    "FP_CUTTER",
    "FP_HANDLES",
    "CUTTER",
}


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def clean_scene():
    """Remove all objects and collections from the scene."""
    if bpy.context.object:
        bpy.ops.object.mode_set(mode='OBJECT')
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete()
    for collection in bpy.data.collections:
        bpy.data.collections.remove(collection)


def load_json(project_dir: str, filename: str):
    """Load a JSON file from the project directory."""
    path = os.path.join(project_dir, filename)
    if not os.path.exists(path):
        return None
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def create_collection(name: str):
    """Create and link a new collection."""
    coll = bpy.data.collections.new(name)
    bpy.context.scene.collection.children.link(coll)
    return coll


def fml_to_blender(x, y, z=0, base_z=0):
    """Convert FML coordinates (cm, Y-down) to Blender (m, Z-up)."""
    return (x * SCALE, -y * SCALE, base_z + z * SCALE)


def srgb_to_linear(c):
    """Convert sRGB color component to linear color space."""
    if c <= 0.04045:
        return c / 12.92
    return ((c + 0.055) / 1.055) ** 2.4


def hex_to_linear_rgb(hex_color):
    """Convert hex color string to linear RGB tuple."""
    h = hex_color.lstrip('#')
    srgb = tuple(int(h[i:i+2], 16) / 255 for i in (0, 2, 4))
    return tuple(srgb_to_linear(c) for c in srgb)


def get_or_create_material(name, color=None, hex_color=None, texture_path=None):
    """Get or create a material with the given color or texture."""
    mat = bpy.data.materials.get(name)
    if mat:
        return mat
    
    mat = bpy.data.materials.new(name=name)
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    bsdf = nodes.get("Principled BSDF")
    
    if bsdf:
        if texture_path and os.path.exists(texture_path):
            tex_node = nodes.new('ShaderNodeTexImage')
            tex_node.location = (-300, 300)
            tex_node.projection = 'BOX'
            tex_node.projection_blend = 0.2
            
            img = bpy.data.images.load(texture_path)
            tex_node.image = img
            
            tex_coord = nodes.new('ShaderNodeTexCoord')
            tex_coord.location = (-700, 300)
            
            mapping = nodes.new('ShaderNodeMapping')
            mapping.location = (-500, 300)
            mapping.inputs['Scale'].default_value = (2.0, 2.0, 2.0)
            
            links.new(tex_coord.outputs['Object'], mapping.inputs['Vector'])
            links.new(mapping.outputs['Vector'], tex_node.inputs['Vector'])
            links.new(tex_node.outputs['Color'], bsdf.inputs['Base Color'])
        elif hex_color and hex_color.startswith('#'):
            rgb = hex_to_linear_rgb(hex_color)
            bsdf.inputs['Base Color'].default_value = (*rgb, 1)
        elif color:
            if all(c <= 1.0 for c in color):
                linear_color = tuple(srgb_to_linear(c) for c in color)
            else:
                linear_color = color
            bsdf.inputs['Base Color'].default_value = (*linear_color, 1)
    
    return mat


def setup_glass_material(obj):
    """Configure glass materials for transparency."""
    if 'GLASS' not in obj.name.upper():
        return
    
    glass_mat = bpy.data.materials.get("Glass_Transparent")
    if not glass_mat:
        glass_mat = bpy.data.materials.new(name="Glass_Transparent")
        glass_mat.use_nodes = True
        nodes = glass_mat.node_tree.nodes
        links = glass_mat.node_tree.links
        
        nodes.clear()
        
        output = nodes.new('ShaderNodeOutputMaterial')
        output.location = (400, 0)
        
        principled = nodes.new('ShaderNodeBsdfPrincipled')
        principled.location = (0, 0)
        principled.inputs['Base Color'].default_value = (0.95, 0.97, 1.0, 1.0)
        principled.inputs['Roughness'].default_value = 0.0
        principled.inputs['IOR'].default_value = 1.45
        principled.inputs['Transmission Weight'].default_value = 1.0
        principled.inputs['Alpha'].default_value = 0.1
        
        links.new(principled.outputs['BSDF'], output.inputs['Surface'])
        
        glass_mat.blend_method = 'BLEND'
        glass_mat.use_backface_culling = False
        glass_mat.use_screen_refraction = True
    
    if obj.data and hasattr(obj.data, 'materials'):
        if obj.data.materials:
            obj.data.materials[0] = glass_mat
        else:
            obj.data.materials.append(glass_mat)


def apply_material(obj, mat):
    """Apply material to object."""
    if obj.data and hasattr(obj.data, 'materials'):
        if obj.data.materials:
            obj.data.materials[0] = mat
        else:
            obj.data.materials.append(mat)


# =============================================================================
# GEOMETRY BUILDERS
# =============================================================================

def create_floor(area_data, base_z, collection, manifest=None):
    """Create a floor polygon from area data."""
    poly = area_data.get('poly', [])
    if len(poly) < 3:
        return None
    
    name = area_data.get('name', "Floor")
    
    verts = [fml_to_blender(p['x'], p['y'], 0, base_z) for p in poly]
    faces = [list(range(len(verts)))]
    
    mesh = bpy.data.meshes.new(f"{name}Mesh")
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    
    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)
    
    refid = area_data.get('refid')
    color = area_data.get('color')
    
    if refid and manifest:
        texture_path = manifest.get(refid)
        if texture_path and texture_path.endswith(('.jpg', '.jpeg', '.png')):
            mat = get_or_create_material(f"FloorTexture_{refid}", texture_path=texture_path)
            apply_material(obj, mat)
        elif color:
            mat = get_or_create_material(f"Floor_{color}", hex_color=color)
            apply_material(obj, mat)
    elif color:
        mat = get_or_create_material(f"Floor_{color}", hex_color=color)
        apply_material(obj, mat)
    
    return obj


def create_surface(surface_data, base_z, collection, manifest=None):
    """Create a 3D surface (roof, platform) from surface data."""
    poly = surface_data.get('poly', [])
    if len(poly) < 3 or surface_data.get('isCutout'):
        return None
    
    name = surface_data.get('customName') or surface_data.get('name', "Surface")
    is_roof = surface_data.get('isRoof', False)
    thickness = surface_data.get('thickness', 0) * SCALE
    
    top_verts = []
    for p in poly:
        z = p.get('z', 0)
        pos = fml_to_blender(p['x'], p['y'], z, base_z)
        top_verts.append(Vector(pos))
    
    if thickness > 0.001:
        bottom_verts = [Vector((v.x, v.y, v.z - thickness)) for v in top_verts]
        verts = top_verts + bottom_verts
        n = len(top_verts)
        
        faces = [list(range(n))]
        faces.append(list(range(2*n - 1, n - 1, -1)))
        for i in range(n):
            next_i = (i + 1) % n
            faces.append([i, next_i, next_i + n, i + n])
    else:
        verts = top_verts
        faces = [list(range(len(verts)))]
    
    mesh = bpy.data.meshes.new(f"{name}Mesh")
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    
    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)
    
    color = surface_data.get('color')
    refid = surface_data.get('refid')
    
    if refid and manifest:
        texture_path = manifest.get(refid)
        if texture_path and texture_path.endswith(('.jpg', '.jpeg', '.png')):
            mat = get_or_create_material(f"SurfaceTexture_{refid}", texture_path=texture_path)
            apply_material(obj, mat)
            return obj
    
    if color:
        mat = get_or_create_material(f"Surface_{color}", hex_color=color)
        apply_material(obj, mat)
    else:
        mat_name = "Roof_Default" if is_roof else "Surface_Default"
        mat = get_or_create_material(mat_name, color=(0.95, 0.95, 0.95))
        apply_material(obj, mat)
    
    return obj


def cut_wall_opening(wall_obj, opening_info, base_z):
    """Cut a hole in the wall for a window or door."""
    if not wall_obj or not opening_info:
        return
    
    x = opening_info['x'] * SCALE
    y = -opening_info['y'] * SCALE
    z = opening_info['z'] * SCALE + base_z
    width = opening_info['width'] * SCALE
    height = opening_info['height'] * SCALE
    thickness = opening_info['thickness'] * SCALE * 2
    angle = math.radians(-opening_info['wall_angle'])
    
    bpy.ops.mesh.primitive_cube_add(size=1)
    cutter = bpy.context.active_object
    cutter.name = "WallCutter"
    
    cutter.scale = (width, thickness, height)
    cutter.location = (x, y, z + height / 2)
    cutter.rotation_euler = (0, 0, angle)
    
    bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
    
    bool_mod = wall_obj.modifiers.new(name="CutOpening", type='BOOLEAN')
    bool_mod.operation = 'DIFFERENCE'
    bool_mod.object = cutter
    bool_mod.solver = 'EXACT'
    
    bpy.context.view_layer.objects.active = wall_obj
    bpy.ops.object.modifier_apply(modifier=bool_mod.name)
    
    bpy.data.objects.remove(cutter, do_unlink=True)


def create_wall(wall_data, base_z, collection, manifest=None):
    """Create a wall with proper materials for each side."""
    p1, p2 = wall_data.get('a'), wall_data.get('b')
    if not p1 or not p2:
        return None
    
    h1 = wall_data.get('az', {}).get('h', 0) * SCALE
    h2 = wall_data.get('bz', {}).get('h', 0) * SCALE
    if h1 < 0.01 and h2 < 0.01:
        return None
    
    thickness = wall_data.get('thickness', DEFAULT_WALL_THICKNESS * 100) * SCALE
    if thickness < 0.01:
        thickness = DEFAULT_WALL_THICKNESS
    
    v_start = Vector(fml_to_blender(p1['x'], p1['y'], 0, base_z))
    v_end = Vector(fml_to_blender(p2['x'], p2['y'], 0, base_z))
    
    wall_vec = v_end - v_start
    if wall_vec.length < 0.01:
        return None
    
    balance = wall_data.get('balance', 0.5)
    
    perp = Vector((-wall_vec.y, wall_vec.x, 0)).normalized()
    offset_left = perp * (thickness * balance)
    offset_right = perp * (thickness * (1 - balance))
    
    b1 = v_start + offset_left
    b2 = v_start - offset_right
    b3 = v_end - offset_right
    b4 = v_end + offset_left
    
    t1 = b1.copy(); t1.z += h1
    t2 = b2.copy(); t2.z += h1
    t3 = b3.copy(); t3.z += h2
    t4 = b4.copy(); t4.z += h2
    
    verts = [b1, b2, b3, b4, t1, t2, t3, t4]
    
    faces = [
        (0, 1, 2, 3),  # bottom
        (4, 7, 6, 5),  # top
        (0, 4, 5, 1),  # start cap
        (2, 6, 7, 3),  # end cap
        (1, 5, 6, 2),  # right side
        (3, 7, 4, 0),  # left side
    ]
    
    mesh = bpy.data.meshes.new("WallMesh")
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    
    obj = bpy.data.objects.new("Wall", mesh)
    collection.objects.link(obj)
    
    decor = wall_data.get('decor', {})
    default_mat = get_or_create_material("Wall_Default", color=(0.9, 0.9, 0.9))
    
    def get_decor_material(decor_data):
        if not decor_data or not isinstance(decor_data, dict):
            return default_mat
        
        if 'color' in decor_data:
            return get_or_create_material(f"WallFinish_{decor_data['color']}", hex_color=decor_data['color'])
        elif 'refid' in decor_data and manifest:
            refid = decor_data['refid']
            texture_path = manifest.get(refid)
            if texture_path and texture_path.endswith(('.jpg', '.jpeg', '.png')):
                return get_or_create_material(f"WallTexture_{refid}", texture_path=texture_path)
            return get_or_create_material(f"WallFinish_{refid}", color=(0.95, 0.93, 0.88))
        return default_mat
    
    left_mat = get_decor_material(decor.get('left'))
    right_mat = get_decor_material(decor.get('right'))
    
    mesh.materials.append(default_mat)  # bottom
    mesh.materials.append(default_mat)  # top
    mesh.materials.append(default_mat)  # start cap
    mesh.materials.append(default_mat)  # end cap
    mesh.materials.append(right_mat)    # right side
    mesh.materials.append(left_mat)     # left side
    
    for i, face in enumerate(mesh.polygons):
        face.material_index = i
    
    return obj


def import_asset(item_data, manifest, products, base_z, collection):
    """Import and position a GLB asset based on FML item data."""
    asset_id = item_data.get('refid') or item_data.get('asset_id')
    if not asset_id or asset_id in SKIP_ASSET_IDS:
        return None
    
    filepath = manifest.get(asset_id)
    if not filepath or not os.path.exists(filepath):
        alt_id = asset_id.replace('rs-', '') if 'rs-' in asset_id else f"rs-{asset_id}"
        filepath = manifest.get(alt_id)
        if not filepath or not os.path.exists(filepath):
            return None
    
    if not filepath.endswith('.glb'):
        return None
    
    try:
        bpy.ops.import_scene.gltf(filepath=filepath)
    except Exception as e:
        print(f"Failed to import {filepath}: {e}")
        return None
    
    selected = bpy.context.selected_objects
    if not selected:
        return None
    
    # Filter out non-visible objects
    visible_objects = []
    for obj in selected:
        skip = any(pattern in obj.name.upper() for pattern in SKIP_OBJECT_PATTERNS)
        if skip:
            bpy.data.objects.remove(obj, do_unlink=True)
        else:
            visible_objects.append(obj)
    
    if not visible_objects:
        return None
    
    # Make single user
    bpy.ops.object.select_all(action='DESELECT')
    for obj in visible_objects:
        obj.select_set(True)
    bpy.ops.object.make_single_user(type='ALL', object=True, obdata=True)
    bpy.context.view_layer.update()
    
    visible_objects = list(bpy.context.selected_objects)
    
    # Apply transforms from glTF import
    for obj in visible_objects:
        bpy.ops.object.select_all(action='DESELECT')
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
    
    # Calculate bounding box to detect Y-up orientation
    temp_min = Vector((float('inf'), float('inf'), float('inf')))
    temp_max = Vector((float('-inf'), float('-inf'), float('-inf')))
    for obj in visible_objects:
        if obj.type == 'MESH' and obj.data.vertices:
            for corner in obj.bound_box:
                world_corner = obj.matrix_world @ Vector(corner)
                temp_min.x = min(temp_min.x, world_corner.x)
                temp_min.y = min(temp_min.y, world_corner.y)
                temp_min.z = min(temp_min.z, world_corner.z)
                temp_max.x = max(temp_max.x, world_corner.x)
                temp_max.y = max(temp_max.y, world_corner.y)
                temp_max.z = max(temp_max.z, world_corner.z)
    
    temp_dims = temp_max - temp_min if temp_min.x != float('inf') else Vector((1, 1, 1))
    
    # If Y dimension is significantly larger than Z, model needs standup rotation
    needs_standup = temp_dims.y > temp_dims.z * 1.5 and temp_dims.y > 0.3
    
    if needs_standup:
        temp_parent = bpy.data.objects.new("TempParent", None)
        bpy.context.scene.collection.objects.link(temp_parent)
        
        for obj in visible_objects:
            obj.parent = temp_parent
        bpy.context.view_layer.update()
        
        temp_parent.rotation_euler = (math.radians(90), 0, 0)
        bpy.context.view_layer.update()
        
        bpy.ops.object.select_all(action='DESELECT')
        temp_parent.select_set(True)
        for obj in visible_objects:
            obj.select_set(True)
        bpy.context.view_layer.objects.active = temp_parent
        bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
        
        for obj in visible_objects:
            obj.parent = None
        bpy.context.view_layer.update()
        
        bpy.data.objects.remove(temp_parent, do_unlink=True)
    
    # Create parent empty if multiple roots
    roots = [o for o in visible_objects if not o.parent]
    
    if len(roots) > 1:
        root = bpy.data.objects.new("AssetRoot", None)
        bpy.context.scene.collection.objects.link(root)
        for obj in roots:
            obj.parent = root
        bpy.context.view_layer.update()
    else:
        root = roots[0]
    
    # Set name from product metadata
    product = products.get(asset_id, {})
    if product.get('name'):
        root.name = product['name']
    
    bpy.ops.object.select_all(action='DESELECT')
    root.select_set(True)
    for child in root.children_recursive:
        child.select_set(True)
    bpy.context.view_layer.objects.active = root
    
    root.location = (0, 0, 0)
    root.rotation_euler = (0, 0, 0)
    root.scale = (1, 1, 1)
    bpy.context.view_layer.update()
    
    # Calculate bounding box
    all_objects = [root] + list(root.children_recursive)
    min_co = Vector((float('inf'), float('inf'), float('inf')))
    max_co = Vector((float('-inf'), float('-inf'), float('-inf')))
    
    for obj in all_objects:
        if obj.type == 'MESH' and obj.data.vertices:
            for corner in obj.bound_box:
                world_corner = obj.matrix_world @ Vector(corner)
                min_co.x = min(min_co.x, world_corner.x)
                min_co.y = min(min_co.y, world_corner.y)
                min_co.z = min(min_co.z, world_corner.z)
                max_co.x = max(max_co.x, world_corner.x)
                max_co.y = max(max_co.y, world_corner.y)
                max_co.z = max(max_co.z, world_corner.z)
    
    # Setup glass materials
    for obj in all_objects:
        if obj.type == 'MESH':
            setup_glass_material(obj)
    
    dims = max_co - min_co if min_co.x != float('inf') else Vector((0.001, 0.001, 0.001))
    
    # Target dimensions from FML (in meters)
    fml_x = item_data.get('width', 0) * SCALE
    fml_y = item_data.get('height', 0) * SCALE
    fml_z = item_data.get('z_height', 0) * SCALE
    
    is_opening = item_data.get('is_opening', False)
    if is_opening:
        sx = fml_x / dims.x if dims.x > 0.001 and fml_x > 0 else 1
        sy = sx
        sz = fml_z / dims.z if dims.z > 0.001 and fml_z > 0 else sx
    else:
        sx = fml_x / dims.x if dims.x > 0.001 and fml_x > 0 else 1
        sy = fml_y / dims.y if dims.y > 0.001 and fml_y > 0 else 1
        sz = fml_z / dims.z if dims.z > 0.001 and fml_z > 0 else 1
    
    # Apply mirroring
    mirrored = item_data.get('mirrored', [0, 0])
    if mirrored and len(mirrored) >= 2:
        if mirrored[0]:
            sx = -sx
        if mirrored[1]:
            sy = -sy
    
    rotation_rad = math.radians(-item_data.get('rotation', 0))
    
    x = item_data.get('x', 0)
    y = item_data.get('y', 0)
    z = item_data.get('z', 0)
    
    mat_scale = Matrix.Diagonal((sx, sy, sz, 1))
    mat_rot = Matrix.Rotation(rotation_rad, 4, 'Z')
    mat_loc = Matrix.Translation(fml_to_blender(x, y, z, base_z))
    
    # Apply scale and rotation first, then adjust for bottom offset, then translate
    root.matrix_world = mat_loc @ mat_rot @ mat_scale
    
    
    # Move to collection
    for obj in [root] + list(root.children_recursive):
        for coll in obj.users_collection:
            coll.objects.unlink(obj)
        collection.objects.link(obj)
    
    return root


def import_opening(opening_data, wall_data, manifest, products, base_z, collection):
    """Import a window or door opening from wall data."""
    asset_id = opening_data.get('refid')
    if not asset_id or asset_id in SKIP_ASSET_IDS:
        return None
    
    if asset_id not in manifest:
        alt_id = asset_id.replace('rs-', '') if 'rs-' in asset_id else f"rs-{asset_id}"
        if alt_id not in manifest:
            return None
    
    p1, p2 = wall_data.get('a'), wall_data.get('b')
    if not p1 or not p2:
        return None
    
    t = opening_data.get('t', 0.5)
    
    x = p1['x'] + t * (p2['x'] - p1['x'])
    y = p1['y'] + t * (p2['y'] - p1['y'])
    z = opening_data.get('z', 0)
    
    dx = p2['x'] - p1['x']
    dy = p2['y'] - p1['y']
    wall_angle = math.degrees(math.atan2(dy, dx))
    
    opening_width = opening_data.get('width', 100)
    opening_height = opening_data.get('z_height', 200)
    
    z_offset = z + opening_height / 2
    
    item_data = {
        'refid': asset_id,
        'x': x,
        'y': y,
        'z': z_offset,
        'width': opening_width,
        'height': 0,
        'z_height': opening_height,
        'rotation': wall_angle,
        'mirrored': opening_data.get('mirrored', [0, 0]),
        'is_opening': True
    }
    
    result = import_asset(item_data, manifest, products, base_z, collection)
    
    if result:
        return {
            'object': result,
            'x': x,
            'y': y,
            'z': z,
            'width': opening_width,
            'height': opening_height,
            'wall_angle': wall_angle,
            'thickness': wall_data.get('thickness', 30)
        }
    return None


# =============================================================================
# MAIN BUILD LOGIC
# =============================================================================

def build_design(design_data: dict, floor_name: str, manifest: dict, products: dict, base_z: float):
    """Build all geometry for a single design/floor."""
    coll = create_collection(floor_name)
    
    # Create sub-collections
    walls_coll = create_collection(f"{floor_name}_Walls")
    floors_coll = create_collection(f"{floor_name}_Floors")
    surfaces_coll = create_collection(f"{floor_name}_Surfaces")
    furniture_coll = create_collection(f"{floor_name}_Furniture")
    openings_coll = create_collection(f"{floor_name}_Openings")
    
    for sub_coll in [walls_coll, floors_coll, surfaces_coll, furniture_coll, openings_coll]:
        bpy.context.scene.collection.children.unlink(sub_coll)
        coll.children.link(sub_coll)
    
    # Build floors
    for area in design_data.get('areas', []):
        if 'poly' in area:
            create_floor(area, base_z, floors_coll, manifest)
    
    # Build surfaces
    surface_count = 0
    for surface in design_data.get('surfaces', []):
        if 'poly' in surface:
            if create_surface(surface, base_z, surfaces_coll, manifest):
                surface_count += 1
    if surface_count > 0:
        print(f"  Created {surface_count} surfaces")
    
    # Build walls and openings
    wall_count = 0
    opening_count = 0
    for wall in design_data.get('walls', []) + design_data.get('lines', []):
        wall_obj = create_wall(wall, base_z, walls_coll, manifest)
        if wall_obj:
            wall_count += 1
        
        for opening in wall.get('openings', []):
            opening_info = import_opening(opening, wall, manifest, products, base_z, openings_coll)
            if opening_info:
                opening_count += 1
                if wall_obj:
                    cut_wall_opening(wall_obj, opening_info, base_z)
    
    print(f"  Created {wall_count} walls, {opening_count} openings")
    
    # Import furniture
    furniture_count = 0
    
    def process_items(items):
        nonlocal furniture_count
        for item in items:
            if 'items' in item and isinstance(item['items'], list):
                process_items(item['items'])
            if import_asset(item, manifest, products, base_z, furniture_coll):
                furniture_count += 1
    
    all_items = design_data.get('items', []) + design_data.get('objects', [])
    process_items(all_items)
    
    print(f"  Imported {furniture_count} furniture items")


def build_floor(project_dir: str, fml_filename: str, manifest: dict, products: dict, base_z: float):
    """Build all geometry for a single floor from FML data."""
    print(f"\nBuilding {fml_filename}...")
    data = load_json(project_dir, fml_filename)
    if not data:
        return
    
    # Check if this is a nested project file (has 'floors' array)
    if 'floors' in data:
        for i, floor in enumerate(data['floors']):
            floor_name = floor.get('name', f"Floor_{i}")
            floor_base_z = base_z + i * LEVEL_HEIGHT
            print(f"\n  Processing floor: {floor_name}")
            
            for design in floor.get('designs', []):
                build_design(design, floor_name, manifest, products, floor_base_z)
    else:
        # Flat structure (single floor) - treat the whole file as a design
        floor_name = data.get('name', fml_filename.replace(".fml", "").replace(".json", ""))
        build_design(data, floor_name, manifest, products, base_z)


def build(project_dir: str, level_height: float = LEVEL_HEIGHT):
    """
    Main entry point - build all floors from FML files.
    
    Args:
        project_dir: Directory containing FML files and manifest.json
        level_height: Height between floors in meters (default: 2.8)
    """
    if not HAS_BLENDER:
        raise RuntimeError("This module must be run inside Blender")
    
    clean_scene()
    
    manifest = load_json(project_dir, "manifest.json")
    if not manifest:
        print("Error: manifest.json not found")
        print("Run 'fml2blender harvest' first to download assets")
        return
    
    products = load_json(project_dir, "products.json") or {}
    
    fml_files = sorted([f for f in os.listdir(project_dir) 
                        if f.endswith(('.fml', '.fml.json'))])
    
    if not fml_files:
        print("No FML files found in project directory")
        return
    
    for i, filename in enumerate(fml_files):
        build_floor(project_dir, filename, manifest, products, base_z=i * level_height)
    
    print("\nBuild complete!")


# Allow running directly in Blender
if __name__ == "__main__":
    import sys
    
    # Find project directory from command line args
    project_dir = None
    for i, arg in enumerate(sys.argv):
        if arg == "--":
            if i + 1 < len(sys.argv):
                project_dir = sys.argv[i + 1]
            break
    
    if project_dir:
        build(project_dir)
    else:
        print("Usage: blender -b -P build.py -- /path/to/project")
