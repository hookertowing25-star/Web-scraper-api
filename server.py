from fastapi import FastAPI, APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import Response
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
import re
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
import uuid
from datetime import datetime
import httpx
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import json

load_dotenv()

# Environment variables
MONGO_URL = os.getenv('MONGO_URL', 'mongodb://localhost:27017')
DB_NAME = os.getenv('DB_NAME', 'webscraper')
BROWSERLESS_API_KEY = os.getenv('BROWSERLESS_API_KEY', '2Tthi4OMNonpB1T45eabd9db6951daffd987e750e5678050f')
BROWSERLESS_URL = f"https://chrome.browserless.io"

client = AsyncIOMotorClient(MONGO_URL)
db = client[DB_NAME]

app = FastAPI(title="Marketing Scraper API", version="3.0.0")
api_router = APIRouter(prefix="/api")

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ========== MODELS ==========

class ScrapeOptions(BaseModel):
    # Lead Scraper Options
    extract_emails: bool = True
    extract_phones: bool = True
    extract_names: bool = True
    extract_companies: bool = True
    extract_social_links: bool = True
    
    # Site Cloner Options
    extract_html: bool = True
    extract_css: bool = True
    extract_images: bool = True
    extract_links: bool = True
    
    # Video Grabber Options
    extract_youtube: bool = True
    extract_vimeo: bool = True
    extract_all_videos: bool = True

class ScrapeRequest(BaseModel):
    url: str
    options: ScrapeOptions = ScrapeOptions()
    session_id: Optional[str] = None

class SessionRequest(BaseModel):
    name: Optional[str] = None

class CompleteSessionRequest(BaseModel):
    session_id: str
    webhook_url: Optional[str] = None

# ========== EXTRACTION FUNCTIONS ==========

def extract_emails(text: str, html: str) -> List[str]:
    """Extract email addresses"""
    email_pattern = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
    emails = set(re.findall(email_pattern, text))
    emails.update(re.findall(email_pattern, html))
    # Filter out common false positives
    filtered = [e for e in emails if not any(x in e.lower() for x in ['example.com', 'test.com', 'email.com', '.png', '.jpg', '.gif'])]
    return list(filtered)[:50]  # Limit to 50

def extract_phones(text: str) -> List[str]:
    """Extract phone numbers"""
    patterns = [
        r'\+?1?\s*\(?[0-9]{3}\)?[-.\s]?[0-9]{3}[-.\s]?[0-9]{4}',
        r'\+?[0-9]{1,3}[-.\s]?[0-9]{3,4}[-.\s]?[0-9]{3,4}[-.\s]?[0-9]{3,4}',
        r'\([0-9]{3}\)\s*[0-9]{3}[-.\s]?[0-9]{4}',
    ]
    phones = set()
    for pattern in patterns:
        matches = re.findall(pattern, text)
        phones.update(matches)
    return list(phones)[:30]

def extract_names(soup: BeautifulSoup) -> List[Dict[str, str]]:
    """Extract potential names from common patterns"""
    names = []
    
    # Look for common name containers
    name_selectors = [
        'h1', 'h2', 'h3',
        '[class*="name"]', '[class*="author"]', '[class*="contact"]',
        '[class*="team"]', '[class*="staff"]', '[class*="person"]',
        '[itemprop="name"]', '[data-name]'
    ]
    
    for selector in name_selectors:
        elements = soup.select(selector)
        for el in elements[:20]:
            text = el.get_text(strip=True)
            # Filter to likely names (2-4 words, reasonable length)
            if text and 3 < len(text) < 50 and 1 <= text.count(' ') <= 3:
                if not any(char.isdigit() for char in text):
                    names.append({'name': text, 'source': selector})
    
    return names[:20]

def extract_companies(soup: BeautifulSoup, text: str) -> List[str]:
    """Extract company names"""
    companies = set()
    
    # Look for common company indicators
    company_selectors = [
        '[class*="company"]', '[class*="business"]', '[class*="organization"]',
        '[itemprop="organization"]', '[class*="brand"]'
    ]
    
    for selector in company_selectors:
        elements = soup.select(selector)
        for el in elements[:10]:
            name = el.get_text(strip=True)
            if name and 2 < len(name) < 100:
                companies.add(name)
    
    # Look for LLC, Inc, Corp, etc.
    corp_pattern = r'([A-Z][A-Za-z\s&]+(?:LLC|Inc|Corp|Ltd|Company|Co\.|Limited)\.?)'
    matches = re.findall(corp_pattern, text)
    companies.update(matches[:10])
    
    return list(companies)[:15]

