# Camel Live Scraper API

A Flask API for scraping live football matches from Camel1.live

## Features

- Scrape live matches from Camel1.live
- Extract stream URLs
- Caching system for performance
- Fallback scraping methods
- Vercel deployment ready

## API Endpoints

- `GET /` - API information
- `GET /api/health` - Health check
- `GET /api/matches` - Get all matches
- `GET /api/match?url=URL` - Get specific match
- `GET /api/stream?url=URL` - Get stream URL

## Deployment

1. Push to GitHub
2. Connect repository to Vercel
3. Set environment variables
4. Deploy!

## Environment Variables

- `DEBUG`: Enable debug mode
- `CACHE_TIMEOUT`: Cache duration in seconds
- `RATE_LIMIT`: Requests per second
- `ENABLE_SELENIUM`: Enable/disable Selenium