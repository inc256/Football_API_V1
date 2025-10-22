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
    print("=" * 60)
    print("ERROR: Missing dependencies!")
    print("Please run: pip install flask flask-cors selenium webdriver-manager requests")
    print("=" * 60)
    SELENIUM_AVAILABLE = False

app = Flask(__name__)
CORS(app)

# Configuration
class Config:
    DEBUG = os.getenv('DEBUG', 'False').lower() == 'true'
    HOST = os.getenv('HOST', '0.0.0.0')
    PORT = int(os.getenv('PORT', 5000))
    CACHE_TIMEOUT = int(os.getenv('CACHE_TIMEOUT', 300))  # 5 minutes
    RATE_LIMIT = int(os.getenv('RATE_LIMIT', 2))  # requests per second
    REQUEST_TIMEOUT = int(os.getenv('REQUEST_TIMEOUT', 30))  # seconds

app.config.from_object(Config)

class RateLimiter:
    def __init__(self, calls_per_second=2):
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
        
        # Enhanced Chrome options for better compatibility
        self.chrome_options.add_argument('--headless=new')
        self.chrome_options.add_argument('--no-sandbox')
        self.chrome_options.add_argument('--disable-dev-shm-usage')
        self.chrome_options.add_argument('--disable-blink-features=AutomationControlled')
        self.chrome_options.add_argument('--disable-gpu')
        self.chrome_options.add_argument('--window-size=1920,1080')
        self.chrome_options.add_argument('--disable-extensions')
        self.chrome_options.add_argument('--disable-plugins')
        self.chrome_options.add_argument('--disable-images')
        self.chrome_options.add_argument('user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
        
        self.chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
        self.chrome_options.add_experimental_option('useAutomationExtension', False)
        
        self.rate_limiter = RateLimiter(calls_per_second=app.config['RATE_LIMIT'])
        self.cache = {}
        self.cache_timeout = timedelta(seconds=app.config['CACHE_TIMEOUT'])
        
        logger.info("Setting up ChromeDriver...")
        try:
            # Try multiple ways to setup ChromeDriver for Vercel compatibility
            self.service = Service(ChromeDriverManager().install())
            logger.info("✓ ChromeDriver ready!")
        except Exception as e:
            logger.warning(f"Error with ChromeDriverManager: {e}")
            try:
                # Fallback to system Chrome
                self.service = Service(ChromeDriverManager(chrome_type=ChromeType.CHROMIUM).install())
                logger.info("✓ ChromiumDriver ready!")
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
        
        # Scrape fresh data
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
            
            # Use WebDriverWait instead of static sleep
            wait = WebDriverWait(driver, 10)
            wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
            
            logger.info(f"Page title: {driver.title}")
            
            # Look for match links with multiple strategies
            match_links = self._find_match_links(driver)
            logger.info(f"✓ Found {len(match_links)} match links")
            
            # Process matches with progress
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
            import traceback
            traceback.print_exc()
        finally:
            if driver:
                driver.quit()
                logger.info("✓ Browser closed")
        
        return matches
    
    def _find_match_links(self, driver):
        """Find all match links using multiple strategies"""
        match_links = set()
        
        # Strategy 1: Find links containing '/game/'
        all_links = driver.find_elements(By.TAG_NAME, 'a')
        logger.info(f"Found {len(all_links)} total links on page")
        
        for link in all_links:
            try:
                href = link.get_attribute('href')
                if href and ('/game/' in href or '/match-' in href):
                    # Normalize URL
                    if href.startswith('/'):
                        href = f"https://www.camel1.live{href}"
                    match_links.add(href)
            except:
                continue
        
        # Strategy 2: Look for specific match elements
        selectors = [
            '[href*="/game/"]',
            '[href*="/match-"]',
            '.match-link',
            '.game-link',
            '[class*="match"] a',
            '[class*="game"] a'
        ]
        
        for selector in selectors:
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, selector)
                for elem in elements:
                    href = elem.get_attribute('href')
                    if href:
                        if href.startswith('/'):
                            href = f"https://www.camel1.live{href}"
                        match_links.add(href)
            except:
                continue
        
        return list(match_links)
    
    def scrape_match_page(self, url):
        """Scrape detailed match info and stream link from match page"""
        driver = None
        match_data = {'match_url': url}
        
        try:
            self.rate_limiter.wait()
            driver = self.get_driver()
            
            logger.info(f"Loading match page: {url}")
            driver.get(url)
            
            # Wait for page to load
            wait = WebDriverWait(driver, 10)
            wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
            
            # Extract basic match info from URL
            self._extract_match_info_from_url(url, match_data)
            
            # Get page source for analysis
            page_source = driver.page_source
            
            # Enhanced stream URL extraction
            stream_url = self.extract_stream_url_enhanced(driver, page_source, url)
            match_data['stream_url'] = stream_url
            
            # Extract additional match details
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
        """Extract match information from URL pattern"""
        try:
            # Extract from URL pattern like /game/match-team1-team2/video/abc123
            match = re.search(r'/match-([^/]+)', url)
            if match:
                teams = match.group(1).replace('-', ' ').title()
                match_data['match_name'] = teams
                
                # Enhanced team name parsing
                separators = [' Vs ', ' vs ', ' - ', ' – ']
                for sep in separators:
                    if sep in teams:
                        parts = teams.split(sep)
                        if len(parts) == 2:
                            match_data['home_team'] = parts[0].strip()
                            match_data['away_team'] = parts[1].strip()
                            break
                else:
                    # Fallback: split by space in middle
                    words = teams.split()
                    if len(words) >= 2:
                        mid = len(words) // 2
                        match_data['home_team'] = ' '.join(words[:mid])
                        match_data['away_team'] = ' '.join(words[mid:])
        except Exception as e:
            logger.warning(f"Could not extract match info from URL: {e}")
    
    def _extract_match_details(self, driver, match_data):
        """Extract additional match details from page"""
        try:
            # Status extraction
            status_selectors = [
                '.status', '[class*="status"]', '[class*="live"]', 
                '.live-indicator', '.match-status', '.time'
            ]
            for selector in status_selectors:
                try:
                    elements = driver.find_elements(By.CSS_SELECTOR, selector)
                    for elem in elements:
                        text = elem.text.strip()
                        if text and len(text) < 50:  # Reasonable status length
                            match_data['status'] = text
                            break
                    if 'status' in match_data:
                        break
                except:
                    continue
            
            # Score extraction
            score_selectors = [
                '.score', '[class*="score"]', '.goals', '.result',
                '.home-score', '.away-score'
            ]
            for selector in score_selectors:
                try:
                    scores = driver.find_elements(By.CSS_SELECTOR, selector)
                    if len(scores) >= 2:
                        match_data['home_score'] = scores[0].text.strip()
                        match_data['away_score'] = scores[1].text.strip()
                        break
                except:
                    continue
            
            # Logo extraction
            try:
                logos = driver.find_elements(By.CSS_SELECTOR, "img")
                logo_urls = []
                for img in logos:
                    try:
                        src = img.get_attribute('src')
                        if src and any(ext in src.lower() for ext in ['.png', '.jpg', '.jpeg', '.svg']):
                            logo_urls.append(src)
                    except:
                        continue
                
                if len(logo_urls) >= 2:
                    match_data['home_logo'] = logo_urls[0]
                    match_data['away_logo'] = logo_urls[1]
            except Exception as e:
                logger.warning(f"Could not extract logos: {e}")
                
        except Exception as e:
            logger.warning(f"Could not extract all match details: {e}")
    
    def extract_stream_url_enhanced(self, driver, page_source, page_url):
        """Enhanced stream URL extraction with multiple methods"""
        stream_sources = []
        
        # Method 1: Check video elements
        video_src = self._check_video_elements(driver)
        if video_src:
            stream_sources.append(('video_element', video_src))
        
        # Method 2: Check iframes
        iframe_src = self._check_iframes(driver)
        if iframe_src:
            stream_sources.append(('iframe', iframe_src))
        
        # Method 3: Check for m3u8 in page source
        m3u8_sources = self._check_m3u8_sources(page_source, page_url)
        stream_sources.extend(m3u8_sources)
        
        # Method 4: Check JavaScript variables
        js_sources = self._check_javascript_sources(driver)
        stream_sources.extend(js_sources)
        
        # Method 5: Check data attributes
        data_sources = self._check_data_attributes(driver)
        stream_sources.extend(data_sources)
        
        # Select the best stream source
        best_stream = self._select_best_stream(stream_sources)
        
        logger.info(f"Found {len(stream_sources)} potential stream sources")
        for source_type, url in stream_sources:
            logger.info(f"  {source_type}: {url}")
        
        return best_stream
    
    def _check_video_elements(self, driver):
        """Check video elements for stream sources"""
        try:
            videos = driver.find_elements(By.TAG_NAME, 'video')
            for video in videos:
                # Check direct src
                src = video.get_attribute('src')
                if src and self._is_stream_url(src):
                    return src
                
                # Check source tags
                sources = video.find_elements(By.TAG_NAME, 'source')
                for source in sources:
                    src = source.get_attribute('src')
                    if src and self._is_stream_url(src):
                        return src
                
                # Check currentSrc via JavaScript
                try:
                    current_src = driver.execute_script("return arguments[0].currentSrc", video)
                    if current_src and self._is_stream_url(current_src):
                        return current_src
                except:
                    pass
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
                
                # Check for data-src or other attributes
                data_src = iframe.get_attribute('data-src')
                if data_src and self._is_stream_url(data_src):
                    return data_src
        except:
            pass
        return None
    
    def _check_m3u8_sources(self, page_source, base_url):
        """Check page source for m3u8 URLs"""
        sources = []
        
        # Pattern for m3u8 URLs
        patterns = [
            r'["\'](https?://[^"\'<>]+\.m3u8[^"\'<>]*)["\']',
            r'["\'](/[^"\'<>]+\.m3u8[^"\'<>]*)["\']',
            r'src["\']?\s*[:=]\s*["\']([^"\'<>]+\.m3u8[^"\'<>]*)["\']',
            r'url["\']?\s*[:=]\s*["\']([^"\'<>]+\.m3u8[^"\'<>]*)["\']',
        ]
        
        for pattern in patterns:
            matches = re.findall(pattern, page_source, re.IGNORECASE)
            for match in matches:
                if match.startswith('/'):
                    # Convert relative URL to absolute
                    match = urljoin(base_url, match)
                if self._is_stream_url(match):
                    sources.append(('m3u8_pattern', match))
        
        return sources
    
    def _check_javascript_sources(self, driver):
        """Check JavaScript variables for stream URLs"""
        sources = []
        js_scripts = [
            "return window.player && window.player.src",
            "return document.querySelector('video') && document.querySelector('video').src",
            "return Object.values(window).find(v => v && typeof v === 'string' && v.includes('.m3u8'))",
        ]
        
        for script in js_scripts:
            try:
                result = driver.execute_script(script)
                if result and self._is_stream_url(result):
                    sources.append(('javascript', result))
            except:
                pass
        
        return sources
    
    def _check_data_attributes(self, driver):
        """Check data attributes for stream URLs"""
        sources = []
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, "[data-src], [data-url], [data-stream]")
            for elem in elements:
                for attr in ['data-src', 'data-url', 'data-stream']:
                    value = elem.get_attribute(attr)
                    if value and self._is_stream_url(value):
                        sources.append(('data_attribute', value))
        except:
            pass
        return sources
    
    def _is_stream_url(self, url):
        """Check if URL looks like a stream"""
        if not url or len(url) < 10:
            return False
        
        stream_indicators = ['.m3u8', '.mp4', 'stream', 'live', 'hls', 'video', 'embed']
        url_lower = url.lower()
        
        return any(indicator in url_lower for indicator in stream_indicators)
    
    def _select_best_stream(self, stream_sources):
        """Select the best stream URL from available sources"""
        if not stream_sources:
            return None
        
        # Priority order: m3u8 > video elements > iframes > others
        priority = {
            'm3u8_pattern': 1,
            'video_element': 2,
            'iframe': 3,
            'javascript': 4,
            'data_attribute': 5
        }
        
        # Sort by priority
        sorted_sources = sorted(stream_sources, key=lambda x: priority.get(x[0], 999))
        
        # Return the highest priority stream
        return sorted_sources[0][1] if sorted_sources else None

