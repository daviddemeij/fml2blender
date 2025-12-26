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
import sys

# Blender imports (only available when running inside Blender)
try:
    import bpy
    import bmesh
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
WALL_TOP_GAP = 0.01  # leave a 1cm gap below the next floor to avoid z-fighting

# Object name patterns to skip
SKIP_OBJECT_PATTERNS = {
    "FP_CUTTER",
    "FP_HANDLES",
    "CUTTER",
}

# Global toggle set from CLI (--no-lights)
NO_LIGHTS = False

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


def clamp_value(value, default=0.0, min_value=0.0, max_value=1.0):
    """Clamp numeric value to a range, with a default fallback."""
    try:
        if value is None:
            return default
        return max(min_value, min(max_value, float(value)))
    except (TypeError, ValueError):
        return default


def get_with_alias(mapping: dict, key: str):
    """Get a value from mapping by key, trying rs-/non-rs aliases."""
    if not mapping or not key:
        return None
    if key in mapping:
        return mapping[key]
    if key.startswith("rs-"):
        base = key.replace("rs-", "", 1)
        if base in mapping:
            return mapping[base]
    else:
        alt = f"rs-{key}"
        if alt in mapping:
            return mapping[alt]
    return None


def create_material_from_metadata(refid: str, manifest=None, materials_meta=None):
    """
    Build a Blender material from stored material metadata (color + PBR maps).
    Falls back to manifest texture if metadata is missing.
    """
    if not HAS_BLENDER or not refid:
        return None

    meta = get_with_alias(materials_meta or {}, refid)
    local_files = meta.get("local_files", {}) if meta else {}
    texture_path = local_files.get("texture") or get_with_alias(manifest or {}, refid)
    bump_path = local_files.get("bump")
    reflection_path = local_files.get("reflection")
    gloss_path = local_files.get("gloss")
    hex_color = meta.get("color") if meta else None
    palette_color = meta.get("palette_color") if meta else None

    mat_name = f"Material_{refid}"
    mat = bpy.data.materials.get(mat_name)
    if mat:
        return mat

    mat = bpy.data.materials.new(name=mat_name)
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()

    output = nodes.new('ShaderNodeOutputMaterial')
    output.location = (600, 0)
    bsdf = nodes.new('ShaderNodeBsdfPrincipled')
    bsdf.location = (250, 0)
    links.new(bsdf.outputs['BSDF'], output.inputs['Surface'])

    def input_socket(names):
        """Return the first available input socket from a list of candidate names."""
        for name in names:
            sock = bsdf.inputs.get(name)
            if sock:
                return sock
        return None

    roughness_in = input_socket(["Roughness"])
    specular_in = input_socket(["Specular", "Specular IOR Level"])
    ior_in = input_socket(["IOR"])

    mat_props = (meta or {}).get("material_properties", {}) or {}
    if roughness_in:
        roughness_in.default_value = clamp_value(mat_props.get("base_roughness"), 0.5)
    glossy_weight = mat_props.get("glossy_weight")
    if glossy_weight is None and meta:
        glossy_weight = meta.get("reflectivity")
    if specular_in:
        specular_in.default_value = clamp_value(glossy_weight, 0.5)
    if ior_in and mat_props.get("material_ior"):
        try:
            ior_in.default_value = max(1.0, float(mat_props["material_ior"]))
        except (TypeError, ValueError):
            pass

    tex_coord = mapping = None
    if texture_path or bump_path or reflection_path or gloss_path:
        tex_coord = nodes.new('ShaderNodeTexCoord')
        tex_coord.location = (-900, 0)
        mapping = nodes.new('ShaderNodeMapping')
        mapping.location = (-700, 0)
        tiling = mat_props.get("texture_tiling") or {}
        mapping.inputs['Scale'].default_value[0] = tiling.get("x", 1.0)
        mapping.inputs['Scale'].default_value[1] = tiling.get("y", 1.0)
        mapping.inputs['Scale'].default_value[2] = tiling.get("z", 1.0)

    def connect_mapping(tex_node):
        if mapping and tex_coord and tex_node:
            coord_output = tex_coord.outputs.get('Object') or tex_coord.outputs.get('UV')
            if coord_output:
                links.new(coord_output, mapping.inputs['Vector'])
                links.new(mapping.outputs['Vector'], tex_node.inputs['Vector'])

    def load_image_safe(path, colorspace=None):
        if not path or not os.path.exists(path):
            return None
        try:
            img = bpy.data.images.load(path)
            if colorspace:
                img.colorspace_settings.name = colorspace
            return img
        except Exception as exc:
            print(f"    Failed to load texture {path}: {exc}")
            return None

    if texture_path:
        tex_node = nodes.new('ShaderNodeTexImage')
        tex_node.location = (-350, 200)
        tex_node.projection = 'BOX'
        tex_node.projection_blend = 0.15
        img = load_image_safe(texture_path)
        if img:
            tex_node.image = img
            connect_mapping(tex_node)
            links.new(tex_node.outputs['Color'], bsdf.inputs['Base Color'])
    elif hex_color:
        base_color = bsdf.inputs.get('Base Color')
        if base_color:
            base_color.default_value = (*hex_to_linear_rgb(hex_color), 1.0)
    elif palette_color:
        # Use palette hint to give a subtle tint when no explicit color exists
        tint = (0.8, 0.8, 0.8)
        if isinstance(palette_color, str):
            tint = tuple([srgb_to_linear(0.7)] * 3)
        base_color = bsdf.inputs.get('Base Color')
        if base_color:
            base_color.default_value = (*tint, 1.0)

    if bump_path:
        bump_img = load_image_safe(bump_path, colorspace="Non-Color")
        if bump_img:
            bump_tex = nodes.new('ShaderNodeTexImage')
            bump_tex.location = (-350, -250)
            bump_tex.image = bump_img
            bump_node = nodes.new('ShaderNodeBump')
            bump_node.location = (-50, -250)
            bump_strength = clamp_value(mat_props.get("base_bump_amount"), 0.2)
            bump_node.inputs['Strength'].default_value = bump_strength
            connect_mapping(bump_tex)
            links.new(bump_tex.outputs['Color'], bump_node.inputs['Height'])
            links.new(bump_node.outputs['Normal'], bsdf.inputs['Normal'])

    if reflection_path and specular_in:
        refl_img = load_image_safe(reflection_path, colorspace="Non-Color")
        if refl_img:
            refl_tex = nodes.new('ShaderNodeTexImage')
            refl_tex.location = (-350, -30)
            refl_tex.image = refl_img
            rgb_to_bw = nodes.new('ShaderNodeRGBToBW')
            rgb_to_bw.location = (-100, -30)
            connect_mapping(refl_tex)
            links.new(refl_tex.outputs['Color'], rgb_to_bw.inputs['Color'])
            links.new(rgb_to_bw.outputs['Val'], specular_in)

    if gloss_path and roughness_in:
        gloss_img = load_image_safe(gloss_path, colorspace="Non-Color")
        if gloss_img:
            gloss_tex = nodes.new('ShaderNodeTexImage')
            gloss_tex.location = (-350, -500)
            gloss_tex.image = gloss_img
        gloss_bw = nodes.new('ShaderNodeRGBToBW')
        gloss_bw.location = (-150, -500)
        invert = nodes.new('ShaderNodeInvert')
        invert.location = (50, -500)
        connect_mapping(gloss_tex)
        links.new(gloss_tex.outputs['Color'], gloss_bw.inputs['Color'])
        links.new(gloss_bw.outputs['Val'], invert.inputs['Color'])
        links.new(invert.outputs['Color'], roughness_in)

    return mat


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


