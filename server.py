from fastapi import FastAPI, APIRouter, HTTPException, BackgroundTasks
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import Response
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

load_dotenv()

MONGO_URL = os.getenv('MONGO_URL', 'mongodb://localhost:27017')
DB_NAME = os.getenv('DB_NAME', 'webscraper')

client = AsyncIOMotorClient(MONGO_URL)
db = client[DB_NAME]

app = FastAPI(title="Web Scraper API", version="2.0.0")
api_router = APIRouter(prefix="/api")

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

class ScrapeMode(BaseModel):
    html: bool = True
    css: bool = True
    images: bool = True
    links: bool = True
    scripts: bool = False
    text_only: bool = False

class ScrapedPage(BaseModel):
    page_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    url: str
    title: Optional[str] = None
    html: Optional[str] = None
    css: Optional[str] = None
    text: Optional[str] = None
    images: List[Dict[str, str]] = []
    links: List[str] = []
    scripts: List[str] = []
    scraped_at: datetime = Field(default_factory=datetime.utcnow)
    mode: Dict[str, bool] = {}

class ScrapeSession(BaseModel):
    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: Optional[str] = None
    pages: List[ScrapedPage] = []
    total_pages: int = 0
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None
    status: str = "active"
    webhook_url: Optional[str] = None
    webhook_sent: bool = False

class StartSessionRequest(BaseModel):
    name: Optional[str] = None
    webhook_url: Optional[str] = None

class ScrapePageRequest(BaseModel):
    session_id: str
    url: str
    mode: ScrapeMode = ScrapeMode()

class CompleteSessionRequest(BaseModel):
    session_id: str
    webhook_url: Optional[str] = None

class WebScrapeRequest(BaseModel):
    url: str
    mode: ScrapeMode = ScrapeMode()

async def scrape_url(url: str, mode: ScrapeMode) -> Dict[str, Any]:
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as http_client:
            response = await http_client.get(url, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            })
            response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        result = {
            'url': url,
            'title': soup.title.string if soup.title else url,
            'scraped_at': datetime.utcnow().isoformat(),
            'mode': mode.dict()
        }
        if mode.html:
            if mode.text_only:
                result['text'] = soup.get_text(separator='\n', strip=True)
            else:
                result['html'] = str(soup)
        if mode.css:
            css = []
            for style_tag in soup.find_all('style'):
                if style_tag.string:
                    css.append(style_tag.string)
            for link_tag in soup.find_all('link', rel='stylesheet'):
                href = link_tag.get('href')
                if href:
                    absolute_url = urljoin(url, href)
                    css.append(f"/* External stylesheet: {absolute_url} */")
                    try:
                        async with httpx.AsyncClient(timeout=10.0) as css_client:
                            css_response = await css_client.get(absolute_url)
                            if css_response.status_code == 200:
                                css.append(css_response.text)
                    except:
                        pass
            result['css'] = '\n\n'.join(css)
        if mode.images:
            images = []
            for img in soup.find_all('img'):
                img_src = img.get('src', '')
                if img_src:
                    absolute_url = urljoin(url, img_src)
                    images.append({'url': absolute_url, 'alt': img.get('alt', ''), 'width': img.get('width', ''), 'height': img.get('height', '')})
            result['images'] = images
        if mode.links:
            links = []
            for a_tag in soup.find_all('a', href=True):
                href = a_tag['href']
                if href and not href.startswith('#') and not href.startswith('javascript:'):
                    absolute_url = urljoin(url, href)
                    links.append({'url': absolute_url, 'text': a_tag.get_text(strip=True)[:100]})
            result['links'] = links
        if mode.scripts:
            scripts = []
            for script_tag in soup.find_all('script'):
                if script_tag.get('src'):
                    scripts.append({'type': 'external', 'url': urljoin(url, script_tag['src'])})
                elif script_tag.string:
                    scripts.append({'type': 'inline', 'content': script_tag.string[:500]})
            result['scripts'] = scripts
        return result
    except Exception as e:
        logging.error(f"Error scraping {url}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to scrape URL: {str(e)}")

