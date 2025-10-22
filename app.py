from flask import Flask, jsonify, request
from flask_cors import CORS
import requests
import re
import os
from datetime import datetime, timedelta
import logging
from urllib.parse import urljoin
import json

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

# Configuration
class Config:
    DEBUG = os.getenv('DEBUG', 'False').lower() == 'true'
    CACHE_TIMEOUT = int(os.getenv('CACHE_TIMEOUT', 300))
    REQUEST_TIMEOUT = int(os.getenv('REQUEST_TIMEOUT', 10))

app.config.from_object(Config)

class CamelLiveScraper:
    def __init__(self):
        self.cache = {}
        self.cache_timeout = timedelta(seconds=app.config['CACHE_TIMEOUT'])
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        })
    
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
        """Scrape all matches from homepage using requests"""
        logger.info("Scraping homepage with requests...")
        matches = []
        
        try:
            response = self.session.get(
                'https://www.camel1.live/home', 
                timeout=app.config['REQUEST_TIMEOUT']
            )
            response.raise_for_status()
            
            # Extract match links from HTML
            match_links = self._extract_match_links(response.text)
            logger.info(f"Found {len(match_links)} match links")
            
            # Process matches (limit for Vercel)
            for i, link in enumerate(match_links[:5], 1):
                logger.info(f"[{i}/{len(match_links[:5])}] Processing: {link}")
                try:
                    match_info = self.scrape_match_page(link)
                    if match_info:
                        matches.append(match_info)
                        logger.info(f"  ✓ Match data extracted")
                except Exception as e:
                    logger.error(f"  ✗ Error: {e}")
                    # Add basic match info even if scraping fails
                    matches.append({
                        'match_url': link,
                        'match_name': self._extract_teams_from_url(link),
                        'stream_url': None,
                        'error': str(e)
                    })
            
            logger.info(f"Scraping complete! Found {len(matches)} matches")
            
        except Exception as e:
            logger.error(f"Error scraping homepage: {e}")
            # Return empty matches with error
            matches = [{
                'error': f'Failed to scrape homepage: {str(e)}',
                'match_url': 'https://www.camel1.live/home',
                'match_name': 'Homepage Scraping Failed'
            }]
        
        return matches
    
    def _extract_match_links(self, html):
        """Extract match links from homepage HTML"""
        links = set()
        
        # Pattern for match URLs
        patterns = [
            r'href=["\'](/game/match-[^"\']+)["\']',
            r'href=["\'](https://www\.camel1\.live/game/match-[^"\']+)["\']',
        ]
        
        for pattern in patterns:
            matches = re.findall(pattern, html)
            for match in matches:
                if match.startswith('/'):
                    full_url = f"https://www.camel1.live{match}"
                else:
                    full_url = match
                links.add(full_url)
        
        return list(links)
    
    def scrape_match_page(self, url):
        """Scrape match page using requests"""
        logger.info(f"Scraping match page: {url}")
        match_data = {'match_url': url}
        
        try:
            response = self.session.get(url, timeout=app.config['REQUEST_TIMEOUT'])
            response.raise_for_status()
            
            # Extract match info from URL
            self._extract_match_info_from_url(url, match_data)
            
            # Extract stream URL
            stream_url = self._extract_stream_url(response.text, url)
            match_data['stream_url'] = stream_url
            
            # Extract additional info from page
            self._extract_match_details(response.text, match_data)
            
            if stream_url:
                logger.info(f"✓ Stream found: {stream_url}")
            else:
                logger.info(f"✗ No stream found")
                
        except Exception as e:
            logger.error(f"Error scraping match page: {e}")
            match_data['error'] = str(e)
            # Ensure we have at least basic info
            if 'match_name' not in match_data:
                match_data['match_name'] = self._extract_teams_from_url(url)
        
        return match_data
    
    def _extract_match_info_from_url(self, url, match_data):
        """Extract match information from URL pattern"""
        try:
            match = re.search(r'/match-([^/]+)', url)
            if match:
                teams = match.group(1).replace('-', ' ').title()
                match_data['match_name'] = teams
                
                # Try to split teams
                separators = [' Vs ', ' vs ', ' - ', ' – ']
                for sep in separators:
                    if sep in teams:
                        parts = teams.split(sep)
                        if len(parts) == 2:
                            match_data['home_team'] = parts[0].strip()
                            match_data['away_team'] = parts[1].strip()
                            break
                else:
                    # Fallback split
                    words = teams.split()
                    if len(words) >= 2:
                        mid = len(words) // 2
                        match_data['home_team'] = ' '.join(words[:mid])
                        match_data['away_team'] = ' '.join(words[mid:])
        except Exception as e:
            logger.warning(f"Could not extract match info from URL: {e}")
            match_data['match_name'] = self._extract_teams_from_url(url)
    
    def _extract_teams_from_url(self, url):
        """Extract team names from URL"""
        try:
            match = re.search(r'/match-([^/]+)', url)
            if match:
                teams = match.group(1).replace('-', ' ').title()
                return teams
        except:
            pass
        return "Football Match"
    
    def _extract_stream_url(self, html, base_url):
        """Extract stream URL from page HTML"""
        stream_sources = []
        
        # Look for m3u8 files
        m3u8_patterns = [
            r'["\'](https?://[^"\']+\.m3u8[^"\']*)["\']',
            r'["\'](/[^"\']+\.m3u8[^"\']*)["\']',
            r'src=["\']([^"\']+\.m3u8[^"\']*)["\']',
            r'url=["\']([^"\']+\.m3u8[^"\']*)["\']',
        ]
        
        for pattern in m3u8_patterns:
            matches = re.findall(pattern, html, re.IGNORECASE)
            for match in matches:
                if match.startswith('/'):
                    full_url = urljoin(base_url, match)
                else:
                    full_url = match
                if self._is_stream_url(full_url):
                    stream_sources.append(full_url)
        
        # Look for video elements
        video_patterns = [
            r'<video[^>]*src=["\']([^"\']+)["\'][^>]*>',
            r'<source[^>]*src=["\']([^"\']+)["\'][^>]*>',
        ]
        
        for pattern in video_patterns:
            matches = re.findall(pattern, html, re.IGNORECASE)
            for match in matches:
                if match.startswith('/'):
                    full_url = urljoin(base_url, match)
                else:
                    full_url = match
                if self._is_stream_url(full_url):
                    stream_sources.append(full_url)
        
        # Look for iframes
        iframe_pattern = r'<iframe[^>]*src=["\']([^"\']+)["\'][^>]*>'
        iframe_matches = re.findall(iframe_pattern, html, re.IGNORECASE)
        for match in iframe_matches:
            if self._is_stream_url(match):
                stream_sources.append(match)
        
        # Return the first valid stream URL
        return stream_sources[0] if stream_sources else None
    
    def _extract_match_details(self, html, match_data):
        """Extract additional match details from HTML"""
        try:
            # Extract status
            status_patterns = [
                r'<div[^>]*class=["\'][^"\']*status[^"\']*["\'][^>]*>([^<]+)</div>',
                r'<span[^>]*class=["\'][^"\']*status[^"\']*["\'][^>]*>([^<]+)</span>',
                r'<div[^>]*class=["\'][^"\']*live[^"\']*["\'][^>]*>([^<]+)</div>',
            ]
            
            for pattern in status_patterns:
                matches = re.findall(pattern, html, re.IGNORECASE)
                if matches:
                    match_data['status'] = matches[0].strip()
                    break
            
            # Extract scores
            score_pattern = r'<div[^>]*class=["\'][^"\']*score[^"\']*["\'][^>]*>([^<]+)</div>'
            score_matches = re.findall(score_pattern, html, re.IGNORECASE)
            if len(score_matches) >= 2:
                match_data['home_score'] = score_matches[0].strip()
                match_data['away_score'] = score_matches[1].strip()
                
        except Exception as e:
            logger.warning(f"Could not extract match details: {e}")
    
    def _is_stream_url(self, url):
        """Check if URL looks like a stream"""
        if not url or len(url) < 10:
            return False
        
        stream_indicators = ['.m3u8', '.mpd', '.mp4', 'stream', 'live', 'hls', 'video', 'embed']
        url_lower = url.lower()
        
        return any(indicator in url_lower for indicator in stream_indicators)