def triangulate_mesh(mesh):
    """Triangulate all faces of a mesh to avoid concave/ngon boolean issues."""
    bm = bmesh.new()
    bm.from_mesh(mesh)
    bmesh.ops.triangulate(bm, faces=bm.faces)
    bm.to_mesh(mesh)
    bm.free()
    mesh.update()


def assign_top_bottom_materials(mesh, top_index: int, bottom_index: int):
    """Assign materials by comparing polygon height to top/bottom planes."""
    if not mesh or not mesh.polygons or not mesh.vertices:
        return
    z_vals = [v.co.z for v in mesh.vertices]
    top_z = max(z_vals)
    bottom_z = min(z_vals)
    span = top_z - bottom_z
    tol = max(0.001, span * 0.1)
    for poly in mesh.polygons:
        center_z = sum(mesh.vertices[i].co.z for i in poly.vertices) / max(1, len(poly.vertices))
        if abs(center_z - top_z) <= tol:
            poly.material_index = top_index
        elif abs(center_z - bottom_z) <= tol:
            poly.material_index = bottom_index
        else:
            poly.material_index = top_index


def apply_wall_uv(mesh, wall_direction: Vector):
    """
    UV map walls so U follows wall length and V follows height.
    This prevents heavy stretching from box projection on tall walls.
    """
    if not mesh or not mesh.polygons or wall_direction.length < 1e-6:
        return
    dir2d = Vector((wall_direction.x, wall_direction.y)).normalized()
    uv_layer = mesh.uv_layers.new(name="UVMap")
    for poly in mesh.polygons:
        for loop_index in poly.loop_indices:
            vert_index = mesh.loops[loop_index].vertex_index
            v = mesh.vertices[vert_index].co
            u = dir2d.dot(Vector((v.x, v.y)))
            uv_layer.data[loop_index].uv = (u, v.z)


def setup_glass_material(obj):
    """Configure glass materials for transparency."""
    if 'GLASS' not in obj.name.upper():
        return
    
    glass_mat = get_or_create_glass_material()
    apply_glass_to_object(obj, glass_mat)


def apply_material(obj, mat):
    """Apply material to object."""
    if obj.data and hasattr(obj.data, 'materials'):
        if obj.data.materials:
            obj.data.materials[0] = mat
        else:
            obj.data.materials.append(mat)


def get_or_create_glass_material():
    """Return a shared transparent glass material."""
    glass_mat = bpy.data.materials.get("Glass_Transparent")
    if glass_mat:
        return glass_mat
    
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
    return glass_mat


def apply_glass_to_object(obj, glass_mat):
    """Replace the first material slot with glass."""
    if not obj or not obj.data or not hasattr(obj.data, 'materials'):
        return
    if obj.data.materials:
        obj.data.materials[0] = glass_mat
    else:
        obj.data.materials.append(glass_mat)


def ensure_glass_materials(objs):
    """Apply glass material to any mesh whose material name suggests glass."""
    glass_mat = get_or_create_glass_material()
    for obj in objs:
        if not obj or obj.type != 'MESH':
            continue
        applied = False
        for slot in obj.material_slots:
            if slot.material and "glass" in slot.material.name.lower():
                slot.material = glass_mat
                applied = True
        if not applied and "glass" in obj.name.lower():
            apply_glass_to_object(obj, glass_mat)


