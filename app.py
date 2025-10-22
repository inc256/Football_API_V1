from flask import Flask, jsonify, request
from flask_cors import CORS
import time
import re
import os
from datetime import datetime, timedelta
import functools
import logging
import requests
from urllib.parse import urljoin, urlparse
import json

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Try importing selenium, if fails show clear error
try:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import TimeoutException, WebDriverException
    from webdriver_manager.chrome import ChromeDriverManager
    from webdriver_manager.core.os_manager import ChromeType
    SELENIUM_AVAILABLE = True
except ImportError as e:
    logger.error("=" * 60)
    logger.error("ERROR: Missing dependencies!")
    logger.error("Please run: pip install flask flask-cors selenium webdriver-manager requests")
    logger.error("=" * 60)
    SELENIUM_AVAILABLE = False

app = Flask(__name__)
CORS(app)

# Configuration
class Config:
    DEBUG = os.getenv('DEBUG', 'False').lower() == 'true'
    HOST = os.getenv('HOST', '0.0.0.0')
    PORT = int(os.getenv('PORT', 5000))
    CACHE_TIMEOUT = int(os.getenv('CACHE_TIMEOUT', 300))
    RATE_LIMIT = int(os.getenv('RATE_LIMIT', 1))
    REQUEST_TIMEOUT = int(os.getenv('REQUEST_TIMEOUT', 30))
    ENABLE_SELENIUM = os.getenv('ENABLE_SELENIUM', 'True').lower() == 'true'

app.config.from_object(Config)

class RateLimiter:
    def __init__(self, calls_per_second=1):
        self.calls_per_second = calls_per_second
        self.last_call = 0
    
    def wait(self):
        now = time.time()
        elapsed = now - self.last_call
        wait_time = 1.0 / self.calls_per_second - elapsed
        if wait_time > 0:
            time.sleep(wait_time)
        self.last_call = time.time()

