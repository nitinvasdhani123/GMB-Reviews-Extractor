from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import subprocess
import json
import os
import sys

app = FastAPI()


class ScrapeRequest(BaseModel):
    url: str
    sort: str = "newest"


@app.get("/")
def home():
    return {"status": "running"}


@app.post("/scrape")
def scrape(data: ScrapeRequest):

    scraper_path = os.path.join(
        os.path.dirname(__file__),
        "scraper.py"
    )

    cmd = [
        sys.executable,
        scraper_path,
        f"--url={data.url}",
        f"--sort={data.sort}",
        "--output=json",
        "--headless"
    ]
    # cmd = [
    #     "xvfb-run",
    #     "-a",
    #     sys.executable,
    #     scraper_path,
    #     f"--url={data.url}",
    #     f"--sort={data.sort}",
    #     "--output=json"
    # ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=600
        )

        print("===== STDERR =====")
        print(result.stderr)

        print("===== STDOUT =====")
        print(result.stdout)

        if result.returncode != 0:
            raise HTTPException(
                status_code=500,
                detail=result.stderr
            )
        
        if not result.stdout:
            raise HTTPException(
                status_code=500,
                detail="Scraper returned empty output"
            )

        # print("RETURN CODE:", result.returncode)
        # print("STDOUT:", repr(result.stdout))
        # print("STDERR:", repr(result.stderr))

        # return json.loads(result.stdout)
        output = result.stdout.strip()
        json_start = output.find('{')
        if json_start == -1:
            raise HTTPException(
                status_code=500,
                detail="JSON not found in scraper output"
            )

        return json.loads(output[json_start:])

    except json.JSONDecodeError:
        raise HTTPException(
            status_code=500,
            detail="Invalid JSON returned from scraper"
        )

    except subprocess.TimeoutExpired:
        raise HTTPException(
            status_code=408,
            detail="Scraper timeout"
        )

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=str(e)
        )