def setup_diffuser_material(mat):
    """Make a diffuser material semi-transparent (no emission)."""
    if not mat:
        return
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()

    output = nodes.new('ShaderNodeOutputMaterial')
    output.location = (400, 0)

    principled = nodes.new('ShaderNodeBsdfPrincipled')
    principled.location = (0, 0)
    base_color = principled.inputs.get('Base Color')
    if base_color:
        base_color.default_value = (1.0, 1.0, 1.0, 1.0)
    alpha_socket = principled.inputs.get('Alpha')
    if alpha_socket:
        alpha_socket.default_value = 0.5

    links.new(principled.outputs['BSDF'], output.inputs['Surface'])
    mat.blend_method = 'BLEND'
    mat.use_backface_culling = False


def add_light_for_emitters(objs):
    """
    Heuristic: add a point light at the centroid of small emitter-like meshes
    (e.g., filaments named Capsule/Filament).
    """
    if NO_LIGHTS:
        return
    emitter_names = ("capsule", "filament")

    def world_bbox_size(obj):
        """Return world-space bounding box size as a Vector."""
        if not obj or obj.type != 'MESH' or not obj.data.vertices:
            return None
        corners = [obj.matrix_world @ Vector(corner) for corner in obj.bound_box]
        min_c = Vector((min(v.x for v in corners), min(v.y for v in corners), min(v.z for v in corners)))
        max_c = Vector((max(v.x for v in corners), max(v.y for v in corners), max(v.z for v in corners)))
        return max_c - min_c

    emitters = []
    for obj in objs:
        if obj.type != 'MESH':
            continue
        name_lower = obj.name.lower()
        parent_name = obj.parent.name.lower() if obj.parent else ""
        if "beam" in name_lower or "beam" in parent_name:
            continue
        if "column" in name_lower or "column" in parent_name:
            continue

        if any(token in name_lower for token in emitter_names):
            size = world_bbox_size(obj)
            if not size:
                continue
            if max(size) > 0.25:
                continue  # ignore anything larger than ~25cm in any axis
            emitters.append(obj)
    if not emitters:
        return

    # Compute average position of emitters
    total = Vector((0.0, 0.0, 0.0))
    count = 0
    for obj in emitters:
        total += obj.matrix_world.translation
        count += 1
    center = total / max(count, 1)

    light = bpy.data.lights.new(name="AutoEmitterLight", type='POINT')
    light.energy = 20.0
    light.color = (1.0, 0.95, 0.85)
    light_obj = bpy.data.objects.new(name="AutoEmitterLight", object_data=light)
    light_obj.location = center
    bpy.context.scene.collection.objects.link(light_obj)


def add_point_for_diffusers(root, objs):
    """
    Place a point light at the centroid of meshes using MAT_SELFILL* materials.
    """
    if NO_LIGHTS:
        return
    diffuser_meshes = []
    for obj in objs:
        if obj.type != 'MESH' or not obj.data.vertices:
            continue
        for slot in obj.material_slots:
            if slot.material and slot.material.name.startswith("MAT_SELFILL"):
                diffuser_meshes.append(obj)
                break
    if not diffuser_meshes:
        return

    min_co = Vector((float('inf'), float('inf'), float('inf')))
    max_co = Vector((float('-inf'), float('-inf'), float('-inf')))
    for obj in diffuser_meshes:
        for corner in obj.bound_box:
            world_corner = obj.matrix_world @ Vector(corner)
            min_co.x = min(min_co.x, world_corner.x)
            min_co.y = min(min_co.y, world_corner.y)
            min_co.z = min(min_co.z, world_corner.z)
            max_co.x = max(max_co.x, world_corner.x)
            max_co.y = max(max_co.y, world_corner.y)
            max_co.z = max(max_co.z, world_corner.z)
    if min_co.x == float('inf'):
        return

    center = (min_co + max_co) * 0.5
    light = bpy.data.lights.new(name="DiffuserPoint", type='POINT')
    light.energy = 60.0
    light.color = (1.0, 0.95, 0.85)
    light_obj = bpy.data.objects.new("DiffuserPoint", light)
    light_obj.location = center
    light_obj.parent = root
    light_obj.matrix_parent_inverse = root.matrix_world.inverted()
    bpy.context.scene.collection.objects.link(light_obj)

def add_light_for_lamp(root, min_co, max_co, product_name: str | None, level: int | None):
    """
    Add an area light for lamp-like assets (pendants, lamps, lights).
    """
    if NO_LIGHTS:
        return
    name = (product_name or root.name or "").lower()
    tokens = ("lamp", "light", "pendant", "led")
    if not any(t in name for t in tokens):
        return
    if level is not None and level not in (-2, 1, 2):
        # Skip wall/door etc; allow ceiling/floor/counter lamps
        return

    center = (min_co + max_co) * 0.5
    dims = max_co - min_co
    size_x = max(dims.x, 0.05)
    size_y = max(dims.y, 0.05)

    light_data = bpy.data.lights.new(name=f"{root.name}_Light", type='AREA')
    light_data.shape = 'RECTANGLE'
    light_data.size = size_x
    light_data.size_y = size_y
    light_data.energy = 150.0
    light_data.color = (1.0, 0.93, 0.85)

    light_obj = bpy.data.objects.new(name=f"{root.name}_Light", object_data=light_data)
    light_obj.location = center
    bpy.context.scene.collection.objects.link(light_obj)
    light_obj.parent = root
    light_obj.matrix_parent_inverse = root.matrix_world.inverted()




# =============================================================================
# GEOMETRY BUILDERS
# =============================================================================