# Initialize scraper
scraper = CamelLiveScraper()

@app.route('/')
def home():
    return jsonify({
        'status': 'online',
        'message': 'Camel Live Scraper API (Requests Version)',
        'timestamp': datetime.now().isoformat(),
        'version': '1.0.0',
        'environment': 'Vercel',
        'endpoints': {
            '/': 'GET - API information',
            '/api/matches': 'GET - Get all matches from homepage',
            '/api/match': 'GET - Get specific match details (add ?url=MATCH_URL)',
            '/api/stream': 'GET - Get stream URL only (add ?url=MATCH_URL)',
            '/api/health': 'GET - Health check'
        },
        'example': {
            'get_matches': '/api/matches',
            'get_match': '/api/match?url=https://www.camel1.live/game/match-example-team1-example-team2/video/abc123',
            'get_stream': '/api/stream?url=https://www.camel1.live/game/match-example-team1-example-team2/video/abc123'
        }
    })

@app.route('/api/health')
def health_check():
    """Health check endpoint"""
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'environment': 'Vercel',
        'method': 'requests'
    })

@app.route('/api/matches', methods=['GET'])
def get_matches():
    """Get all matches from homepage"""
    try:
        matches = scraper.get_cached_or_scrape('home_matches', scraper.scrape_home_matches)
        
        return jsonify({
            'success': True,
            'count': len(matches),
            'matches': matches,
            'timestamp': datetime.now().isoformat(),
            'cached': 'home_matches' in scraper.cache
        })
    except Exception as e:
        logger.error(f"Error in /api/matches: {e}")
        return jsonify({
            'success': False,
            'error': str(e),
            'timestamp': datetime.now().isoformat()
        }), 500