class CamelLiveScraper:
    def __init__(self):
        if not SELENIUM_AVAILABLE:
            raise Exception("Selenium not installed. Run: pip install selenium webdriver-manager")
        
        logger.info("Initializing Chrome options...")
        self.chrome_options = Options()
        
        # Enhanced Chrome options
        self.chrome_options.add_argument('--headless=new')
        self.chrome_options.add_argument('--no-sandbox')
        self.chrome_options.add_argument('--disable-dev-shm-usage')
        self.chrome_options.add_argument('--disable-blink-features=AutomationControlled')
        self.chrome_options.add_argument('--disable-gpu')
        self.chrome_options.add_argument('--window-size=1920,1080')
        self.chrome_options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
        
        self.chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
        self.chrome_options.add_experimental_option('useAutomationExtension', False)
        
        self.rate_limiter = RateLimiter(calls_per_second=app.config['RATE_LIMIT'])
        self.cache = {}
        self.cache_timeout = timedelta(seconds=app.config['CACHE_TIMEOUT'])
        
        logger.info("Setting up ChromeDriver...")
        try:
            self.service = Service(ChromeDriverManager().install())
            logger.info("✓ ChromeDriver ready!")
        except Exception as e:
            logger.warning(f"Error with ChromeDriverManager: {e}")
            try:
                self.service = Service()
                logger.info("✓ Using system ChromeDriver")
            except Exception as e2:
                logger.error(f"All ChromeDriver setup failed: {e2}")
                raise
    
    def get_driver(self):
        """Create a new Chrome driver instance"""
        try:
            driver = webdriver.Chrome(service=self.service, options=self.chrome_options)
            driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            return driver
        except Exception as e:
            logger.error(f"Error creating Chrome driver: {e}")
            raise
    
    def get_cached_or_scrape(self, key, scraper_func, *args):
        """Get from cache or scrape fresh data"""
        now = datetime.now()
        if key in self.cache:
            data, timestamp = self.cache[key]
            if now - timestamp < self.cache_timeout:
                logger.info(f"Using cached data for: {key}")
                return data
        
        data = scraper_func(*args)
        self.cache[key] = (data, now)
        return data
    
    def scrape_home_matches(self):
        """Scrape all matches from homepage"""
        driver = None
        matches = []
        
        try:
            logger.info("Starting to scrape homepage...")
            
            driver = self.get_driver()
            logger.info("✓ Chrome driver created")
            
            logger.info("Loading https://www.camel1.live/home ...")
            driver.get("https://www.camel1.live/home")
            
            wait = WebDriverWait(driver, 15)
            wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
            
            logger.info(f"Page title: {driver.title}")
            
            match_links = self._find_match_links(driver)
            logger.info(f"✓ Found {len(match_links)} match links")
            
            # Limit matches for performance
            max_matches = 3
            match_links = match_links[:max_matches]
            
            for i, link in enumerate(match_links, 1):
                logger.info(f"[{i}/{len(match_links)}] Processing: {link}")
                try:
                    match_info = self.scrape_match_page(link)
                    if match_info:
                        matches.append(match_info)
                        logger.info(f"  ✓ Match data extracted")
                except Exception as e:
                    logger.error(f"  ✗ Error: {e}")
            
            logger.info(f"Scraping complete! Found {len(matches)} matches with data")
            
        except Exception as e:
            logger.error(f"Error in scrape_home_matches: {e}")
            matches = self._fallback_home_matches()
        finally:
            if driver:
                driver.quit()
                logger.info("✓ Browser closed")
        
        return matches
    
    def _fallback_home_matches(self):
        """Fallback method using requests"""
        logger.info("Using fallback scraping method...")
        try:
            response = requests.get('https://www.camel1.live/home', timeout=10)
            matches = []
            
            match_links = re.findall(r'href=["\'](/game/match-[^"\']+)["\']', response.text)
            for link in match_links[:3]:
                full_url = f"https://www.camel1.live{link}"
                match_data = {
                    'match_url': full_url,
                    'match_name': self._extract_teams_from_url(full_url),
                    'stream_url': None,
                    'fallback': True
                }
                matches.append(match_data)
            
            return matches
        except Exception as e:
            logger.error(f"Fallback scraping failed: {e}")
            return []
    
    def _extract_teams_from_url(self, url):
        """Extract team names from URL"""
        try:
            match = re.search(r'/match-([^/]+)', url)
            if match:
                teams = match.group(1).replace('-', ' ').title()
                return teams
        except:
            pass
        return "Unknown Match"
    
    def _find_match_links(self, driver):
        """Find all match links"""
        match_links = set()
        
        all_links = driver.find_elements(By.TAG_NAME, 'a')
        logger.info(f"Found {len(all_links)} total links on page")
        
        for link in all_links:
            try:
                href = link.get_attribute('href')
                if href and ('/game/' in href or '/match-' in href):
                    if href.startswith('/'):
                        href = f"https://www.camel1.live{href}"
                    match_links.add(href)
            except:
                continue
        
        return list(match_links)
    
    def scrape_match_page(self, url):
        """Scrape detailed match info and stream link"""
        driver = None
        match_data = {'match_url': url}
        
        try:
            self.rate_limiter.wait()
            driver = self.get_driver()
            
            logger.info(f"Loading match page: {url}")
            driver.get(url)
            
            wait = WebDriverWait(driver, 15)
            wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
            
            self._extract_match_info_from_url(url, match_data)
            
            page_source = driver.page_source
            stream_url = self.extract_stream_url_enhanced(driver, page_source, url)
            match_data['stream_url'] = stream_url
            
            self._extract_match_details(driver, match_data)
            
            if stream_url:
                logger.info(f"✓ Stream found: {stream_url}")
            else:
                logger.info(f"✗ No stream found")
            
        except Exception as e:
            logger.error(f"Error in scrape_match_page: {e}")
            match_data['error'] = str(e)
        finally:
            if driver:
                driver.quit()
        
        return match_data
    
    def _extract_match_info_from_url(self, url, match_data):
        """Extract match information from URL"""
        try:
            match = re.search(r'/match-([^/]+)', url)
            if match:
                teams = match.group(1).replace('-', ' ').title()
                match_data['match_name'] = teams
                
                separators = [' Vs ', ' vs ', ' - ', ' – ']
                for sep in separators:
                    if sep in teams:
                        parts = teams.split(sep)
                        if len(parts) == 2:
                            match_data['home_team'] = parts[0].strip()
                            match_data['away_team'] = parts[1].strip()
                            break
                else:
                    words = teams.split()
                    if len(words) >= 2:
                        mid = len(words) // 2
                        match_data['home_team'] = ' '.join(words[:mid])
                        match_data['away_team'] = ' '.join(words[mid:])
        except Exception as e:
            logger.warning(f"Could not extract match info from URL: {e}")
    
    def _extract_match_details(self, driver, match_data):
        """Extract additional match details"""
        try:
            # Status extraction
            status_selectors = ['.status', '[class*="status"]', '[class*="live"]']
            for selector in status_selectors:
                try:
                    elements = driver.find_elements(By.CSS_SELECTOR, selector)
                    for elem in elements:
                        text = elem.text.strip()
                        if text and len(text) < 50:
                            match_data['status'] = text
                            break
                    if 'status' in match_data:
                        break
                except:
                    continue
            
            # Score extraction
            score_selectors = ['.score', '[class*="score"]']
            for selector in score_selectors:
                try:
                    scores = driver.find_elements(By.CSS_SELECTOR, selector)
                    if len(scores) >= 2:
                        match_data['home_score'] = scores[0].text.strip()
                        match_data['away_score'] = scores[1].text.strip()
                        break
                except:
                    continue
                    
        except Exception as e:
            logger.warning(f"Could not extract all match details: {e}")
    
    def extract_stream_url_enhanced(self, driver, page_source, page_url):
        """Enhanced stream URL extraction"""
        stream_sources = []
        
        # Check video elements
        video_src = self._check_video_elements(driver)
        if video_src:
            stream_sources.append(('video_element', video_src))
        
        # Check iframes
        iframe_src = self._check_iframes(driver)
        if iframe_src:
            stream_sources.append(('iframe', iframe_src))
        
        # Check for m3u8
        m3u8_sources = self._check_m3u8_sources(page_source, page_url)
        stream_sources.extend(m3u8_sources)
        
        # Select best stream
        best_stream = self._select_best_stream(stream_sources)
        
        logger.info(f"Found {len(stream_sources)} potential stream sources")
        return best_stream
    
    def _check_video_elements(self, driver):
        """Check video elements for stream sources"""
        try:
            videos = driver.find_elements(By.TAG_NAME, 'video')
            for video in videos:
                src = video.get_attribute('src')
                if src and self._is_stream_url(src):
                    return src
                
                sources = video.find_elements(By.TAG_NAME, 'source')
                for source in sources:
                    src = source.get_attribute('src')
                    if src and self._is_stream_url(src):
                        return src
        except:
            pass
        return None
    
    def _check_iframes(self, driver):
        """Check iframes for embedded streams"""
        try:
            iframes = driver.find_elements(By.TAG_NAME, 'iframe')
            for iframe in iframes:
                src = iframe.get_attribute('src')
                if src and self._is_stream_url(src):
                    return src
        except:
            pass
        return None
    
    def _check_m3u8_sources(self, page_source, base_url):
        """Check page source for m3u8 URLs"""
        sources = []
        
        patterns = [
            r'["\'](https?://[^"\'<>]+\.m3u8[^"\'<>]*)["\']',
            r'["\'](/[^"\'<>]+\.m3u8[^"\'<>]*)["\']',
        ]
        
        for pattern in patterns:
            matches = re.findall(pattern, page_source, re.IGNORECASE)
            for match in matches:
                if match.startswith('/'):
                    match = urljoin(base_url, match)
                if self._is_stream_url(match):
                    sources.append(('m3u8_pattern', match))
        
        return sources
    
    def _is_stream_url(self, url):
        """Check if URL looks like a stream"""
        if not url or len(url) < 10:
            return False
        
        stream_indicators = ['.m3u8', '.mp4', 'stream', 'live', 'hls', 'video']
        url_lower = url.lower()
        
        return any(indicator in url_lower for indicator in stream_indicators)
    
    def _select_best_stream(self, stream_sources):
        """Select the best stream URL"""
        if not stream_sources:
            return None
        
        priority = {
            'm3u8_pattern': 1,
            'video_element': 2,
            'iframe': 3,
        }
        
        sorted_sources = sorted(stream_sources, key=lambda x: priority.get(x[0], 999))
        return sorted_sources[0][1] if sorted_sources else None

