#!/usr/bin/env python3
"""
fml2blender - Command line interface.

Convert Floorplanner FML files to Blender 3D scenes.
"""

import argparse
import os
import subprocess
import sys


def cmd_harvest(args):
    """Download assets referenced in FML files."""
    from fml2blender.harvest import harvest
    
    project_dir = os.path.abspath(args.project_dir)
    assets_dir = os.path.abspath(args.assets_dir) if args.assets_dir else None
    
    if not os.path.isdir(project_dir):
        print(f"Error: Directory not found: {project_dir}")
        sys.exit(1)
    
    harvest(project_dir, assets_dir)


def cmd_build(args):
    """Build Blender scene from FML files."""
    project_dir = os.path.abspath(args.project_dir)
    
    if not os.path.isdir(project_dir):
        print(f"Error: Directory not found: {project_dir}")
        sys.exit(1)
    
    manifest_path = os.path.join(project_dir, "manifest.json")
    if not os.path.exists(manifest_path):
        print("Error: manifest.json not found.")
        print("Run 'fml2blender harvest <project_dir>' first.")
        sys.exit(1)
    
    # Find Blender executable
    blender = args.blender or find_blender()
    if not blender:
        print("Error: Blender not found.")
        print("Install Blender or specify path with --blender")
        sys.exit(1)
    
    # Get path to build script (in same directory as this file)
    build_script = os.path.join(os.path.dirname(__file__), "build.py")
    
    # Build command
    cmd = [blender]
    
    if not args.gui:
        cmd.append("-b")  # Background mode
    
    cmd.extend(["-P", build_script, "--", project_dir])

    if args.no_lights:
        cmd.append("--no-lights")

    if args.level_height:
        cmd.extend(["--level-height", str(args.level_height)])
    
    print(f"Running: {' '.join(cmd)}")
    subprocess.run(cmd)
    
    if args.output:
        # Save the blend file
        save_cmd = [blender, "-b", args.output, "--python-expr", 
                    f"import bpy; bpy.ops.wm.save_as_mainfile(filepath='{args.output}')"]
        subprocess.run(save_cmd)


def find_blender():
    """Find Blender executable on the system."""
    import shutil
    
    # Check PATH first
    blender = shutil.which("blender")
    if blender:
        return blender
    
    # Common locations
    locations = [
        "/Applications/Blender.app/Contents/MacOS/Blender",  # macOS
        "/usr/bin/blender",  # Linux
        "/snap/bin/blender",  # Linux Snap
        "C:\\Program Files\\Blender Foundation\\Blender\\blender.exe",  # Windows
    ]
    
    for loc in locations:
        if os.path.exists(loc):
            return loc
    
    return None


def main():
    parser = argparse.ArgumentParser(
        prog="fml2blender",
        description="Convert Floorplanner FML files to Blender 3D scenes",
    )
    parser.add_argument("--version", action="version", version="%(prog)s 0.1.0")
    
    subparsers = parser.add_subparsers(dest="command", required=True)
    
    # Harvest command
    harvest_parser = subparsers.add_parser(
        "harvest",
        help="Download assets (3D models, textures) from Floorplanner CDN",
    )
    harvest_parser.add_argument(
        "project_dir",
        help="Directory containing FML files",
    )
    harvest_parser.add_argument(
        "--assets-dir", "-a",
        help="Directory to save assets (default: <project_dir>/assets)",
    )
    harvest_parser.set_defaults(func=cmd_harvest)
    
    # Build command
    build_parser = subparsers.add_parser(
        "build",
        help="Build Blender scene from FML files",
    )
    build_parser.add_argument(
        "project_dir",
        help="Directory containing FML files and manifest.json",
    )
    build_parser.add_argument(
        "--blender", "-b",
        help="Path to Blender executable",
    )
    build_parser.add_argument(
        "--output", "-o",
        help="Output .blend file path",
    )
    build_parser.add_argument(
        "--level-height",
        type=float,
        default=2.8,
        help="Height between floors in meters (default: 2.8)",
    )
    build_parser.add_argument(
        "--gui",
        action="store_true",
        help="Open Blender GUI instead of running in background",
    )
    build_parser.add_argument(
        "--no-lights",
        action="store_true",
        help="Skip auto-adding lights (emitters/lamps/diffusers)",
    )
    build_parser.set_defaults(func=cmd_build)
    
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