def extract_social_links(soup: BeautifulSoup, base_url: str) -> List[Dict[str, str]]:
    """Extract social media links"""
    social_platforms = {
        'facebook.com': 'Facebook',
        'twitter.com': 'Twitter',
        'x.com': 'Twitter/X',
        'linkedin.com': 'LinkedIn',
        'instagram.com': 'Instagram',
        'youtube.com': 'YouTube',
        'tiktok.com': 'TikTok',
        'pinterest.com': 'Pinterest',
        'github.com': 'GitHub',
    }
    
    social_links = []
    for a_tag in soup.find_all('a', href=True):
        href = a_tag['href']
        for domain, platform in social_platforms.items():
            if domain in href:
                social_links.append({
                    'platform': platform,
                    'url': href
                })
                break
    
    # Remove duplicates
    seen = set()
    unique_links = []
    for link in social_links:
        if link['url'] not in seen:
            seen.add(link['url'])
            unique_links.append(link)
    
    return unique_links[:20]

def extract_videos(soup: BeautifulSoup, html: str, options: ScrapeOptions) -> List[Dict[str, Any]]:
    """Extract video URLs"""
    videos = []
    
    # YouTube
    if options.extract_youtube:
        yt_patterns = [
            r'(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/)([a-zA-Z0-9_-]{11})',
        ]
        for pattern in yt_patterns:
            matches = re.findall(pattern, html)
            for video_id in set(matches):
                videos.append({
                    'platform': 'YouTube',
                    'video_id': video_id,
                    'url': f'https://www.youtube.com/watch?v={video_id}',
                    'embed_url': f'https://www.youtube.com/embed/{video_id}'
                })
    
    # Vimeo
    if options.extract_vimeo:
        vimeo_patterns = [
            r'vimeo\.com/(\d+)',
            r'player\.vimeo\.com/video/(\d+)'
        ]
        for pattern in vimeo_patterns:
            matches = re.findall(pattern, html)
            for video_id in set(matches):
                videos.append({
                    'platform': 'Vimeo',
                    'video_id': video_id,
                    'url': f'https://vimeo.com/{video_id}',
                    'embed_url': f'https://player.vimeo.com/video/{video_id}'
                })
    
    # Generic video tags
    if options.extract_all_videos:
        for video_tag in soup.find_all('video'):
            src = video_tag.get('src')
            if src:
                videos.append({
                    'platform': 'Direct',
                    'url': src,
                    'type': 'video'
                })
            for source in video_tag.find_all('source'):
                src = source.get('src')
                if src:
                    videos.append({
                        'platform': 'Direct',
                        'url': src,
                        'type': source.get('type', 'video')
                    })
        
        # Look for video in iframes
        for iframe in soup.find_all('iframe'):
            src = iframe.get('src', '')
            if any(x in src for x in ['video', 'embed', 'player']):
                videos.append({
                    'platform': 'Embedded',
                    'url': src,
                    'type': 'iframe'
                })
    
    return videos[:30]

def extract_images(soup: BeautifulSoup, base_url: str) -> List[Dict[str, str]]:
    """Extract all images"""
    images = []
    for img in soup.find_all('img'):
        src = img.get('src', '') or img.get('data-src', '')
        if src:
            absolute_url = urljoin(base_url, src)
            images.append({
                'url': absolute_url,
                'alt': img.get('alt', ''),
                'width': img.get('width', ''),
                'height': img.get('height', '')
            })
    
    # Also get background images from style attributes
    for el in soup.find_all(style=True):
        style = el.get('style', '')
        urls = re.findall(r'url\(["\']?([^"\')\s]+)["\']?\)', style)
        for url in urls:
            absolute_url = urljoin(base_url, url)
            images.append({
                'url': absolute_url,
                'alt': 'background-image',
                'type': 'background'
            })
    
    return images[:100]

def extract_css(soup: BeautifulSoup, base_url: str) -> Dict[str, Any]:
    """Extract CSS"""
    css_data = {
        'inline_styles': [],
        'external_stylesheets': [],
        'total_css': ''
    }
    
    # Inline styles
    for style in soup.find_all('style'):
        if style.string:
            css_data['inline_styles'].append(style.string)
    
    # External stylesheets
    for link in soup.find_all('link', rel='stylesheet'):
        href = link.get('href')
        if href:
            absolute_url = urljoin(base_url, href)
            css_data['external_stylesheets'].append(absolute_url)
    
    css_data['total_css'] = '\n\n'.join(css_data['inline_styles'])
    
    return css_data