@api_router.post("/session/start")
async def start_session(request: StartSessionRequest):
    session = ScrapeSession(name=request.name or f"Session {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}", webhook_url=request.webhook_url)
    await db.sessions.insert_one(session.dict())
    return {"success": True, "session_id": session.session_id, "name": session.name, "message": "Session started. You can now scrape multiple pages."}

@api_router.post("/session/scrape")
async def scrape_to_session(request: ScrapePageRequest):
    session = await db.sessions.find_one({"session_id": request.session_id})
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.get('status') == 'completed':
        raise HTTPException(status_code=400, detail="Session already completed")
    scraped_data = await scrape_url(request.url, request.mode)
    page = ScrapedPage(url=request.url, title=scraped_data.get('title'), html=scraped_data.get('html'), css=scraped_data.get('css'), text=scraped_data.get('text'), images=scraped_data.get('images', []), links=[l['url'] for l in scraped_data.get('links', [])], scripts=[s.get('url') or 'inline' for s in scraped_data.get('scripts', [])], mode=request.mode.dict())
    await db.sessions.update_one({"session_id": request.session_id}, {"$push": {"pages": page.dict()}, "$inc": {"total_pages": 1}, "$set": {"updated_at": datetime.utcnow()}})
    return {"success": True, "session_id": request.session_id, "page_id": page.page_id, "url": request.url, "title": page.title, "scraped": {"has_html": bool(page.html), "has_css": bool(page.css), "has_text": bool(page.text), "image_count": len(page.images), "link_count": len(page.links)}, "message": f"Page scraped and added to session"}

@api_router.get("/session/{session_id}")
async def get_session(session_id: str):
    session = await db.sessions.find_one({"session_id": session_id})
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    session.pop('_id', None)
    return session

@api_router.get("/session/{session_id}/summary")
async def get_session_summary(session_id: str):
    session = await db.sessions.find_one({"session_id": session_id})
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    pages_summary = []
    for page in session.get('pages', []):
        pages_summary.append({'page_id': page.get('page_id'), 'url': page.get('url'), 'title': page.get('title'), 'image_count': len(page.get('images', [])), 'link_count': len(page.get('links', [])), 'has_html': bool(page.get('html')), 'has_css': bool(page.get('css')), 'scraped_at': page.get('scraped_at')})
    return {'session_id': session_id, 'name': session.get('name'), 'status': session.get('status'), 'total_pages': session.get('total_pages', 0), 'pages': pages_summary, 'created_at': session.get('created_at'), 'updated_at': session.get('updated_at')}

@api_router.delete("/session/{session_id}/page/{page_id}")
async def remove_page_from_session(session_id: str, page_id: str):
    result = await db.sessions.update_one({"session_id": session_id}, {"$pull": {"pages": {"page_id": page_id}}, "$inc": {"total_pages": -1}, "$set": {"updated_at": datetime.utcnow()}})
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Session or page not found")
    return {"success": True, "message": "Page removed from session"}

@api_router.post("/session/complete")
async def complete_session(request: CompleteSessionRequest):
    session = await db.sessions.find_one({"session_id": request.session_id})
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    webhook_url = request.webhook_url or session.get('webhook_url')
    session.pop('_id', None)
    session_data = {'session_id': session['session_id'], 'name': session.get('name'), 'total_pages': session.get('total_pages', 0), 'pages': session.get('pages', []), 'created_at': str(session.get('created_at')), 'completed_at': str(datetime.utcnow())}
    await db.sessions.update_one({"session_id": request.session_id}, {"$set": {"status": "completed", "completed_at": datetime.utcnow()}})
    webhook_result = None
    if webhook_url:
        try:
            async with httpx.AsyncClient(timeout=30.0) as http_client:
                response = await http_client.post(webhook_url, json=session_data, headers={'Content-Type': 'application/json'})
                webhook_result = {'sent': True, 'status_code': response.status_code, 'response': response.text[:500] if response.text else None}
                await db.sessions.update_one({"session_id": request.session_id}, {"$set": {"webhook_sent": True, "status": "sent"}})
        except Exception as e:
            webhook_result = {'sent': False, 'error': str(e)}
    return {'success': True, 'session_id': request.session_id, 'total_pages': session_data['total_pages'], 'webhook': webhook_result, 'data': session_data, 'message': 'Session completed!' + (' Data sent to webhook.' if webhook_result and webhook_result.get('sent') else '')}

