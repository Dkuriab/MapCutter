"""Local web server: map in the browser -> STL zip."""
from pathlib import Path
from typing import List

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

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
def build(job: Job):  # plain def: FastAPI runs it in a worker thread
    log = []
    try:
        data, n = core.run(job.bbox, job.width_mm, job.leds, bed=job.bed_mm,
                           log=lambda s: (log.append(s), print(s)))
    except core.BuildError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"{type(e).__name__}: {e}")
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": "attachment; filename=city_map.zip",
                             "X-Pieces": str(n),
                             "Access-Control-Expose-Headers": "X-Pieces"})