def create_floor(area_data, base_z, collection, manifest=None, materials=None):
    """Create a floor polygon from area data with thickness for boolean operations."""
    poly = area_data.get('poly', [])
    if len(poly) < 3:
        return None
    
    name = area_data.get('name', "Floor")
    floor_thickness = 0.02  # 2cm thickness for boolean compatibility
    
    # Create top vertices
    top_verts = [Vector(fml_to_blender(p['x'], p['y'], 0, base_z)) for p in poly]
    # Create bottom vertices
    bottom_verts = [Vector((v.x, v.y, v.z - floor_thickness)) for v in top_verts]
    
    verts = top_verts + bottom_verts
    n = len(top_verts)
    
    faces = []
    # Top face
    faces.append(list(range(n)))
    # Bottom face (reversed winding)
    faces.append(list(range(2*n - 1, n - 1, -1)))
    # Side faces
    for i in range(n):
        next_i = (i + 1) % n
        faces.append([i, next_i, next_i + n, i + n])
    
    mesh = bpy.data.meshes.new(f"{name}Mesh")
    mesh.from_pydata(verts, [], faces)
    mesh.validate()  # Validate mesh for boolean operations
    triangulate_mesh(mesh)  # Triangulate to handle concave polygons
    mesh.update()
    if hasattr(mesh, "calc_normals"):
        mesh.calc_normals()
    
    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)
    
    refid = area_data.get('refid')
    color = area_data.get('color')
    
    # Materials: top from refid/color, bottom always white ceiling
    top_mat = None
    if refid:
        top_mat = create_material_from_metadata(refid, manifest, materials)
        if not top_mat and color:
            top_mat = get_or_create_material(f"Floor_{color}", hex_color=color)
    elif color:
        top_mat = get_or_create_material(f"Floor_{color}", hex_color=color)
    if not top_mat:
        top_mat = get_or_create_material("Floor_Default", color=(0.9, 0.9, 0.9))
    bottom_mat = get_or_create_material("Ceiling_White", color=(0.98, 0.98, 0.98))

    obj.data.materials.clear()
    obj.data.materials.append(top_mat)     # index 0 (top)
    obj.data.materials.append(bottom_mat)  # index 1 (bottom)

    assign_top_bottom_materials(mesh, top_index=0, bottom_index=1)
    
    return obj


def create_cutout_cutter(surface_data, base_z):
    """Create a cutter object from cutout surface data for boolean operations."""
    poly = surface_data.get('poly', [])
    if len(poly) < 3:
        return None
    
    # Create a prism that extends above and below the floor to ensure clean cut
    verts = []
    cut_depth = 1.0  # 1 meter below floor
    cut_height = 5.0  # 5 meters above floor (to cut through any floor thickness)
    
    # Bottom vertices
    for p in poly:
        pos = fml_to_blender(p['x'], p['y'], 0, base_z)
        verts.append(Vector((pos[0], pos[1], pos[2] - cut_depth)))
    
    # Top vertices
    for p in poly:
        pos = fml_to_blender(p['x'], p['y'], 0, base_z)
        verts.append(Vector((pos[0], pos[1], pos[2] + cut_height)))
    
    n = len(poly)
    faces = []
    # Bottom face
    faces.append(list(range(n - 1, -1, -1)))
    # Top face
    faces.append(list(range(n, 2 * n)))
    # Side faces
    for i in range(n):
        next_i = (i + 1) % n
        faces.append([i, next_i, next_i + n, i + n])
    
    mesh = bpy.data.meshes.new("CutoutMesh")
    mesh.from_pydata(verts, [], faces)
    mesh.validate()  # Validate mesh for boolean operations
    triangulate_mesh(mesh)
    mesh.update()
    
    obj = bpy.data.objects.new("FloorCutter", mesh)
    bpy.context.scene.collection.objects.link(obj)
    
    return obj


def bboxes_overlap_xy(obj1, obj2):
    """Check if two objects' bounding boxes overlap in the XY plane."""
    # Compute bounds directly from mesh vertices (more reliable than bound_box for new objects)
    def get_xy_bounds(obj):
        if obj.type != 'MESH' or not obj.data.vertices:
            return None
        xs = [obj.matrix_world @ v.co for v in obj.data.vertices]
        min_x = min(v.x for v in xs)
        max_x = max(v.x for v in xs)
        min_y = min(v.y for v in xs)
        max_y = max(v.y for v in xs)
        return (min_x, max_x, min_y, max_y)
    
    bounds1 = get_xy_bounds(obj1)
    bounds2 = get_xy_bounds(obj2)
    
    if not bounds1 or not bounds2:
        return False
    
    min1_x, max1_x, min1_y, max1_y = bounds1
    min2_x, max2_x, min2_y, max2_y = bounds2
    
    # Check for overlap
    overlap_x = max1_x >= min2_x and max2_x >= min1_x
    overlap_y = max1_y >= min2_y and max2_y >= min1_y
    
    return overlap_x and overlap_y


def clean_floor_mesh(obj, merge_dist=1e-5):
    """Merge near-duplicate verts left by booleans to avoid tiny slivers."""
    try:
        bm = bmesh.new()
        bm.from_mesh(obj.data)
        bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=merge_dist)
        bmesh.ops.dissolve_limit(bm, angle_limit=0.01, verts=bm.verts, edges=bm.edges)
        bm.to_mesh(obj.data)
        bm.free()
        obj.data.update()
    except Exception as e:
        print(f"    Cleanup failed on {obj.name}: {e}")


