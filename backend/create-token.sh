#!/bin/bash

# Load environment variables from .env file
set -a
source "$(dirname "$0")/.env"
set +a

# Default values
ROOM="${1:-test_room}"
IDENTITY="${2:-test_user}"
VALID_FOR="${3:-24h}"

echo "Creating LiveKit token..."
echo "  Room: $ROOM"
echo "  Identity: $IDENTITY"
echo "  Valid for: $VALID_FOR"
echo ""

lk token create \
  --api-key "$LIVEKIT_API_KEY" \
  --api-secret "$LIVEKIT_API_SECRET" \
  --join --room "$ROOM" --identity "$IDENTITY" \
  --valid-for "$VALID_FOR"