def extract_links(soup: BeautifulSoup, base_url: str) -> List[Dict[str, str]]:
    """Extract all links"""
    links = []
    for a_tag in soup.find_all('a', href=True):
        href = a_tag['href']
        if href and not href.startswith('#') and not href.startswith('javascript:'):
            absolute_url = urljoin(base_url, href)
            links.append({
                'url': absolute_url,
                'text': a_tag.get_text(strip=True)[:100],
                'is_external': urlparse(absolute_url).netloc != urlparse(base_url).netloc
            })
    return links[:200]

# ========== BROWSERLESS SCRAPING ==========

async def scrape_with_browserless(url: str) -> Dict[str, Any]:
    """Scrape a URL using Browserless.io (handles JavaScript)"""
    
    # Use Browserless content API
    browserless_endpoint = f"{BROWSERLESS_URL}/content?token={BROWSERLESS_API_KEY}"
    
    payload = {
        "url": url,
        "waitFor": 3000,  # Wait for JS to load
        "gotoOptions": {
            "waitUntil": "networkidle2"
        }
    }
    
    try:
        async with httpx.AsyncClient(timeout=60.0) as http_client:
            response = await http_client.post(
                browserless_endpoint,
                json=payload,
                headers={'Content-Type': 'application/json'}
            )
            
            if response.status_code == 200:
                return {
                    'success': True,
                    'html': response.text,
                    'status_code': 200
                }
            else:
                # Fallback to simple HTTP request
                logging.warning(f"Browserless failed ({response.status_code}), falling back to simple request")
                return await simple_scrape(url)
                
    except Exception as e:
        logging.error(f"Browserless error: {str(e)}")
        return await simple_scrape(url)

async def simple_scrape(url: str) -> Dict[str, Any]:
    """Simple HTTP scrape fallback"""
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as http_client:
            response = await http_client.get(url, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            })
            return {
                'success': True,
                'html': response.text,
                'status_code': response.status_code
            }
    except Exception as e:
        return {
            'success': False,
            'error': str(e)
        }

# ========== MAIN SCRAPE ENDPOINT ==========

@api_router.post("/scrape")
async def scrape_url(request: ScrapeRequest):
    """Main scraping endpoint with all features"""
    
    url = request.url
    options = request.options
    
    logging.info(f"Scraping {url} with options: {options.dict()}")
    
    # Scrape the page (try Browserless first for JS-heavy sites)
    scrape_result = await scrape_with_browserless(url)
    
    if not scrape_result.get('success'):
        raise HTTPException(status_code=500, detail=f"Failed to scrape: {scrape_result.get('error')}")
    
    html = scrape_result['html']
    soup = BeautifulSoup(html, 'html.parser')
    text = soup.get_text(separator=' ', strip=True)
    
    # Build response based on options
    result = {
        'url': url,
        'title': soup.title.string if soup.title else url,
        'scraped_at': datetime.utcnow().isoformat(),
    }
    
    # Lead Scraper
    leads = {}
    if options.extract_emails:
        leads['emails'] = extract_emails(text, html)
    if options.extract_phones:
        leads['phones'] = extract_phones(text)
    if options.extract_names:
        leads['names'] = extract_names(soup)
    if options.extract_companies:
        leads['companies'] = extract_companies(soup, text)
    if options.extract_social_links:
        leads['social_links'] = extract_social_links(soup, url)
    
    if leads:
        result['leads'] = leads
    
    # Site Cloner
    site_data = {}
    if options.extract_html:
        site_data['html'] = html
        site_data['text'] = text[:10000]  # First 10k chars of text
    if options.extract_css:
        site_data['css'] = extract_css(soup, url)
    if options.extract_images:
        site_data['images'] = extract_images(soup, url)
    if options.extract_links:
        site_data['links'] = extract_links(soup, url)
    
    if site_data:
        result['site'] = site_data
    
    # Video Grabber
    if options.extract_youtube or options.extract_vimeo or options.extract_all_videos:
        result['videos'] = extract_videos(soup, html, options)
    
    # Save to database
    scrape_id = str(uuid.uuid4())
    await db.scrapes.insert_one({
        'scrape_id': scrape_id,
        'url': url,
        'options': options.dict(),
        'result': result,
        'created_at': datetime.utcnow()
    })
    
    result['scrape_id'] = scrape_id
    
    return result

# ========== SESSION ENDPOINTS ==========

@api_router.post("/session/start")
async def start_session(request: SessionRequest):
    """Start a new scraping session"""
    session_id = str(uuid.uuid4())
    session = {
        'session_id': session_id,
        'name': request.name or f"Session {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}",
        'pages': [],
        'total_pages': 0,
        'created_at': datetime.utcnow(),
        'status': 'active'
    }
    
    await db.sessions.insert_one(session)
    
    return {
        'success': True,
        'session_id': session_id,
        'name': session['name']
    }