def apply_floor_cutout(floor_obj, cutter_obj):
    """Apply a boolean difference to cut a hole in the floor using modifiers."""
    if not floor_obj or not cutter_obj:
        return False
    
    # Check if bounding boxes overlap in XY plane before attempting boolean
    if not bboxes_overlap_xy(floor_obj, cutter_obj):
        print(f"    Skip cut: no XY overlap between {floor_obj.name} and {cutter_obj.name}")
        return False
    
    try:
        bpy.context.view_layer.update()
        if bpy.context.object and bpy.context.object.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        
        # Apply transforms on cutter to avoid non-unit scale/rotation issues
        bpy.ops.object.select_all(action='DESELECT')
        cutter_obj.select_set(True)
        bpy.context.view_layer.objects.active = cutter_obj
        bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
        
        # Clean up floor mesh before boolean to improve reliability
        clean_floor_mesh(floor_obj, merge_dist=1e-6)
        
        # Ensure the floor is active for modifier application
        bpy.ops.object.select_all(action='DESELECT')
        floor_obj.select_set(True)
        bpy.context.view_layer.objects.active = floor_obj
        faces_before = len(floor_obj.data.polygons)
        verts_before = len(floor_obj.data.vertices)
        print(f"    Cutting {floor_obj.name}: before faces={faces_before}, verts={verts_before}")

        bool_mod = floor_obj.modifiers.new(name="FloorCutout", type='BOOLEAN')
        bool_mod.operation = 'DIFFERENCE'
        bool_mod.object = cutter_obj
        bool_mod.solver = 'EXACT'
        bool_mod.use_self = False
        
        bpy.ops.object.modifier_apply(modifier=bool_mod.name)
        floor_obj.data.update()
        
        # Clean up the mesh after boolean - select and delete internal faces
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_all(action='DESELECT')
        bpy.ops.object.mode_set(mode='OBJECT')
        
        # Find and mark faces that are inside the cutter for deletion
        cutter_min = Vector((min(v.co.x for v in cutter_obj.data.vertices),
                              min(v.co.y for v in cutter_obj.data.vertices),
                              min(v.co.z for v in cutter_obj.data.vertices)))
        cutter_max = Vector((max(v.co.x for v in cutter_obj.data.vertices),
                              max(v.co.y for v in cutter_obj.data.vertices),
                              max(v.co.z for v in cutter_obj.data.vertices)))
        
        # Transform cutter bounds to floor object space
        cutter_to_floor = floor_obj.matrix_world.inverted() @ cutter_obj.matrix_world
        cutter_min_local = cutter_to_floor @ cutter_min
        cutter_max_local = cutter_to_floor @ cutter_max
        
        # Select faces whose centers are inside the cutter bounding box
        for face in floor_obj.data.polygons:
            face_center = sum((floor_obj.data.vertices[v].co for v in face.vertices), Vector((0,0,0))) / len(face.vertices)
            if (cutter_min_local.x <= face_center.x <= cutter_max_local.x and
                cutter_min_local.y <= face_center.y <= cutter_max_local.y and
                cutter_min_local.z <= face_center.z <= cutter_max_local.z):
                face.select = True
        
        # Delete selected faces and clean up
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.delete(type='FACE')
        bpy.ops.mesh.select_all(action='SELECT')
        bpy.ops.mesh.normals_make_consistent(inside=False)
        bpy.ops.mesh.delete_loose()
        bpy.ops.object.mode_set(mode='OBJECT')
        
        bpy.context.view_layer.update()
        faces_after = len(floor_obj.data.polygons)
        verts_after = len(floor_obj.data.vertices)
        print(f"    Cut result for {floor_obj.name} (EXACT): faces={faces_after}, verts={verts_after}")
        if faces_after == faces_before:
            print(f"    Cut had no face change on {floor_obj.name}, retrying with FAST solver")
            bool_mod_fast = floor_obj.modifiers.new(name="FloorCutoutFast", type='BOOLEAN')
            bool_mod_fast.operation = 'DIFFERENCE'
            bool_mod_fast.object = cutter_obj
            bool_mod_fast.solver = 'FAST'
            bool_mod_fast.use_self = False
            bpy.ops.object.modifier_apply(modifier=bool_mod_fast.name)
            floor_obj.data.update()
            
            # Clean up the mesh after boolean - select and delete internal faces
            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_all(action='DESELECT')
            bpy.ops.object.mode_set(mode='OBJECT')
            
            # Find and mark faces that are inside the cutter for deletion
            cutter_min = Vector((min(v.co.x for v in cutter_obj.data.vertices),
                                  min(v.co.y for v in cutter_obj.data.vertices),
                                  min(v.co.z for v in cutter_obj.data.vertices)))
            cutter_max = Vector((max(v.co.x for v in cutter_obj.data.vertices),
                                  max(v.co.y for v in cutter_obj.data.vertices),
                                  max(v.co.z for v in cutter_obj.data.vertices)))
            
            # Transform cutter bounds to floor object space
            cutter_to_floor = floor_obj.matrix_world.inverted() @ cutter_obj.matrix_world
            cutter_min_local = cutter_to_floor @ cutter_min
            cutter_max_local = cutter_to_floor @ cutter_max
            
            # Select faces whose centers are inside the cutter bounding box
            for face in floor_obj.data.polygons:
                face_center = sum((floor_obj.data.vertices[v].co for v in face.vertices), Vector((0,0,0))) / len(face.vertices)
                if (cutter_min_local.x <= face_center.x <= cutter_max_local.x and
                    cutter_min_local.y <= face_center.y <= cutter_max_local.y and
                    cutter_min_local.z <= face_center.z <= cutter_max_local.z):
                    face.select = True
            
            # Delete selected faces and clean up
            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.delete(type='FACE')
            bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.mesh.normals_make_consistent(inside=False)
            bpy.ops.mesh.delete_loose()
            bpy.ops.object.mode_set(mode='OBJECT')
            
            bpy.context.view_layer.update()
            faces_fast = len(floor_obj.data.polygons)
            verts_fast = len(floor_obj.data.vertices)
            print(f"    Cut result for {floor_obj.name} (FAST): faces={faces_fast}, verts={verts_fast}")
            if faces_fast == faces_before:
                print(f"    FAST also unchanged on {floor_obj.name}, falling back to bmesh boolean")
                if _bmesh_boolean_difference(floor_obj, cutter_obj):
                    faces_bm = len(floor_obj.data.polygons)
                    verts_bm = len(floor_obj.data.vertices)
                    print(f"    Cut result for {floor_obj.name} (bmesh): faces={faces_bm}, verts={verts_bm}")
                    clean_floor_mesh(floor_obj)
                    return True
                return False
        clean_floor_mesh(floor_obj)
        return True
    except Exception as e:
        print(f"    Boolean cut failed for {floor_obj.name}: {e}")
        return False