# Initialize scraper
scraper = None

def get_scraper():
    """Get or initialize scraper instance"""
    global scraper
    if scraper is None and SELENIUM_AVAILABLE and app.config['ENABLE_SELENIUM']:
        try:
            scraper = CamelLiveScraper()
        except Exception as e:
            logger.error(f"Failed to initialize scraper: {e}")
            return None
    return scraper

@app.route('/')
def home():
    return jsonify({
        'status': 'online',
        'message': 'Camel Live Scraper API',
        'timestamp': datetime.now().isoformat(),
        'selenium_available': SELENIUM_AVAILABLE,
        'endpoints': {
            '/api/matches': 'GET - Get all matches from homepage',
            '/api/match': 'GET - Get specific match details',
            '/api/stream': 'GET - Get stream URL only',
            '/api/health': 'GET - Health check'
        }
    })

@app.route('/api/health')
def health_check():
    """Health check endpoint"""
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'selenium_available': SELENIUM_AVAILABLE,
        'selenium_enabled': app.config['ENABLE_SELENIUM']
    })

@app.route('/api/matches', methods=['GET'])
def get_matches():
    """Get all matches from homepage"""
    try:
        if not SELENIUM_AVAILABLE or not app.config['ENABLE_SELENIUM']:
            fallback_scraper = CamelLiveScraper()
            matches = fallback_scraper._fallback_home_matches()
            return jsonify({
                'success': True,
                'count': len(matches),
                'matches': matches,
                'fallback': True
            })
        
        scraper_instance = get_scraper()
        
        if not scraper_instance:
            return jsonify({
                'success': False,
                'error': 'Scraper not available'
            }), 500
        
        matches = scraper_instance.scrape_home_matches()
        
        return jsonify({
            'success': True,
            'count': len(matches),
            'matches': matches,
            'fallback': False
        })
    except Exception as e:
        logger.error(f"Error in /api/matches: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/match', methods=['GET'])
def get_match():
    """Get specific match details"""
    url = request.args.get('url')
    
    if not url:
        return jsonify({
            'success': False,
            'error': 'URL parameter is required'
        }), 400
    
    try:
        scraper_instance = get_scraper()
        
        if not scraper_instance:
            return jsonify({
                'success': False,
                'error': 'Scraper not available'
            }), 500
        
        match_data = scraper_instance.scrape_match_page(url)
        
        return jsonify({
            'success': True,
            'match': match_data
        })
    except Exception as e:
        logger.error(f"Error in /api/match: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/stream', methods=['GET'])
def get_stream():
    """Get stream URL for a match"""
    url = request.args.get('url')
    
    if not url:
        return jsonify({
            'success': False,
            'error': 'URL parameter is required'
        }), 400
    
    try:
        scraper_instance = get_scraper()
        
        if not scraper_instance:
            return jsonify({
                'success': False,
                'error': 'Scraper not available'
            }), 500
        
        match_data = scraper_instance.scrape_match_page(url)
        
        return jsonify({
            'success': True,
            'stream_url': match_data.get('stream_url'),
            'match_url': url
        })
    except Exception as e:
        logger.error(f"Error in /api/stream: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

# Vercel handler
def handler(request, context):
    return app(request, context)

if __name__ == '__main__':
    print("Starting Flask server...")
    app.run(debug=app.config['DEBUG'], host=app.config['HOST'], port=app.config['PORT'])
