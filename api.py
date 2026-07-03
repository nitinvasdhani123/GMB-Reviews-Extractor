from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import subprocess
import json
import os
import sys
import asyncio
import logging
import traceback

# ── logging to stdout and app.log ─────────────────────────────────────────────
log_handlers = [
    logging.StreamHandler(sys.stdout),
    logging.FileHandler(os.path.join(os.path.dirname(__file__), "app.log"), encoding="utf-8")
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
    handlers=log_handlers
)
log = logging.getLogger("api")

app = FastAPI()


class ScrapeRequest(BaseModel):
    url: str
    sort: str = "newest"
    limit: int = 50


@app.get("/")
def home():
    log.info("Home route accessed")
    return {"status": "running"}


@app.post("/scrape")
async def scrape(data: ScrapeRequest):
    log.info(f"API request received: URL={data.url}, sort={data.sort}, limit={data.limit}")

    scraper_path = os.path.join(
        os.path.dirname(__file__),
        "scraper.py"
    )

    cmd = [
        sys.executable,
        scraper_path,
        f"--url={data.url}",
        f"--sort={data.sort}",
        f"--max-reviews={data.limit}",
        "--output=json",
        "--headless"
    ]

    try:
        log.info(f"Launching scraper process: {' '.join(cmd)}")
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        try:
            # Set a timeout of 120 seconds to prevent Railway gateway timeout
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=120)
        except asyncio.TimeoutError:
            log.error("Scraper process timed out (exceeded 120s). Terminating process.")
            try:
                process.kill()
                await process.wait()
            except Exception as kill_err:
                log.error(f"Failed to kill scraper process: {kill_err}")
            raise HTTPException(
                status_code=408,
                detail="Scraper execution timed out (limit 120 seconds)"
            )

        stdout_str = stdout.decode("utf-8", errors="replace")
        stderr_str = stderr.decode("utf-8", errors="replace")

        if stderr_str:
            log.info(f"Scraper stderr output:\n{stderr_str}")

        if process.returncode != 0:
            log.error(f"Scraper exited with non-zero return code {process.returncode}. Stderr:\n{stderr_str}")
            raise HTTPException(
                status_code=500,
                detail=f"Scraper failed: {stderr_str}"
            )

        if not stdout_str.strip():
            log.error("Scraper returned empty stdout")
            raise HTTPException(
                status_code=500,
                detail="Scraper returned empty output"
            )

        output = stdout_str.strip()
        json_start = output.find('{')
        if json_start == -1:
            log.error(f"JSON not found in scraper output. Raw output: {output[:1000]}")
            raise HTTPException(
                status_code=500,
                detail="JSON not found in scraper output"
            )

        result_data = json.loads(output[json_start:])
        log.info(f"Successfully scraped {len(result_data.get('reviews', []))} reviews for place: {result_data.get('business_name')}")
        return result_data

    except json.JSONDecodeError as e:
        log.error(f"Invalid JSON returned from scraper: {e}\n{traceback.format_exc()}")
        raise HTTPException(
            status_code=500,
            detail="Invalid JSON returned from scraper"
        )
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Unexpected error: {e}\n{traceback.format_exc()}")
        raise HTTPException(
            status_code=500,
            detail=str(e)
        )