def _bmesh_boolean_difference(floor_obj, cutter_obj):
    """Fallback bmesh boolean difference if modifiers did not change the mesh."""
    try:
        bm_target = bmesh.new()
        bm_target.from_mesh(floor_obj.data)
        bm_cutter = bmesh.new()
        bm_cutter.from_mesh(cutter_obj.data)
        cutter_to_target = floor_obj.matrix_world.inverted() @ cutter_obj.matrix_world
        bmesh.ops.transform(bm_cutter, matrix=cutter_to_target, verts=bm_cutter.verts)
        bmesh.ops.boolean(
            bm_target,
            geom=bm_target.verts[:] + bm_target.edges[:] + bm_target.faces[:],
            cutter=bm_cutter,
            operation='DIFFERENCE',
        )
        bm_target.to_mesh(floor_obj.data)
        floor_obj.data.update()
        bm_target.free()
        bm_cutter.free()
        return True
    except Exception as e:
        print(f"    bmesh boolean failed for {floor_obj.name}: {e}")
        return False


def create_surface(surface_data, base_z, collection, manifest=None, materials=None):
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
    if hasattr(mesh, "calc_normals"):
        mesh.calc_normals()
    
    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)
    
    color = surface_data.get('color')
    refid = surface_data.get('refid')
    
    top_mat = None
    if refid:
        top_mat = create_material_from_metadata(refid, manifest, materials)
    if not top_mat and color:
        top_mat = get_or_create_material(f"Surface_{color}", hex_color=color)
    if not top_mat:
        mat_name = "Roof_Default" if is_roof else "Surface_Default"
        top_mat = get_or_create_material(mat_name, color=(0.95, 0.95, 0.95))

    # If there is thickness, give the bottom a white ceiling material
    if thickness > 0.001:
        bottom_mat = get_or_create_material("Ceiling_White", color=(0.98, 0.98, 0.98))
        obj.data.materials.clear()
        obj.data.materials.append(top_mat)     # 0 top
        obj.data.materials.append(bottom_mat)  # 1 bottom

        assign_top_bottom_materials(mesh, top_index=0, bottom_index=1)
    else:
        apply_material(obj, top_mat)
    
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


def create_wall(wall_data, base_z, collection, manifest=None, materials=None):
    """Create a wall with proper materials for each side."""
    p1, p2 = wall_data.get('a'), wall_data.get('b')
    if not p1 or not p2:
        return None
    
    h1_raw = wall_data.get('az', {}).get('h', 0) * SCALE
    h2_raw = wall_data.get('bz', {}).get('h', 0) * SCALE
    h1 = max(0.0, h1_raw - WALL_TOP_GAP)
    h2 = max(0.0, h2_raw - WALL_TOP_GAP)
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
        elif 'refid' in decor_data:
            refid = decor_data['refid']
            mat = create_material_from_metadata(refid, manifest, materials)
            if mat:
                return mat
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


