"""Local web server: map in the browser -> STL zip."""
import json
import shutil
import tempfile
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field, ValidationError

import core

HERE = Path(__file__).resolve().parent
app = FastAPI(title="City Map 3D")


class Job(BaseModel):
    bbox: List[float] = Field(..., min_length=4, max_length=4)  # [west, south, east, north]
    width_mm: float = Field(400, gt=50, le=3000)
    bed_mm: float = Field(256, ge=100, le=1000)
    leds: List[List[float]] = []  # [[lat, lon], ...]


@app.get("/")
def index():
    return FileResponse(HERE / "static" / "index.html")


@app.post("/build")
def build(job: str = Form(...), osm: Optional[UploadFile] = File(None)):
    # plain def: FastAPI runs it in a worker thread
    try:
        j = Job(**json.loads(job))
    except (ValueError, ValidationError) as e:
        raise HTTPException(422, str(e))
    tmpdir = None
    try:
        osm_path = None
        if osm is not None and osm.filename:
            tmpdir = tempfile.mkdtemp(prefix="citymap_")
            osm_path = Path(tmpdir) / Path(osm.filename).name
            with open(osm_path, "wb") as f:
                shutil.copyfileobj(osm.file, f)
        data, n = core.run(j.bbox, j.width_mm, j.leds, bed=j.bed_mm, osm_file=osm_path, log=print)
    except core.BuildError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"{type(e).__name__}: {e}")
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": "attachment; filename=city_map.zip",
                             "X-Pieces": str(n),
                             "Access-Control-Expose-Headers": "X-Pieces"})