# Initialize scraper
scraper = None

def get_scraper():
    """Get or initialize scraper instance"""
    global scraper
    if scraper is None and SELENIUM_AVAILABLE:
        scraper = CamelLiveScraper()
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
            '/api/match': 'GET - Get specific match details (requires ?url= parameter)',
            '/api/stream': 'GET - Get stream URL only (requires ?url= parameter)',
            '/api/test': 'GET - Test if scraper is working',
            '/api/health': 'GET - Comprehensive health check'
        }
    })

@app.route('/api/health')
def health_check():
    """Comprehensive health check"""
    global scraper
    
    health_info = {
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'selenium_available': SELENIUM_AVAILABLE,
        'scraper_initialized': scraper is not None,
    }
    
    # Test Chrome driver if available
    if SELENIUM_AVAILABLE:
        try:
            test_scraper = get_scraper()
            test_driver = test_scraper.get_driver()
            test_driver.quit()
            health_info['chrome_driver'] = 'working'
        except Exception as e:
            health_info['chrome_driver'] = 'error'
            health_info['error'] = str(e)
            health_info['status'] = 'unhealthy'
    
    status_code = 200 if health_info['status'] == 'healthy' else 503
    return jsonify(health_info), status_code

@app.route('/api/test')
def test_scraper():
    """Test endpoint to verify scraper works"""
    try:
        if not SELENIUM_AVAILABLE:
            return jsonify({
                'success': False,
                'error': 'Selenium not installed. Run: pip install selenium webdriver-manager'
            }), 500
        
        test_scraper = get_scraper()
        
        return jsonify({
            'success': True,
            'message': 'Scraper initialized successfully',
            'chrome_ready': True
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/matches', methods=['GET'])
def get_matches():
    """Get all matches from the homepage"""
    try:
        if not SELENIUM_AVAILABLE:
            return jsonify({
                'success': False,
                'error': 'Selenium not installed'
            }), 500
        
        scraper_instance = get_scraper()
        
        # Use cached version if available
        matches = scraper_instance.get_cached_or_scrape('home_matches', scraper_instance.scrape_home_matches)
        
        return jsonify({
            'success': True,
            'count': len(matches),
            'matches': matches,
            'cached': 'home_matches' in scraper_instance.cache
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
        if not SELENIUM_AVAILABLE:
            return jsonify({
                'success': False,
                'error': 'Selenium not installed'
            }), 500
        
        scraper_instance = get_scraper()
        
        # Use cache key based on URL
        cache_key = f'match_{hash(url)}'
        match_data = scraper_instance.get_cached_or_scrape(cache_key, scraper_instance.scrape_match_page, url)
        
        return jsonify({
            'success': True,
            'match': match_data,
            'cached': cache_key in scraper_instance.cache
        })
    except Exception as e:
        logger.error(f"Error in /api/match: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/stream', methods=['GET'])
def get_stream():
    """Get only the stream URL for a match"""
    url = request.args.get('url')
    
    if not url:
        return jsonify({
            'success': False,
            'error': 'URL parameter is required'
        }), 400
    
    try:
        if not SELENIUM_AVAILABLE:
            return jsonify({
                'success': False,
                'error': 'Selenium not installed'
            }), 500
        
        scraper_instance = get_scraper()
        
        # Use cache key based on URL
        cache_key = f'stream_{hash(url)}'
        match_data = scraper_instance.get_cached_or_scrape(cache_key, scraper_instance.scrape_match_page, url)
        
        return jsonify({
            'success': True,
            'stream_url': match_data.get('stream_url'),
            'match_url': url,
            'cached': cache_key in scraper_instance.cache
        })
    except Exception as e:
        logger.error(f"Error in /api/stream: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

# Vercel compatibility
@app.route('/api/vercel-test')
def vercel_test():
    """Test endpoint for Vercel deployment"""
    return jsonify({
        'success': True,
        'message': 'API is running on Vercel',
        'timestamp': datetime.now().isoformat()
    })

if __name__ == '__main__':
    print("\n" + "=" * 60)
    print("CAMEL LIVE SCRAPER API - ENHANCED VERSION")
    print("=" * 60)
    
    if not SELENIUM_AVAILABLE:
        print("\n❌ ERROR: Missing dependencies!")
        print("Please run: pip install flask flask-cors selenium webdriver-manager requests")
        print("\n" + "=" * 60)
        exit(1)
    
    print("\n✓ All dependencies installed")
    print("✓ Starting Flask server...")
    print("\nAPI will be available at:")
    print("  - http://localhost:5000")
    print("  - http://127.0.0.1:5000")
    print("\nPress CTRL+C to stop the server")
    print("=" * 60 + "\n")
    
    try:
        app.run(debug=app.config['DEBUG'], host=app.config['HOST'], port=app.config['PORT'], use_reloader=False)
    except KeyboardInterrupt:
        print("\n\n✓ Server stopped")
    except Exception as e:
        print(f"\n❌ Error starting server: {e}")
        import traceback
        traceback.print_exc()