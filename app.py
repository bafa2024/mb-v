# app.py - Integrated Weather Visualization Application
from fastapi import FastAPI, UploadFile, File, Request, Form, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
import xarray as xr
import rioxarray
import numpy as np
import os
import json
import tempfile
import traceback
from pathlib import Path
from typing import Optional, Dict, List
import aiofiles
from datetime import datetime
import logging
import asyncio
import subprocess
from pydantic import BaseModel
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Import your existing modules
from tileset_management import MapboxTilesetManager

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Create FastAPI app
app = FastAPI(title="Weather Visualization Platform", version="3.0.0")

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configuration
class Config:
    UPLOAD_DIR = Path("uploads")
    PROCESSED_DIR = Path("processed")
    RECIPE_DIR = Path("recipes")
    STATIC_DIR = Path("static")
    TEMPLATES_DIR = Path("templates")
    MAX_FILE_SIZE = 500 * 1024 * 1024  # 500MB
    
    # Load Mapbox credentials from environment
    MAPBOX_USERNAME = os.getenv("MAPBOX_USERNAME", "")
    MAPBOX_TOKEN = os.getenv("MAPBOX_TOKEN", "")
    MAPBOX_PUBLIC_TOKEN = os.getenv("MAPBOX_PUBLIC_TOKEN", "")
    
    # Use public token if available, otherwise fall back to main token
    if not MAPBOX_PUBLIC_TOKEN and MAPBOX_TOKEN:
        MAPBOX_PUBLIC_TOKEN = MAPBOX_TOKEN
    
# Create directories
for dir_path in [Config.UPLOAD_DIR, Config.PROCESSED_DIR, Config.RECIPE_DIR, 
                 Config.STATIC_DIR, Config.TEMPLATES_DIR]:
    dir_path.mkdir(exist_ok=True)

