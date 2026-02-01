from fastapi import FastAPI, APIRouter, HTTPException, BackgroundTasks
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
from pathlib import Path
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
import uuid
from datetime import datetime
import httpx
from bs4 import BeautifulSoup
import base64
from urllib.parse import urljoin, urlparse

# Load environment variables
load_dotenv()

# MongoDB connection - use environment variable
MONGO_URL = os.getenv('MONGO_URL', 'mongodb://localhost:27017')
DB_NAME = os.getenv('DB_NAME', 'webscraper')

client = AsyncIOMotorClient(MONGO_URL)
db = client[DB_NAME]

# Create the main app
app = FastAPI(title="Web Scraper API", version="1.0.0")

# Create a router with the /api prefix
api_router = APIRouter(prefix="/api")

# CORS - allow all origins for now
app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Models
class ScrapeOptions(BaseModel):
    clone_page: bool = True
    clone_entire_site: bool = False
    deep_scrape: bool = False
    include_images: bool = True
    include_css: bool = True
    include_javascript: bool = False
    follow_external_links: bool = False
    max_pages: int = 50

class ScrapedPage(BaseModel):
    page_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    url: str
    title: Optional[str] = None
    html: str
    css: Optional[str] = None
    images: List[Dict[str, str]] = []
    scripts: List[str] = []
    links: List[str] = []
    metadata: Dict[str, Any] = {}
    scraped_at: datetime = Field(default_factory=datetime.utcnow)

class ScrapeJob(BaseModel):
    job_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    start_url: str
    options: ScrapeOptions
    status: str = "in_progress"
    pages: List[ScrapedPage] = []
    total_pages: int = 0
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None
    external_app_url: Optional[str] = None

class StartScrapeRequest(BaseModel):
    start_url: str
    options: ScrapeOptions

class AddPageRequest(BaseModel):
    job_id: str
    page: ScrapedPage

class UpdatePageRequest(BaseModel):
    html: Optional[str] = None
    css: Optional[str] = None
    images: Optional[List[Dict[str, str]]] = None

class CompleteScrapeRequest(BaseModel):
    job_id: str
    external_app_url: str

class WebScrapeRequest(BaseModel):
    url: str
    options: Optional[ScrapeOptions] = ScrapeOptions()
    convert_images_to_base64: bool = False

class WebScrapeResponse(BaseModel):
    success: bool
    data: Optional[Dict[str, Any]] = None
    message: str
    scrape_id: str

# Web scraping function
async def scrape_url_simple(url: str, include_images: bool = True) -> Dict[str, Any]:
    """Scrape a URL using simple HTTP request + BeautifulSoup"""
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            response = await client.get(url, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            })
            response.raise_for_status()
            
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Extract title
        title = soup.title.string if soup.title else url
        
        # Extract all CSS
        css = []
        for style_tag in soup.find_all('style'):
            css.append(style_tag.string or '')
        for link_tag in soup.find_all('link', rel='stylesheet'):
            if link_tag.get('href'):
                css.append(f"/* External: {link_tag.get('href')} */")
        
        # Extract images
        images = []
        if include_images:
            for img in soup.find_all('img'):
                img_src = img.get('src', '')
                if img_src:
                    absolute_url = urljoin(url, img_src)
                    images.append({
                        'url': absolute_url,
                        'alt': img.get('alt', ''),
                        'width': img.get('width', ''),
                        'height': img.get('height', '')
                    })
        
        # Extract all links
        links = []
        for a_tag in soup.find_all('a', href=True):
            absolute_url = urljoin(url, a_tag['href'])
            links.append(absolute_url)
        
        # Extract scripts
        scripts = []
        for script_tag in soup.find_all('script', src=True):
            absolute_url = urljoin(url, script_tag['src'])
            scripts.append(absolute_url)
        
        return {
            'url': url,
            'title': title,
            'html': str(soup),
            'css': '\n'.join(css),
            'images': images,
            'links': list(set(links)),
            'scripts': scripts,
            'status_code': response.status_code,
            'content_type': response.headers.get('content-type', '')
        }
    except Exception as e:
        logging.error(f"Error scraping {url}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to scrape URL: {str(e)}")

# Routes
@api_router.get("/")
async def root():
    return {"message": "Web Scraper API", "version": "1.0.0"}

@api_router.post("/scrape/web", response_model=WebScrapeResponse)
async def scrape_from_web(request: WebScrapeRequest):
    """Web-based scraping endpoint - No mobile app required!"""
    try:
        logging.info(f"Web scraping request for: {request.url}")
        
        scraped_data = await scrape_url_simple(
            request.url,
            include_images=request.options.include_images
        )
        
        scrape_id = str(uuid.uuid4())
        scrape_record = {
            'scrape_id': scrape_id,
            'url': request.url,
            'data': scraped_data,
            'options': request.options.dict(),
            'created_at': datetime.utcnow(),
            'source': 'web_api'
        }
        
        await db.web_scrapes.insert_one(scrape_record)
        
        logging.info(f"Successfully scraped {request.url}, scrape_id: {scrape_id}")
        
        return WebScrapeResponse(
            success=True,
            data=scraped_data,
            message="URL scraped successfully",
            scrape_id=scrape_id
        )
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Web scraping error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Scraping failed: {str(e)}")

@api_router.get("/scrape/web/{scrape_id}")
async def get_web_scrape(scrape_id: str):
    """Retrieve a web scrape by ID"""
    scrape = await db.web_scrapes.find_one({"scrape_id": scrape_id})
    
    if not scrape:
        raise HTTPException(status_code=404, detail="Scrape not found")
    
    scrape.pop('_id', None)
    return scrape

@api_router.get("/scrape/web")
async def list_web_scrapes(limit: int = 50):
    """List recent web scrapes"""
    scrapes = await db.web_scrapes.find().sort("created_at", -1).limit(limit).to_list(limit)
    
    for scrape in scrapes:
        scrape.pop('_id', None)
    
    return {"scrapes": scrapes, "count": len(scrapes)}

# Health check
@app.get("/health")
async def health_check():
    return {"status": "healthy", "timestamp": datetime.utcnow()}

# Include the router in the main app
app.include_router(api_router)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

@app.on_event("startup")
async def startup_event():
    logger.info("Starting Web Scraper API")
    logger.info(f"MongoDB URL: {MONGO_URL}")

@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