def import_asset(item_data, manifest, products, base_z, collection, no_lights: bool = False):
    """Import and position a GLB asset based on FML item data."""
    asset_id = item_data.get('refid') or item_data.get('asset_id')
    if not asset_id:
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
    
    # Get product metadata to determine model orientation
    product = get_with_alias(products, asset_id) or {}
    bbox_min = product.get('bbox_min')
    bbox_max = product.get('bbox_max')
    api_height = product.get('height', 0)  # Intended vertical dimension
    level = product.get('level')  # 1=floor, 2=counter-top, -1=wall/openings, -2=ceiling
    has_opening = product.get('has_opening', False)  # True for doors/windows
    
    # Determine if model needs standup rotation (Y-up → Z-up)
    # Using deterministic rules based on API data:
    #
    # 1. Ceiling-mounted (level=-2): Never rotate - they hang correctly from z=0
    # 2. Doors/windows (has_opening=True): Always rotate - modeled Y-up by convention
    # 3. Wall-mounted without opening (level=-1, TVs/radiators): Check if depth ≈ bbox_y
    #    (For these items, API 'depth' is the vertical dimension, not 'height')
    # 4. Other items: Compare API height with bbox Z span
    #    - If height ≈ bbox_z: model is Z-up (correct)
    #    - If height ≈ bbox_y: model is Y-up (needs rotation)
    
    is_ceiling_mounted = level == -2
    is_wall_mounted = level == -1
    api_depth = product.get('depth', 0)
    
    needs_standup = False
    if is_ceiling_mounted:
        needs_standup = False
    elif has_opening:
        needs_standup = True
    elif is_wall_mounted and bbox_min and bbox_max and api_depth:
        # Wall-mounted items (TVs, radiators): 'depth' is the vertical dimension
        bbox_y_span = bbox_max[1] - bbox_min[1]
        needs_standup = abs(bbox_y_span - api_depth) < 1.0
    elif bbox_min and bbox_max and api_height:
        bbox_z_span = bbox_max[2] - bbox_min[2]
        bbox_y_span = bbox_max[1] - bbox_min[1]
        
        # Model is Y-up if API height matches Y span (not Z span)
        height_matches_z = abs(bbox_z_span - api_height) < 1.0
        height_matches_y = abs(bbox_y_span - api_height) < 1.0
        needs_standup = height_matches_y and not height_matches_z

    if needs_standup:
        temp_parent = bpy.data.objects.new("TempParent", None)
        bpy.context.scene.collection.objects.link(temp_parent)
        
        for obj in visible_objects:
            obj.parent = temp_parent
        bpy.context.view_layer.update()
        
        # Rotate 90° around X to stand up, then 180° around Z to restore front direction
        # When a Y-up model is rotated 90° around X, its front (-Y) becomes pointing down
        # The 180° Z rotation flips it so the front points in the original direction
        temp_parent.rotation_euler = (math.radians(90), 0, math.radians(180))
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
    product = get_with_alias(products, asset_id) or {}
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
    
    # Setup glass materials (heuristic to catch legacy window glass)
    ensure_glass_materials(all_objects)
    print(f"NO_LIGHTS={no_lights}")
    if not no_lights:
        # Add point light for emitter-like meshes (filaments)
        add_light_for_emitters(all_objects)
        # Add area light for lamp-like assets (pendants, lamps)
        add_light_for_lamp(root, min_co, max_co, product.get('name'), level)
        # Add point light for diffuser meshes (MAT_SELFILL*)
        add_point_for_diffusers(root, all_objects)

        # Make known diffuser materials semi-transparent (allow light to pass)
        for obj in all_objects:
            if obj.type != 'MESH':
                continue
            for slot in obj.material_slots:
                if slot.material and slot.material.name.startswith("MAT_SELFILL"):
                    setup_diffuser_material(slot.material)
    
    dims = max_co - min_co if min_co.x != float('inf') else Vector((0.001, 0.001, 0.001))
    
    # Target dimensions from FML (in meters)
    fml_x = item_data.get('width', 0) * SCALE
    fml_y = item_data.get('height', 0) * SCALE
    fml_z = item_data.get('z_height', 0) * SCALE
    
    is_opening = item_data.get('is_opening', False)
    
    # For Z scaling: use max_co.z (height from origin) if model is positioned above origin
    # This handles wall-mounted items like wash basins where z_height = top position
    # For items sitting at origin (min_co.z near 0), use dims.z as before
    model_z_extent = max_co.z if max_co.z != float('-inf') else dims.z
    use_extent_for_z = min_co.z > 0.1  # Model sits above origin (wall-mounted etc)
    
    if is_opening:
        # Wall openings (doors/windows) - after standup rotation, dimensions are:
        # X = width, Y = thickness, Z = height (was Y before rotation)
        # Scale uniformly for X and Y, scale Z to match z_height
        sx = fml_x / dims.x if dims.x > 0.001 and fml_x > 0 else 1
        sy = sx  # Keep thickness proportional
        sz = fml_z / dims.z if dims.z > 0.001 and fml_z > 0 else sx
    else:
        sx = fml_x / dims.x if dims.x > 0.001 and fml_x > 0 else 1
        sy = fml_y / dims.y if dims.y > 0.001 and fml_y > 0 else 1
        if use_extent_for_z and model_z_extent > 0.001 and fml_z > 0:
            sz = fml_z / model_z_extent
        else:
            sz = fml_z / dims.z if dims.z > 0.001 and fml_z > 0 else 1
    
    # Get mirroring flags
    mirrored = item_data.get('mirrored', [0, 0])
    
    # Apply mirroring to scale factors
    if mirrored and len(mirrored) >= 2:
        if mirrored[0]:
            sx = -sx
        if mirrored[1]:
            sy = -sy
    
    mat_scale = Matrix.Diagonal((sx, sy, sz, 1))
    
    rotation_rad = math.radians(-item_data.get('rotation', 0))
    
    x = item_data.get('x', 0)
    y = item_data.get('y', 0)
    z = item_data.get('z', 0)
    
    mat_scale = Matrix.Diagonal((sx, sy, sz, 1))
    mat_rot = Matrix.Rotation(rotation_rad, 4, 'Z')
    
    # Calculate the offset to compensate for model's origin position
    # After standup rotation, the model's bounding box has changed:
    # - min_co/max_co are recalculated AFTER rotation
    # - For wall-mounted items, FML z is the bottom edge position
    
    is_ceiling_mounted = level == -2
    
    if is_ceiling_mounted:
        # Ceiling items: model origin (z=0) is the mounting point at ceiling
        z_correction = 0
    elif is_wall_mounted and needs_standup and not is_opening:
        # Wall-mounted items (TV, radiator) after standup: min_co.z should be at FML z
        # Exclude openings (doors/windows) - they handle z differently
        if min_co.z != float('inf') and abs(min_co.z) > 0.01:
            z_correction = -min_co.z * sz
        else:
            z_correction = 0
    elif not is_opening and not use_extent_for_z and min_co.z != float('inf') and min_co.z > 0.01:
        # Only apply correction if model is significantly above origin (not floor-mounted items)
        # Floor-mounted items (level=1) with small bbox offsets should not be corrected
        z_correction = -min_co.z * sz
    elif not is_opening and min_co.z != float('inf') and min_co.z < -0.001:
        # Handle models with slightly negative bbox_min (like -0.005) on floor-mounted items
        # Offset upward to bring the bottom to the floor level
        z_correction = -min_co.z * sz
    else:
        z_correction = 0
    
    target_pos = fml_to_blender(x, y, z, base_z)
    corrected_pos = Vector((target_pos[0], target_pos[1], target_pos[2] + z_correction))
    mat_loc = Matrix.Translation(corrected_pos)
    
    root.matrix_world = mat_loc @ mat_rot @ mat_scale
    
    
    # Move to collection
    for obj in [root] + list(root.children_recursive):
        for coll in obj.users_collection:
            coll.objects.unlink(obj)
        collection.objects.link(obj)
    
    return root


