"""Settings for the webhook relay worker."""

import os

# How many times one event is sent before it is parked for manual replay.
MAX_ATTEMPTS = 3

# Seconds to wait for a customer endpoint to answer.
# The load balancer drops idle connections at 15 seconds.
TIMEOUT_SECONDS = 10

# The wait doubles after every failed attempt, up to the cap.
BACKOFF_BASE_SECONDS = 2
BACKOFF_CAP_SECONDS = 300

SIGNING_SECRET = os.environ.get("RELAY_SIGNING_SECRET", "")
# Still accepted by verify() while receivers pick up the new secret.
SIGNING_SECRET_PREVIOUS = os.environ.get("RELAY_SIGNING_SECRET_PREVIOUS", "")