@app.route('/api/match', methods=['GET'])
def get_match():
    """Get specific match details"""
    url = request.args.get('url')
    
    if not url:
        return jsonify({
            'success': False,
            'error': 'URL parameter is required. Example: /api/match?url=https://www.camel1.live/game/match-team1-team2/video/abc123'
        }), 400
    
    try:
        cache_key = f'match_{hash(url)}'
        match_data = scraper.get_cached_or_scrape(cache_key, scraper.scrape_match_page, url)
        
        return jsonify({
            'success': True,
            'match': match_data,
            'timestamp': datetime.now().isoformat(),
            'cached': cache_key in scraper.cache
        })
    except Exception as e:
        logger.error(f"Error in /api/match: {e}")
        return jsonify({
            'success': False,
            'error': str(e),
            'timestamp': datetime.now().isoformat()
        }), 500

@app.route('/api/stream', methods=['GET'])
def get_stream():
    """Get only the stream URL for a match"""
    url = request.args.get('url')
    
    if not url:
        return jsonify({
            'success': False,
            'error': 'URL parameter is required. Example: /api/stream?url=https://www.camel1.live/game/match-team1-team2/video/abc123'
        }), 400
    
    try:
        cache_key = f'stream_{hash(url)}'
        match_data = scraper.get_cached_or_scrape(cache_key, scraper.scrape_match_page, url)
        
        return jsonify({
            'success': True,
            'stream_url': match_data.get('stream_url'),
            'match_url': url,
            'timestamp': datetime.now().isoformat(),
            'cached': cache_key in scraper.cache
        })
    except Exception as e:
        logger.error(f"Error in /api/stream: {e}")
        return jsonify({
            'success': False,
            'error': str(e),
            'timestamp': datetime.now().isoformat()
        }), 500

# Vercel serverless function handler
def handler(request, context):
    return app(request, context)

if __name__ == '__main__':
    print("🚀 Camel Live Scraper API Starting...")
    print("📡 Method: Requests (No Selenium)")
    print("🌐 Environment: Vercel Compatible")
    app.run(debug=app.config['DEBUG'], host='0.0.0.0', port=5000)
