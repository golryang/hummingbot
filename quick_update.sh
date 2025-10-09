#!/bin/bash
# Quick update script - 빌드 없이 코드 변경사항 반영 (5초)

echo "📦 Copying all code changes to container..."

# Check if container is running
if ! docker ps | grep -q hummingbot-local; then
    echo "❌ Container not running. Start with: docker-compose -f docker-compose.local.yml up -d"
    exit 1
fi

# Copy entire hummingbot directory (Python files will overwrite, .so files preserved)
docker cp hummingbot/. hummingbot-local:/home/hummingbot/hummingbot/

# Copy controllers
docker cp controllers/. hummingbot-local:/home/hummingbot/controllers/

# Copy scripts
docker cp scripts/. hummingbot-local:/home/hummingbot/scripts/

echo "✅ All files updated! Restarting container..."

# Restart container
docker restart hummingbot-local

echo "🚀 Done! Attach with: docker attach hummingbot-local"