def import_opening(opening_data, wall_data, manifest, products, base_z, collection, no_lights: bool = False):
    """Import a window or door opening from wall data."""
    asset_id = opening_data.get('refid')
    if not asset_id:
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
    
    result = import_asset(item_data, manifest, products, base_z, collection, no_lights=no_lights)
    
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

def build_design(design_data: dict, floor_name: str, manifest: dict, products: dict, materials: dict, base_z: float, master_walls_coll=None, no_lights: bool = False):
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
    
    # Collect cutout surfaces first
    cutout_surfaces = []
    for surface in design_data.get('surfaces', []):
        if surface.get('isCutout') and 'poly' in surface:
            cutout_surfaces.append(surface)
    
    # Build floors and collect floor objects
    floor_objects = []
    for area in design_data.get('areas', []):
        if 'poly' in area:
            floor_obj = create_floor(area, base_z, floors_coll, manifest, materials)
            if floor_obj:
                floor_objects.append(floor_obj)
    
    # Apply cutouts to floors
    cutout_count = 0
    if cutout_surfaces and floor_objects:
        # Create cutter objects
        cutters = []
        for cutout in cutout_surfaces:
            cutter = create_cutout_cutter(cutout, base_z)
            if cutter:
                cutters.append((cutter, cutout.get('customName', 'unnamed')))
        
        # Apply each cutter to each floor that overlaps with it
        for floor_obj in floor_objects:
            for cutter, cutout_name in cutters:
                if apply_floor_cutout(floor_obj, cutter):
                    print(f"    Cut '{cutout_name}' hole in {floor_obj.name}")
                    cutout_count += 1
        
        # Clean up cutters
        for cutter, _ in cutters:
            bpy.data.objects.remove(cutter, do_unlink=True)
    
    if cutout_count > 0:
        print(f"  Applied {cutout_count} cutout(s) to floors")
    
    # Build surfaces (excluding cutouts, which are handled above)
    surface_count = 0
    for surface in design_data.get('surfaces', []):
        if 'poly' in surface:
            if create_surface(surface, base_z, surfaces_coll, manifest, materials):
                surface_count += 1
    if surface_count > 0:
        print(f"  Created {surface_count} surfaces")
    
    # Build walls and openings
    wall_count = 0
    opening_count = 0
    target_wall_coll = master_walls_coll or walls_coll
    for wall in design_data.get('walls', []) + design_data.get('lines', []):
        wall_obj = create_wall(wall, base_z, target_wall_coll, manifest, materials)
        if wall_obj:
            wall_count += 1
        
        for opening in wall.get('openings', []):
            opening_info = import_opening(opening, wall, manifest, products, base_z, openings_coll, no_lights=no_lights)
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
            if import_asset(item, manifest, products, base_z, furniture_coll, no_lights=no_lights):
                furniture_count += 1
    
    all_items = design_data.get('items', []) + design_data.get('objects', [])
    process_items(all_items)
    
    print(f"  Imported {furniture_count} furniture items")


def build_floor(project_dir: str, fml_filename: str, manifest: dict, products: dict, materials: dict, base_z: float, master_walls_coll=None, no_lights: bool = False):
    """Build all geometry for a single floor from FML data."""
    print(f"\nBuilding {fml_filename}...")
    data = load_json(project_dir, fml_filename)
    if not data:
        return
    
    # Check if this is a nested project file (has 'floors' array)
    if 'floors' in data:
        for i, floor in enumerate(data['floors']):
            floor_name = floor.get('name', f"Floor_{i}")
            # Use the floor's level property for height, not array index
            floor_level = floor.get('level', i)
            floor_base_z = base_z + floor_level * LEVEL_HEIGHT
            print(f"\n  Processing floor: {floor_name} (level {floor_level})")
            
            for design in floor.get('designs', []):
                build_design(design, floor_name, manifest, products, materials, floor_base_z, master_walls_coll, no_lights)
    else:
        # Flat structure (single floor) - treat the whole file as a design
        floor_name = data.get('name', fml_filename.replace(".fml", "").replace(".json", ""))
        build_design(data, floor_name, manifest, products, materials, base_z, master_walls_coll, no_lights)


def build(project_dir: str, level_height: float = LEVEL_HEIGHT):
    """
    Main entry point - build all floors from FML files.
    
    Args:
        project_dir: Directory containing FML files and manifest.json
        level_height: Height between floors in meters (default: 2.8)
    """
    no_lights = "--no-lights" in sys.argv
    global NO_LIGHTS
    NO_LIGHTS = no_lights
    if not HAS_BLENDER:
        raise RuntimeError("This module must be run inside Blender")
    
    clean_scene()
    master_walls_coll = create_collection("All_Walls")
    
    manifest = load_json(project_dir, "manifest.json")
    if not manifest:
        print("Error: manifest.json not found")
        print("Run 'fml2blender harvest' first to download assets")
        return
    
    products = load_json(project_dir, "products.json") or {}
    materials = load_json(project_dir, "materials.json") or {}
    
    fml_files = sorted([f for f in os.listdir(project_dir) 
                        if f.endswith(('.fml', '.fml.json'))])
    
    if not fml_files:
        print("No FML files found in project directory")
        return
    
    for i, filename in enumerate(fml_files):
        build_floor(project_dir, filename, manifest, products, materials, base_z=i * level_height, master_walls_coll=master_walls_coll, no_lights=no_lights)
    
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