# Mount static files
app.mount("/static", StaticFiles(directory=str(Config.STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(Config.TEMPLATES_DIR))

# Log configuration status
logger.info(f"Mapbox Username: {Config.MAPBOX_USERNAME}")
logger.info(f"Mapbox Token configured: {'Yes' if Config.MAPBOX_TOKEN else 'No'}")
logger.info(f"Mapbox Public Token configured: {'Yes' if Config.MAPBOX_PUBLIC_TOKEN else 'No'}")

# Models
class ProcessingStatus(BaseModel):
    job_id: str
    status: str
    message: str
    tileset_id: Optional[str] = None
    visualization_url: Optional[str] = None
    error: Optional[str] = None

# In-memory storage for active visualizations
active_visualizations = {}

# Default weather tileset for main page
DEFAULT_TILESET = {
    "id": "mapbox.gfs-winds",
    "name": "Global Weather Data",
    "type": "default"
}

@app.get("/", response_class=HTMLResponse)
async def main_page(request: Request):
    """Main page with weather visualization"""
    # Get list of available tilesets
    available_tilesets = []
    
    # Add default Mapbox weather data
    available_tilesets.append(DEFAULT_TILESET)
    
    # Add user's uploaded tilesets
    if Config.MAPBOX_TOKEN and Config.MAPBOX_USERNAME:
        try:
            manager = MapboxTilesetManager(Config.MAPBOX_TOKEN, Config.MAPBOX_USERNAME)
            user_tilesets = manager.list_tilesets(limit=10)
            for ts in user_tilesets:
                # Include all user tilesets that might be weather-related
                if any(keyword in ts.get('name', '').lower() for keyword in ['weather', 'netcdf', 'wx_', 'wind', 'flow']):
                    available_tilesets.append({
                        "id": ts['id'],
                        "name": ts.get('name', ts['id']),
                        "type": "user",
                        "created": ts.get('created', ''),
                        "modified": ts.get('modified', '')
                    })
        except Exception as e:
            logger.error(f"Error fetching user tilesets: {e}")
    
    # Log what we're passing to template
    logger.info(f"Passing to template - Token: {Config.MAPBOX_PUBLIC_TOKEN[:10] if Config.MAPBOX_PUBLIC_TOKEN else 'None'}... Username: {Config.MAPBOX_USERNAME}")
    logger.info(f"Available tilesets: {len(available_tilesets)}")
    
    return templates.TemplateResponse("main_weather_map.html", {
        "request": request,
        "mapbox_token": Config.MAPBOX_PUBLIC_TOKEN,  # Use public token for client-side
        "mapbox_username": Config.MAPBOX_USERNAME,
        "available_tilesets": available_tilesets,
        "default_tileset": DEFAULT_TILESET
    })

@app.post("/api/upload-netcdf")
async def upload_netcdf(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    create_tileset: bool = Form(True),
    tileset_name: Optional[str] = Form(None)
):
    """Upload and process NetCDF file"""
    
    # Validate file
    if not file.filename.endswith('.nc'):
        raise HTTPException(400, "Only NetCDF (.nc) files are allowed")
    
    # Check file size
    file_size = 0
    content = await file.read()
    file_size = len(content)
    
    if file_size > Config.MAX_FILE_SIZE:
        raise HTTPException(400, f"File too large. Maximum size is {Config.MAX_FILE_SIZE / 1024 / 1024}MB")
    
    # Create job
    job_id = datetime.now().strftime("%Y%m%d%H%M%S")
    
    # Sanitize filename before saving
    original_filename = file.filename
    # Remove any path components and get just the filename
    safe_filename = Path(original_filename).name
    # Replace special characters with underscores
    safe_filename = ''.join(c if c.isalnum() or c in '.-_' else '_' for c in safe_filename)
    # Ensure it ends with .nc
    if not safe_filename.endswith('.nc'):
        safe_filename = safe_filename.rsplit('.', 1)[0] + '.nc'
    
    # Save uploaded file with sanitized name
    file_path = Config.UPLOAD_DIR / f"{job_id}_{safe_filename}"
    
    logger.info(f"Original filename: {original_filename}")
    logger.info(f"Sanitized filename: {safe_filename}")
    logger.info(f"Full file path: {file_path}")
    
    async with aiofiles.open(file_path, 'wb') as f:
        await f.write(content)
    
    logger.info(f"Saved uploaded file: {file_path}")
    
    # Process file
    try:
        # Initial processing - analyze file
        result = await process_netcdf_file(file_path, job_id, create_tileset, tileset_name)
        
        if create_tileset and Config.MAPBOX_TOKEN and Config.MAPBOX_USERNAME:
            # Start background tileset creation
            background_tasks.add_task(
                create_mapbox_tileset_background,
                file_path,
                job_id,
                result.get('tileset_id')
            )
            
            result['status'] = 'processing'
            result['message'] = 'File uploaded successfully. Creating Mapbox tileset in background...'
        
        return JSONResponse(result)
        
    except Exception as e:
        logger.error(f"Error processing file: {str(e)}")
        logger.error(traceback.format_exc())
        return JSONResponse({
            "success": False,
            "error": str(e)
        }, status_code=500)

async def process_netcdf_file(file_path: Path, job_id: str, create_tileset: bool, tileset_name: Optional[str]) -> Dict:
    """Process NetCDF file and extract metadata"""
    try:
        ds = xr.open_dataset(file_path)
        
        # Extract metadata
        metadata = {
            "dimensions": dict(ds.dims),
            "variables": list(ds.data_vars),
            "coordinates": list(ds.coords),
            "attributes": dict(ds.attrs)
        }
        
        # Analyze variables
        scalar_vars = []
        vector_pairs = []
        
        # Common patterns for vector fields
        vector_patterns = [
            ('u', 'v'), ('u10', 'v10'), ('u_wind', 'v_wind'),
            ('water_u', 'water_v'), ('eastward', 'northward')
        ]
        
        processed_vars = set()
        for u_pattern, v_pattern in vector_patterns:
            u_matches = [v for v in metadata['variables'] if u_pattern in v.lower() and v not in processed_vars]
            v_matches = [v for v in metadata['variables'] if v_pattern in v.lower() and v not in processed_vars]
            
            if u_matches and v_matches:
                for u_var in u_matches:
                    for v_var in v_matches:
                        if u_var.replace(u_pattern, '') == v_var.replace(v_pattern, ''):
                            vector_pairs.append({
                                "name": "wind" if "wind" in u_var.lower() else "flow",
                                "u": u_var,
                                "v": v_var
                            })
                            processed_vars.add(u_var)
                            processed_vars.add(v_var)
                            break
        
        scalar_vars = [v for v in metadata['variables'] if v not in processed_vars]
        
        # Generate tileset ID that meets Mapbox requirements
        if not tileset_name:
            # Get just the filename without path and extension
            filename = Path(file_path).stem
            # Remove all special characters and spaces
            tileset_name = ''.join(c for c in filename if c.isalnum() or c in '-_')
            if not tileset_name:  # If nothing left, use default
                tileset_name = "netcdf_data"
        
        # Ensure tileset name is lowercase and properly formatted
        tileset_name = tileset_name.lower()
        # Replace any remaining special chars with underscore
        tileset_name = ''.join(c if c.isalnum() or c in '-_' else '_' for c in tileset_name)
        # Remove multiple underscores
        tileset_name = '_'.join(part for part in tileset_name.split('_') if part)
        
        # Create a shorter tileset ID to ensure it's under 32 chars
        # Format: wx_[name]_[timestamp]  (wx = weather, shorter prefix)
        timestamp = datetime.now().strftime("%m%d%H%M")  # Even shorter timestamp
        prefix = "wx"
        max_name_length = 32 - len(prefix) - len(timestamp) - 2  # 2 for underscores
        
        if len(tileset_name) > max_name_length:
            tileset_name = tileset_name[:max_name_length]
        
        tileset_id = f"{prefix}_{tileset_name}_{timestamp}"
        
        # Final validation
        tileset_id = tileset_id.lower()
        tileset_id = ''.join(c for c in tileset_id if c.isalnum() or c in '-_')
        tileset_id = tileset_id[:32]
        
        logger.info(f"Original filename: {Path(file_path).name}")
        logger.info(f"Sanitized tileset_name: {tileset_name}")
        logger.info(f"Final tileset_id: {tileset_id} (length: {len(tileset_id)})")
        
        # Create previews for first few variables
        previews = {}
        for var in metadata['variables'][:3]:  # Limit to first 3 variables
            try:
                data = ds[var]
                if 'time' in data.dims:
                    data = data.isel(time=0)
                
                # Get statistics
                valid_data = data.values[~np.isnan(data.values)]
                if len(valid_data) > 0:
                    previews[var] = {
                        "min": float(np.min(valid_data)),
                        "max": float(np.max(valid_data)),
                        "mean": float(np.mean(valid_data)),
                        "units": data.attrs.get('units', 'unknown')
                    }
            except Exception as e:
                logger.warning(f"Could not create preview for {var}: {e}")
        
        ds.close()
        
        # Store in active visualizations
        active_visualizations[job_id] = {
            "file_path": str(file_path),
            "tileset_id": tileset_id,
            "metadata": metadata,
            "scalar_vars": scalar_vars,
            "vector_pairs": vector_pairs,
            "previews": previews,
            "created_at": datetime.now().isoformat()
        }
        
        return {
            "success": True,
            "job_id": job_id,
            "tileset_id": tileset_id,
            "metadata": metadata,
            "scalar_vars": scalar_vars,
            "vector_pairs": vector_pairs,
            "previews": previews
        }
        
    except Exception as e:
        logger.error(f"Error in process_netcdf_file: {str(e)}")
        raise

async def create_mapbox_tileset_background(file_path: Path, job_id: str, tileset_id: str):
    """Background task to create Mapbox tileset"""
    try:
        if not Config.MAPBOX_TOKEN:
            logger.error("Mapbox token not configured for tileset creation")
            if job_id in active_visualizations:
                active_visualizations[job_id]['status'] = 'failed'
                active_visualizations[job_id]['error'] = 'Mapbox token not configured'
            return
            
        manager = MapboxTilesetManager(Config.MAPBOX_TOKEN, Config.MAPBOX_USERNAME)
        
        # Don't use the complex recipe generator - it will be handled in the manager
        # Just pass the basic info
        result = manager.process_netcdf_to_tileset(str(file_path), tileset_id, {})
        
        if result['success']:
            # Update visualization info
            if job_id in active_visualizations:
                active_visualizations[job_id]['mapbox_tileset'] = result['tileset_id']
                active_visualizations[job_id]['status'] = 'completed'
                
                # Save the recipe info for later reference
                recipe_path = Config.RECIPE_DIR / f"recipe_{tileset_id}.json"
                recipe_data = {
                    "tileset_id": tileset_id,
                    "mapbox_tileset": result['tileset_id'],
                    "created": datetime.now().isoformat(),
                    "source_layer": "weather_data"  # This matches what's created in tileset_management
                }
                
                with open(recipe_path, 'w') as f:
                    json.dump(recipe_data, f, indent=2)
                    
                logger.info(f"Saved recipe info to {recipe_path}")
        else:
            if job_id in active_visualizations:
                active_visualizations[job_id]['status'] = 'failed'
                active_visualizations[job_id]['error'] = result.get('error')
                
    except Exception as e:
        logger.error(f"Error creating tileset: {str(e)}")
        if job_id in active_visualizations:
            active_visualizations[job_id]['status'] = 'failed'
            active_visualizations[job_id]['error'] = str(e)

@app.get("/api/visualization-status/{job_id}")
async def get_visualization_status(job_id: str):
    """Get status of visualization processing"""
    if job_id not in active_visualizations:
        raise HTTPException(404, "Job not found")
    
    viz_info = active_visualizations[job_id]
    
    return JSONResponse({
        "job_id": job_id,
        "status": viz_info.get('status', 'processing'),
        "tileset_id": viz_info.get('tileset_id'),
        "mapbox_tileset": viz_info.get('mapbox_tileset'),
        "error": viz_info.get('error'),
        "metadata": viz_info.get('metadata'),
        "previews": viz_info.get('previews')
    })

@app.post("/api/load-tileset")
async def load_tileset(tileset_id: str = Form(...)):
    """Load a specific tileset for visualization"""
    try:
        # Check if it's a default tileset
        if tileset_id == DEFAULT_TILESET['id']:
            return JSONResponse({
                "success": True,
                "tileset_id": tileset_id,
                "type": "default",
                "config": {
                    "layers": ["wind"],
                    "wind_source": tileset_id,
                    "source_layer": "10winds"
                }
            })
        
        # For user tilesets, get the recipe
        # Handle both full tileset IDs and partial IDs
        tileset_name = tileset_id.split('.')[-1] if '.' in tileset_id else tileset_id
        recipe_files = list(Config.RECIPE_DIR.glob(f"*{tileset_name}*.json"))
        
        source_layer = "weather_data"  # Default source layer name from the recipe
        
        if recipe_files:
            try:
                with open(recipe_files[0], 'r') as f:
                    recipe_data = json.load(f)
                
                # Check if it's our simplified recipe format
                if 'source_layer' in recipe_data:
                    source_layer = recipe_data['source_layer']
                # Check if it's the old complex recipe format
                elif 'layers' in recipe_data:
                    layers = recipe_data.get('layers', {})
                    if layers:
                        # Get the first layer name
                        source_layer = list(layers.keys())[0]
                
                logger.info(f"Found recipe for {tileset_name}, using source layer: {source_layer}")
                
                return JSONResponse({
                    "success": True,
                    "tileset_id": tileset_id,
                    "type": "user",
                    "recipe": recipe_data,
                    "config": {
                        "source_layer": source_layer,
                        "visualization_type": "points"
                    },
                    "source_layer": source_layer
                })
            except Exception as e:
                logger.error(f"Error reading recipe file: {e}")
        
        # If no recipe found, try to get info from Mapbox
        if Config.MAPBOX_TOKEN and Config.MAPBOX_USERNAME:
            manager = MapboxTilesetManager(Config.MAPBOX_TOKEN, Config.MAPBOX_USERNAME)
            tileset_info = manager.get_tileset_status(tileset_name)
            
            logger.info(f"No recipe found for {tileset_name}, using default source layer: {source_layer}")
            
            # Default to weather_data for uploaded data
            return JSONResponse({
                "success": True,
                "tileset_id": tileset_id,
                "type": "user",
                "info": tileset_info,
                "source_layer": source_layer
            })
        else:
            return JSONResponse({
                "success": False,
                "error": "Mapbox credentials not configured"
            }, status_code=500)
        
    except Exception as e:
        logger.error(f"Error loading tileset: {str(e)}")
        logger.error(traceback.format_exc())
        return JSONResponse({
            "success": False,
            "error": str(e)
        }, status_code=500)

def extract_visualization_config(recipe: Dict) -> Dict:
    """Extract visualization configuration from recipe"""
    config = {
        "variables": [],
        "vector_fields": [],
        "layers": []
    }
    
    metadata = recipe.get('metadata', {})
    bands_info = metadata.get('bands_info', {})
    
    # Extract scalar variables
    for band_name, info in bands_info.items():
        if info.get('type') == 'scalar':
            config['variables'].append({
                'name': band_name,
                'display_name': info.get('long_name', band_name),
                'units': info.get('units', ''),
                'range': [info['stats']['min'], info['stats']['max']],
                'band_index': info['band_index']
            })
    
    # Extract vector fields
    vector_pairs = metadata.get('vector_pairs', [])
    for pair in vector_pairs:
        u_band = f"{pair['name']}_u"
        v_band = f"{pair['name']}_v"
        if u_band in bands_info and v_band in bands_info:
            config['vector_fields'].append({
                'name': pair['name'],
                'u_band': u_band,
                'v_band': v_band,
                'u_index': bands_info[u_band]['band_index'],
                'v_index': bands_info[v_band]['band_index']
            })
    
    # Available layers
    config['layers'] = list(recipe.get('layers', {}).keys())
    
    return config

@app.get("/api/active-visualizations")
async def get_active_visualizations():
    """Get list of active visualizations"""
    return JSONResponse({
        "visualizations": [
            {
                "job_id": job_id,
                "tileset_id": viz.get('tileset_id'),
                "mapbox_tileset": viz.get('mapbox_tileset'),
                "status": viz.get('status', 'processing'),
                "created_at": viz.get('created_at'),
                "metadata": {
                    "variables": viz.get('metadata', {}).get('variables', []),
                    "dimensions": viz.get('metadata', {}).get('dimensions', {})
                }
            }
            for job_id, viz in active_visualizations.items()
        ]
    })

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "mapbox_configured": bool(Config.MAPBOX_TOKEN and Config.MAPBOX_USERNAME),
        "mapbox_public_token": bool(Config.MAPBOX_PUBLIC_TOKEN),
        "active_jobs": len(active_visualizations),
        "version": "3.0.0"
    }

# Cleanup old files periodically
async def cleanup_old_files():
    """Remove old temporary files"""
    try:
        cutoff_time = datetime.now().timestamp() - (24 * 3600)  # 24 hours
        
        for dir_path in [Config.UPLOAD_DIR, Config.PROCESSED_DIR]:
            for file_path in dir_path.glob("*"):
                if file_path.is_file() and file_path.stat().st_mtime < cutoff_time:
                    file_path.unlink()
                    logger.info(f"Cleaned up old file: {file_path}")
                    
    except Exception as e:
        logger.error(f"Error during cleanup: {e}")

# Schedule cleanup on startup
@app.on_event("startup")
async def startup_event():
    logger.info("Starting Weather Visualization Platform...")
    logger.info(f"Environment: Mapbox Username: {Config.MAPBOX_USERNAME}")
    logger.info(f"Environment: Mapbox Token Set: {'Yes' if Config.MAPBOX_TOKEN else 'No'}")
    logger.info(f"Environment: Mapbox Public Token Set: {'Yes' if Config.MAPBOX_PUBLIC_TOKEN else 'No'}")
    await cleanup_old_files()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)