@api_router.post("/session/scrape")
async def scrape_to_session(request: ScrapeRequest):
    """Scrape a page and add to session"""
    
    if not request.session_id:
        raise HTTPException(status_code=400, detail="session_id is required")
    
    # Check session exists
    session = await db.sessions.find_one({'session_id': request.session_id})
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    
    # Scrape the page
    scrape_result = await scrape_url(request)
    
    # Add to session
    page_data = {
        'page_id': scrape_result.get('scrape_id'),
        'url': request.url,
        'title': scrape_result.get('title'),
        'data': scrape_result,
        'scraped_at': datetime.utcnow()
    }
    
    await db.sessions.update_one(
        {'session_id': request.session_id},
        {
            '$push': {'pages': page_data},
            '$inc': {'total_pages': 1},
            '$set': {'updated_at': datetime.utcnow()}
        }
    )
    
    return {
        'success': True,
        'session_id': request.session_id,
        'page': page_data,
        'scrape_result': scrape_result
    }

@api_router.get("/session/{session_id}")
async def get_session(session_id: str):
    """Get session data"""
    session = await db.sessions.find_one({'session_id': session_id})
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    session.pop('_id', None)
    return session

@api_router.delete("/session/{session_id}/page/{page_id}")
async def delete_page(session_id: str, page_id: str):
    """Remove a page from session"""
    result = await db.sessions.update_one(
        {'session_id': session_id},
        {
            '$pull': {'pages': {'page_id': page_id}},
            '$inc': {'total_pages': -1}
        }
    )
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Page not found")
    return {'success': True}

@api_router.post("/session/complete")
async def complete_session(request: CompleteSessionRequest):
    """Complete session and optionally send to webhook"""
    
    session = await db.sessions.find_one({'session_id': request.session_id})
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    
    session.pop('_id', None)
    
    # Update status
    await db.sessions.update_one(
        {'session_id': request.session_id},
        {'$set': {'status': 'completed', 'completed_at': datetime.utcnow()}}
    )
    
    # Send to webhook if provided
    webhook_result = None
    if request.webhook_url:
        try:
            async with httpx.AsyncClient(timeout=30.0) as http_client:
                response = await http_client.post(
                    request.webhook_url,
                    json=session,
                    headers={'Content-Type': 'application/json'}
                )
                webhook_result = {
                    'sent': True,
                    'status_code': response.status_code
                }
        except Exception as e:
            webhook_result = {'sent': False, 'error': str(e)}
    
    return {
        'success': True,
        'session': session,
        'webhook': webhook_result
    }

# ========== BROWSERLESS EMBED ==========

@api_router.get("/browser/embed")
async def get_browser_embed():
    """Get Browserless embed URL for live browser"""
    
    # Browserless live view URL
    embed_url = f"https://chrome.browserless.io/?token={BROWSERLESS_API_KEY}"
    
    return {
        'embed_url': embed_url,
        'api_key': BROWSERLESS_API_KEY[:10] + '...'  # Partial for security
    }

# ========== PROXY ENDPOINT ==========

@api_router.get("/proxy")
async def proxy_website(url: str):
    """Proxy a website for iframe display"""
    if not url:
        raise HTTPException(status_code=400, detail="URL required")
    
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as http_client:
            response = await http_client.get(url, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            })
        
        content_type = response.headers.get('content-type', 'text/html')
        content = response.content
        
        if 'text/html' in content_type:
            soup = BeautifulSoup(response.text, 'html.parser')
            base_tag = soup.new_tag('base', href=url)
            if soup.head:
                soup.head.insert(0, base_tag)
            content = str(soup).encode('utf-8')
        
        return Response(
            content=content,
            status_code=response.status_code,
            media_type=content_type,
            headers={
                'Access-Control-Allow-Origin': '*',
                'Access-Control-Allow-Methods': '*',
                'Access-Control-Allow-Headers': '*',
            }
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ========== ROOT ==========

@api_router.get("/")
async def root():
    return {
        "message": "Marketing Scraper API",
        "version": "3.0.0",
        "features": [
            "Lead Scraper (emails, phones, names, companies, social)",
            "Site Cloner (HTML, CSS, images, links)",
            "Video Grabber (YouTube, Vimeo, all videos)",
            "Session management",
            "Browserless.io integration"
        ]
    }

@app.get("/health")
async def health():
    return {"status": "healthy"}

app.include_router(api_router)

logging.basicConfig(level=logging.INFO)

@app.on_event("startup")
async def startup():
    logging.info("Starting Marketing Scraper API v3.0.0")

@app.on_event("shutdown") 
async def shutdown():
    client.close()