@api_router.get("/sessions")
async def list_sessions(limit: int = 20, status: Optional[str] = None):
    query = {}
    if status:
        query['status'] = status
    sessions = await db.sessions.find(query).sort("created_at", -1).limit(limit).to_list(limit)
    result = []
    for session in sessions:
        session.pop('_id', None)
        session['pages'] = len(session.get('pages', []))
        result.append(session)
    return {'sessions': result, 'count': len(result)}

@api_router.post("/scrape/quick")
async def quick_scrape(request: WebScrapeRequest):
    scraped_data = await scrape_url(request.url, request.mode)
    scrape_id = str(uuid.uuid4())
    await db.quick_scrapes.insert_one({'scrape_id': scrape_id, 'url': request.url, 'data': scraped_data, 'created_at': datetime.utcnow()})
    return {'success': True, 'scrape_id': scrape_id, 'data': scraped_data}

@api_router.get("/proxy")
async def proxy_website(url: str):
    if not url:
        raise HTTPException(status_code=400, detail="URL parameter is required")
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as http_client:
            response = await http_client.get(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'})
        content_type = response.headers.get('content-type', 'text/html')
        content = response.content
        if 'text/html' in content_type:
            soup = BeautifulSoup(response.text, 'html.parser')
            base_tag = soup.new_tag('base', href=url)
            if soup.head:
                soup.head.insert(0, base_tag)
            elif soup.html:
                head = soup.new_tag('head')
                head.append(base_tag)
                soup.html.insert(0, head)
            content = str(soup).encode('utf-8')
        return Response(content=content, status_code=response.status_code, media_type=content_type, headers={'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Methods': '*', 'Access-Control-Allow-Headers': '*'})
    except Exception as e:
        logging.error(f"Proxy error for {url}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to proxy URL: {str(e)}")

class LegacyScrapeOptions(BaseModel):
    clone_page: bool = True
    clone_entire_site: bool = False
    deep_scrape: bool = False
    include_images: bool = True
    include_css: bool = True
    include_javascript: bool = False
    follow_external_links: bool = False
    max_pages: int = 50

class LegacyWebScrapeRequest(BaseModel):
    url: str
    options: Optional[LegacyScrapeOptions] = LegacyScrapeOptions()
    convert_images_to_base64: bool = False

@api_router.post("/scrape/web")
async def legacy_scrape_from_web(request: LegacyWebScrapeRequest):
    mode = ScrapeMode(html=True, css=request.options.include_css, images=request.options.include_images, links=True, scripts=request.options.include_javascript)
    scraped_data = await scrape_url(request.url, mode)
    scrape_id = str(uuid.uuid4())
    await db.web_scrapes.insert_one({'scrape_id': scrape_id, 'url': request.url, 'data': scraped_data, 'created_at': datetime.utcnow()})
    return {'success': True, 'data': scraped_data, 'message': 'URL scraped successfully', 'scrape_id': scrape_id}

@api_router.get("/")
async def root():
    return {"message": "Web Scraper API", "version": "2.0.0", "endpoints": {"proxy": "GET /api/proxy?url=<url>", "start_session": "POST /api/session/start", "scrape_page": "POST /api/session/scrape", "get_session": "GET /api/session/<id>", "complete_session": "POST /api/session/complete", "quick_scrape": "POST /api/scrape/quick"}}

@app.get("/health")
async def health_check():
    return {"status": "healthy", "timestamp": datetime.utcnow()}

app.include_router(api_router)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

@app.on_event("startup")
async def startup_event():
    logger.info("Starting Web Scraper API v2.0.0")

@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
