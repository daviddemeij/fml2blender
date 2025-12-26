# fml2blender

Convert [Floorplanner](https://floorplanner.com) floor plans to [Blender](https://blender.org) 3D scenes.

![Example render](docs/example.png)

## Features

- 🏠 **Walls** with textures and proper thickness
- 🚪 **Windows & Doors** with automatic wall cutouts
- 🪑 **Furniture** positioned and scaled correctly
- 🎨 **Materials** including textures and colors
- 🏗️ **Multi-floor** support

## Quick Start

### 1. Install

```bash
pip install fml2blender
```

Or install from source:

```bash
git clone https://github.com/daviddemeij/fml2blender
cd fml2blender
pip install -e .
```

### 2. Get FML Files

FML files contain floor plan data used by Floorplanner. You can capture them from various website that use floorplanner such as real estate listing websites like Funda.nl

#### From Floorplanner.com

1. Open your project on [floorplanner.com](https://floorplanner.com)
2. Open browser DevTools → **Network** tab
3. Filter by `fml` or `XHR`
4. Reload the page and you'll find one response for each floor (the 1st floor is generally the first request)
5. Right-click → **Copy Response** → Save as `floor1.fml`. Do this for each floor.

#### From Funda.nl

1. Open a listing with an interactive floor plan
2. Open browser DevTools → **Network** tab
3. Click "Plattegrond" to load the interactive floorplan
4. Filter by `fml`
5. Save the `fml` file (usually one file for all floors)

### 3. Download Assets

```bash
fml2blender harvest /path/to/project
```

This parses your FML files and downloads:

- 3D models (GLB) for furniture, windows, doors
- Textures (JPG/PNG) for walls and floors

Assets are saved to `assets/` and a `manifest.json` is created.

### 4. Build Blender Scene

```bash
fml2blender build /path/to/project
```

This runs Blender in background mode and builds the 3D scene.

Options:

```bash
fml2blender build /path/to/project --gui          # Open in Blender GUI
fml2blender build /path/to/project -o scene.blend # Save to file
fml2blender build /path/to/project --level-height 3.0  # Custom floor height
```

## Project Structure

After harvesting, your project directory should look like:

```
my-house/
├── floor1.fml          # Ground floor
├── floor2.fml          # First floor
├── floor3.fml          # Second floor
├── manifest.json       # Asset ID → file path mapping
├── products.json       # Product metadata (dimensions, names)
├── materials.json      # Material metadata (colors, PBR maps, tiling)
└── assets/
    ├── abc123.glb      # 3D models
    ├── def456.glb
    └── texture.jpg     # Textures
```

## Requirements

- Python 3.10+
- [Blender](https://blender.org) 4.0+ (for build command)
- For legacy GLB v1 assets (rare older windows/doors), install Node.js and `gltf-pipeline` so harvest can auto-upgrade to glTF 2: `npm i -g gltf-pipeline`

The `harvest` command only needs Python. The `build` command requires Blender to be installed.

## How It Works

1. **Harvest**: Parses FML JSON files to extract asset IDs, resolves them via Floorplanner's search API, downloads GLB models and textures from their CDN.

2. **Build**: Runs inside Blender to:
   - Create walls from line segments with proper thickness
   - Apply wall textures to left/right sides
   - Import GLB models for furniture and openings
   - Cut holes in walls for windows/doors
   - Set up glass materials for transparency

## License

MIT

## Contributing

Contributions welcome!
